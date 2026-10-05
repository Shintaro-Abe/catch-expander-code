"""7 つのツールの中身（design.md §7。``FixToolSession``）。SDK には依存しない。

対応: AC-6（複製の外・書き込めない場所を、呼び出しの時点で拒否する）、AC-7（テスト実行ツール）、
AC-29 の部品（入力の検証。足取りと回数に残る。不正な値を返さない）、R9（上限）、R17（足取り）。
実物の MCP サーバーを通した確認は ``test_sdk_server.py``、実物の pytest は ``test_runners.py``。

読み方（設計に、結果の項目の名前が無い点）。developer は、この名前に合わせる:

- ``list_files``: ``entries``（``{"name", "type"}`` の list。``type`` は ``file`` / ``dir``）、``truncated``
- ``read_file``: ``content``（行番号つきの本文）、``total_lines``、``truncated``
- ``search_code``: ``matches``（``{"path", "line", "text"}`` の list）、``truncated``
- ``write_file``: ``bytes_written``
- ``replace_in_file``: ``line``
- ``run_tests``: ``passed``・``failed``・``errors``、``failed_tests``、``output_tail``（テストが落ちても ``ok``）
- ``finish``: ``accepted``
- ``invalid_input``: ``invalid_fields``（不正な項目の名前。第 1 段階と同じ）
"""

import json
import os
from pathlib import Path

import pytest

from . import fakes
from .conftest import OUTSIDE_MARK
from .fakes import FINISH_DECLINED, FINISH_FIXED, FakeTestRunner, cases_of, check_each
from .repo_factory import (
    EXISTING_CONFTEST,
    EXISTING_TEST,
    FILES,
    FIX_NEW,
    FIX_OLD,
    LEGACY,
    NEW_TEST,
    NEW_TEST_SOURCE,
    SRC,
)

TOOL_NAMES = ("list_files", "read_file", "search_code", "write_file", "replace_in_file", "run_tests", "finish")
TOOL_OUTCOMES = (
    "ok",
    "invalid_input",
    "not_found",
    "denied",
    "call_limit",
    "size_limit",
    "timeout",
    "internal_error",
    "deadline_exceeded",  # レビュー対応 F3
)
VALUE_MARK = "TOOL_INPUT_VALUE_MARK_7719"
MAIN = "src/agent/main.py"
# 複製の外を指すパス（どのツールでも ``denied``）
OUTSIDE_PATHS = [
    "/etc/passwd",
    "../outside/secret.py",
    "src/../../outside/secret.py",
    "..",
    ".git/config",
    "src\\agent",
]


def make_session(workspace, *, limits=None, runner=None, **kwargs):
    from src.fixer.models import FixLimits
    from src.fixer.tools import FixToolSession

    return FixToolSession(workspace, limits or FixLimits(), runner or FakeTestRunner(), **kwargs)


def small_limits(**overrides):
    from src.fixer.models import FixLimits

    return FixLimits(**overrides)


def root_of(workspace) -> Path:
    return Path(workspace.root)


def snapshot(workspace) -> dict[str, bytes]:
    root = root_of(workspace)
    return {rel: (root / rel).read_bytes() for rel in fakes.list_tree(root) if (root / rel).is_file()}


def assert_small_and_clean(result, *forbidden: str) -> None:
    text = result.to_text()
    assert len(text) <= 4_000
    for value in forbidden:
        assert value not in text


# ---------------------------------------------------------------------------
# 決まった値
# ---------------------------------------------------------------------------


class TestConstants:
    def test_models_tool_names_and_outcomes_match_the_design(self):
        from src.fixer import models

        assert tuple(models.TOOL_NAMES) == TOOL_NAMES
        assert set(models.TOOL_OUTCOMES) == set(TOOL_OUTCOMES)
        assert len(models.TOOL_OUTCOMES) == len(TOOL_OUTCOMES)
        assert tuple(models.RUN_STATUSES) == (
            "proposed",
            "rejected",
            "declined",
            "incomplete",
            "not_eligible",
            "failed",
        )
        assert tuple(models.GATE_IDS) == ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10")
        assert tuple(models.PATCH_KINDS) == ("symptomatic", "structural", "unspecified")

    def test_fix_limits_defaults_match_the_design(self):
        """R9: 上限の既定値。すべて設定で変えられる（``FixLimits`` の引数）。"""
        from src.fixer.models import FixLimits

        limits = FixLimits()

        assert limits.max_turns == 30
        assert limits.timeout_seconds == 1200.0
        assert limits.max_tool_calls == 120
        assert limits.max_test_runs == 10
        assert limits.test_timeout_seconds == 300.0
        assert limits.max_read_bytes == 60_000
        assert limits.max_write_bytes == 200_000
        assert limits.max_changed_files == 5
        assert limits.max_changed_lines == 200
        assert limits.max_explanation_chars == 800
        assert limits.max_budget_usd is None
        assert FixLimits(max_turns=3, max_test_runs=1).max_turns == 3

    def test_tool_result_reports_outcome_as_json_text_and_error_flag(self, workspace):
        session = make_session(workspace)

        good = session.call("read_file", {"path": SRC})
        bad = session.call("read_file", {"path": "/etc/passwd"})

        assert good.is_error is False
        assert bad.is_error is True
        assert json.loads(good.to_text())["outcome"] == "ok"
        assert json.loads(bad.to_text())["outcome"] == "denied"


# ---------------------------------------------------------------------------
# 読むツール
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_list_files_directory_returns_names_and_kinds(self, workspace):
        result = make_session(workspace).call("list_files", {"path": "src/agent"})

        assert result.outcome == "ok"
        entries = {entry["name"]: entry["type"] for entry in result.data["entries"]}
        assert entries["orchestrator.py"] == "file"
        assert entries["storage"] == "dir"
        assert result.data["truncated"] is False

    def test_list_files_hides_cache_directories(self, workspace):
        root = root_of(workspace)
        (root / "src" / "agent" / "__pycache__").mkdir()
        (root / "src" / "agent" / "__pycache__" / "x.pyc").write_bytes(b"x")
        (root / "src" / ".pytest_cache").mkdir()
        (root / "src" / ".ruff_cache").mkdir()

        agent = make_session(workspace).call("list_files", {"path": "src/agent"})
        src = make_session(workspace).call("list_files", {"path": "src"})

        assert "__pycache__" not in [entry["name"] for entry in agent.data["entries"]]
        assert {".pytest_cache", ".ruff_cache"}.isdisjoint(entry["name"] for entry in src.data["entries"])

    def test_list_files_more_than_200_entries_is_truncated(self, workspace):
        many = root_of(workspace) / "src" / "agent" / "many"
        many.mkdir()
        for index in range(260):
            (many / f"module_{index:03d}.py").write_text("X = 1\n", encoding="utf-8")

        result = make_session(workspace).call("list_files", {"path": "src/agent/many"})

        assert result.outcome == "ok"
        assert len(result.data["entries"]) == 200
        assert result.data["truncated"] is True

    def test_list_files_missing_directory_returns_not_found(self, workspace):
        assert make_session(workspace).call("list_files", {"path": "src/nope"}).outcome == "not_found"

    def test_list_files_on_a_file_is_an_error(self, workspace):
        assert make_session(workspace).call("list_files", {"path": SRC}).outcome == "invalid_input"

    def test_list_files_path_outside_the_workspace_returns_denied(self, workspace, outside):
        """AC-6: 一覧のツール。複製の外は、呼び出しの時点で拒否する。"""
        cases = cases_of("path", OUTSIDE_PATHS)

        def check(path):
            result = make_session(workspace).call("list_files", {"path": path})

            assert result.outcome == "denied"
            assert "secret.py" not in result.to_text()

        check_each(cases, check)

    def test_list_files_symlinked_directory_outside_returns_denied_without_listing(self, workspace, outside):
        os.symlink(outside, root_of(workspace) / "src" / "agent" / "linkdir")

        result = make_session(workspace).call("list_files", {"path": "src/agent/linkdir"})

        assert result.outcome == "denied"
        assert "secret.py" not in result.to_text()


