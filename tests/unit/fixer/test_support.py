"""テストの補助コードそのものの確認。**``src.fixer`` が無くても通る**（実装に依存しない）。

修正役のテストは、補助コード（見本のリポジトリ、``subprocess`` の記録、偽の SDK、偽の ``TestRunner``）が
正しく働くことを前提にしている。補助が黙って何も確かめていない、という状態を防ぐため、
「守りが外れていれば、補助がそれを検出する」ことを、ここで確かめる。

- ``subprocess`` の記録が、リモートに触れる git の操作・一覧にない操作を、実際に違反として数える。
- 開発中のリポジトリ本体に対する git の呼び出しを、実行する前に止める。
- 見本のリポジトリの不具合は、実物の pytest で「修正前に落ち、修正後に通る」（G-5・G-6 の通しの確認が空振りしない）。
- 見本のリポジトリに仕込んだフックは、ふつうの ``git commit`` では実際に走る（「走らなかった」の確認が空振りしない）。
- 偽の ``TestRunner`` は、複製の中身から A〜D の結果を決める。
- 偽の SDK は、実物の MCP サーバーを通してツールを呼ぶ。
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    create_sdk_mcp_server,
    tool,
)

from . import conftest, fakes, repo_factory
from .conftest import ProcessCall, RealRepositoryTouchedError, git_subcommand, git_violations
from .fakes import FakeLintRunner, FakeTestRunner, apply_good_fix_directly
from .repo_factory import EXISTING_TEST, FILES, HOOK_NAMES, NEW_TEST, NEW_TEST_SOURCE, SRC
from .sdk_fake import NOT_TOOL_OUTPUT, FakeSdk, full_name, result_message


def call(*argv: str, by_helper: bool = False, env: dict | None = None, shell: bool = False) -> ProcessCall:
    environment = {"PATH": "/usr/bin"} if env is None else env
    return ProcessCall(argv=argv, cwd="/tmp/somewhere", env=environment, shell=shell, by_helper=by_helper)  # noqa: S108


@dataclass
class StandInReport:
    outcomes: dict
    exit_code: int | None
    timed_out: bool
    output_tail: str


def stand_in_report(outcomes, *, exit_code, timed_out, tail):
    return StandInReport(outcomes, exit_code, timed_out, tail)


def child_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TMPDIR": "/tmp",  # noqa: S108
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def run_python(tree: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, *args],
        cwd=str(tree),
        env=child_env(home),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# ---------------------------------------------------------------------------
# subprocess の記録（AC-22 の土台）
# ---------------------------------------------------------------------------


class TestProcessRecorder:
    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (("git", "push", "origin", "main"), "push"),
            (("/usr/bin/git", "-C", "/repo", "fetch"), "fetch"),
            (("git", "-c", "user.name=x", "-C", "/repo", "commit-tree", "abc"), "commit-tree"),
            (("git", "--git-dir=/repo/.git", "--no-pager", "rev-parse", "HEAD"), "rev-parse"),
            (("git", "--git-dir", "/repo/.git", "ls-remote"), "ls-remote"),
            (("git", "--version"), None),
            (("git",), None),
        ],
    )
    def test_git_subcommand_skips_global_options(self, argv, expected):
        assert git_subcommand(argv) == expected

    @pytest.mark.parametrize("subcommand", sorted(conftest.REMOTE_GIT_SUBCOMMANDS))
    def test_git_violations_flags_remote_operations_from_helper_and_implementation(self, subcommand):
        for by_helper in (False, True):
            problems = git_violations([call("git", "-C", "/repo", subcommand, "origin", by_helper=by_helper)])
            assert any("remote operation" in problem for problem in problems), (subcommand, by_helper)

    @pytest.mark.parametrize(
        "subcommand", ["commit", "add", "status", "checkout", "worktree", "stash", "reset", "clean", "config", "gc"]
    )
    def test_git_violations_flags_implementation_calls_outside_the_fixed_list(self, subcommand):
        assert git_violations([call("git", subcommand)]) != []

    @pytest.mark.parametrize("subcommand", sorted(conftest.IMPLEMENTATION_GIT_SUBCOMMANDS))
    def test_git_violations_accepts_the_fixed_list_for_the_implementation(self, subcommand):
        assert git_violations([call("git", subcommand, "x")]) == []

    def test_git_violations_flags_inherited_environment_git_dir_and_shell(self):
        assert git_violations([ProcessCall(("git", "rev-parse", "HEAD"), "/tmp/x", None, False, False)]) != []  # noqa: S108
        assert git_violations([call("git", "rev-parse", "HEAD", env={"GIT_DIR": "/elsewhere/.git"})]) != []
        assert git_violations([call("git rev-parse HEAD", shell=True)]) != []  # noqa: S604

    def test_git_violations_ignores_processes_that_are_not_git(self):
        assert git_violations([call(sys.executable, "-m", "pytest", "push"), call("ruff", "check", "fetch")]) == []

    def test_recorder_sees_helper_git_calls_made_through_subprocess_run(self, sample_repo, process_recorder):
        sample_repo.git("rev-parse", "HEAD")

        recorded = process_recorder.git_calls[-1]
        assert recorded.git_subcommand == "rev-parse"
        assert recorded.by_helper is True
        assert Path(recorded.cwd) == sample_repo.path
        assert process_recorder.implementation_git_calls == []

    def test_recorder_classifies_a_call_without_the_helper_mark_as_implementation(self, sample_repo, process_recorder):
        env = {
            key: value
            for key, value in repo_factory.helper_env(sample_repo.home).items()
            if key != repo_factory.HELPER_ENV_MARK
        }

        command = ["git", "rev-parse", "HEAD"]
        subprocess.run(command, cwd=str(sample_repo.path), env=env, capture_output=True, check=True)  # noqa: S603

        assert [item.git_subcommand for item in process_recorder.implementation_git_calls] == ["rev-parse"]

    def test_recorder_blocks_git_against_the_real_repository_before_it_runs(self, process_recorder, tmp_path):
        """取り違えの備え: 開発中のリポジトリ本体を作業ディレクトリにした git は、実行する前に止まる。"""
        env = repo_factory.helper_env(tmp_path / "home")
        attempts = [
            {"args": ["git", "rev-parse", "HEAD"], "cwd": str(conftest.REAL_REPO)},
            {"args": ["git", "-C", str(conftest.REAL_REPO), "rev-parse", "HEAD"], "cwd": str(tmp_path)},
            {"args": ["git", "rev-parse", "HEAD"], "cwd": str(conftest.REAL_REPO / "tests")},
            {"args": ["git", f"--git-dir={conftest.REAL_REPO / '.git'}", "rev-parse", "HEAD"], "cwd": str(tmp_path)},
        ]

        for attempt in attempts:
            with pytest.raises(RealRepositoryTouchedError):
                subprocess.run(attempt["args"], cwd=attempt["cwd"], env=env, capture_output=True, check=False)  # noqa: S603
        with pytest.raises(RealRepositoryTouchedError):
            subprocess.run(  # noqa: S603
                ["git", "rev-parse", "HEAD"],  # noqa: S607
                cwd=str(tmp_path),
                env={**env, "GIT_DIR": str(conftest.REAL_REPO / ".git")},
                capture_output=True,
                check=False,
            )

    def test_isolation_removes_git_and_credential_variables_and_points_home_at_an_empty_directory(self, _isolation):
        for name in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "AWS_SESSION_TOKEN",
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
        ):
            assert name not in os.environ, name
        assert os.environ["HOME"] == str(_isolation)
        assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
        assert Path(os.environ["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8") == ""
        assert not Path(os.environ["HOME"]).resolve().is_relative_to(conftest.REAL_REPO)

    def test_network_is_blocked(self):
        """AC-26: ソケットでの通信は、できない。"""
        with pytest.raises(conftest.NetworkBlockedError):
            socket.create_connection(("example.invalid", 443), timeout=0.1)
        with pytest.raises(conftest.NetworkBlockedError):
            socket.getaddrinfo("example.invalid", 443)
        probe = socket.socket()
        try:
            with pytest.raises(conftest.NetworkBlockedError):
                probe.connect(("127.0.0.1", 9))
        finally:
            probe.close()


# ---------------------------------------------------------------------------
# 見本のリポジトリ
# ---------------------------------------------------------------------------


class TestSampleRepository:
    def test_sample_repo_is_a_clean_repository_outside_the_real_one(self, sample_repo):
        state = sample_repo.state()

        assert state.branch == "main"
        assert state.status == ""
        assert sample_repo.branches() == ["main"]
        assert not sample_repo.path.resolve().is_relative_to(conftest.REAL_REPO)
        assert set(sample_repo.tree_files(sample_repo.head)) == set(FILES)
        assert sample_repo.author(sample_repo.head) == repo_factory.AUTHOR_NAME

    def test_sample_repo_state_is_stable_and_detects_each_kind_of_change(self, sample_repo):
        before = sample_repo.state()
        assert before.differences(sample_repo.state()) == []

        (sample_repo.path / "README.md").write_text("# changed\n", encoding="utf-8")
        assert {"status", "worktree"} <= set(before.differences(sample_repo.state()))
        sample_repo.git("add", "README.md")
        assert "index_sha256" in before.differences(sample_repo.state())
        sample_repo.commit_all("change")
        assert "head" in before.differences(sample_repo.state())

    def test_sample_repo_hooks_really_fire_on_an_ordinary_commit(self, sample_repo):
        """「フックが走らなかった」の確認が空振りしないこと: ふつうの ``git commit`` では、目印が残る。"""
        sample_repo.install_hooks()
        (sample_repo.path / "README.md").write_text("# changed\n", encoding="utf-8")
        sample_repo.git("add", "README.md")

        sample_repo.git("commit", "-q", "-m", "ordinary commit")

        assert sample_repo.state().hook_marks == tuple(sorted(HOOK_NAMES))

    def test_sample_bug_fails_before_the_fix_and_passes_after_under_real_pytest(self, tmp_path, _isolation):
        """G-5・G-6 の通しの確認が空振りしないこと。既存のテストの成否は、修正の前後で変わらない。"""
        tree = tmp_path / "tree"
        repo_factory.write_files(tree, FILES)
        (tree / NEW_TEST).write_text(NEW_TEST_SOURCE, encoding="utf-8")
        command = ("-m", "pytest", "-q", "-p", "no:cacheprovider", "--")

        before = run_python(tree, _isolation, *command, NEW_TEST)
        existing_before = run_python(tree, _isolation, *command, EXISTING_TEST)
        apply_good_fix_directly(tree)
        after = run_python(tree, _isolation, *command, NEW_TEST)
        existing_after = run_python(tree, _isolation, *command, EXISTING_TEST)

        assert before.returncode == 1, before.stdout
        assert "ZeroDivisionError" in before.stdout
        assert after.returncode == 0, after.stdout
        assert "1 failed, 2 passed" in existing_before.stdout
        assert "1 failed, 2 passed" in existing_after.stdout

    def test_sample_repo_has_one_lint_finding_before_and_after_the_fix(self, tmp_path, _isolation):
        """G-8 の通しの確認の前提: 正しい修正は、ruff の指摘を増やさない。"""
        tree = tmp_path / "tree"
        repo_factory.write_files(tree, FILES)
        command = ("-m", "ruff", "check", "--output-format", "json", "--no-cache", "src", "tests")

        before = json.loads(run_python(tree, _isolation, *command).stdout)
        apply_good_fix_directly(tree)
        after = json.loads(run_python(tree, _isolation, *command).stdout)

        assert [item["code"] for item in before] == ["F401"]
        assert [item["code"] for item in after] == ["F401"]

    def test_function_span_and_default_handoff_point_inside_the_buggy_function(self):
        start, end = repo_factory.function_span(SRC, "average")
        entry = repo_factory.handoff_dict()["code_locations"][0]

        assert FILES[SRC].splitlines()[start - 1] == "def average(values):"
        assert start < entry["line"] <= end
        assert repo_factory.FIX_OLD in FILES[SRC]
        assert FILES[SRC].count(repo_factory.FIX_OLD) == 1


# ---------------------------------------------------------------------------
# 偽の TestRunner・LintRunner
# ---------------------------------------------------------------------------


@pytest.fixture
def trees(tmp_path):
    base, pre, fix = tmp_path / "base", tmp_path / "pre", tmp_path / "fix"
    for tree in (base, pre, fix):
        repo_factory.write_files(tree, FILES)
    (pre / NEW_TEST).write_text(NEW_TEST_SOURCE, encoding="utf-8")
    apply_good_fix_directly(fix)
    return base, pre, fix


class TestFakeRunners:
    def test_fake_test_runner_decides_results_from_the_tree_contents(self, trees):
        base, pre, fix = trees
        runner = FakeTestRunner(report_factory=stand_in_report)
        new_id = "tests.unit.test_average_empty::test_case_0"

        run_a = runner.run(base, ["tests"])
        run_b = runner.run(pre, [NEW_TEST])
        run_c = runner.run(fix, ["tests"], ignore=[NEW_TEST])
        run_d = runner.run(fix, [NEW_TEST])

        assert run_a.outcomes == fakes.EXISTING_IDS
        assert run_b.outcomes == {new_id: "failed"}
        assert run_c.outcomes == fakes.EXISTING_IDS
        assert run_d.outcomes == {new_id: "passed"}
        assert (run_a.exit_code, run_b.exit_code, run_c.exit_code, run_d.exit_code) == (1, 1, 1, 0)
        assert [(item.has_fix, item.new_tests) for item in runner.calls] == [
            (False, ()),
            (False, (NEW_TEST,)),
            (True, (NEW_TEST,)),
            (True, (NEW_TEST,)),
        ]

    def test_fake_test_runner_gives_a_different_answer_when_the_wrong_tree_is_used(self, trees):
        """検査を、修正の入っていない複製や、新しいテストを混ぜた形で走らせると、結果に現れる。"""
        base, pre, fix = trees
        runner = FakeTestRunner(report_factory=stand_in_report)

        assert runner.run(pre, [NEW_TEST]).outcomes != runner.run(fix, [NEW_TEST]).outcomes
        assert runner.run(fix, ["tests"]).outcomes != runner.run(fix, ["tests"], ignore=[NEW_TEST]).outcomes
        assert runner.run(base, [NEW_TEST]).outcomes == {}
        assert runner.run(base, [NEW_TEST]).exit_code == 5

    def test_fake_test_runner_records_stray_files_in_the_tree(self, trees):
        _base, _pre, fix = trees
        (fix / ".pytest_cache").mkdir()
        (fix / ".pytest_cache" / "lastfailed").write_text("{}", encoding="utf-8")
        runner = FakeTestRunner(report_factory=stand_in_report)

        runner.run(fix, ["tests"])

        assert ".pytest_cache/lastfailed" in runner.calls[0].listing
        assert runner.gate_runs(fix) == []
        assert len(runner.agent_runs(fix)) == 1

    def test_fake_lint_runner_counts_by_whether_the_fix_is_present(self, trees):
        base, _pre, fix = trees
        lint = FakeLintRunner(before=1, after=2)

        assert (lint.count(base), lint.count(fix)) == (1, 2)
        assert lint.trees == [base, fix]


# ---------------------------------------------------------------------------
# 偽の SDK
# ---------------------------------------------------------------------------


def demo_options(seen: list, cwd: Path, *, schema: dict) -> ClaudeAgentOptions:
    async def handler(args):
        seen.append(args)
        return {"content": [{"type": "text", "text": json.dumps({"outcome": "ok", "count": len(seen)})}]}

    server = create_sdk_mcp_server(name="fixer", tools=[tool("read_file", "read", schema)(handler)])
    return ClaudeAgentOptions(mcp_servers={"fixer": server}, cwd=str(cwd))


def collect(sdk: FakeSdk, options: ClaudeAgentOptions) -> list:
    async def gather() -> list:
        return [message async for message in sdk("prompt", options, {})]

    return asyncio.run(gather())


LOOSE = {"type": "object", "properties": {"path": {"description": "path"}}}
STRICT = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}


class TestFakeSdk:
    def test_fake_sdk_calls_tools_through_the_real_mcp_server_and_streams_sdk_messages(self, tmp_path):
        seen: list = []
        replies: list = []

        async def script(agent):
            replies.append(await agent.call("read_file", {"path": "a.py"}))
            replies.append(await agent.call("read_file", {"path": 3, "unknown": True}))
            (agent.root / "written-by-script.txt").write_text("x", encoding="utf-8")

        sdk = FakeSdk(script=script, init_tools=[full_name("read_file")], final_text="done")

        messages = collect(sdk, demo_options(seen, tmp_path, schema=LOOSE))

        assert [type(message) for message in messages] == [
            SystemMessage,
            AssistantMessage,
            AssistantMessage,
            AssistantMessage,
            ResultMessage,
        ]
        assert seen == [{"path": "a.py"}, {"path": 3, "unknown": True}]
        assert [reply.outcome for reply in replies] == ["ok", "ok"]
        assert messages[1].content[1].name == "mcp__fixer__read_file"
        assert (tmp_path / "written-by-script.txt").is_file()
        assert sdk.root == tmp_path
        assert sdk.invocation_count == 1

    def test_fake_sdk_reveals_calls_stopped_by_a_strict_schema(self, tmp_path):
        """厳しいスキーマを渡すと、形の不正な呼び出しはハンドラーに届かない。偽物は、それを隠さない。"""
        seen: list = []
        replies: list = []

        async def script(agent):
            replies.append(await agent.call("read_file", {"path": 3}))
            replies.append(await agent.call("read_file", {"path": "a.py", "unknown": True}))

        collect(FakeSdk(script=script), demo_options(seen, tmp_path, schema=STRICT))

        assert seen == []
        assert [reply.outcome for reply in replies] == [NOT_TOOL_OUTPUT, NOT_TOOL_OUTPUT]

    def test_fake_sdk_fails_loudly_when_the_options_have_no_fixer_server(self, tmp_path):
        async def script(agent):
            await agent.call("read_file", {"path": "a.py"})

        with pytest.raises(AssertionError):
            collect(FakeSdk(script=script), ClaudeAgentOptions(cwd=str(tmp_path)))

    def test_fake_sdk_can_end_in_each_way_the_tests_need(self, tmp_path):
        options = demo_options([], tmp_path, schema=LOOSE)

        without_result = collect(FakeSdk(result=None), options)
        max_turns = collect(FakeSdk(result=result_message("error_max_turns", is_error=True)), options)
        foreign = collect(FakeSdk(foreign_tool_uses=("Bash",)), options)
        with pytest.raises(RuntimeError):
            collect(FakeSdk(raises=RuntimeError("boom")), options)

        assert without_result == []
        assert max_turns[-1].subtype == "error_max_turns"
        assert foreign[0].content[1].name == "Bash"
