"""パッケージ全体にかかる制約。実装のソースを文字列として確かめる（実行はしない）。

対応: R3・AC-11（書き込みの操作を持たない）、R17（既存の再試行に乗せない）、design.md §1
（``orchestrator`` を import しない）、R14（``bypassPermissions`` を使わない）、AC-28（手動手順の文書と、
pytest の既定実行では手動の入口が走らないこと）。
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_DIR = REPO_ROOT / "src" / "agent" / "diagnosis"
DOC_PATH = REPO_ROOT / "docs" / "execution-diagnostician.md"
CLI_TEST_FILE = "test_cli.py"  # 手動の入口（main(argv)）のテスト。入口に触れてよいのはここだけ
EXPECTED_MODULES = (
    "__init__.py",
    "models.py",
    "redaction.py",
    "records.py",
    "tools.py",
    "handoff.py",
    "prompt.py",
    "sdk_server.py",
    "runner.py",
    "notify.py",
    "__main__.py",
)
WRITE_OPERATIONS = (
    "put_item",
    "update_item",
    "delete_item",
    "batch_writer",
    "batch_write_item",
    "put_object",
    "delete_object",
    "copy_object",
    "upload_file",
    "upload_fileobj",
)


def _sources() -> dict[str, str]:
    assert PACKAGE_DIR.is_dir(), f"{PACKAGE_DIR} does not exist"
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(PACKAGE_DIR.glob("*.py"))}


class TestPackageLayout:
    def test_package_contains_modules_from_design(self):
        assert set(EXPECTED_MODULES) <= set(_sources())

    @pytest.mark.parametrize("module", ["models", "redaction", "records", "tools", "handoff", "prompt", "notify"])
    def test_sdk_independent_module_does_not_import_claude_agent_sdk(self, module):
        """R13: ツールの中身は SDK に依存しない関数に分ける。"""
        source = _sources()[f"{module}.py"]

        assert "claude_agent_sdk" not in source


class TestSafetyConstraints:
    def test_package_does_not_use_write_operations(self):
        for name, source in _sources().items():
            for operation in WRITE_OPERATIONS:
                assert operation not in source, f"{name} mentions {operation}"

    def test_package_does_not_import_orchestrator_or_reuse_its_retries(self):
        for name, source in _sources().items():
            assert not re.search(r"^\s*(from|import)\s+orchestrator\b", source, re.MULTILINE), name
            assert "_run_claude_with_retries" not in source, name
            assert "call_claude" not in source, name

    def test_package_does_not_bypass_permissions(self):
        for name, source in _sources().items():
            assert "bypassPermissions" not in source, name

    def test_system_prompt_is_a_constant_that_marks_tool_output_as_untrusted(self):
        from diagnosis.prompt import SYSTEM_PROMPT

        assert isinstance(SYSTEM_PROMPT, str)
        assert "submit_diagnosis" in SYSTEM_PROMPT
        assert "evidence_id" in SYSTEM_PROMPT
        assert "insufficient_records" in SYSTEM_PROMPT
        assert "{" not in SYSTEM_PROMPT  # 値を埋め込む書式になっていない


class TestManualProcedure:
    def test_manual_entry_point_exists_without_being_executed(self):
        assert importlib.util.find_spec("diagnosis.__main__") is not None

    def test_tests_do_not_execute_manual_entry_point(self):
        """AC-28: pytest の既定実行では、手動の入口は走らない。

        入口のモジュールに触れてよいのは ``test_cli.py`` だけ（``main(argv)`` の引数の処理・出力・
        終了コードを確かめる。design.md §17 の 19）。そこでも、スクリプトとしては実行せず、
        ``diagnose_execution`` を必ず偽物に差し替える。
        """
        this_file = Path(__file__).resolve()
        test_files = [*Path(__file__).parent.glob("*.py"), Path(__file__).parents[1] / "test_main.py"]
        patterns = (
            r"\brunpy\b",
            r"import\s+diagnosis\.__main__",
            r"from\s+diagnosis\s+import\s+__main__",
            r"diagnosis\.__main__",
        )

        for path in test_files:
            if path.resolve() == this_file or path.name == CLI_TEST_FILE:
                continue
            source = path.read_text(encoding="utf-8")
            for pattern in patterns:
                assert not re.search(pattern, source), f"{path.name} matches {pattern}"

    def test_cli_test_never_runs_entry_point_as_script_and_always_replaces_diagnosis(self):
        """``test_cli.py`` は、入口をスクリプトとして実行せず、診断を必ず差し替える。"""
        source = (Path(__file__).parent / CLI_TEST_FILE).read_text(encoding="utf-8")

        for pattern in (r"\brunpy\b", r"\bsubprocess\b", r"\bPopen\b", r"os\.system", r"run_module", r"\bexec\("):
            assert not re.search(pattern, source), f"{CLI_TEST_FILE} matches {pattern}"
        assert re.search(r"@pytest\.fixture\(autouse=True\)\s+def _never_run_real_diagnosis", source)
        assert 'monkeypatch.setattr(module, "diagnose_execution"' in source
        assert "diagnose_execution(" not in source.replace("assert_called_once_with", "")  # 本物を直接は呼ばない

    def test_entry_point_module_only_runs_main_when_executed_as_script(self):
        """入口は ``if __name__ == "__main__":`` の下でだけ ``main()`` を呼ぶ（読み込むだけでは走らない）。"""
        source = _sources()["__main__.py"]
        guard = 'if __name__ == "__main__":'

        assert source.count(guard) == 1
        before_guard = source[: source.index(guard)]
        top_level_calls = [line for line in before_guard.splitlines() if re.match(r"(sys\.exit|main)\(", line)]
        assert top_level_calls == []

    def test_manual_procedure_document_covers_required_topics(self):
        """AC-28: 実行 ID を渡して 1 回動かす手順（認証、必要な権限）が文書にある。"""
        assert DOC_PATH.is_file(), f"{DOC_PATH} does not exist"
        text = DOC_PATH.read_text(encoding="utf-8")

        for keyword in (
            "python -m diagnosis",
            "DYNAMODB_TABLE_PREFIX",
            "EVENTS_TABLE",
            "PROMPTS_BUCKET",
            "EXECUTION_DIAGNOSIS",
            "--handoff-out",
            "サブスクリプション",
            "権限",
        ):
            assert keyword in text, f"{keyword} is not documented"