class TestReadFile:
    def test_read_file_returns_numbered_content_and_line_count(self, workspace):
        result = make_session(workspace).call("read_file", {"path": SRC})

        assert result.outcome == "ok"
        lines = FILES[SRC].splitlines()
        assert result.data["total_lines"] == len(lines)
        assert result.data["truncated"] is False
        shown = result.data["content"].splitlines()
        for number, text in enumerate(lines, start=1):
            if text.strip():
                assert any(str(number) in line and text in line for line in shown), (number, text)

    def test_read_file_window_returns_only_the_requested_lines(self, workspace):
        start = FILES[SRC].splitlines().index("def average(values):") + 1

        result = make_session(workspace).call("read_file", {"path": SRC, "start_line": start, "max_lines": 2})

        content = result.data["content"]
        assert "def average(values):" in content
        assert "total = sum(values)" in content
        assert "return total / len(values)" not in content
        assert "class WorkflowError" not in content
        assert result.data["truncated"] is True
        assert result.data["total_lines"] == len(FILES[SRC].splitlines())

    def test_read_file_default_window_is_400_lines(self, workspace):
        body = "".join(f"line_{index:04d} = {index}\n" for index in range(1, 1001))
        (root_of(workspace) / "src" / "agent" / "long.py").write_text(body, encoding="utf-8")

        result = make_session(workspace).call("read_file", {"path": "src/agent/long.py"})

        assert "line_0400" in result.data["content"]
        assert "line_0401" not in result.data["content"]
        assert result.data["truncated"] is True
        assert result.data["total_lines"] == 1000

    def test_read_file_can_read_files_that_cannot_be_written(self, workspace):
        session = make_session(workspace)

        for path in (MAIN, EXISTING_TEST, "README.md", "pyproject.toml"):
            assert session.call("read_file", {"path": path}).outcome == "ok", path

    def test_read_file_missing_file_returns_not_found(self, workspace):
        assert make_session(workspace).call("read_file", {"path": "src/agent/nope.py"}).outcome == "not_found"

    def test_read_file_on_a_directory_is_an_error(self, workspace):
        assert make_session(workspace).call("read_file", {"path": "src/agent"}).outcome == "invalid_input"

    def test_read_file_path_outside_the_workspace_returns_denied(self, workspace, outside):
        """AC-6: 読み取りのツール。"""
        cases = cases_of("path", OUTSIDE_PATHS)

        def check(path):
            result = make_session(workspace).call("read_file", {"path": path})

            assert result.outcome == "denied"
            assert OUTSIDE_MARK not in result.to_text()

        check_each(cases, check)

    def test_read_file_absolute_path_of_an_outside_file_is_never_read(self, workspace, outside):
        result = make_session(workspace).call("read_file", {"path": str(outside / "secret.py")})

        assert result.outcome == "denied"
        assert OUTSIDE_MARK not in result.to_text()

    @pytest.mark.parametrize("kind", ["file", "directory", "relative", "chain"])
    def test_read_file_symlink_leading_outside_returns_denied_without_reading(self, workspace, outside, kind):
        """AC-6: 複製の外を指すシンボリックリンク（修正役が走らせたテストのコードが作りうる）。"""
        agent = root_of(workspace) / "src" / "agent"
        if kind == "file":
            os.symlink(outside / "secret.py", agent / "leak.py")
            path = "src/agent/leak.py"
        elif kind == "directory":
            os.symlink(outside, agent / "linkdir")
            path = "src/agent/linkdir/secret.py"
        elif kind == "relative":
            os.symlink(os.path.relpath(outside / "secret.py", agent), agent / "leak.py")
            path = "src/agent/leak.py"
        else:
            os.symlink(outside / "secret.py", agent / "hop2.py")
            os.symlink("hop2.py", agent / "leak.py")
            path = "src/agent/leak.py"

        result = make_session(workspace).call("read_file", {"path": path})

        assert result.outcome == "denied"
        assert OUTSIDE_MARK not in result.to_text()

    @pytest.mark.parametrize(
        "payload",
        [b"\xff\xfe\x00\x01", "caf\xe9 = 1\n".encode("latin-1"), b"X = 1\n\xc3", b"\x80" * 100_000],
        ids=["binary", "latin-1", "cut-multibyte", "large-binary"],
    )
    def test_read_file_content_that_is_not_utf8_returns_invalid_input(self, workspace, payload):
        """design.md §7: UTF-8 で読めないファイルは ``invalid_input``。中身は返さない。"""
        (root_of(workspace) / "src" / "agent" / "blob.py").write_bytes(payload)

        result = make_session(workspace).call("read_file", {"path": "src/agent/blob.py"})

        assert result.outcome == "invalid_input"
        assert len(result.to_text()) <= 4_000

    def test_read_file_larger_than_the_read_limit_is_cut_and_marked(self, workspace):
        """上限: 1 回の読み取りは ``max_read_bytes`` まで。"""
        body = "".join(f"value_{index:05d} = '{'x' * 80}'\n" for index in range(5_000))
        (root_of(workspace) / "src" / "agent" / "big.py").write_text(body, encoding="utf-8")
        limits = small_limits(max_read_bytes=5_000)

        result = make_session(workspace, limits=limits).call(
            "read_file", {"path": "src/agent/big.py", "max_lines": 400}
        )

        assert result.outcome == "ok"
        assert result.data["truncated"] is True
        assert len(result.data["content"].encode()) <= 5_000 + 400 * 12
        assert "value_04999" not in result.data["content"]

    def test_read_file_one_very_long_line_is_cut_to_the_read_limit(self, workspace):
        """上限のすり抜け: 行数では 1 行でも、大きさの上限を超えるファイル。"""
        (root_of(workspace) / "src" / "agent" / "oneline.py").write_text(
            "X = '" + "a" * 1_000_000 + "'\n", encoding="utf-8"
        )
        limits = small_limits(max_read_bytes=5_000)

        result = make_session(workspace, limits=limits).call("read_file", {"path": "src/agent/oneline.py"})

        assert result.outcome == "ok"
        assert len(result.to_text().encode()) <= 5_000 + 1_000
        if result.outcome == "ok":
            assert result.data["truncated"] is True

    def test_read_file_multibyte_content_is_limited_by_bytes_not_characters(self, workspace):
        (root_of(workspace) / "src" / "agent" / "wide.py").write_text("# " + "あ" * 4_000 + "\n", encoding="utf-8")
        limits = small_limits(max_read_bytes=5_000)

        result = make_session(workspace, limits=limits).call("read_file", {"path": "src/agent/wide.py"})

        assert result.outcome == "ok"
        assert len(result.data.get("content", "").encode()) <= 5_000 + 100

    def test_read_file_malformed_input_returns_invalid_input(self, workspace):
        cases = cases_of(
            "args",
            [
                {},
                {"path": None},
                {"path": 3},
                {"path": ["src/agent/orchestrator.py"]},
                {"path": {"value": SRC}},
                {"path": SRC, "start_line": 0},
                {"path": SRC, "start_line": -1},
                {"path": SRC, "start_line": "1"},
                {"path": SRC, "start_line": 1.5},
                {"path": SRC, "start_line": True},
                {"path": SRC, "max_lines": 0},
                {"path": SRC, "max_lines": -5},
                {"path": SRC, "max_lines": "10"},
                {"path": SRC, "max_lines": True},
                {"path": SRC, "max_lines": [10]},
                {"path": SRC, "encoding": "latin-1"},
                {"path": SRC, "follow_symlinks": True},
                {"file_path": SRC},
            ],
        )

        def check(args):
            result = make_session(workspace).call("read_file", args)

            assert result.outcome == "invalid_input"
            assert isinstance(result.data["invalid_fields"], list)
            assert result.data["invalid_fields"]

        check_each(cases, check)


