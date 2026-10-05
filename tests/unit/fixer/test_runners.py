"""テストとリントの実行（design.md §8）。**実物の pytest と ruff** を、小さな複製に対して走らせる。

対応: AC-7（コードが決めたコマンドで走り、成功・失敗の件数が返る。制限時間）、AC-8（テストから見える環境変数は
決まった一覧だけ。親の秘密の値は見えない。ホームは空の一時ディレクトリ）、G-8 の材料（指摘の数）。

読み方（設計に引数が無い点）: ``PytestRunner(timeout_seconds=...)``・``RuffRunner()`` で作れる。
引数なしの ``PytestRunner()`` も作れる（既定の制限時間は ``FixLimits`` と同じ 300 秒）。

T0-1 で確かめた時間: この大きさの複製なら、pytest は 1 回 0.1〜0.2 秒、ruff は 0.1 秒未満。
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from . import repo_factory
from .fakes import PARENT_SECRETS, RUNTIME_ADDED_KEYS, SAFE_ENV_KEYS, SECRET_MARK, list_tree
from .repo_factory import EXISTING_TEST, FILES, FIX_NEW, FIX_OLD, NEW_TEST, NEW_TEST_SOURCE, SRC

ENV_DUMP_TEST = "tests/unit/test_env_dump.py"


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    repo_factory.write_files(root, FILES)
    return root


def apply_fix(root: Path) -> None:
    (root / NEW_TEST).write_text(NEW_TEST_SOURCE, encoding="utf-8")
    source = root / SRC
    source.write_text(source.read_text(encoding="utf-8").replace(FIX_OLD, FIX_NEW), encoding="utf-8")


def pytest_runner(**kwargs):
    from src.fixer.runners import PytestRunner

    return PytestRunner(**kwargs)


def by_name(report) -> dict[str, str]:
    """テストの ID（``classname::name``）の末尾の名前で引けるようにする。"""
    return {key.rsplit("::", 1)[-1]: value for key, value in report.outcomes.items()}


def env_dump_source(dump: Path) -> str:
    return (
        "import json\nimport os\nimport sys\n\n\n"
        "def test_dump_environment():\n"
        "    home = os.environ.get('HOME', '')\n"
        "    record = {\n"
        "        'env': dict(os.environ),\n"
        "        'home_is_dir': os.path.isdir(home),\n"
        "        'home_listing': sorted(os.listdir(home)) if os.path.isdir(home) else None,\n"
        "        'tmpdir_is_dir': os.path.isdir(os.environ.get('TMPDIR', '')),\n"
        "        'cwd': os.getcwd(),\n"
        "        'argv': sys.argv,\n"
        "        'pid': os.getpid(),\n"
        "    }\n"
        f"    with open({str(dump)!r}, 'a', encoding='utf-8') as file:\n"
        "        file.write(json.dumps(record) + '\\n')\n"
    )


def read_dumps(dump: Path) -> list[dict]:
    return [json.loads(line) for line in dump.read_text(encoding="utf-8").splitlines() if line]


def plant_parent_secrets(monkeypatch) -> None:
    for name in PARENT_SECRETS:
        monkeypatch.setenv(name, f"{SECRET_MARK}-{name}")


# ---------------------------------------------------------------------------
# R8: 環境変数
# ---------------------------------------------------------------------------


class TestSafeEnv:
    def test_safe_env_contains_exactly_the_fixed_variables(self, tmp_path, monkeypatch):
        from src.fixer.runners import safe_env

        plant_parent_secrets(monkeypatch)
        home, tmp = tmp_path / "home", tmp_path / "tmp"

        env = safe_env(home, tmp)

        assert set(env) == SAFE_ENV_KEYS
        assert env["PATH"] == os.environ["PATH"]
        assert env["HOME"] == str(home)
        assert env["TMPDIR"] == str(tmp)
        assert env["LANG"] == "C.UTF-8"
        assert env["LC_ALL"] == "C.UTF-8"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["AWS_DEFAULT_REGION"] == "ap-northeast-1"
        assert env["AWS_ACCESS_KEY_ID"] == "dummy"
        assert env["AWS_SECRET_ACCESS_KEY"] == "dummy"
        assert env["AWS_EC2_METADATA_DISABLED"] == "true"

    def test_safe_env_never_carries_parent_values_other_than_path(self, tmp_path, monkeypatch):
        from src.fixer.runners import safe_env

        plant_parent_secrets(monkeypatch)

        env = safe_env(tmp_path / "home", tmp_path / "tmp")

        assert all(SECRET_MARK not in value for value in env.values())
        assert all(isinstance(value, str) for value in env.values())

    def test_safe_env_aws_files_point_at_paths_that_do_not_exist(self, tmp_path):
        from src.fixer.runners import safe_env

        env = safe_env(tmp_path / "home", tmp_path / "tmp")

        assert not os.path.exists(env["AWS_SHARED_CREDENTIALS_FILE"])
        assert not os.path.exists(env["AWS_CONFIG_FILE"])


# ---------------------------------------------------------------------------
# 実物の pytest
# ---------------------------------------------------------------------------


class TestPytestRunnerOutcomes:
    def test_run_existing_tests_reports_each_test_by_classname_and_name(self, tree):
        """AC-7: 成功・失敗が、テストごとに返る（JUnit の XML から読む。ID は ``classname::name``）。"""
        report = pytest_runner().run(tree, ["tests"])

        assert report.timed_out is False
        assert report.exit_code == 1
        assert report.outcomes["tests.unit.test_orchestrator::test_average_returns_mean"] == "passed"
        assert report.outcomes["tests.unit.test_orchestrator::test_label_prefixes_name"] == "passed"
        assert report.outcomes["tests.unit.test_orchestrator::test_known_failure_at_baseline"] == "failed"
        assert len(report.outcomes) == 5
        assert set(report.outcomes.values()) == {"passed", "failed"}
        assert isinstance(report.output_tail, str)

    def test_run_new_test_fails_before_the_fix_and_passes_after(self, tree):
        """G-5・G-6 の材料: 見本の不具合は、実物の pytest で「修正前に落ち、修正後に通る」。"""
        (tree / NEW_TEST).write_text(NEW_TEST_SOURCE, encoding="utf-8")
        runner = pytest_runner()

        before = runner.run(tree, [NEW_TEST])
        apply_fix(tree)
        after = runner.run(tree, [NEW_TEST])

        assert by_name(before) == {"test_average_of_empty_list_is_zero": "failed"}
        assert before.exit_code == 1
        assert by_name(after) == {"test_average_of_empty_list_is_zero": "passed"}
        assert after.exit_code == 0

    def test_run_with_ignore_leaves_the_ignored_file_out(self, tree):
        """G-7 の材料: 既存のテストだけを走らせる（新しいテストファイルを ``--ignore``）。"""
        apply_fix(tree)
        runner = pytest_runner()

        with_new = runner.run(tree, ["tests"])
        without_new = runner.run(tree, ["tests"], ignore=[NEW_TEST])

        assert "test_average_of_empty_list_is_zero" in by_name(with_new)
        assert "test_average_of_empty_list_is_zero" not in by_name(without_new)
        assert len(without_new.outcomes) == 5

    def test_run_reports_errors_and_skips_separately(self, tree):
        (tree / "tests/unit/test_kinds.py").write_text(
            "import pytest\n\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError('setup')\n\n\n"
            "def test_with_error(broken):\n    assert True\n\n\n"
            "def test_skipped():\n    pytest.skip('later')\n\n\n"
            "def test_fine():\n    assert True\n",
            encoding="utf-8",
        )

        report = pytest_runner().run(tree, ["tests/unit/test_kinds.py"])

        assert by_name(report) == {"test_with_error": "error", "test_skipped": "skipped", "test_fine": "passed"}
        assert report.exit_code == 1

    def test_run_parametrized_tests_have_distinct_ids(self, tree):
        (tree / "tests/unit/test_params.py").write_text(
            "import pytest\n\n\n@pytest.mark.parametrize('value', [1, 2, 3])\n"
            "def test_value(value):\n    assert value != 2\n",
            encoding="utf-8",
        )

        report = pytest_runner().run(tree, ["tests/unit/test_params.py"])

        assert len(report.outcomes) == 3
        assert sorted(report.outcomes.values()) == ["failed", "passed", "passed"]

    def test_run_file_with_collection_error_has_no_passed_test_and_exit_code_two(self, tree):
        """G-5 の材料: 集めるときのエラー（import できない、構文エラー）。"""
        (tree / "tests/unit/test_broken.py").write_text(
            "import module_that_does_not_exist\n\n\ndef test_x():\n    assert True\n", encoding="utf-8"
        )

        report = pytest_runner().run(tree, ["tests/unit/test_broken.py"])

        assert report.exit_code == 2
        assert "passed" not in report.outcomes.values()
        assert report.timed_out is False

    def test_run_path_without_tests_returns_exit_code_five_and_no_outcomes(self, tree):
        """G-5・G-6 の材料: テストが 1 件も集まらない。"""
        report = pytest_runner().run(tree, ["tests/unit/__init__.py"])

        assert report.exit_code == 5
        assert report.outcomes == {}

    def test_run_does_not_leave_files_in_the_tree(self, tree):
        """JUnit の XML は複製の外に置く。キャッシュも作らない（``-p no:cacheprovider``、バイトコードなし）。"""
        apply_fix(tree)
        before = list_tree(tree)

        pytest_runner().run(tree, ["tests"])

        assert list_tree(tree) == before


class TestPytestRunnerCommand:
    def test_run_uses_the_fixed_command_in_the_tree(self, tree, process_recorder):
        """AC-7・R7: コードが決めたコマンドで走る。モデルが指定できるのは、``--`` の後ろのパスだけ。"""
        apply_fix(tree)

        pytest_runner().run(tree, [NEW_TEST, EXISTING_TEST], ignore=["tests/unit/fixer"])

        calls = [call for call in process_recorder.implementation_calls if "pytest" in call.argv]
        assert len(calls) == 1
        call = calls[0]
        argv = list(call.argv)
        assert argv[:6] == [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        assert call.shell is False
        assert Path(call.cwd).resolve() == tree.resolve()
        separator = argv.index("--")
        assert argv[separator + 1 :] == [NEW_TEST, EXISTING_TEST]
        # ``--junitxml <file>`` と ``--junitxml=<file>`` のどちらの書き方でもよい（``--ignore`` も同じ）
        options = " ".join(argv[6:separator]).replace("=", " ").split(" ")
        junit = options[options.index("--junitxml") + 1]
        assert Path(junit).is_absolute()
        assert not Path(junit).resolve().is_relative_to(tree.resolve())
        assert options[options.index("--ignore") + 1] == "tests/unit/fixer"
        assert set(options) - {junit, "tests/unit/fixer"} == {"--junitxml", "--ignore"}

    def test_run_passes_only_the_fixed_environment_to_the_process(self, tree, process_recorder, monkeypatch):
        plant_parent_secrets(monkeypatch)

        pytest_runner().run(tree, ["tests"])

        call = next(call for call in process_recorder.implementation_calls if "pytest" in call.argv)
        assert call.env is not None
        assert set(call.env) == SAFE_ENV_KEYS
        assert all(SECRET_MARK not in value for value in call.env.values())


class TestPytestRunnerEnvironment:
    def test_tests_cannot_see_parent_secrets_and_see_only_the_fixed_variables(
        self, tree, tmp_path, monkeypatch, _isolation
    ):
        """AC-8: 親の環境変数に目印の値を置いてテストを走らせる → テストの中から、その値は見えない。"""
        plant_parent_secrets(monkeypatch)
        (_isolation / "parent-home-marker.txt").write_text("x\n", encoding="utf-8")
        dump = tmp_path / "env-dump.jsonl"
        (tree / ENV_DUMP_TEST).write_text(env_dump_source(dump), encoding="utf-8")

        report = pytest_runner().run(tree, [ENV_DUMP_TEST])

        assert by_name(report) == {"test_dump_environment": "passed"}
        (record,) = read_dumps(dump)
        env = record["env"]
        leaked = {name: value for name, value in env.items() if SECRET_MARK in value}
        assert leaked == {}
        assert set(env) <= SAFE_ENV_KEYS | RUNTIME_ADDED_KEYS, sorted(set(env) - SAFE_ENV_KEYS - RUNTIME_ADDED_KEYS)
        assert set(env) >= SAFE_ENV_KEYS
        assert env["AWS_ACCESS_KEY_ID"] == "dummy"
        assert env["AWS_SECRET_ACCESS_KEY"] == "dummy"
        assert env["AWS_DEFAULT_REGION"] == "ap-northeast-1"
        assert "AWS_SESSION_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert Path(record["cwd"]).resolve() == tree.resolve()

    def test_tests_run_with_an_empty_temporary_home_that_is_removed_afterwards(self, tree, tmp_path, _isolation):
        """AC-8: ホームディレクトリは空の一時ディレクトリ。実行のたびに作って消す。"""
        (_isolation / "parent-home-marker.txt").write_text("x\n", encoding="utf-8")
        dump = tmp_path / "env-dump.jsonl"
        (tree / ENV_DUMP_TEST).write_text(env_dump_source(dump), encoding="utf-8")
        runner = pytest_runner()

        runner.run(tree, [ENV_DUMP_TEST])
        runner.run(tree, [ENV_DUMP_TEST])

        first, second = read_dumps(dump)
        for record in (first, second):
            home = record["env"]["HOME"]
            assert record["home_is_dir"] is True
            assert record["home_listing"] == []
            assert Path(home).resolve() != Path(os.environ["HOME"]).resolve()
            assert Path(home).resolve() != Path(os.path.expanduser("~")).resolve()
            assert not Path(home).resolve().is_relative_to(tree.resolve())
            assert not os.path.exists(home)
            assert record["tmpdir_is_dir"] is True
            assert Path(record["env"]["TMPDIR"]).resolve().is_relative_to(Path("/tmp").resolve())  # noqa: S108
            assert not os.path.exists(record["env"]["TMPDIR"])
        assert first["env"]["HOME"] != second["env"]["HOME"]


class TestPytestRunnerTimeout:
    def test_run_over_the_time_limit_is_stopped_and_reported_as_timed_out(self, tree, tmp_path):
        """AC-7: 制限時間を超える → プロセスを止めて ``timed_out``。"""
        pid_file = tmp_path / "pid.txt"
        (tree / "tests/unit/test_slow.py").write_text(
            "import os\nimport time\n\n\ndef test_slow():\n"
            f"    with open({str(pid_file)!r}, 'w') as file:\n        file.write(str(os.getpid()))\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        started = time.monotonic()

        report = pytest_runner(timeout_seconds=1.5).run(tree, ["tests/unit/test_slow.py"])

        elapsed = time.monotonic() - started
        assert report.timed_out is True
        assert "passed" not in report.outcomes.values()
        assert elapsed < 20
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
            else:
                time.sleep(0.05)
        assert alive is False, "the pytest process is still running after the timeout"


# ---------------------------------------------------------------------------
# 実物の ruff
# ---------------------------------------------------------------------------


class TestRuffRunner:
    def test_count_returns_the_number_of_findings_in_the_tree(self, tree):
        """見本の複製には、基準の時点で指摘が 1 件ある（``legacy.py`` の使っていない import）。"""
        from src.fixer.runners import RuffRunner

        assert RuffRunner().count(tree) == 1

    def test_count_does_not_change_with_a_clean_fix(self, tree):
        from src.fixer.runners import RuffRunner

        apply_fix(tree)

        assert RuffRunner().count(tree) == 1

    def test_count_increases_by_one_with_one_new_finding(self, tree):
        """G-8 の材料: 修正で指摘が 1 件増える。"""
        from src.fixer.runners import RuffRunner

        (tree / "src/agent/extra.py").write_text("import sys\n\n\ndef extra():\n    return 1\n", encoding="utf-8")

        assert RuffRunner().count(tree) == 2

    def test_count_covers_tests_as_well_as_src(self, tree):
        from src.fixer.runners import RuffRunner

        (tree / "tests/unit/test_extra.py").write_text(
            "import sys\n\n\ndef test_extra():\n    assert True\n", encoding="utf-8"
        )

        assert RuffRunner().count(tree) == 2

    def test_count_uses_the_fixed_command_and_environment(self, tree, process_recorder, monkeypatch):
        from src.fixer.runners import RuffRunner

        plant_parent_secrets(monkeypatch)

        RuffRunner().count(tree)

        calls = [call for call in process_recorder.implementation_calls if "ruff" in call.argv]
        assert len(calls) == 1
        call = calls[0]
        assert list(call.argv) == [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format",
            "json",
            "--no-cache",
            "src",
            "tests",
        ]
        assert Path(call.cwd).resolve() == tree.resolve()
        assert call.shell is False
        assert call.env is not None
        assert set(call.env) == SAFE_ENV_KEYS
        assert all(SECRET_MARK not in value for value in call.env.values())

    def test_count_does_not_leave_files_in_the_tree(self, tree):
        from src.fixer.runners import RuffRunner

        before = list_tree(tree)

        RuffRunner().count(tree)

        assert list_tree(tree) == before

    def test_count_returns_none_when_ruff_cannot_run(self, tmp_path):
        """design.md §8: 走らせられなければ None（G-8 は通さない）。"""
        from src.fixer.runners import RuffRunner

        assert RuffRunner().count(tmp_path / "no-such-tree") is None
