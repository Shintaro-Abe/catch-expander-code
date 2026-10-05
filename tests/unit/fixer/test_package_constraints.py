"""パッケージ全体にかかる制約。実装のソースを文字列として確かめる（実行はしない）。

対応: AC-25（置き場所。本番のイメージに入らない）、AC-27（手動手順の文書と、pytest の既定実行では手動の入口が
走らないこと）、R7（シェルを持たせない）、R10（既存の再試行に乗せない）、R14（リモートに触れない）、
R18（本番の記録に書かない）、R19、design.md §2（ファイル構成と SDK への依存）。

AC-25 の後半（``git diff main -- ...`` が空）は、リポジトリ本体に対する git の実行が要るので、ここでは確かめない
（T3-1 で、手で確かめる）。
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "src" / "fixer"
DOC_PATH = REPO_ROOT / "docs" / "execution-fixer.md"
CLI_TEST_FILE = "test_cli.py"  # 手動の入口（main(argv)）のテスト。入口に触れてよいのはここだけ
EXPECTED_MODULES = (
    "__init__.py",
    "models.py",
    "handoff_input.py",
    "exception_types.py",
    "gitio.py",
    "workspace.py",
    "tools.py",
    "runners.py",
    "gates.py",
    "prompt.py",
    "sdk_server.py",
    "runner.py",
    "draft.py",
    "__main__.py",
)
# design.md §2.2: SDK に依存してよいのは、sdk_server.py と runner.py だけ
SDK_FREE_MODULES = (
    "models",
    "handoff_input",
    "exception_types",
    "gitio",
    "workspace",
    "tools",
    "runners",
    "gates",
    "prompt",
    "draft",
    "__main__",
)


def _sources() -> dict[str, str]:
    assert PACKAGE_DIR.is_dir(), f"{PACKAGE_DIR} does not exist"
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(PACKAGE_DIR.glob("*.py"))}


class TestPlacement:
    def test_package_contains_the_modules_from_the_design(self):
        assert set(EXPECTED_MODULES) <= set(_sources())

    def test_package_is_outside_the_directories_copied_into_the_production_image(self):
        """AC-25・R19: 修正役のファイルが ``src/agent/`` と ``src/observability/`` の下にない。"""
        assert PACKAGE_DIR.is_dir()
        for directory in (REPO_ROOT / "src" / "agent", REPO_ROOT / "src" / "observability"):
            names = [
                path.relative_to(directory).as_posix() for path in directory.rglob("*") if "fixer" in path.name.lower()
            ]
            assert names == [], names

    def test_production_image_and_template_do_not_reference_the_fixer(self):
        """AC-25: Dockerfile が入れるのは ``src/agent/`` と ``src/observability/`` だけ。"""
        dockerfile = (REPO_ROOT / "src" / "agent" / "Dockerfile").read_text(encoding="utf-8")
        template = (REPO_ROOT / "template.yaml").read_text(encoding="utf-8")

        assert "fixer" not in dockerfile.lower()
        assert "src/fixer" not in template
        copies = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("COPY src/")]
        assert sorted(copies) == ["src/agent/", "src/observability/"]

    def test_public_names_are_re_exported_from_the_package(self):
        import src.fixer as package

        for name in ("run_fix", "FixRun", "FixLimits"):
            assert hasattr(package, name), name

    def test_production_code_does_not_import_the_fixer(self):
        for directory in ("agent", "observability", "trigger", "token_monitor", "dashboard_api"):
            for path in (REPO_ROOT / "src" / directory).rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                assert not re.search(r"^\s*(from|import)\s+(src\.)?fixer\b", source, re.MULTILINE), path


class TestDependencies:
    @pytest.mark.parametrize("module", SDK_FREE_MODULES)
    def test_sdk_independent_module_does_not_import_claude_agent_sdk(self, module):
        source = _sources()[f"{module}.py"]

        assert "claude_agent_sdk" not in source

    def test_handoff_validation_is_independent_of_stage_one_handoff_module(self):
        """design.md §2.3: 第 1 段階の ``handoff.py`` は使わない（形が変わったときに、黙って追随しないため）。"""
        for name, source in _sources().items():
            assert not re.search(r"diagnosis\.handoff\b", source), name
            assert "is_fixer_eligible" not in source, name
            assert "build_handoff" not in source, name

    def test_package_does_not_import_aws_slack_or_http_clients(self):
        """R18: 本番の記録（events テーブル、S3）には書かない。修正役は、通信のクライアントを持たない。"""
        pattern = r"^\s*(import|from)\s+(boto3|botocore|slack_sdk|requests|httpx|urllib\.request|http\.client|socket)\b"
        for name, source in _sources().items():
            assert not re.search(pattern, source, re.MULTILINE), name
            for word in ("put_item", "put_object", "EventEmitter"):
                assert word not in source, f"{name} mentions {word}"


class TestSafetyConstraints:
    def test_package_never_runs_a_shell(self):
        """R7: シェルを持たせない。外部のコマンドは、引数の list で起こす。"""
        for name, source in _sources().items():
            assert "shell=True" not in source, name
            assert "os.system" not in source, name
            assert "os.popen" not in source, name

    def test_only_gitio_and_runners_start_processes(self):
        for name, source in _sources().items():
            if name in ("gitio.py", "runners.py"):
                continue
            assert not re.search(r"^\s*(import subprocess|from subprocess import)", source, re.MULTILINE), name

    def test_package_does_not_reuse_orchestrator_retries(self):
        """R10: 上限への到達と SDK の失敗を、既存の再試行・上位モデルへの切り替えに乗せない。"""
        for name, source in _sources().items():
            assert not re.search(r"^\s*(from|import)\s+orchestrator\b", source, re.MULTILINE), name
            assert "_run_claude_with_retries" not in source, name
            assert "call_claude" not in source, name

    def test_package_does_not_bypass_permissions_or_use_unverified_sdk_features(self):
        """design.md §9.2: ``permission_mode``・``can_use_tool``・``hooks``・``sandbox``・``add_dirs`` は使わない。"""
        for name, source in _sources().items():
            assert "bypassPermissions" not in source, name
            assert "acceptEdits" not in source, name
            for keyword in ("can_use_tool=", "permission_mode=", "add_dirs=", "sandbox=", "hooks="):
                assert keyword not in source, f"{name} passes {keyword}"

    def test_package_has_no_remote_git_operations_or_github_clients(self):
        """R14: プッシュしない。リモートに触れる操作を一切行わない。"""
        for name, source in _sources().items():
            for pattern in (r"""["']push["']""", r"""["']fetch["']""", r"""["']pull["']""", r"""["']clone["']"""):
                assert not re.search(pattern, source), f"{name} matches {pattern}"
            for word in ("api.github.com", "PyGithub"):
                assert word not in source, f"{name} mentions {word}"

    def test_package_does_not_use_worktrees_or_checkouts(self):
        """design.md §18: ``git worktree`` も、``.git`` を含む複製も使わない。"""
        for name, source in _sources().items():
            for pattern in (
                r"""["']worktree["']""",
                r"""["']checkout["']""",
                r"""["']stash["']""",
                r"""["']reset["']""",
            ):
                assert not re.search(pattern, source), f"{name} matches {pattern}"