class TestSearchCode:
    def test_search_code_returns_matching_lines_with_path_and_line_number(self, workspace):
        result = make_session(workspace).call("search_code", {"pattern": "def average"})

        assert result.outcome == "ok"
        line = FILES[SRC].splitlines().index("def average(values):") + 1
        matches = [(item["path"], item["line"]) for item in result.data["matches"]]
        assert (SRC, line) in matches
        assert all("def average" in item["text"] for item in result.data["matches"])

    def test_search_code_pattern_is_a_fixed_string_not_a_regular_expression(self, workspace):
        session = make_session(workspace)

        literal = session.call("search_code", {"pattern": "average(values)"})
        regex = session.call("search_code", {"pattern": "aver.ge"})
        wildcard = session.call("search_code", {"pattern": ".*"})

        assert literal.outcome == "ok"
        assert len(literal.data["matches"]) >= 1
        assert regex.data["matches"] == []
        assert wildcard.data["matches"] == []

    def test_search_code_only_searches_python_files(self, workspace):
        result = make_session(workspace).call("search_code", {"pattern": "sample"})

        paths = {item["path"] for item in result.data["matches"]}
        assert EXISTING_CONFTEST in paths
        assert all(path.endswith(".py") for path in paths)

    def test_search_code_path_limits_the_search_to_a_directory(self, workspace):
        result = make_session(workspace).call("search_code", {"pattern": "average", "path": "tests"})

        paths = {item["path"] for item in result.data["matches"]}
        assert paths == {EXISTING_TEST}

    def test_search_code_more_than_100_matches_is_truncated(self, workspace):
        (root_of(workspace) / "src" / "agent" / "many.py").write_text("needle_mark = 1\n" * 300, encoding="utf-8")

        result = make_session(workspace).call("search_code", {"pattern": "needle_mark"})

        assert len(result.data["matches"]) == 100
        assert result.data["truncated"] is True

    def test_search_code_does_not_follow_symlinks_out_of_the_workspace(self, workspace, outside):
        """AC-6: 検索のツール。複製の外のファイルは読まれない。"""
        agent = root_of(workspace) / "src" / "agent"
        os.symlink(outside, agent / "linkdir")
        os.symlink(outside / "secret.py", agent / "leak.py")
        session = make_session(workspace)

        everywhere = session.call("search_code", {"pattern": OUTSIDE_MARK})
        in_src = session.call("search_code", {"pattern": OUTSIDE_MARK, "path": "src"})
        through_link = session.call("search_code", {"pattern": OUTSIDE_MARK, "path": "src/agent/linkdir"})

        assert everywhere.data.get("matches", []) == []
        assert in_src.data.get("matches", []) == []
        assert through_link.outcome == "denied"
        for result in (everywhere, in_src, through_link):
            assert "secret.py" not in result.to_text()

    def test_search_code_path_outside_the_workspace_returns_denied(self, workspace, outside):
        cases = cases_of("path", OUTSIDE_PATHS)

        def check(path):
            result = make_session(workspace).call("search_code", {"pattern": "MARK", "path": path})

            assert result.outcome == "denied"

        check_each(cases, check)

    def test_search_code_skips_unreadable_files_and_still_finds_others(self, workspace):
        agent = root_of(workspace) / "src" / "agent"
        (agent / "blob.py").write_bytes(b"\xff\xfe def average\n")
        (agent / "nul.py").write_bytes(b"\x00" * 10_000)
        (agent / "huge.py").write_text("X = '" + "a" * 2_000_000 + "'\n", encoding="utf-8")

        result = make_session(workspace).call("search_code", {"pattern": "def average"})

        assert result.outcome == "ok"
        assert SRC in {item["path"] for item in result.data["matches"]}

    def test_search_code_missing_directory_returns_not_found(self, workspace):
        assert make_session(workspace).call("search_code", {"pattern": "x", "path": "src/nope"}).outcome == "not_found"

    def test_search_code_malformed_input_returns_invalid_input(self, workspace):
        cases = cases_of(
            "args",
            [
                {},
                {"pattern": None},
                {"pattern": 3},
                {"pattern": ["average"]},
                {"pattern": "a" * 201},
                {"pattern": "average", "path": 3},
                {"pattern": "average", "regex": True},
                {"pattern": "average", "glob": "*.md"},
                {"query": "average"},
            ],
        )

        def check(args):
            assert make_session(workspace).call("search_code", args).outcome == "invalid_input"

        check_each(cases, check)

    def test_search_code_pattern_of_200_characters_is_accepted(self, workspace):
        assert make_session(workspace).call("search_code", {"pattern": "a" * 200}).outcome == "ok"


# ---------------------------------------------------------------------------
# 書くツール
# ---------------------------------------------------------------------------

DENIED_WRITES = [
    # src/・tests/ の外
    "README.md",
    "pyproject.toml",
    "template.yaml",
    "CLAUDE.md",
    ".github/workflows/x.yml",
    ".steering/x/requirements.md",
    "docs/guide.md",
    "new_top_level.py",
    "conftest.py",
    # 禁止の場所
    "src/fixer/gates.py",
    "src/fixer/new_module.py",
    "src/agent/diagnosis/tools.py",
    "src/agent/prompts/builder.py",
    MAIN,
    "src/trigger/slack_verify.py",
    "src/trigger/app.py",
    "src/token_monitor/handler.py",
    "src/dashboard_api/authorizer/app.py",
    "src/dashboard_api/auth_login/app.py",
    "src/agent/requirements.txt",
    "src/agent/Dockerfile",
    "src/agent/conftest.py",
    "src/agent/sitecustomize.py",
    "tests/unit/fixer/test_new.py",
    "tests/unit/agent/diagnosis/test_new.py",
    # 既存のテストと、テストの集め方を変えるファイル
    EXISTING_TEST,
    EXISTING_CONFTEST,
    "tests/unit/__init__.py",
    "tests/conftest.py",
    "tests/unit/new_package/conftest.py",
    "tests/unit/helper.py",
    "tests/unit/pytest.ini",
    # .py でないもの
    "src/agent/data.json",
    "src/agent/script.sh",
    *OUTSIDE_PATHS,
]


class TestWriteFile:
    def test_write_file_creates_a_new_test_file_and_reports_bytes(self, workspace):
        session = make_session(workspace)

        result = session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})

        assert result.outcome == "ok"
        assert result.data["bytes_written"] == len(NEW_TEST_SOURCE.encode())
        target = root_of(workspace) / NEW_TEST
        assert target.read_text(encoding="utf-8") == NEW_TEST_SOURCE
        assert not os.access(target, os.X_OK)
        assert not target.is_symlink()

    def test_write_file_creates_missing_directories_under_tests(self, workspace):
        session = make_session(workspace)

        first = session.call("write_file", {"path": "tests/unit/new_package/__init__.py", "content": ""})
        second = session.call(
            "write_file", {"path": "tests/unit/new_package/test_inside.py", "content": NEW_TEST_SOURCE}
        )

        assert (first.outcome, second.outcome) == ("ok", "ok")
        assert (root_of(workspace) / "tests/unit/new_package/test_inside.py").is_file()

    def test_write_file_rewrites_an_existing_source_file(self, workspace):
        fixed = FILES[SRC].replace(FIX_OLD, FIX_NEW)

        result = make_session(workspace).call("write_file", {"path": SRC, "content": fixed})

        assert result.outcome == "ok"
        assert (root_of(workspace) / SRC).read_text(encoding="utf-8") == fixed

    def test_write_file_can_rewrite_a_test_file_it_created(self, workspace):
        session = make_session(workspace)
        session.call("write_file", {"path": NEW_TEST, "content": "def test_draft():\n    assert False\n"})

        result = session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})

        assert result.outcome == "ok"
        assert (root_of(workspace) / NEW_TEST).read_text(encoding="utf-8") == NEW_TEST_SOURCE

    def test_write_file_forbidden_place_returns_denied_and_writes_nothing(self, workspace, outside):
        """R6a・AC-6: 書き込みは ``src/``・``tests/`` の下だけ。禁止の場所・既存のテストも、その場で拒否する。"""
        cases = cases_of("path", DENIED_WRITES)

        def check(path):
            before = snapshot(workspace)

            result = make_session(workspace).call("write_file", {"path": path, "content": f"# {VALUE_MARK}\n"})

            assert result.outcome == "denied"
            assert snapshot(workspace) == before
            assert (outside / "secret.py").read_text(encoding="utf-8") == f"# {OUTSIDE_MARK}\n"
            assert sorted(item.name for item in outside.iterdir()) == ["secret.py"]
            assert_small_and_clean(result, VALUE_MARK)

        check_each(cases, check)

    @pytest.mark.parametrize(
        "kind", ["directory-outside", "directory-inside", "file-outside", "file-inside", "dangling"]
    )
    def test_write_file_through_a_symlink_returns_denied_and_writes_nothing(self, workspace, outside, kind):
        """AC-6: シンボリックリンク越しの書き込み（先が複製の中でも外でも）。"""
        agent = root_of(workspace) / "src" / "agent"
        if kind == "directory-outside":
            os.symlink(outside, agent / "linkdir")
            path = "src/agent/linkdir/planted.py"
        elif kind == "directory-inside":
            os.symlink("diagnosis", agent / "harmless")
            path = "src/agent/harmless/tools.py"
        elif kind == "file-outside":
            os.symlink(outside / "secret.py", agent / "alias.py")
            path = "src/agent/alias.py"
        elif kind == "file-inside":
            os.symlink("main.py", agent / "alias.py")
            path = "src/agent/alias.py"
        else:
            os.symlink(outside / "planted.py", agent / "alias.py")
            path = "src/agent/alias.py"

        result = make_session(workspace).call("write_file", {"path": path, "content": f"# {VALUE_MARK}\n"})

        assert result.outcome == "denied"
        assert sorted(item.name for item in outside.iterdir()) == ["secret.py"]
        assert (outside / "secret.py").read_text(encoding="utf-8") == f"# {OUTSIDE_MARK}\n"
        assert (agent / "main.py").read_text(encoding="utf-8") == FILES[MAIN]
        assert (agent / "diagnosis" / "tools.py").read_text(encoding="utf-8") == FILES["src/agent/diagnosis/tools.py"]

    @pytest.mark.parametrize(
        "path", ["src/agent/MAIN.py", "src/agent/Main.py", "src/FIXER/gates.py", "tests/unit/Test_orchestrator.py"]
    )
    def test_write_file_case_variant_of_a_forbidden_path_never_changes_the_forbidden_file(self, workspace, path):
        """大文字と小文字を区別しないファイルシステム（macOS の既定）では、別の綴りで同じファイルに届く。"""
        before = {rel: (root_of(workspace) / rel).read_bytes() for rel in (MAIN, "src/fixer/gates.py", EXISTING_TEST)}

        make_session(workspace).call("write_file", {"path": path, "content": f"# {VALUE_MARK}\n"})

        after = {rel: (root_of(workspace) / rel).read_bytes() for rel in before}
        assert after == before

    def test_write_file_content_at_the_limit_is_written_and_one_byte_over_is_refused(self, workspace):
        limits = small_limits(max_write_bytes=100)
        session = make_session(workspace, limits=limits)

        at_limit = session.call("write_file", {"path": "src/agent/at_limit.py", "content": "#" * 99 + "\n"})
        over = session.call("write_file", {"path": "src/agent/over.py", "content": "#" * 100 + "\n"})

        assert at_limit.outcome == "ok"
        assert over.outcome == "size_limit"
        assert not (root_of(workspace) / "src/agent/over.py").exists()

    def test_write_file_limit_counts_bytes_not_characters(self, workspace):
        """上限のすり抜け: 字数では上限以内でも、UTF-8 のバイト数で超える。"""
        limits = small_limits(max_write_bytes=100)
        content = "# " + "あ" * 40 + "\n"
        assert len(content) < 100 < len(content.encode())

        result = make_session(workspace, limits=limits).call(
            "write_file", {"path": "src/agent/wide.py", "content": content}
        )

        assert result.outcome == "size_limit"
        assert not (root_of(workspace) / "src/agent/wide.py").exists()

    def test_write_file_over_the_default_limit_is_refused_without_echoing_content(self, workspace):
        content = f"# {VALUE_MARK}\n" + "x = 1\n" * 40_000
        assert len(content.encode()) > 200_000

        result = make_session(workspace).call("write_file", {"path": "src/agent/huge.py", "content": content})

        assert result.outcome == "size_limit"
        assert not (root_of(workspace) / "src/agent/huge.py").exists()
        assert_small_and_clean(result, VALUE_MARK)

    def test_write_file_content_that_cannot_be_encoded_is_an_error_and_writes_nothing(self, workspace):
        """JSON は、単独のサロゲート（``\\ud800``）を運べる。UTF-8 にできない中身でも、例外を外に出さない。"""
        result = make_session(workspace).call("write_file", {"path": "src/agent/odd.py", "content": "X = '\ud800'\n"})

        assert (result.outcome, result.data) == ("invalid_input", {"invalid_fields": ["content"]})
        assert not (root_of(workspace) / "src/agent/odd.py").exists()

    def test_write_file_malformed_input_returns_invalid_input_and_writes_nothing(self, workspace):
        cases = cases_of(
            "args",
            [
                {},
                {"path": NEW_TEST},
                {"content": "x"},
                {"path": NEW_TEST, "content": None},
                {"path": NEW_TEST, "content": 3},
                {"path": NEW_TEST, "content": ["x = 1"]},
                {"path": NEW_TEST, "content": {"text": "x"}},
                {"path": 3, "content": "x"},
                {"path": [NEW_TEST], "content": "x"},
                {"path": NEW_TEST, "content": "x", "mode": "0755"},
                {"path": NEW_TEST, "content": "x", "executable": True},
                {"path": NEW_TEST, "content": "x", "append": True},
            ],
        )

        def check(args):
            before = snapshot(workspace)

            result = make_session(workspace).call("write_file", args)

            assert result.outcome == "invalid_input"
            assert snapshot(workspace) == before

        check_each(cases, check)