class TestManualProcedure:
    def test_manual_entry_point_exists_without_being_executed(self):
        assert importlib.util.find_spec("src.fixer.__main__") is not None

    def test_tests_do_not_execute_the_manual_entry_point(self):
        """AC-27: pytest の既定実行では、手動の入口は走らない。

        入口のモジュールに触れてよいのは ``test_cli.py`` だけ。そこでも、スクリプトとしては実行せず、
        ``run_fix`` を必ず偽物に差し替える。
        """
        this_file = Path(__file__).resolve()
        patterns = (
            r"\brunpy\b",
            r"import\s+src\.fixer\.__main__",
            r"from\s+src\.fixer\s+import\s+__main__",
            r"fixer\.__main__",
        )

        for path in Path(__file__).parent.glob("*.py"):
            if path.resolve() == this_file or path.name == CLI_TEST_FILE:
                continue
            source = path.read_text(encoding="utf-8")
            for pattern in patterns:
                assert not re.search(pattern, source), f"{path.name} matches {pattern}"

    def test_cli_test_never_runs_the_entry_point_as_a_script_and_always_replaces_run_fix(self):
        source = (Path(__file__).parent / CLI_TEST_FILE).read_text(encoding="utf-8")

        for pattern in (r"\brunpy\b", r"\bsubprocess\b", r"\bPopen\b", r"os\.system", r"run_module", r"\bexec\("):
            assert not re.search(pattern, source), f"{CLI_TEST_FILE} matches {pattern}"
        assert re.search(r"@pytest\.fixture\(autouse=True\)\s+def _never_run_real_fixer", source)
        assert 'monkeypatch.setattr(module, "run_fix"' in source

    def test_entry_point_module_only_runs_main_when_executed_as_a_script(self):
        source = _sources()["__main__.py"]
        guard = 'if __name__ == "__main__":'

        assert source.count(guard) == 1
        before_guard = source[: source.index(guard)]
        top_level_calls = [line for line in before_guard.splitlines() if re.match(r"(sys\.exit|main)\(", line)]
        assert top_level_calls == []

    def test_manual_procedure_document_covers_the_required_topics(self):
        """AC-27: 手元で動かす手順（本番の認証情報がない環境で動かすこと、成果物の確認、人がプッシュして PR を作る
        2 コマンド、取り込む前に規律どおり行うこと）が文書にある。"""
        assert DOC_PATH.is_file(), f"{DOC_PATH} does not exist"
        text = DOC_PATH.read_text(encoding="utf-8")

        for keyword in (
            "python -m src.fixer",
            "PYTHONPATH=src/agent",
            "--handoff",
            "--out-dir",
            "--base",
            "--handoff-out",
            "本番の認証情報",
            "サブスクリプション",
            "git push origin fix/diagnosed-",
            "pr-draft.md",
            "result.json",
            "steering",
            "Codex",
            "既知の制限",
        ):
            assert keyword in text, f"{keyword} is not documented"
        assert re.search(r"gh pr create|プルリクエストの作成|PR の作成|PR を作", text)
        assert re.search(r"git branch -D|ブランチの消し方|ブランチを消", text)