class TestReplaceInFile:
    def test_replace_in_file_single_occurrence_is_replaced_and_line_is_reported(self, workspace):
        result = make_session(workspace).call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})

        assert result.outcome == "ok"
        assert result.data["line"] == FILES[SRC].splitlines().index("    total = sum(values)") + 1
        assert (root_of(workspace) / SRC).read_text(encoding="utf-8") == FILES[SRC].replace(FIX_OLD, FIX_NEW)

    @pytest.mark.parametrize(
        ("old", "outcome"),
        [
            ("no such text in the file", "not_found"),
            ("return", "invalid_input"),
            ("", "invalid_input"),
            ("\n", "invalid_input"),
        ],
        ids=["absent", "repeated", "empty", "newline"],
    )
    def test_replace_in_file_old_text_not_found_exactly_once_is_an_error_and_changes_nothing(
        self, workspace, old, outcome
    ):
        """design.md §7・§19.3 の 21: ``old`` がちょうど 1 か所に現れるときだけ置き換える。"""
        result = make_session(workspace).call("replace_in_file", {"path": SRC, "old": old, "new": "X"})

        assert result.outcome == outcome
        assert (root_of(workspace) / SRC).read_text(encoding="utf-8") == FILES[SRC]

    def test_replace_in_file_forbidden_place_returns_denied_and_changes_nothing(self, workspace, outside):
        cases = cases_of(
            "path",
            [
                MAIN,
                "src/fixer/gates.py",
                "src/agent/diagnosis/tools.py",
                EXISTING_TEST,
                EXISTING_CONFTEST,
                "README.md",
                "pyproject.toml",
                *OUTSIDE_PATHS,
            ],
        )

        def check(path):
            before = snapshot(workspace)

            result = make_session(workspace).call("replace_in_file", {"path": path, "old": "return", "new": VALUE_MARK})

            assert result.outcome == "denied"
            assert snapshot(workspace) == before
            assert (outside / "secret.py").read_text(encoding="utf-8") == f"# {OUTSIDE_MARK}\n"

        check_each(cases, check)

    def test_replace_in_file_through_a_symlink_returns_denied(self, workspace, outside):
        os.symlink(outside / "secret.py", root_of(workspace) / "src" / "agent" / "alias.py")

        result = make_session(workspace).call(
            "replace_in_file", {"path": "src/agent/alias.py", "old": OUTSIDE_MARK, "new": "X"}
        )

        assert result.outcome == "denied"
        assert (outside / "secret.py").read_text(encoding="utf-8") == f"# {OUTSIDE_MARK}\n"

    def test_replace_in_file_missing_file_returns_not_found(self, workspace):
        result = make_session(workspace).call("replace_in_file", {"path": "src/agent/nope.py", "old": "a", "new": "b"})

        assert result.outcome == "not_found"
        assert not (root_of(workspace) / "src/agent/nope.py").exists()

    def test_replace_in_file_result_over_the_write_limit_is_refused(self, workspace):
        limits = small_limits(max_write_bytes=len(FILES[SRC].encode()) + 50)

        result = make_session(workspace, limits=limits).call(
            "replace_in_file", {"path": SRC, "old": FIX_OLD, "new": "#" * 200 + "\n"}
        )

        assert result.outcome == "size_limit"
        assert (root_of(workspace) / SRC).read_text(encoding="utf-8") == FILES[SRC]

    def test_replace_in_file_target_that_is_not_utf8_returns_invalid_input(self, workspace):
        target = root_of(workspace) / "src" / "agent" / "blob.py"
        payload = b"\xff\xfe marker = 1\n"
        target.write_bytes(payload)

        result = make_session(workspace).call(
            "replace_in_file", {"path": "src/agent/blob.py", "old": "marker", "new": "x"}
        )

        assert result.outcome == "invalid_input"
        assert target.read_bytes() == payload

    def test_replace_in_file_malformed_input_returns_invalid_input_and_changes_nothing(self, workspace):
        cases = cases_of(
            "args",
            [
                {},
                {"path": SRC, "old": FIX_OLD},
                {"path": SRC, "new": FIX_NEW},
                {"old": FIX_OLD, "new": FIX_NEW},
                {"path": SRC, "old": None, "new": "x"},
                {"path": SRC, "old": FIX_OLD, "new": 3},
                {"path": SRC, "old": [FIX_OLD], "new": "x"},
                {"path": SRC, "old": FIX_OLD, "new": FIX_NEW, "count": 2},
                {"path": SRC, "old": FIX_OLD, "new": FIX_NEW, "regex": True},
            ],
        )

        def check(args):
            result = make_session(workspace).call("replace_in_file", args)

            assert result.outcome == "invalid_input"
            assert (root_of(workspace) / SRC).read_text(encoding="utf-8") == FILES[SRC]

        check_each(cases, check)


# ---------------------------------------------------------------------------
# AC-7: テスト実行ツール
# ---------------------------------------------------------------------------


def session_with_new_test(workspace, **kwargs):
    runner = kwargs.pop("runner", None) or FakeTestRunner()
    session = make_session(workspace, runner=runner, **kwargs)
    assert session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE}).outcome == "ok"
    return session, runner


class TestRunTests:
    def test_run_tests_runs_the_given_path_in_the_workspace_and_returns_counts(self, workspace):
        """AC-7: 複製内のテストのパスを指定 → 成功・失敗の件数が返る。"""
        session, runner = session_with_new_test(workspace)

        result = session.call("run_tests", {"paths": [NEW_TEST]})

        assert result.outcome == "ok"
        assert (result.data["passed"], result.data["failed"], result.data["errors"]) == (0, 1, 0)
        assert len(result.data["failed_tests"]) == 1
        assert "test_average_empty" in result.data["failed_tests"][0]
        assert isinstance(result.data["output_tail"], str)
        assert len(runner.calls) == 1
        assert runner.calls[0].tree.resolve() == root_of(workspace).resolve()
        assert runner.calls[0].paths == (NEW_TEST,)
        assert runner.calls[0].ignore == ()

    def test_run_tests_after_the_fix_reports_the_test_as_passed(self, workspace):
        session, _runner = session_with_new_test(workspace)
        session.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})

        result = session.call("run_tests", {"paths": [NEW_TEST]})

        assert (result.data["passed"], result.data["failed"], result.data["errors"]) == (1, 0, 0)
        assert result.data["failed_tests"] == []

    def test_run_tests_accepts_a_directory_and_existing_tests(self, workspace):
        session, runner = session_with_new_test(workspace)

        result = session.call("run_tests", {"paths": ["tests/unit", EXISTING_TEST]})

        assert result.outcome == "ok"
        assert runner.calls[0].paths == ("tests/unit", EXISTING_TEST)
        assert result.data["passed"] == 2
        assert result.data["failed"] == 2

    def test_run_tests_counts_errors_separately(self, workspace):
        runner = FakeTestRunner(new_before="error")
        session, _ = session_with_new_test(workspace, runner=runner)

        result = session.call("run_tests", {"paths": [NEW_TEST]})

        assert (result.data["passed"], result.data["failed"], result.data["errors"]) == (0, 0, 1)

    def test_run_tests_option_or_separator_in_paths_returns_invalid_input_and_runs_nothing(self, workspace):
        """AC-7: オプションの文字列（``-`` で始まる値）・区切り文字（``::``）を混ぜる → 何も実行されない。"""
        cases = cases_of(
            "paths",
            [
                ["-x"],
                ["--collect-only"],
                ["-p", "no:cacheprovider"],
                ["--rootdir=/"],
                ["-c", "/etc/passwd"],
                [NEW_TEST, "-x"],
                [NEW_TEST, "--junitxml=/tmp/planted.xml"],
                [NEW_TEST + "::test_average_of_empty_list_is_zero"],
                ["tests/unit::x"],
                ["--"],
            ],
            ids=lambda paths: "_".join(paths)[-40:],
        )

        def check(paths):
            session, runner = session_with_new_test(workspace)

            result = session.call("run_tests", {"paths": paths})

            assert result.outcome == "invalid_input"
            assert runner.calls == []

        check_each(cases, check)

    def test_run_tests_path_outside_the_workspace_or_outside_tests_is_refused_and_runs_nothing(
        self, workspace, outside
    ):
        """AC-7: 複製の外のパスを混ぜる → 何も実行されない。

        design.md §19.2 の 7: 複製の外・``tests/`` の外は ``denied``（読み取りのツールと、分類を揃える）。
        """
        cases = cases_of(
            "paths",
            [
                ["/etc"],
                ["../outside"],
                ["tests/../../outside"],
                ["tests/unit/../../src/agent"],
                [NEW_TEST, "../outside/secret.py"],
                ["src/agent"],
                [SRC],
                ["README.md"],
                [".git"],
            ],
            ids=lambda paths: "_".join(paths)[-40:],
        )

        def check(paths):
            session, runner = session_with_new_test(workspace)

            result = session.call("run_tests", {"paths": paths})

            assert result.outcome == "denied"
            assert runner.calls == []

        check_each(cases, check)

    def test_run_tests_symlink_under_tests_pointing_outside_is_refused(self, workspace, outside):
        os.symlink(outside, root_of(workspace) / "tests" / "unit" / "linked")
        session, runner = session_with_new_test(workspace)

        result = session.call("run_tests", {"paths": ["tests/unit/linked"]})

        assert result.outcome == "denied"
        assert runner.calls == []

    def test_run_tests_missing_path_returns_not_found_and_runs_nothing(self, workspace):
        session, runner = session_with_new_test(workspace)

        result = session.call("run_tests", {"paths": ["tests/unit/test_nope.py"]})

        assert result.outcome == "not_found"
        assert runner.calls == []

    def test_run_tests_malformed_input_returns_invalid_input_and_runs_nothing(self, workspace):
        cases = cases_of(
            "args",
            [
                {},
                {"paths": NEW_TEST},
                {"paths": []},
                {"paths": None},
                {"paths": [3]},
                {"paths": [None]},
                {"paths": [[NEW_TEST]]},
                {"paths": {"0": NEW_TEST}},
                {"paths": ["tests/unit"] * 6},
                {"paths": [NEW_TEST], "args": ["-x"]},
                {"paths": [NEW_TEST], "ignore": [EXISTING_TEST]},
                {"paths": [NEW_TEST], "timeout": 99999},
                {"paths": [NEW_TEST], "env": {"X": "1"}},
                {"path": NEW_TEST},
            ],
        )

        def check(args):
            session, runner = session_with_new_test(workspace)

            result = session.call("run_tests", args)

            assert result.outcome == "invalid_input"
            assert runner.calls == []

        check_each(cases, check)

    def test_run_tests_five_paths_are_accepted(self, workspace):
        session, runner = session_with_new_test(workspace)

        result = session.call(
            "run_tests", {"paths": [NEW_TEST, EXISTING_TEST, "tests/unit", "tests/unit/fixer", NEW_TEST]}
        )

        assert result.outcome == "ok"
        assert len(runner.calls) == 1

    def test_run_tests_over_the_run_limit_returns_call_limit_and_runs_nothing_more(self, workspace):
        """AC-7: 回数の上限を超える → ``call_limit``。"""
        session, runner = session_with_new_test(workspace, limits=small_limits(max_test_runs=2))

        outcomes = [session.call("run_tests", {"paths": [NEW_TEST]}).outcome for _ in range(4)]

        assert outcomes == ["ok", "ok", "call_limit", "call_limit"]
        assert len(runner.calls) == 2

    def test_run_tests_default_limit_is_ten_runs(self, workspace):
        session, runner = session_with_new_test(workspace)

        outcomes = [session.call("run_tests", {"paths": [NEW_TEST]}).outcome for _ in range(11)]

        assert outcomes == ["ok"] * 10 + ["call_limit"]
        assert len(runner.calls) == 10

    def test_run_tests_rejected_calls_also_use_up_the_run_limit(self, workspace):
        """R7b: 検証に落ちた呼び出しも、回数に数える。"""
        session, runner = session_with_new_test(workspace, limits=small_limits(max_test_runs=2))

        first = session.call("run_tests", {"paths": ["-x"]})
        second = session.call("run_tests", {"paths": NEW_TEST})
        third = session.call("run_tests", {"paths": [NEW_TEST]})

        assert (first.outcome, second.outcome, third.outcome) == ("invalid_input", "invalid_input", "call_limit")
        assert runner.calls == []

    def test_run_tests_run_limit_is_separate_from_the_tool_call_limit(self, workspace):
        limits = small_limits(max_tool_calls=2, max_test_runs=2)
        session = make_session(workspace, limits=limits)
        session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
        session.call("read_file", {"path": SRC})

        assert session.call("read_file", {"path": SRC}).outcome == "call_limit"
        assert session.call("run_tests", {"paths": [NEW_TEST]}).outcome == "ok"
        assert session.call("run_tests", {"paths": [NEW_TEST]}).outcome == "ok"
        assert session.call("run_tests", {"paths": [NEW_TEST]}).outcome == "call_limit"

    def test_run_tests_timed_out_run_returns_timeout(self, workspace):
        """AC-7: 制限時間を超える → ``timeout``。"""
        runner = FakeTestRunner(override=lambda run: fakes.make_report({}, exit_code=None, timed_out=True))
        session, _ = session_with_new_test(workspace, runner=runner)

        result = session.call("run_tests", {"paths": [NEW_TEST]})

        assert result.outcome == "timeout"
        assert result.is_error is True

    def test_run_tests_runner_failure_returns_internal_error_without_the_exception_text(self, workspace):
        def explode(run):
            raise RuntimeError(f"boom {VALUE_MARK}")

        session, _ = session_with_new_test(workspace, runner=FakeTestRunner(override=explode))

        result = session.call("run_tests", {"paths": [NEW_TEST]})

        assert result.outcome == "internal_error"
        assert VALUE_MARK not in result.to_text()

    def test_run_tests_caps_failed_test_names_and_output_tail(self, workspace):
        """上限: 落ちたテストの名前は 20 件まで、出力の末尾は 4,000 字まで。"""
        from src.fixer.runners import TestReport

        outcomes = {f"tests.unit.test_many::test_{index:03d}": "failed" for index in range(50)}
        report = TestReport(outcomes=outcomes, exit_code=1, timed_out=False, output_tail="HEAD" + "x" * 20_000 + "TAIL")
        session, _ = session_with_new_test(workspace, runner=FakeTestRunner(override=lambda run: report))

        result = session.call("run_tests", {"paths": [NEW_TEST]})

        assert result.data["failed"] == 50
        assert len(result.data["failed_tests"]) == 20
        assert len(result.data["output_tail"]) <= 4_000
        assert result.data["output_tail"].endswith("TAIL")
        assert "HEAD" not in result.data["output_tail"]

    def test_run_tests_long_test_names_do_not_blow_up_the_result(self, workspace):
        from src.fixer.runners import TestReport

        outcomes = {f"tests.unit.test_many::test_{'n' * 50_000}_{index}": "failed" for index in range(20)}
        report = TestReport(outcomes=outcomes, exit_code=1, timed_out=False, output_tail="")
        session, _ = session_with_new_test(workspace, runner=FakeTestRunner(override=lambda run: report))

        result = session.call("run_tests", {"paths": [NEW_TEST]})

        assert len(result.to_text()) <= 20_000


# ---------------------------------------------------------------------------
# 終了の申告
# ---------------------------------------------------------------------------


class TestFinish:
    def test_finish_fixed_is_accepted_and_recorded(self, workspace):
        session = make_session(workspace)

        result = session.call("finish", dict(FINISH_FIXED))

        assert result.outcome == "ok"
        assert result.data["accepted"] is True
        assert session.finished.outcome == "fixed"
        assert session.finished.patch_kind == "symptomatic"
        assert session.finished.explanation == FINISH_FIXED["explanation"]

    def test_finish_is_none_before_any_accepted_claim(self, workspace):
        session = make_session(workspace)
        session.call("read_file", {"path": SRC})

        assert session.finished is None

    @pytest.mark.parametrize(("outcome", "patch_kind"), [("fixed", "structural"), ("declined", "symptomatic")])
    def test_finish_accepts_the_listed_values(self, workspace, outcome, patch_kind):
        session = make_session(workspace)

        result = session.call("finish", {"outcome": outcome, "patch_kind": patch_kind, "explanation": "x"})

        assert result.outcome == "ok"
        assert (session.finished.outcome, session.finished.patch_kind) == (outcome, patch_kind)

    def test_finish_last_accepted_claim_wins(self, workspace):
        session = make_session(workspace)

        session.call("finish", dict(FINISH_FIXED))
        session.call("finish", dict(FINISH_DECLINED))

        assert session.finished.outcome == "declined"

    def test_finish_malformed_claim_returns_invalid_input_and_is_not_recorded(self, workspace):
        cases = cases_of(
            "args",
            [
                {},
                {"patch_kind": "symptomatic", "explanation": "x"},
                {**FINISH_FIXED, "outcome": "done"},
                {**FINISH_FIXED, "outcome": "proposed"},
                {**FINISH_FIXED, "outcome": None},
                {**FINISH_FIXED, "outcome": ["fixed"]},
                {**FINISH_FIXED, "outcome": "FIXED"},
                {**FINISH_FIXED, "patch_kind": "unspecified"},
                {**FINISH_FIXED, "patch_kind": "refactor"},
                {**FINISH_FIXED, "patch_kind": 1},
                {**FINISH_FIXED, "explanation": "x" * 801},
                {**FINISH_FIXED, "explanation": None},
                {**FINISH_FIXED, "explanation": ["x"]},
                {**FINISH_FIXED, "explanation": {"text": "x"}},
                {**FINISH_FIXED, "tests_passed": True},
                {**FINISH_FIXED, "status": "proposed"},
                {**FINISH_FIXED, "skip_gates": True},
            ],
        )

        def check(args):
            session = make_session(workspace)

            result = session.call("finish", args)

            assert result.outcome == "invalid_input"
            assert session.finished is None

        check_each(cases, check)

    def test_finish_explanation_of_800_characters_is_accepted(self, workspace):
        session = make_session(workspace)

        result = session.call("finish", {**FINISH_FIXED, "explanation": "あ" * 800})

        assert result.outcome == "ok"
        assert len(session.finished.explanation) == 800

    def test_finish_malformed_claim_does_not_replace_an_accepted_one(self, workspace):
        session = make_session(workspace)
        session.call("finish", dict(FINISH_DECLINED))

        session.call("finish", {**FINISH_FIXED, "outcome": "done"})

        assert session.finished.outcome == "declined"

    def test_finish_sixth_claim_returns_call_limit_and_does_not_replace_the_accepted_one(self, workspace):
        """design.md §7: ``finish`` は 5 回まで。"""
        session = make_session(workspace)

        outcomes = [session.call("finish", dict(FINISH_DECLINED)).outcome for _ in range(5)]
        sixth = session.call("finish", dict(FINISH_FIXED))

        assert outcomes == ["ok"] * 5
        assert sixth.outcome == "call_limit"
        assert session.finished.outcome == "declined"

    def test_finish_is_still_accepted_after_the_tool_call_limit(self, workspace):
        session = make_session(workspace, limits=small_limits(max_tool_calls=1))
        session.call("read_file", {"path": SRC})
        assert session.call("read_file", {"path": SRC}).outcome == "call_limit"

        assert session.call("finish", dict(FINISH_DECLINED)).outcome == "ok"


# ---------------------------------------------------------------------------
# 上限・入力の検証・足取り
# ---------------------------------------------------------------------------


class TestLimits:
    def test_call_over_the_tool_call_limit_returns_call_limit_and_does_nothing(self, workspace):
        session = make_session(workspace, limits=small_limits(max_tool_calls=3))

        outcomes = [session.call("read_file", {"path": SRC}).outcome for _ in range(3)]
        refused_write = session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
        refused_read = session.call("read_file", {"path": SRC})

        assert outcomes == ["ok", "ok", "ok"]
        assert refused_write.outcome == "call_limit"
        assert refused_read.outcome == "call_limit"
        assert "content" not in refused_read.data
        assert not (root_of(workspace) / NEW_TEST).exists()

    def test_rejected_calls_also_use_up_the_tool_call_limit(self, workspace):
        """R7b: 検証に落ちた呼び出しも、回数に数える（不正な入力の繰り返しにも上限を効かせる）。"""
        session = make_session(workspace, limits=small_limits(max_tool_calls=3))

        first = session.call("read_file", {"path": 3})
        second = session.call("write_file", {"path": MAIN, "content": "x"})
        third = session.call("read_file", {"bogus": 1})
        fourth = session.call("read_file", {"path": SRC})

        assert (first.outcome, second.outcome, third.outcome) == ("invalid_input", "denied", "invalid_input")
        assert fourth.outcome == "call_limit"

    def test_default_tool_call_limit_is_120(self, workspace):
        session = make_session(workspace)

        outcomes = [session.call("list_files", {"path": "src"}).outcome for _ in range(121)]

        assert outcomes == ["ok"] * 120 + ["call_limit"]


class TestInputValidation:
    def test_call_unknown_tool_name_returns_invalid_input_and_is_traced_as_unknown(self, workspace):
        cases = cases_of(
            "name",
            ["Bash", "Read", "Write", "mcp__fixer__read_file", "read_file ", "", "READ_FILE", "submit_diagnosis"],
        )

        def check(name):
            session = make_session(workspace)

            result = session.call(name, {"path": SRC, "command": f"echo {VALUE_MARK}"})

            assert result.outcome == "invalid_input"
            assert session.trace[-1].tool == "unknown"
            assert_small_and_clean(result, VALUE_MARK)

        check_each(cases, check)

    def test_call_non_string_tool_name_does_not_raise(self, workspace):
        cases = cases_of("name", [None, 3, ["read_file"], {"name": "read_file"}, b"read_file"])

        def check(name):
            session = make_session(workspace)

            result = session.call(name, {"path": SRC})

            assert result.outcome == "invalid_input"
            assert session.trace[-1].tool == "unknown"

        check_each(cases, check)

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_call_arguments_that_are_not_a_dict_return_invalid_input_without_raising(self, workspace, tool):
        """上限のすり抜け: 想定外の型（集合、バイト列、入れ子）でも、例外を出さず、足取りに残す。"""
        cases = [None, "text", 3, 3.5, True, [], [{"path": SRC}], ("path", SRC), {"path"}, b"{}", [[[[[[[[[[1]]]]]]]]]]]

        def check(args):
            session = make_session(workspace)

            result = session.call(tool, args)

            assert result.outcome == "invalid_input"
            assert len(session.trace) == 1
            assert session.trace[0].outcome == "invalid_input"
            assert session.trace[0].path is None

        check_each(cases, check)

    def test_invalid_input_result_names_the_fields_but_never_echoes_the_values(self, workspace):
        """AC-29: 結果に、不正な値そのものは含まれない（返すのは、不正な項目の名前だけ）。"""
        cases = cases_of(
            ("tool", "args"),
            [
                ("read_file", {"path": VALUE_MARK * 3, "start_line": VALUE_MARK}),
                ("read_file", {"path": {"nested": VALUE_MARK}}),
                ("read_file", {"path": SRC, "unknown_field": VALUE_MARK}),
                ("write_file", {"path": NEW_TEST, "content": {"text": VALUE_MARK}}),
                ("write_file", {"path": NEW_TEST, "content": "x", "note": VALUE_MARK}),
                ("search_code", {"pattern": [VALUE_MARK]}),
                ("replace_in_file", {"path": SRC, "old": {VALUE_MARK: 1}, "new": "x"}),
                ("run_tests", {"paths": [VALUE_MARK, "-x"]}),
                ("run_tests", {"paths": f"-{VALUE_MARK}"}),
                ("finish", {**FINISH_FIXED, "outcome": VALUE_MARK}),
                ("finish", {**FINISH_FIXED, "explanation": [VALUE_MARK]}),
                ("list_files", {"path": 3, "depth": VALUE_MARK}),
            ],
        )

        def check(case):
            tool, args = case
            session = make_session(workspace)

            result = session.call(tool, args)

            assert result.outcome == "invalid_input"
            assert VALUE_MARK not in result.to_text()
            assert isinstance(result.data["invalid_fields"], list)
            assert all(isinstance(name, str) for name in result.data["invalid_fields"])
            assert VALUE_MARK not in json.dumps([call.path for call in session.trace])

        check_each(cases, check)

    def test_invalid_input_result_stays_small_for_thousands_of_unknown_fields(self, workspace):
        """上限のすり抜け: 未定義の項目を大量に、または長い名前で渡しても、結果は小さい。"""
        session = make_session(workspace)
        many = {f"field_{index}": index for index in range(10_000)}
        long_name = {"path": SRC, "x" * 100_000: 1}

        for args in (many, long_name):
            result = session.call("read_file", args)
            assert result.outcome == "invalid_input"
            assert len(result.to_text()) <= 4_000

    def test_huge_input_is_rejected_with_a_small_result(self, workspace):
        cases = cases_of(
            ("tool", "args"),
            [
                ("read_file", {"path": "a" * 1_000_000}),
                ("list_files", {"path": "a/" * 500_000}),
                ("search_code", {"pattern": "a" * 1_000_000}),
                ("write_file", {"path": "src/" + "a" * 1_000_000 + ".py", "content": "x"}),
                ("replace_in_file", {"path": SRC, "old": "a" * 5_000_000, "new": "b" * 5_000_000}),
                ("run_tests", {"paths": ["tests/" + "a" * 1_000_000]}),
                ("run_tests", {"paths": ["tests/unit"] * 100_000}),
                ("finish", {**FINISH_FIXED, "explanation": "a" * 5_000_000}),
            ],
            ids=["read", "list", "search", "write", "replace", "run-long", "run-many", "finish"],
        )

        def check(case):
            tool, args = case
            session = make_session(workspace)

            result = session.call(tool, args)

            assert result.is_error is True
            assert result.outcome in TOOL_OUTCOMES
            assert len(result.to_text()) <= 4_000
            assert (root_of(workspace) / SRC).read_text(encoding="utf-8") == FILES[SRC]
            assert session.finished is None

        check_each(cases, check)


class TestTrace:
    def test_trace_records_every_call_in_order_with_paths_and_outcomes(self, workspace):
        """R17: 呼んだツールを、順番どおりに記録する（ツール名、対象のパス、結果の分類、所要時間）。"""
        session, _ = session_with_new_test(workspace)
        session.call("read_file", {"path": SRC})
        session.call("run_tests", {"paths": [NEW_TEST]})
        session.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})
        session.call("run_tests", {"paths": [NEW_TEST]})
        session.call("search_code", {"pattern": "average", "path": "src"})
        session.call("list_files", {"path": "tests/unit"})
        session.call("finish", dict(FINISH_FIXED))

        trace = session.trace

        assert [call.seq for call in trace] == list(range(1, 9))
        assert [call.tool for call in trace] == [
            "write_file",
            "read_file",
            "run_tests",
            "replace_in_file",
            "run_tests",
            "search_code",
            "list_files",
            "finish",
        ]
        assert [call.outcome for call in trace] == ["ok"] * 8
        assert [call.path for call in trace] == [NEW_TEST, SRC, NEW_TEST, SRC, NEW_TEST, "src", "tests/unit", None]
        assert [(call.tests_passed, call.tests_failed) for call in trace if call.tool == "run_tests"] == [
            (0, 1),
            (1, 0),
        ]
        assert all(
            call.tests_passed is None and call.tests_failed is None for call in trace if call.tool != "run_tests"
        )
        assert all(isinstance(call.duration_ms, int) and call.duration_ms >= 0 for call in trace)

    def test_trace_records_rejected_and_limited_calls_without_their_values(self, workspace):
        """R7b: 検証に落ちた呼び出し、上限で断った呼び出しも、足取りに残す。形を確かめていない値は残さない。"""
        session = make_session(workspace, limits=small_limits(max_tool_calls=3))

        session.call("read_file", {"path": f"/abs/{VALUE_MARK}"})
        session.call("write_file", {"path": f"../{VALUE_MARK}.py", "content": "x"})
        session.call("read_file", {"path": [VALUE_MARK]})
        session.call("read_file", {"path": SRC})
        session.call(f"tool_{VALUE_MARK}", {})

        trace = session.trace
        assert [call.outcome for call in trace] == ["denied", "denied", "invalid_input", "call_limit", "invalid_input"]
        assert [call.tool for call in trace] == ["read_file", "write_file", "read_file", "read_file", "unknown"]
        assert [call.path for call in trace[:3]] == [None, None, None]
        assert VALUE_MARK not in repr(trace)

    def test_trace_duration_uses_the_given_clock(self, workspace):
        ticks = iter(float(index) for index in range(0, 1000, 2))
        session = make_session(workspace, clock=lambda: next(ticks))

        session.call("read_file", {"path": SRC})

        # 時計を何回読むかは決めない（1 回読むごとに 2 秒進む）
        assert session.trace[0].duration_ms >= 2_000
        assert session.trace[0].duration_ms % 2_000 == 0

    def test_call_is_safe_from_several_threads(self, workspace):
        """design.md §7: 1 回の呼び出し全体をロックの中で処理する（上限の判定と加算が、ずれない）。"""
        import threading

        session = make_session(workspace, limits=small_limits(max_tool_calls=20))
        threads = [
            threading.Thread(target=lambda: [session.call("read_file", {"path": LEGACY}) for _ in range(10)])
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        outcomes = [call.outcome for call in session.trace]
        assert outcomes.count("ok") == 20
        assert outcomes.count("call_limit") == 60
        assert [call.seq for call in session.trace] == list(range(1, 81))
