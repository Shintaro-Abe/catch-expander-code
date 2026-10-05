"""レビュー（reviewer と Codex）で再現された穴の、修正の仕様のテスト（F1〜F12）。

実装の前に書いたテスト。レビューで使われた再現の筋書きを、そのままテストにしている。偽物では隠れる種類のもの
（F1・F2・F6）は、実物の git・pytest・ruff で確かめる。

テストが決めた呼び方（developer は、これに合わせる）:

- F2: ``stop_detail`` の値 ``repository_tampered``。git のすべての呼び出しに ``-c core.hooksPath=/dev/null`` と
  ``-c core.fsmonitor=false``。環境変数 ``GIT_CONFIG_GLOBAL=/dev/null``・``GIT_CONFIG_NOSYSTEM=1``。
- F3: ``FixToolSession(..., deadline=<clock と同じ時計の時刻>)``、``outcome`` の値 ``deadline_exceeded``、
  ``TestRunner.run(tree, paths, *, ignore=(), timeout_seconds=None)``、
  ``FixLimits.gate_timeout_seconds``（既定 900.0）。
- F6: 出力の上限は ``src.fixer.runners.MAX_OUTPUT_BYTES``（既定 50,000,000）。超えた実行の結果は、
  ``outcomes`` が空で ``exit_code`` が ``None``。
- F10: ``stop_detail`` の値 ``unsupported_attributes``。
- F11: ``run_fix(..., allow_unisolated=False)``、``stop_detail`` の値 ``unisolated_environment``、
  本物のホームを返す差し替え口 ``src.fixer.runner._real_home_directory()``、入口の ``--allow-unisolated``。
- 決まった語句: 下書きの冒頭の「認証情報を扱うファイル」、
  ``SYSTEM_PROMPT`` の「集めるときのエラー」「import するだけ」。
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from . import fakes, repo_factory
from .conftest import REAL_HOME_SEAM
from .fakes import (
    FINISH_FIXED,
    FakeLintRunner,
    FakeSdk,
    FakeTestRunner,
    all_of,
    append_text,
    assert_discarded,
    good_fix,
    good_fix_without_finish,
    good_fix_without_test_runs,
    make_fix_run,
    make_report,
    run_fixer,
    section,
    with_tamper,
    write_text,
)
from .repo_factory import (
    BRANCH,
    EXECUTION_ID,
    EXISTING_TEST,
    FILES,
    FIX_NEW,
    FIX_OLD,
    LEGACY,
    MAIN,
    NEW_TEST,
    NEW_TEST_SOURCE,
    NOTION,
    SRC,
    handoff_dict,
)
from .test_hardening import (
    CASE,
    commit,
    dynamic,
    failed,
    git_error,
    make_git,
    make_session,
    rewriting_test,
    static,
    wait_until_dead,
)

FIXED_SOURCE = FILES[SRC].replace(FIX_OLD, FIX_NEW)
MARK = "REVIEW_FIX_MARK_6642"
NULL_DEVICE = "/dev/null"


def limits(**overrides):
    from src.fixer.models import FixLimits

    return FixLimits(**overrides)


def stop_gates(outcome) -> set[str]:
    return set(outcome.run.stop_detail.split("+"))


def kind_of(run) -> str:
    """検査の実行の種類（design.md §10.2 の A〜D）。"""
    if run.paths == ("tests",):
        return "C" if run.has_fix else "A"
    return "D" if run.has_fix else "B"


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    repo_factory.write_files(root, FILES)
    return root


# ===========================================================================
# F1: 検査用の複製が、検査の途中で書き換えられる
# ===========================================================================

REWRITE_OTHER_TREES_TEST = """from pathlib import Path

from orchestrator import average

GOOD = {good!r}  # noqa: E501


def test_average_of_empty_list_is_zero():
    here = Path.cwd().resolve()
    for level in (here.parent, here.parent.parent):
        for candidate in level.glob("*/src/agent/orchestrator.py"):
            if here not in candidate.resolve().parents:
                candidate.write_text(GOOD, encoding="utf-8")
        for candidate in level.glob("*/*/src/agent/orchestrator.py"):
            if here not in candidate.resolve().parents:
                candidate.write_text(GOOD, encoding="utf-8")
    assert average([]) == 0.0
"""
# 壊れた修正: 空のリストでは 0.0 を返すが、平均を計算しない（既存のテストが落ちる）。リントの指摘も 1 件増える
BROKEN_SOURCE = FILES[SRC].replace(
    "def average(values):\n    total = sum(values)\n    return total / len(values)\n",
    "def average(values):\n    import os\n\n    return 0.0\n",
)
# 自分を書き換える修正: 読み込まれたときに、自分のファイルを正しい実装に書き換える（読み込まれた中身は、壊れたまま）
SELF_REWRITING_SOURCE = FILES[SRC].replace(
    "\n\nclass WorkflowError(Exception):",
    f'\nfrom pathlib import Path\n\nPath(__file__).write_text({FIXED_SOURCE!r}, encoding="utf-8")  # noqa: E501\n'
    "\n\nclass WorkflowError(Exception):",
)


def write_script(source: str, test_source: str = NEW_TEST_SOURCE):
    async def script(agent) -> None:
        first = await agent.call("write_file", {"path": NEW_TEST, "content": test_source})
        second = await agent.call("write_file", {"path": SRC, "content": source})
        assert (first.outcome, second.outcome) == ("ok", "ok")
        await agent.call("finish", dict(FINISH_FIXED))

    return script


class TestF1GateTreesWithRealTools:
    def test_new_test_that_rewrites_sibling_trees_cannot_get_a_broken_fix_committed(self, sample_repo, tmp_path):
        """再現（reviewer・窓口）: 実行 B の新しいテストが、隣の検査用の複製を正しい実装に書き換える。

        修正そのものは壊れている（既存のテストが落ち、リントの指摘が増える）。書き換える先の複製が、
        その時点で存在しなければ、検査は壊れた修正をそのまま見る。
        """
        assert FILES[SRC] != BROKEN_SOURCE
        test_source = REWRITE_OTHER_TREES_TEST.format(good=FIXED_SOURCE)

        outcome = run_fixer(
            sample_repo, tmp_path, script=write_script(BROKEN_SOURCE, test_source), test_runner=None, lint_runner=None
        )

        assert outcome.run.status == "rejected", outcome.run.stop_detail
        assert {"G7", "G8"} <= stop_gates(outcome)
        assert_discarded(outcome)

    def test_fix_that_rewrites_itself_while_existing_tests_run_does_not_pass_the_new_test(self, sample_repo, tmp_path):
        """再現（reviewer）: 実行 C の中で動いた修正コードが自分を書き換え、同じ複製を使う実行 D が通る。"""
        assert FILES[SRC] != SELF_REWRITING_SOURCE

        outcome = run_fixer(
            sample_repo, tmp_path, script=write_script(SELF_REWRITING_SOURCE), test_runner=None, lint_runner=None
        )

        assert outcome.run.status == "rejected", outcome.run.stop_detail
        assert "G6" in stop_gates(outcome)
        assert_discarded(outcome)

    def test_new_test_that_changes_its_own_tree_is_rejected(self, sample_repo, tmp_path):
        """実行の前後で、複製の中身が「組み立てた中身」と一致しなければ、その実行の検査は不合格。"""
        test_source = (
            "from pathlib import Path\n\nfrom orchestrator import average\n\n\n"
            "def test_average_of_empty_list_is_zero():\n"
            "    root = Path(__file__).resolve().parents[2]\n"
            "    (root / 'src' / 'agent' / 'planted.py').write_text('PLANTED = 1\\n', encoding='utf-8')\n"
            "    assert average([]) == 0.0\n"
        )

        outcome = run_fixer(
            sample_repo, tmp_path, script=write_script(FIXED_SOURCE, test_source), test_runner=None, lint_runner=None
        )

        assert outcome.run.status == "rejected", outcome.run.stop_detail
        assert {"G5", "G6"} <= stop_gates(outcome)
        assert_discarded(outcome)


class TestF1GateTrees:
    def test_gate_trees_exist_one_at_a_time_each_under_its_own_parent(self, sample_repo, tmp_path):
        """検査用の複製は、使う実行の直前に 1 つずつ作り、終わったら、次を作る前に消す。C と D も別の複製。"""
        lint_trees: list[Path] = []
        lint_alive_during_tests: list[str] = []

        class Lint(FakeLintRunner):
            def count(self, tree, **kwargs):
                lint_trees.append(Path(tree))
                return super().count(tree, **kwargs)

        def watch(run):
            lint_alive_during_tests.extend(str(tree) for tree in lint_trees if tree.exists() and tree != run.tree)

        runner = FakeTestRunner(override=watch)

        outcome = run_fixer(
            sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=runner, lint_runner=Lint()
        )

        runs = runner.calls
        assert outcome.run.status == "proposed"
        assert sorted(kind_of(run) for run in runs) == ["A", "B", "C", "D"]
        assert [run.earlier_trees_alive for run in runs] == [()] * 4
        assert len({run.tree for run in runs}) == 4
        assert len({run.tree.parent for run in runs}) == 4
        assert lint_alive_during_tests == []
        assert len(lint_trees) == 2
        assert {tree.parent for tree in lint_trees}.isdisjoint({run.tree.parent for run in runs})

    def test_trusted_steps_run_before_any_step_that_runs_model_written_code(self, sample_repo, tmp_path):
        """リント（基準・修正後）と実行 A を先に済ませ、その後で B・C・D を走らせる。"""
        log: list[str] = []

        class Lint(FakeLintRunner):
            def count(self, tree, **kwargs):
                has_fix = repo_factory.FIX_MARKER in (Path(tree) / SRC).read_text(encoding="utf-8")
                log.append("lint-fix" if has_fix else "lint-base")
                return super().count(tree, **kwargs)

        runner = FakeTestRunner(override=lambda run: log.append(kind_of(run)))

        outcome = run_fixer(
            sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=runner, lint_runner=Lint()
        )

        assert outcome.run.status == "proposed"
        assert len(log) == 6
        assert set(log[:3]) == {"lint-base", "lint-fix", "A"}
        assert set(log[3:]) == {"B", "C", "D"}

    @pytest.mark.parametrize(("which", "gate"), [("A", "G7"), ("B", "G5"), ("C", "G7"), ("D", "G6")])
    def test_tree_changed_during_a_gate_run_fails_the_gate_of_that_run(self, sample_repo, tmp_path, which, gate):
        def meddle(run):
            if kind_of(run) == which:
                (run.tree / "src" / "agent" / "planted_by_the_run.py").write_text("PLANTED = 1\n", encoding="utf-8")

        outcome = run_fixer(
            sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=FakeTestRunner(override=meddle)
        )

        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", gate)
        assert_discarded(outcome)

    @pytest.mark.parametrize("change", ["modify", "delete", "chmod", "symlink", "new-directory-file", "modify-test"])
    def test_any_kind_of_change_to_the_tree_during_a_run_is_noticed(self, sample_repo, tmp_path, change):
        """照合するのは、ファイルの一覧・種類・モード・中身。"""

        def meddle(run):
            if kind_of(run) != "B":
                return
            if change == "modify":
                (run.tree / LEGACY).write_text("CHANGED = 1\n", encoding="utf-8")
            elif change == "delete":
                (run.tree / LEGACY).unlink()
            elif change == "chmod":
                (run.tree / LEGACY).chmod(0o755)
            elif change == "symlink":
                os.symlink("legacy.py", run.tree / "src" / "agent" / "link.py")
            elif change == "new-directory-file":
                (run.tree / "extra").mkdir()
                (run.tree / "extra" / "note.txt").write_text("x\n", encoding="utf-8")
            else:
                (run.tree / EXISTING_TEST).write_text(
                    "def test_average_returns_mean():\n    assert True\n", encoding="utf-8"
                )

        outcome = run_fixer(
            sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=FakeTestRunner(override=meddle)
        )

        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", "G5")

    def test_workspace_rewritten_during_the_gates_does_not_change_what_is_checked_or_committed(
        self, sample_repo, tmp_path
    ):
        """検査の間に、作業した複製が書き換えられても、検査とコミットは、取り出した時点の中身で行う。"""
        seen: dict[str, Path] = {}

        async def script(agent):
            seen["root"] = agent.root
            await good_fix_without_test_runs(agent)

        def rewrite_workspace(run):
            if seen["root"].exists():
                (seen["root"] / SRC).write_text("REPLACED_DURING_THE_GATES = 1\n", encoding="utf-8")

        outcome = run_fixer(
            sample_repo, tmp_path, script=script, test_runner=FakeTestRunner(override=rewrite_workspace)
        )

        assert outcome.run.status == "proposed"
        assert sample_repo.changed_paths(sample_repo.head, outcome.run.commit) == sorted([SRC, NEW_TEST])
        assert sample_repo.file_at(outcome.run.commit, SRC) == FIXED_SOURCE


# ===========================================================================
# F2: 元のリポジトリにフックを置かれ、修正役自身がそれを実行する
# ===========================================================================


def hook_script(mark: Path) -> str:
    return f'#!/bin/sh\necho ran >> "{mark}"\n'


def plant_hook(repo_path: Path, mark: Path, name: str = "reference-transaction") -> None:
    hook = repo_path / ".git" / "hooks" / name
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(hook_script(mark), encoding="utf-8")
    hook.chmod(0o755)


def home_with_global_config(tmp_path, monkeypatch, text: str) -> Path:
    """全体設定（``~/.gitconfig``）のあるホームに切り替える。土台が入れた隔離の環境変数は外す。"""
    home = tmp_path / "home-with-global-config"
    home.mkdir()
    (home / ".gitconfig").write_text(text, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    return home


class TestF2GitHardening:
    def test_every_git_call_disables_hooks_fsmonitor_and_outside_configuration(self, sample_repo, process_recorder):
        git = make_git(sample_repo.path)
        base = git.resolve_commit("HEAD")
        git.branch_exists(BRANCH)
        git.export_tree(base, sample_repo.path.parent / "exported")
        commit(sample_repo)

        calls = process_recorder.implementation_git_calls
        assert {call.git_subcommand for call in calls} >= {
            "rev-parse",
            "show-ref",
            "archive",
            "commit-tree",
            "update-ref",
        }
        for call in calls:
            argv = list(call.argv)
            before_subcommand = argv[1 : argv.index(call.git_subcommand)]
            pairs = list(zip(before_subcommand[::2], before_subcommand[1::2], strict=False))
            assert ("-c", f"core.hooksPath={NULL_DEVICE}") in pairs, argv
            assert ("-c", "core.fsmonitor=false") in pairs, argv
            assert all(flag == "-c" for flag, _value in pairs), argv
            assert call.env["GIT_CONFIG_GLOBAL"] == NULL_DEVICE
            assert call.env["GIT_CONFIG_NOSYSTEM"] == "1"

    def test_commit_arguments_decided_in_the_design(self, sample_repo, process_recorder):
        """§19.3 の 16・17: ``hash-object`` に ``--path`` なし。``--no-gpg-sign``、``--no-deref``、古い値は全ゼロ。"""
        commit(sample_repo)

        calls = {call.git_subcommand: list(call.argv) for call in process_recorder.implementation_git_calls}

        def arguments(subcommand: str) -> list[str]:
            return calls[subcommand][calls[subcommand].index(subcommand) :]

        assert arguments("hash-object") == ["hash-object", "-w", "--stdin"]
        assert "--no-gpg-sign" in arguments("commit-tree")
        update = arguments("update-ref")
        assert update[:3] == ["update-ref", "--no-deref", f"refs/heads/{BRANCH}"]
        assert update[-1] == "0" * 40
        assert len(update) == 5

    def test_hooks_already_in_the_repository_are_never_run_by_the_commit(self, sample_repo):
        """最初から置いてあるフック（``reference-transaction`` を含む）も、実行されない。"""
        sample_repo.install_hooks()
        assert "reference-transaction" in sample_repo.installed_hooks

        sha = commit(sample_repo)

        assert sample_repo.rev(BRANCH) == sha
        assert sample_repo.state().hook_marks == ()

    def test_hooks_path_in_the_global_configuration_has_no_effect(self, sample_repo, tmp_path, monkeypatch):
        """開発者の全体設定の ``core.hooksPath`` は効かない（全体設定そのものを読まない）。"""
        mark = tmp_path / "global-hook-ran"
        hooks = tmp_path / "global-hooks"
        hooks.mkdir()
        for name in ("reference-transaction", "pre-commit", "post-commit", "commit-msg"):
            (hooks / name).write_text(hook_script(mark), encoding="utf-8")
            (hooks / name).chmod(0o755)
        home_with_global_config(tmp_path, monkeypatch, f"[core]\n\thooksPath = {hooks}\n")

        sha = commit(sample_repo)

        assert sample_repo.rev(BRANCH) == sha
        assert not mark.exists()

    def test_identity_only_in_the_global_configuration_is_not_used(self, sample_repo, tmp_path, monkeypatch):
        """作者は、リポジトリの中の設定にあればそれを使い、無ければ決まった名前。全体設定の作者は使わない。"""
        home_with_global_config(tmp_path, monkeypatch, GLOBAL_IDENTITY)
        sample_repo.git("config", "--unset", "user.name")
        sample_repo.git("config", "--unset", "user.email")

        sha = commit(sample_repo)

        assert sample_repo.author(sha) == "catch-expander-fixer"

    def test_identity_in_the_repository_configuration_is_used(self, sample_repo, tmp_path, monkeypatch):
        home_with_global_config(tmp_path, monkeypatch, GLOBAL_IDENTITY)

        sha = commit(sample_repo)

        assert sample_repo.author(sha) == repo_factory.AUTHOR_NAME


GLOBAL_IDENTITY = "[user]\n\tname = Global Only\n\temail = global" + "@" + "example.invalid\n"
TAMPERS = {
    "new-hook": lambda repo, mark: plant_hook(repo, mark),
    "new-pre-commit-hook": lambda repo, mark: plant_hook(repo, mark, "pre-commit"),
    "config": lambda repo, mark: (repo / ".git" / "config").open("a").write("[core]\n\tfsmonitor = /bin/false\n")
    and None,
    "config-hooks-path": lambda repo, mark: (repo / ".git" / "config")
    .open("a")
    .write("[core]\n\thooksPath = /elsewhere\n")
    and None,
}


class TestF2RepositoryTampering:
    @pytest.mark.parametrize("kind", list(TAMPERS))
    def test_repository_changed_during_the_gates_is_not_written_to(self, sample_repo, tmp_path, kind):
        """再現（reviewer）: テストの実行の間に、元のリポジトリの ``.git/hooks``・``.git/config`` が書き換わる。"""
        mark = tmp_path / "hook-ran"

        def tamper(run):
            TAMPERS[kind](sample_repo.path, mark)

        outcome = run_fixer(
            sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=FakeTestRunner(override=tamper)
        )

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "repository_tampered")
        assert outcome.run.branch is None
        assert outcome.run.commit is None
        assert outcome.new_branches == []
        assert not mark.exists()
        assert outcome.leftover_temp_dirs == []
        assert (
            json.loads((outcome.out_dir / "result.json").read_text(encoding="utf-8"))["stop_detail"]
            == "repository_tampered"
        )
        assert not (outcome.out_dir / "pr-draft.md").exists()

    def test_repository_changed_while_the_fixer_is_working_is_not_written_to(self, sample_repo, tmp_path):
        mark = tmp_path / "hook-ran"

        async def script(agent):
            await good_fix_without_finish(agent)
            plant_hook(sample_repo.path, mark)
            await agent.call("finish", dict(FINISH_FIXED))

        outcome = run_fixer(sample_repo, tmp_path, script=script)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "repository_tampered")
        assert outcome.new_branches == []
        assert not mark.exists()

    def test_existing_hook_whose_content_was_replaced_is_noticed(self, sample_repo, tmp_path):
        sample_repo.install_hooks()
        mark = tmp_path / "hook-ran"

        def tamper(run):
            plant_hook(sample_repo.path, mark, "post-commit")

        outcome = run_fixer(
            sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=FakeTestRunner(override=tamper)
        )

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "repository_tampered")
        assert not mark.exists()
        assert outcome.after.hook_marks == ()

    def test_untouched_repository_with_hooks_is_proposed_without_running_them(self, sample_repo, tmp_path):
        """改ざんではない（最初からフックがある）場合は ``proposed``。そのフックは、1 つも実行されない。"""
        sample_repo.install_hooks()

        outcome = run_fixer(sample_repo, tmp_path)

        assert outcome.run.status == "proposed"
        assert outcome.after.hook_marks == ()

    def test_new_test_that_plants_a_hook_in_the_repository_with_real_tools(self, sample_repo, tmp_path):
        """再現（reviewer）: 新しいテストが、元のリポジトリに ``reference-transaction`` フックを置く。"""
        mark = tmp_path / "hook-ran"
        hook = sample_repo.path / ".git" / "hooks" / "reference-transaction"
        test_source = (
            "from pathlib import Path\n\nfrom orchestrator import average\n\n"
            f"HOOK = Path({str(hook)!r})  # noqa: E501\n"
            f"SCRIPT = {hook_script(mark)!r}  # noqa: E501\n\n\n"
            "def test_average_of_empty_list_is_zero():\n"
            "    HOOK.parent.mkdir(exist_ok=True)\n"
            "    HOOK.write_text(SCRIPT, encoding='utf-8')\n"
            "    HOOK.chmod(0o755)\n"
            "    assert average([]) == 0.0\n"
        )

        outcome = run_fixer(
            sample_repo, tmp_path, script=write_script(FIXED_SOURCE, test_source), test_runner=None, lint_runner=None
        )

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "repository_tampered")
        assert outcome.new_branches == []
        assert not mark.exists()
        assert (
            hook.exists()
        )  # 置かれたフックは、人が見られるよう、そのまま残る（修正役は、元のリポジトリを書き換えない）


# ===========================================================================
# F3: 全体の制限時間が、上限になっていない
# ===========================================================================


def slow_runner(seconds: float, started: list[float] | None = None) -> FakeTestRunner:
    def slow(run):
        if started is not None:
            started.append(time.monotonic())
        time.sleep(seconds)

    return FakeTestRunner(override=slow)


class TestF3Deadline:
    def test_parallel_test_runs_do_not_start_after_the_overall_timeout(self, sample_repo, tmp_path):
        """再現（reviewer）: 並列に呼ばれたテスト実行が、時間切れの後も、順に始まる。"""
        started: list[float] = []
        runner = slow_runner(1.0, started)

        async def script(agent):
            await agent.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
            await asyncio.gather(*(agent.call("run_tests", {"paths": [NEW_TEST]}) for _ in range(4)))

        begin = time.monotonic()

        outcome = run_fixer(
            sample_repo, tmp_path, script=script, limits=limits(timeout_seconds=0.4), test_runner=runner
        )

        elapsed = time.monotonic() - begin
        assert (outcome.run.status, outcome.run.stop_detail) == ("incomplete", "timeout")
        assert len(started) == 1, f"{len(started)} test runs were started"
        assert elapsed < 0.4 + 1.0 + 1.5, f"run_fix returned after {elapsed:.1f} seconds"
        assert_discarded(outcome)

    def test_queued_tool_calls_do_not_touch_the_workspace_after_the_overall_timeout(self, sample_repo, tmp_path):
        """時間切れのときに待ち行列にあった書き込みは、実行されない。"""
        seen: dict[str, Path] = {}

        async def script(agent):
            seen["root"] = agent.root
            await agent.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
            await asyncio.gather(
                agent.call("run_tests", {"paths": [NEW_TEST]}),
                agent.call("write_file", {"path": "src/agent/queued.py", "content": "QUEUED = 1\n"}),
                agent.call("finish", dict(FINISH_FIXED)),
            )

        outcome = run_fixer(
            sample_repo, tmp_path, script=script, limits=limits(timeout_seconds=0.3), test_runner=slow_runner(1.0)
        )

        assert (outcome.run.status, outcome.run.stop_detail) == ("incomplete", "timeout")
        assert "root" in seen
        assert [call.tool for call in outcome.run.trace if call.outcome == "ok"] == ["write_file", "run_tests"]
        assert outcome.run.patch_kind == "unspecified"

    def test_call_that_starts_after_the_deadline_returns_deadline_exceeded_and_does_nothing(self, workspace):
        """期限を過ぎてから処理を始める呼び出しは、複製に触れず、テストも走らせない。足取りには残す。"""
        from src.fixer.tools import FixToolSession

        runner = FakeTestRunner()
        now = [100.0]
        session = FixToolSession(workspace, limits(), runner, clock=lambda: now[0], deadline=130.0)

        before = session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
        now[0] = 130.0
        results = [
            session.call("read_file", {"path": SRC}),
            session.call("write_file", {"path": "src/agent/late.py", "content": "LATE = 1\n"}),
            session.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW}),
            session.call("run_tests", {"paths": [NEW_TEST]}),
            session.call("finish", dict(FINISH_FIXED)),
        ]

        assert before.outcome == "ok"
        assert [(result.outcome, result.data, result.is_error) for result in results] == [
            ("deadline_exceeded", {}, True)
        ] * 5
        assert [call.outcome for call in session.trace] == ["ok"] + ["deadline_exceeded"] * 5
        assert [call.path for call in session.trace[1:]] == [None] * 5
        assert not (Path(workspace.root) / "src" / "agent" / "late.py").exists()
        assert (Path(workspace.root) / SRC).read_text(encoding="utf-8") == FILES[SRC]
        assert runner.calls == []
        assert session.finished is None

    def test_session_without_a_deadline_never_reports_deadline_exceeded(self, workspace):
        from src.fixer.tools import FixToolSession

        session = FixToolSession(workspace, limits(), FakeTestRunner(), clock=lambda: 10.0**9, deadline=None)

        assert session.call("read_file", {"path": SRC}).outcome == "ok"

    @pytest.mark.parametrize(
        ("deadline", "expected"),
        [(130.0, 30.0), (1_000.0, 300.0), (None, 300.0), (100.5, 0.5)],
        ids=["near", "far", "none", "close"],
    )
    def test_run_tests_is_limited_by_the_shorter_of_its_own_limit_and_the_time_left(
        self, workspace, deadline, expected
    ):
        """``run_tests`` には、1 回の制限時間と、全体の残り時間の、短いほうを渡す。"""
        from src.fixer.tools import FixToolSession

        runner = FakeTestRunner()
        session = FixToolSession(
            workspace, limits(test_timeout_seconds=300.0), runner, clock=lambda: 100.0, deadline=deadline
        )
        session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})

        session.call("run_tests", {"paths": [NEW_TEST]})

        assert runner.calls[0].timeout_seconds == pytest.approx(expected)

    def test_call_waiting_for_the_lock_does_not_start_once_the_session_is_closed(self, workspace):
        """閉じる印は、ロックの外で先に立てる。ロックを待っていた呼び出しは、処理を始めない。"""
        started, release = threading.Event(), threading.Event()

        def slow(run):
            started.set()
            release.wait(5)
            return make_report({})

        runner = FakeTestRunner(override=slow)
        session = make_session(workspace, runner=runner)
        results: dict[str, object] = {}
        first = threading.Thread(
            target=lambda: results.update(first=session.call("run_tests", {"paths": [EXISTING_TEST]}))
        )
        first.start()
        assert started.wait(5)
        queued = [
            threading.Thread(
                target=lambda: results.update(second=session.call("run_tests", {"paths": [EXISTING_TEST]}))
            ),
            threading.Thread(
                target=lambda: results.update(
                    third=session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
                )
            ),
        ]
        for thread in queued:
            thread.start()
        time.sleep(0.2)
        closer = threading.Thread(target=session.close)
        closer.start()
        time.sleep(0.2)
        release.set()
        for thread in (first, *queued, closer):
            thread.join(5)

        assert len(runner.calls) == 1
        assert results["second"].outcome == "internal_error"
        assert results["third"].outcome == "internal_error"
        assert not (Path(workspace.root) / NEW_TEST).exists()

    def test_pytest_runner_accepts_a_shorter_time_limit_for_one_run(self, tree, tmp_path):
        from src.fixer.runners import PytestRunner

        (tree / "tests" / "unit" / "test_slow.py").write_text(
            "import time\n\n\ndef test_slow():\n    time.sleep(60)\n", encoding="utf-8"
        )
        started = time.monotonic()

        report = PytestRunner(timeout_seconds=120.0).run(tree, ["tests/unit/test_slow.py"], timeout_seconds=1.5)

        assert report.timed_out is True
        assert time.monotonic() - started < 20


class TestF3GateBudget:
    def test_fix_limits_has_a_total_time_limit_for_the_gates(self):
        assert limits().gate_timeout_seconds == 900.0
        assert limits(gate_timeout_seconds=5.0).gate_timeout_seconds == 5.0

    def test_gates_stop_when_their_total_time_limit_is_used_up(self, sample_repo, tmp_path):
        """検査（テスト 4 回＋リント）の合計の上限を超えたら、残りは走らせず ``rejected``。"""
        runner = slow_runner(0.4)
        begin = time.monotonic()

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=runner,
            limits=limits(gate_timeout_seconds=0.6),
        )

        assert outcome.run.status == "rejected"
        assert len(runner.calls) < 4
        assert "G6" in stop_gates(outcome)
        assert time.monotonic() - begin < 5
        assert_discarded(outcome)

    def test_each_gate_run_is_limited_by_the_time_left_for_the_gates(self, sample_repo, tmp_path):
        runner = FakeTestRunner()

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=runner,
            limits=limits(gate_timeout_seconds=50.0, test_timeout_seconds=300.0),
        )

        assert outcome.run.status == "proposed"
        assert len(runner.calls) == 4
        assert all(run.timeout_seconds is not None and 0 < run.timeout_seconds <= 50.0 for run in runner.calls)

    def test_gate_runs_use_the_single_run_limit_when_it_is_shorter(self, sample_repo, tmp_path):
        runner = FakeTestRunner()

        run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=runner,
            limits=limits(gate_timeout_seconds=900.0, test_timeout_seconds=7.0),
        )

        assert [run.timeout_seconds for run in runner.calls] == [pytest.approx(7.0)] * 4


# ===========================================================================
# F4: 直してよい範囲の抜け
# ===========================================================================

# (a) 標準ライブラリと同じ名前、(b) ほかの根の直下に同じ名前がある、(c) 依存の最上位の名前
SHADOWING_PATHS = [
    "src/agent/secrets.py",
    "src/agent/json.py",
    "src/agent/os.py",
    "src/trigger/logging.py",
    "src/observability/typing.py",
    "src/subprocess.py",
    "src/agent/Secrets.py",
    "src/agent/JSON.py",
    "src/agent/json/__init__.py",
    "src/agent/json/decoder.py",
    "src/trigger/diagnosis.py",
    "src/trigger/orchestrator.py",
    "src/trigger/storage/__init__.py",
    "src/agent/handler.py",
    "src/agent/emitter.py",
    "src/observability/legacy.py",
    "src/agent/fixer.py",
    "src/trigger/agent/helper.py",
    "src/orchestrator.py",
    "src/dashboard_api/Orchestrator.py",
    "src/agent/boto3.py",
    "src/agent/requests/__init__.py",
    "src/trigger/pytest.py",
    "src/agent/_pytest.py",
    "src/agent/yaml.py",
    "src/agent/mcp/server.py",
    "src/agent/jwt.py",
    "src/agent/claude_agent_sdk.py",
    "src/agent/typing_extensions.py",
    "src/json/helper.py",
]
NOT_SHADOWING_PATHS = [
    "src/agent/storage/json.py",  # 直下ではない
    "src/agent/storage/secrets.py",
    "src/agent/jsonx.py",
    "src/agent/my_requests.py",
    "src/agent/new_module.py",
    "src/agent/new_package/helper.py",
    "src/new_root/helper.py",
    SRC,  # 既にあるファイルの変更
    NOTION,
]


class TestF4Scope:
    def test_write_file_refuses_names_that_would_shadow_other_modules(self, workspace):
        """再現（reviewer）: ``src/agent/secrets.py``・``src/trigger/diagnosis.py`` が ``ok`` になる。"""

        def check(path):
            session = make_session(workspace)
            result = session.call(
                "write_file", {"path": path, "content": "VALUE = 1\n" if "__init__" not in path else ""}
            )
            assert result.outcome == "denied"
            assert workspace.changes().is_empty

        fakes.check_each(SHADOWING_PATHS, check)

    def test_write_file_accepts_names_that_do_not_shadow_anything(self, workspace):
        def check(path):
            assert make_session(workspace).call("write_file", {"path": path, "content": "VALUE = 1\n"}).outcome == "ok"

        fakes.check_each(NOT_SHADOWING_PATHS, check)

    @pytest.mark.parametrize("path", SHADOWING_PATHS)
    def test_file_with_a_shadowing_name_placed_in_the_workspace_fails_g2(self, workspace, path):
        results = static(workspace, write_text(path, "VALUE = 1\n"))

        assert "G2" in failed(results)

    @pytest.mark.parametrize("path", [path for path in NOT_SHADOWING_PATHS if path not in (SRC, NOTION)])
    def test_file_without_a_shadowing_name_passes_g2(self, workspace, path):
        results = static(workspace, write_text(path, "VALUE = 1\n"))

        assert "G2" not in failed(results)

    def test_existing_init_files_under_src_cannot_be_changed_and_new_ones_must_be_empty(self, workspace):
        """``src/`` の下の ``__init__.py`` は、``tests/`` の下と同じ扱い。"""
        session = make_session(workspace)

        existing = session.call("write_file", {"path": "src/agent/storage/__init__.py", "content": "import os\n"})
        existing_blank = session.call("write_file", {"path": "src/observability/__init__.py", "content": ""})
        replaced = session.call("replace_in_file", {"path": "src/agent/diagnosis/__init__.py", "old": "x", "new": "y"})
        filled = session.call("write_file", {"path": "src/agent/new_package/__init__.py", "content": "import os\n"})
        top = session.call("write_file", {"path": "src/__init__.py", "content": "import os\n"})
        agent = session.call("write_file", {"path": "src/agent/__init__.py", "content": "import os\n"})
        blank = session.call("write_file", {"path": "src/agent/new_package/__init__.py", "content": ""})

        outcomes = [
            existing.outcome,
            existing_blank.outcome,
            replaced.outcome,
            filled.outcome,
            top.outcome,
            agent.outcome,
        ]
        assert outcomes == ["denied"] * 6
        assert blank.outcome == "ok"
        assert dict(workspace.changes().added) == {"src/agent/new_package/__init__.py": b""}

    @pytest.mark.parametrize(
        "tamper",
        [
            write_text("src/agent/storage/__init__.py", "import os\n"),
            fakes.delete("src/agent/storage/__init__.py"),
            write_text("src/__init__.py", "import os\n"),
            write_text("src/agent/__init__.py", "import os\n"),
            write_text("src/agent/new_package/__init__.py", "import os\n"),
        ],
        ids=["modify-existing", "delete-existing", "src-init", "agent-init", "new-with-code"],
    )
    def test_init_file_changes_under_src_fail_g2(self, workspace, tamper):
        """再現（Codex）: ``src/__init__.py`` は、``import src.fixer`` より先に実行される。"""
        assert "G2" in failed(static(workspace, tamper))

    def test_new_empty_init_file_under_src_passes_the_static_gates(self, workspace):
        tamper = all_of(
            write_text("src/agent/new_package/__init__.py", ""),
            write_text("src/agent/new_package/helper.py", "VALUE = 1\n"),
        )

        assert failed(static(workspace, tamper)) == set()

    def test_run_fix_with_a_shadowing_module_is_rejected(self, sample_repo, tmp_path):
        outcome = run_fixer(
            sample_repo, tmp_path, script=with_tamper(write_text("src/trigger/diagnosis.py", "VALUE = 1\n"))
        )

        assert outcome.run.status == "rejected"
        assert "G2" in stop_gates(outcome)

    @pytest.mark.parametrize(
        "path",
        [
            "src/agent/storage/github_client.py",
            "src/agent/storage/notion_client.py",
            "src/agent/notify/slack_client.py",
        ],
    )
    def test_draft_warns_when_a_file_that_handles_credentials_was_changed(self, tmp_path, path):
        from src.fixer.draft import render_pr_draft
        from src.fixer.handoff_input import load_handoff

        handoff = load_handoff(repo_factory.write_handoff(tmp_path / "handoff.json", handoff_dict()))

        with_note = render_pr_draft(
            make_fix_run(changed_files=[path, NEW_TEST]), handoff, [(path, 2, 0), (NEW_TEST, 5, 0)]
        )
        without = render_pr_draft(make_fix_run(), handoff, [(SRC, 2, 0), (NEW_TEST, 5, 0)])

        assert "認証情報を扱うファイル" in section(with_note, "取り込む前に人が行うこと")
        assert "認証情報を扱うファイル" not in without

    def test_run_fix_that_changes_a_credential_handling_file_writes_the_warning(self, sample_repo, tmp_path):
        outcome = run_fixer(sample_repo, tmp_path, script=with_tamper(append_text(NOTION, "\nVALUE = 1\n")))
        plain = run_fixer(
            sample_repo,
            tmp_path,
            out_dir=tmp_path / "second-out",
            handoff=handoff_dict(execution_id="exec-20261004120000-abcdef13"),
        )

        assert outcome.run.status == "proposed"
        assert "認証情報を扱うファイル" in (outcome.out_dir / "pr-draft.md").read_text(encoding="utf-8")
        assert plain.run.status == "proposed"
        assert "認証情報を扱うファイル" not in (plain.out_dir / "pr-draft.md").read_text(encoding="utf-8")


# ===========================================================================
# F5: 合否の検査の判定の甘さ
# ===========================================================================

EXISTING = dict(fakes.EXISTING_IDS)
A, B_, C_ = "t::a", "t::b", "t::c"


def gate_of(results, gate_id):
    return next(result for result in results if result.gate == gate_id)


class TestF5Gates:
    @pytest.mark.parametrize("exit_code", [2, 3, 4, 5, -9, 0, 1])
    def test_g7_fails_when_neither_run_has_any_result(self, exit_code):
        """再現（Codex）: 基準と修正後が、どちらも結果が空で、終了コードが同じ。"""
        results = dynamic(
            base_existing=make_report({}, exit_code=exit_code), post_fix_existing=make_report({}, exit_code=exit_code)
        )

        assert gate_of(results, "G7").passed is False

    @pytest.mark.parametrize("exit_code", [2, 3, 4, 5, -9])
    def test_g7_fails_when_the_runs_did_not_end_with_exit_code_zero_or_one(self, exit_code):
        results = dynamic(
            base_existing=make_report(dict(EXISTING), exit_code=exit_code),
            post_fix_existing=make_report(dict(EXISTING), exit_code=exit_code),
        )

        assert gate_of(results, "G7").passed is False

    @pytest.mark.parametrize("outcomes", [{A: "passed"}, {A: "passed", B_: "failed"}], ids=["exit-0", "exit-1"])
    def test_g7_passes_when_both_runs_have_the_same_results_and_exit_code_zero_or_one(self, outcomes):
        results = dynamic(base_existing=make_report(dict(outcomes)), post_fix_existing=make_report(dict(outcomes)))

        assert gate_of(results, "G7").passed is True

    @pytest.mark.parametrize(
        "report",
        [
            lambda: make_report({"::tests.unit.test_new": "error"}, exit_code=2),
            lambda: make_report({}, exit_code=2),
            lambda: make_report({}, exit_code=1),
            lambda: make_report({A: "failed"}, exit_code=2),
            lambda: make_report({A: "failed"}, exit_code=0),
        ],
        ids=["collection-error", "no-results-2", "no-results-1", "failed-exit-2", "failed-exit-0"],
    )
    def test_g5_requires_exit_code_one_and_a_failing_test(self, report):
        """再現（reviewer）: 「集めるときのエラー」（終了コード 2）でも、G-5 が通る。"""
        assert gate_of(dynamic(pre_fix_new=report()), "G5").passed is False

    @pytest.mark.parametrize(
        ("before", "after", "passes"),
        [
            ({A: "failed"}, {A: "passed"}, True),
            ({A: "error"}, {A: "passed"}, True),
            ({A: "failed", B_: "passed"}, {A: "passed", B_: "passed"}, True),
            ({A: "failed"}, {A: "passed", C_: "passed"}, True),
            ({A: "failed", B_: "failed"}, {A: "passed"}, False),
            ({A: "failed"}, {B_: "passed"}, False),
            ({A: "failed"}, {A: "skipped", B_: "passed"}, False),
            ({A: "failed", B_: "error"}, {A: "passed", B_: "skipped"}, False),
            ({A: "failed"}, {}, False),
        ],
        ids=[
            "same-test-passes",
            "error-then-passes",
            "others-unchanged",
            "extra-test-after",
            "failing-test-disappeared",
            "different-test-passes",
            "failing-test-skipped",
            "one-of-two-skipped",
            "nothing-ran",
        ],
    )
    def test_g6_requires_every_test_that_failed_before_to_pass_after(self, before, after, passes):
        """修正前に落ちたテストの ID が、すべて、修正後に通った ID に含まれること。"""
        results = dynamic(pre_fix_new=make_report(dict(before)), post_fix_new=make_report(dict(after), exit_code=0))

        assert gate_of(results, "G6").passed is passes

    def test_tests_whose_names_share_the_first_five_hundred_characters_are_kept_apart(self, tree):
        """再現（Codex）: ID を 500 字で切って比べるので、先頭が同じ 2 つのテストが合流する。"""
        from src.fixer.runners import PytestRunner

        prefix = "n" * 600
        cases = CASE.format(name=prefix + "_first", child="<failure/>") + CASE.format(name=prefix + "_second", child="")
        source = rewriting_test(f"file.write(b'<testsuites>{cases}</testsuites>')")
        (tree / "tests" / "unit" / "test_rewrite.py").write_text(source, encoding="utf-8")

        report = PytestRunner().run(tree, ["tests/unit/test_rewrite.py"])

        assert sorted(report.outcomes.values()) == ["failed", "passed"]
        assert len(report.outcomes) == 2

    def test_run_with_more_results_than_the_limit_cannot_pass_any_gate(self, tree, monkeypatch):
        """上限を超えたぶんを、黙って捨てない。「検査できなかった」として、その検査は不合格。"""
        from src.fixer.runners import PytestRunner

        monkeypatch.setattr("src.fixer.runners.MAX_TEST_CASES", 2)
        cases = "".join(CASE.format(name=f"test_{index}", child="") for index in range(5))
        source = rewriting_test(f"file.write(b'<testsuites>{cases}</testsuites>')")
        (tree / "tests" / "unit" / "test_rewrite.py").write_text(source, encoding="utf-8")

        report = PytestRunner().run(tree, ["tests/unit/test_rewrite.py"])

        as_new = dynamic(pre_fix_new=make_report({"tests.unit.test_rewrite::test_0": "failed"}), post_fix_new=report)
        as_existing = dynamic(base_existing=report, post_fix_existing=report)
        assert gate_of(as_new, "G6").passed is False
        assert gate_of(as_existing, "G7").passed is False

    def test_test_that_only_imports_a_module_added_by_the_fix_is_rejected_with_real_tools(self, sample_repo, tmp_path):
        """再現（reviewer）: 新しいモジュールを足し、それを import するテストを書くだけで、G-5 と G-6 が通る。"""
        module = "def safe_average(values):\n    return sum(values) / len(values) if values else 0.0\n"
        test_source = (
            "from empty_guard import safe_average\n\n\ndef test_safe_average_of_empty_list():\n"
            "    assert safe_average([]) == 0.0\n"
        )

        async def script(agent):
            await agent.call("write_file", {"path": "src/agent/empty_guard.py", "content": module})
            await agent.call("write_file", {"path": NEW_TEST, "content": test_source})
            await agent.call("finish", dict(FINISH_FIXED))

        outcome = run_fixer(sample_repo, tmp_path, script=script, test_runner=None, lint_runner=None)

        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", "G5")

    def test_system_prompt_tells_the_model_that_collection_errors_do_not_count(self):
        from src.fixer.prompt import SYSTEM_PROMPT

        assert "集めるときのエラー" in SYSTEM_PROMPT
        assert "import するだけ" in SYSTEM_PROMPT


# ===========================================================================
# F6: 出力ファイルの扱い
# ===========================================================================

SWAP_OUTPUT_TEST = """\
import os
import stat
from pathlib import Path


def test_swap():
    base = Path(os.environ["TMPDIR"]).parent
    swapped = []
    for path in sorted(base.rglob("*")):
        if stat.S_ISREG(path.lstat().st_mode) and path.suffix != ".xml":
            path.unlink()
            os.mkfifo(path)
            swapped.append(str(path))
    Path({record!r}).write_text("\\n".join(swapped), encoding="utf-8")
"""


def release_fifos(record: Path) -> None:
    """止まってしまった読み手を解放する（FIFO の書き込み側を開いて、閉じる）。"""
    if record.is_file():
        for name in record.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(OSError):
                os.close(os.open(name, os.O_WRONLY | os.O_NONBLOCK))


def run_in_thread(action, seconds: float, record: Path):
    box: dict[str, object] = {}
    thread = threading.Thread(target=lambda: box.update(value=action()), daemon=True)
    thread.start()
    thread.join(seconds)
    blocked = thread.is_alive()
    if blocked:
        release_fifos(record)
        thread.join(15)
    return blocked, box.get("value")


class TestF6Output:
    def test_test_that_replaces_the_output_file_with_a_fifo_does_not_block_the_runner(self, tree, tmp_path):
        """再現（Codex）: テストが、出力ファイルを消して同名の FIFO を作ると、親が読み出しで待ち続ける。"""
        from src.fixer.runners import PytestRunner

        record = tmp_path / "swapped.txt"
        (tree / "tests" / "unit" / "test_swap.py").write_text(
            SWAP_OUTPUT_TEST.format(record=str(record)), encoding="utf-8"
        )

        blocked, report = run_in_thread(
            lambda: PytestRunner(timeout_seconds=30.0).run(tree, ["tests/unit/test_swap.py"]), 12, record
        )

        assert blocked is False, "the runner was still blocked after 12 seconds (the test itself is instant)"
        assert report.timed_out is False
        assert report.outcomes == {"tests.unit.test_swap::test_swap": "passed"}

    def test_report_file_replaced_with_a_fifo_is_treated_as_no_result(self, tree, tmp_path):
        from src.fixer.runners import PytestRunner

        record = tmp_path / "swapped.txt"
        source = rewriting_test("file.write(b'x')").replace(
            "    with open(path, \"wb\") as file:\n        file.write(b'x')",
            "    import os\n\n    os.unlink(path)\n    os.mkfifo(path)\n"
            f"    with open({str(record)!r}, 'w') as file:\n        file.write(path)",
        )
        assert "os.mkfifo(path)" in source
        (tree / "tests" / "unit" / "test_rewrite.py").write_text(source, encoding="utf-8")

        blocked, report = run_in_thread(
            lambda: PytestRunner(timeout_seconds=30.0).run(tree, ["tests/unit/test_rewrite.py"]), 12, record
        )

        assert blocked is False
        assert report.outcomes == {}

    def test_run_that_writes_more_output_than_the_limit_is_stopped_and_has_no_result(self, tree, tmp_path, monkeypatch):
        """出力の大きさに上限がある。超えたら、実行を止め、その実行は「検査できなかった」。"""
        from src.fixer.runners import PytestRunner

        monkeypatch.setattr("src.fixer.runners.MAX_OUTPUT_BYTES", 200_000, raising=False)
        pid_file = tmp_path / "pid.txt"
        (tree / "tests" / "unit" / "test_noisy.py").write_text(
            "import os\nimport time\n\n\ndef test_noisy(capsys):\n"
            f"    with open({str(pid_file)!r}, 'w') as file:\n        file.write(str(os.getpid()))\n"
            "    with capsys.disabled():\n"
            "        for _ in range(400):\n"
            "            print('x' * 10_000, flush=True)\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        started = time.monotonic()

        report = PytestRunner(timeout_seconds=8.0).run(tree, ["tests/unit/test_noisy.py"])

        assert time.monotonic() - started < 6
        assert report.outcomes == {}
        assert report.exit_code is None
        assert len(report.output_tail) <= 4_000
        assert wait_until_dead(int(pid_file.read_text()))

    def test_default_output_limit_is_fifty_megabytes(self):
        from src.fixer import runners

        assert runners.MAX_OUTPUT_BYTES == 50_000_000

    def test_run_tests_tool_reports_an_error_when_the_output_is_too_large(self, workspace, monkeypatch):
        from src.fixer.runners import PytestRunner

        monkeypatch.setattr("src.fixer.runners.MAX_OUTPUT_BYTES", 200_000, raising=False)
        noisy = (
            "def test_noisy(capsys):\n    with capsys.disabled():\n        for _ in range(400):\n"
            "            print('x' * 10_000, flush=True)\n"
        )
        session = make_session(workspace, runner=PytestRunner(timeout_seconds=30.0))
        session.call("write_file", {"path": "tests/unit/test_noisy.py", "content": noisy})

        result = session.call("run_tests", {"paths": ["tests/unit/test_noisy.py"]})

        assert result.is_error is True
        assert len(result.to_text()) < 6_000

    def test_scratch_directories_are_removed_even_when_the_test_locks_a_directory_in_home(self, tree):
        """再現（reviewer）: テストが ``$HOME`` の下に権限 000 のディレクトリを作ると、``/tmp/fixer-run-*`` が残る。"""
        from src.fixer.runners import PytestRunner

        (tree / "tests" / "unit" / "test_lock.py").write_text(
            "import os\nfrom pathlib import Path\n\n\ndef test_lock():\n"
            "    for base in (Path(os.environ['HOME']), Path(os.environ['TMPDIR'])):\n"
            "        inner = base / 'locked' / 'inner'\n"
            "        inner.mkdir(parents=True)\n"
            "        (inner / 'file.txt').write_text('x')\n"
            "        inner.chmod(0o000)\n"
            "        (base / 'locked').chmod(0o000)\n",
            encoding="utf-8",
        )
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def recording(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        with patch.object(tempfile, "mkdtemp", recording):
            report = PytestRunner().run(tree, ["tests/unit/test_lock.py"])

        leftovers = [path for path in created if path.exists()]
        try:
            assert report.outcomes == {"tests.unit.test_lock::test_lock": "passed"}
            assert created, "the runner did not create a scratch directory with tempfile.mkdtemp"
            assert leftovers == []
        finally:
            from src.fixer.workspace import remove_tree

            for path in leftovers:
                remove_tree(path)


# ===========================================================================
# F7: 行数の計算が、二乗の時間
# ===========================================================================

STAGES = (8_000, 50_000, 200_000)
BUDGET_SECONDS = 2.0


def blank_lines(count: int) -> tuple[bytes, bytes]:
    return b"\n" * count + b"last = 1\n", b"\n" * count + b"last = 2\n"


def repeated_lines(count: int) -> tuple[bytes, bytes]:
    half = count // 2
    return b"x = 1\n" * count, b"x = 1\n" * half + b"y = 2\n" + b"x = 1\n" * (count - half - 1)


def distinct_lines(count: int) -> tuple[bytes, bytes]:
    lines = [f"v{index}\n".encode() for index in range(count)]
    changed = list(lines)
    changed[count // 2] = b"changed\n"
    return b"".join(lines), b"".join(changed)


class TestF7LineCounting:
    @pytest.mark.parametrize("build", [blank_lines, repeated_lines, distinct_lines])
    def test_counting_changed_lines_takes_roughly_linear_time(self, workspace, build):
        """再現（Codex）: 空行が並ぶファイルの末尾を 1 行変えると、行数の 2 乗の時間がかかる（8,000 行で 4.6 秒）。

        大きさを段階的に上げ、各段で予算を見る（最初に超えた段で止めるので、遅い実装でも長くは待たない）。
        """
        from src.fixer.gates import check_static
        from src.fixer.models import FixLimits
        from src.fixer.workspace import ChangeSet

        for count in STAGES:
            old, new = build(count)
            baseline = {**workspace.baseline, LEGACY: SimpleNamespace(content=old, mode=0o644, kind="file")}
            changes = ChangeSet(
                added={NEW_TEST: NEW_TEST_SOURCE.encode()},
                modified={LEGACY: new},
                deleted=(),
                irregular=(),
                oversized=(),
            )
            started = time.perf_counter()

            results = check_static(changes, baseline, FixLimits())

            elapsed = time.perf_counter() - started
            assert elapsed < BUDGET_SECONDS, f"{count} lines took {elapsed:.2f} seconds"
            detail = next(result.detail for result in results if result.gate == "G9")
            # 新しいテスト 5 行と、変えた 1 行（足した 1 行＋消した 1 行）
            assert "変更した行 7 行" in detail, detail
            assert "G9" not in failed(results)

    def test_ordinary_changes_are_counted_as_before(self):
        from src.fixer.gates import count_changed_lines

        old = b"a\nb\nc\nd\n"

        assert count_changed_lines(old, b"a\nb\nc\nd\n") == (0, 0)
        assert count_changed_lines(old, b"a\nB\nc\nd\n") == (1, 1)
        assert count_changed_lines(old, b"a\nb\nc\nd\ne\nf\n") == (2, 0)
        assert count_changed_lines(old, b"a\nd\n") == (0, 2)
        assert count_changed_lines(old, b"x\na\nb\nc\nd\n") == (1, 0)
        assert count_changed_lines(None, b"a\nb\n") == (2, 0)
        assert count_changed_lines(old, None) == (0, 4)
        assert count_changed_lines(FILES[SRC].encode(), FIXED_SOURCE.encode()) == (2, 0)


# ===========================================================================
# F8: 検証の根がリンクだと、対象外の場所が別のパスとして通る
# ===========================================================================


def verified(tmp_path, tree, entry):
    from src.fixer.handoff_input import load_handoff, verify_locations

    handoff = load_handoff(repo_factory.write_handoff(tmp_path / "handoff.json", handoff_dict(code_locations=[entry])))
    return verify_locations(handoff, tree)


class TestF8LinkedRoots:
    def test_location_is_dropped_when_the_agent_root_is_a_link_to_the_fixer(self, tmp_path, tree):
        """再現（Codex）: ``src/agent`` が ``fixer`` へのリンクのとき、``/app/gates.py`` が検証を通る。"""
        (tree / "src" / "agent").rename(tree / "src" / "agent-moved")
        os.symlink("fixer", tree / "src" / "agent")

        result = verified(tmp_path, tree, {"path": "/app/gates.py", "function": "check_static", "line": 1})

        assert result == []

    @pytest.mark.parametrize("target", ["observability", "agent-moved"])
    def test_location_is_dropped_when_the_agent_root_is_a_link_to_an_allowed_directory(self, tmp_path, tree, target):
        """検証の根がシンボリックリンクなら、指す先が対象外でなくても、その場所は取り除く。"""
        (tree / "src" / "agent").rename(tree / "src" / "agent-moved")
        os.symlink(target, tree / "src" / "agent")
        entry = (
            {"path": "/app/emitter.py", "function": "emit", "line": 5}
            if target == "observability"
            else {"path": "/app/orchestrator.py", "function": "average", "line": 9}
        )

        assert verified(tmp_path, tree, entry) == []

    @pytest.mark.parametrize(
        ("link", "target", "entry"),
        [
            (
                "src/agent/shortcut",
                "../fixer",
                {"path": "/app/shortcut/gates.py", "function": "check_static", "line": 1},
            ),
            (
                "src/agent/shortcut",
                "storage",
                {"path": "/app/shortcut/notion_client.py", "function": "create_page", "line": 1},
            ),
            ("src/agent/shortcut", "diagnosis", {"path": "/app/shortcut/tools.py", "function": "call", "line": 1}),
            (
                "src/observability/shortcut",
                "../agent",
                {"path": "/app/src/observability/shortcut/main.py", "function": "setup_credentials", "line": 1},
            ),
        ],
        ids=["to-fixer", "to-allowed", "to-diagnosis", "to-agent"],
    )
    def test_location_through_a_linked_directory_is_dropped(self, tmp_path, tree, link, target, entry):
        """途中の要素がシンボリックリンクなら、その場所は取り除く。"""
        os.symlink(target, tree / link)

        assert verified(tmp_path, tree, entry) == []

    def test_location_is_dropped_when_src_itself_is_a_link(self, tmp_path, tree):
        (tree / "src").rename(tree / "src-moved")
        os.symlink("src-moved", tree / "src")

        assert verified(tmp_path, tree, {"path": "/app/orchestrator.py", "function": "average", "line": 9}) == []

    def test_ordinary_location_is_still_kept(self, tmp_path, tree):
        result = verified(tmp_path, tree, {"path": "/app/orchestrator.py", "function": "average", "line": 9})

        assert [(item.path, item.function) for item in result] == [(SRC, "average")]


# ===========================================================================
# F9: 下書きとコミットに入る文字
# ===========================================================================

FENCE = re.compile(r"`{3,}")
HEADING_COUNT = 8  # 題と、7 つの節
INVISIBLE = {
    "u202a": "\u202a",
    "u202b": "\u202b",
    "u202c": "\u202c",
    "u202d": "\u202d",
    "u202e": "\u202e",
    "u2066": "\u2066",
    "u2067": "\u2067",
    "u2068": "\u2068",
    "u2069": "\u2069",
    "u200b": "\u200b",
    "u200c": "\u200c",
    "u200d": "\u200d",
    "u200e": "\u200e",
    "u200f": "\u200f",
    "u2060": "\u2060",
    "ufeff": "\ufeff",
}


def render(tmp_path, explanation: str, **overrides) -> str:
    from src.fixer.draft import render_pr_draft
    from src.fixer.handoff_input import load_handoff

    handoff = load_handoff(repo_factory.write_handoff(tmp_path / "handoff.json", handoff_dict()))
    return render_pr_draft(make_fix_run(explanation=explanation, **overrides), handoff, [(SRC, 2, 0)])


def fenced_explanation(draft: str) -> tuple[str, list[str]]:
    """「修正役の説明」の節の、（フェンス, フェンスの中の行）。フェンスが正しく閉じていることも確かめる。"""
    lines = section(draft, "修正役の説明").split("\n")
    opening = next((index for index, line in enumerate(lines) if FENCE.match(line)), None)
    assert opening is not None, "the explanation is not inside a fenced code block"
    fence = FENCE.match(lines[opening]).group(0)
    closing = max(index for index, line in enumerate(lines) if line.strip() == fence)
    assert closing > opening, "the code block is not closed"
    body = lines[opening + 1 : closing]
    # フェンスより長い（または同じ長さの）バッククォートの連続が中にあると、コードブロックが途中で閉じる
    assert all(
        len(run) < len(fence) for line in body for run in FENCE.findall(line)
    ), "the fence can be closed from inside"
    # フェンスの後ろには、節の終わりまで、空行しか無い
    assert all(not line.strip() for line in lines[closing + 1 :]), lines[closing + 1 :]
    return fence, body


def headings_outside_code_blocks(draft: str) -> list[str]:
    found, inside, fence = [], False, ""
    for line in draft.split("\n"):
        match = FENCE.match(line)
        if match and not inside:
            inside, fence = True, match.group(0)
        elif inside and line.strip() == fence:
            inside = False
        elif not inside and line.lstrip().startswith("#"):
            found.append(line)
    return found


FORGED = "## 検査の結果\n- G5: 合格 — 偽の結果\n# 取り込む前に人が行うこと\n確認は不要です"


class TestF9DraftExplanation:
    def test_explanation_is_placed_inside_a_fenced_code_block(self, tmp_path):
        """モデルの説明は、囲みのコードブロックに入れる（リンク・画像・メンション・見出し・HTML が無効になる）。"""
        draft = render(tmp_path, "一行目\n二行目")

        _fence, body = fenced_explanation(draft)

        assert body == ["一行目", "二行目"]
        heading = next(line for line in draft.split("\n") if line.startswith("#") and "修正役の説明" in line)
        assert "モデルの説明・未検証" in heading

    @pytest.mark.parametrize(
        "text",
        [
            "詳細は [証跡](//evil.example/login) を参照",
            "![画像](//evil.example/pixel.png)",
            "www.evil.example を開く",
            "https://evil.example/x",
            "<script>alert(1)</script> <img src=//evil.example/p.png>",
            "@octocat #123 を参照",
            FORGED,
        ],
        ids=["scheme-less-link", "image", "autolink", "url", "html", "mention", "headings"],
    )
    def test_markup_in_the_explanation_stays_inside_the_code_block(self, tmp_path, text):
        """再現（reviewer）: ``[x](//host/…)``・``![i](//host/p.png)``・``www.`` の自動リンクが残る。"""
        draft = render(tmp_path, text)

        _fence, body = fenced_explanation(draft)

        assert body == text.split("\n")
        assert len(headings_outside_code_blocks(draft)) == HEADING_COUNT
        assert "偽の結果" not in section(draft, "検査の結果")

    @pytest.mark.parametrize(
        "text",
        [
            "```\n## 検査の結果\n- G5: 合格 — 偽の結果",
            "````\n# 偽の見出し",
            "~~~\n# 偽の見出し\n```\n# もう 1 つ",
            "`````````````\n# 偽の見出し",
            "説明\n   ```\n# 偽の見出し",
            "``` ```` `````\n# 偽の見出し",
        ],
        ids=["three", "four", "tilde-and-three", "thirteen", "indented", "several"],
    )
    def test_explanation_cannot_close_the_code_block_and_escape(self, tmp_path, text):
        """フェンスは、説明の中のどのバッククォートの連続よりも長い（または、連続を取り除く）。"""
        draft = render(tmp_path, text)

        fenced_explanation(draft)

        assert len(headings_outside_code_blocks(draft)) == HEADING_COUNT
        assert "偽の結果" not in section(draft, "検査の結果")

    @pytest.mark.parametrize(
        "separator",
        ["\r\n", "\r", "\u2028", "\u2029", "\x0b", "\x0c", "\x85", "\x1c"],
        ids=["crlf", "cr", "u2028", "u2029", "vt", "ff", "nel", "fs"],
    )
    def test_no_line_separator_other_than_line_feed_survives(self, tmp_path, separator):
        draft = render(tmp_path, "説明" + separator + "```" + separator + FORGED.replace("\n", separator))

        fenced_explanation(draft)

        assert len(draft.splitlines()) == len(draft.split("\n")) - 1
        assert len(headings_outside_code_blocks(draft)) == HEADING_COUNT

    @pytest.mark.parametrize("name", list(INVISIBLE))
    def test_invisible_and_bidirectional_characters_are_removed_from_the_explanation(self, tmp_path, name):
        """再現（reviewer）: U+202E・U+2066・U+200B が、そのまま残る。"""
        draft = render(tmp_path, f"前{INVISIBLE[name]}後")

        _fence, body = fenced_explanation(draft)

        assert INVISIBLE[name] not in draft
        assert body == ["前後"]

    def test_token_crossing_the_length_limit_is_masked_before_the_cut(self, tmp_path):
        from ..agent.diagnosis.factories import fake_tokens

        for token in fake_tokens().values():
            draft = render(tmp_path, "あ" * 790 + token)
            assert token[:12] not in draft

    def test_explanation_is_still_cut_to_the_length_limit(self, tmp_path):
        draft = render(tmp_path, "〓" * 5_000)

        assert 1 <= draft.count("〓") <= 800

    def test_empty_explanation_is_shown_as_a_fixed_placeholder(self, tmp_path):
        for explanation in ("", "   ", "\n\n", "\u200b\u2028"):
            draft = render(tmp_path, explanation)
            assert "（なし）" in section(draft, "修正役の説明")
            assert len(headings_outside_code_blocks(draft)) == HEADING_COUNT

    def test_lone_surrogates_are_removed_and_the_draft_can_be_written(self, tmp_path):
        draft = render(tmp_path, "前\ud800中\udfff後")

        assert fenced_explanation(draft)[1] == ["前中後"]
        assert draft.encode("utf-8").decode("utf-8") == draft

    def test_run_fix_writes_every_output_for_an_explanation_full_of_markup(self, sample_repo, tmp_path):
        explanation = "前\ud800後\u2028```\n## 偽の見出し\n[x](//evil.example/a) \u202e"

        async def script(agent):
            await good_fix_without_finish(agent)
            reply = await agent.call("finish", {**FINISH_FIXED, "explanation": explanation})
            assert reply.outcome == "ok"

        outcome = run_fixer(sample_repo, tmp_path, script=script)

        assert outcome.run.status == "proposed"
        assert sorted(path.name for path in outcome.out_dir.iterdir()) == ["pr-draft.md", "result.json", "trace.json"]
        draft = (outcome.out_dir / "pr-draft.md").read_text(encoding="utf-8")
        fenced_explanation(draft)
        assert len(headings_outside_code_blocks(draft)) == HEADING_COUNT
        assert "\u202e" not in draft


class TestF9CommittedCharacters:
    @pytest.mark.parametrize("name", [*INVISIBLE, "cr", "crlf"])
    def test_file_with_invisible_bidirectional_or_carriage_return_characters_fails_g10(self, workspace, name):
        """コミットされる中身に、見た目と実体が違うコードを作れる文字があれば、不合格。"""
        char = {"cr": "\r", "crlf": "\r\n"}.get(name) or INVISIBLE[name]
        tamper = write_text("src/agent/hidden_meaning.py", f"VALUE = 'a{char}b'\n")

        results = static(workspace, tamper)

        assert failed(results) == {"G10"}

    @pytest.mark.parametrize("target", [SRC, NEW_TEST])
    def test_bidirectional_override_in_a_changed_file_fails_g10(self, workspace, target):
        results = static(workspace, append_text(target, "\n# \u202e txet neddih\n"))

        assert failed(results) == {"G10"}

    @pytest.mark.parametrize("name", ["u202e", "u2066", "u200b", "ufeff", "cr", "crlf"])
    def test_write_tools_refuse_content_with_these_characters(self, workspace, name):
        char = {"cr": "\r", "crlf": "\r\n"}.get(name) or INVISIBLE[name]
        session = make_session(workspace)

        written = session.call(
            "write_file", {"path": "src/agent/hidden_meaning.py", "content": f"VALUE = 'a{char}b'\n"}
        )
        replaced = session.call(
            "replace_in_file", {"path": SRC, "old": FIX_OLD, "new": f"    total = sum(values){char}\n"}
        )

        assert (written.outcome, written.data) == ("invalid_input", {"invalid_fields": ["content"]})
        assert (replaced.outcome, replaced.data) == ("invalid_input", {"invalid_fields": ["new"]})
        assert workspace.changes().is_empty

    def test_run_fix_does_not_propose_a_file_written_with_carriage_returns(self, sample_repo, tmp_path):
        crlf_test = NEW_TEST_SOURCE.replace("\n", "\r\n")

        direct = run_fixer(
            sample_repo,
            tmp_path,
            script=with_tamper(lambda root: (root / "tests" / "unit" / "test_crlf.py").write_bytes(crlf_test.encode())),
        )

        assert direct.run.status == "rejected"
        assert "G10" in stop_gates(direct)


# ===========================================================================
# F10: 実際に動かす前に直すもの
# ===========================================================================


@pytest.fixture
def attributed(sample_repo):
    def commit_attributes(path: str, text: str) -> str:
        repo_factory.write_files(sample_repo.path, {path: text})
        return sample_repo.commit_all("attributes")

    return commit_attributes


class TestF10:
    @pytest.mark.parametrize("path", [".", ""])
    def test_list_files_can_list_the_workspace_root(self, workspace, path):
        """reviewer の指摘: 実際のモデルが、最初につまずく。"""
        root = Path(workspace.root)
        (root / ".pytest_cache").mkdir()
        (root / ".git").mkdir()
        session = make_session(workspace)

        result = session.call("list_files", {"path": path})

        entries = {entry["name"]: entry["type"] for entry in result.data["entries"]}
        assert result.outcome == "ok"
        assert entries["src"] == "dir"
        assert entries["tests"] == "dir"
        assert entries["pyproject.toml"] == "file"
        assert ".git" not in entries
        assert ".pytest_cache" not in entries
        assert session.trace[0].outcome == "ok"

    @pytest.mark.parametrize("path", ["./src", "src/.", "..", "./..", "src/./agent"])
    def test_other_paths_with_dot_segments_are_still_denied(self, workspace, path):
        assert make_session(workspace).call("list_files", {"path": path}).outcome == "denied"

    def test_read_file_on_the_workspace_root_is_not_a_file(self, workspace):
        assert make_session(workspace).call("read_file", {"path": "."}).is_error is True

    def test_hard_linked_files_are_neither_read_nor_written(self, workspace, outside):
        """リンク数が 2 以上の通常のファイルは、読み取り・書き込みとも断る。"""
        root = Path(workspace.root)
        os.link(outside / "secret.py", root / "src" / "agent" / "hard.py")
        os.link(root / MAIN, root / "src" / "agent" / "twin.py")
        session = make_session(workspace)

        results = [
            session.call("read_file", {"path": "src/agent/hard.py"}),
            session.call("write_file", {"path": "src/agent/hard.py", "content": "X = 1\n"}),
            session.call("replace_in_file", {"path": "src/agent/hard.py", "old": "OUTSIDE", "new": "X"}),
            session.call("read_file", {"path": "src/agent/twin.py"}),
            session.call("write_file", {"path": "src/agent/twin.py", "content": "X = 1\n"}),
            session.call("read_file", {"path": MAIN}),
        ]
        search = session.call("search_code", {"pattern": "OUTSIDE_FILE_MARK"})

        assert [result.outcome for result in results] == ["denied"] * 6
        assert all("OUTSIDE_FILE_MARK" not in result.to_text() for result in results)
        assert search.data["matches"] == []
        assert (outside / "secret.py").read_text(encoding="utf-8").startswith("# OUTSIDE_FILE_MARK")
        assert (root / MAIN).read_text(encoding="utf-8") == FILES[MAIN]

    @pytest.mark.parametrize(
        ("path", "text"),
        [
            (".gitattributes", f"{LEGACY} export-ignore\n"),
            (".gitattributes", f"{NOTION} export-subst\n"),
            ("src/agent/.gitattributes", "legacy.py export-ignore\n"),
            ("tests/unit/.gitattributes", "test_orchestrator.py export-ignore\n"),
            (".gitattributes", "* text=auto eol=lf\n*.py export-subst\n"),
        ],
        ids=["ignore", "subst", "nested-ignore", "nested-tests", "mixed"],
    )
    def test_repository_with_export_attributes_is_refused_before_starting(
        self, sample_repo, tmp_path, attributed, path, text
    ):
        """基準のコミットの ``.gitattributes``（どの階層でも）に ``export-ignore``・``export-subst`` があれば、断る。"""
        attributed(path, text)

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unsupported_attributes")
        assert outcome.sdk.invocation_count == 0
        assert not outcome.out_dir.exists()
        assert outcome.new_branches == []

    def test_repository_with_ordinary_attributes_is_accepted(self, sample_repo, tmp_path, attributed):
        attributed(
            ".gitattributes", "* text=auto eol=lf\n*.png binary\n# export-ignore is only mentioned in this comment\n"
        )

        outcome = run_fixer(sample_repo, tmp_path)

        assert outcome.run.status == "proposed"

    def test_attributes_are_read_from_the_base_commit_not_from_the_working_tree(self, sample_repo, tmp_path):
        (sample_repo.path / ".gitattributes").write_text(f"{LEGACY} export-ignore\n", encoding="utf-8")

        outcome = run_fixer(sample_repo, tmp_path)

        assert outcome.run.status == "proposed"

    @pytest.mark.parametrize("target", ["refs/heads/somewhere-else", "refs/heads/main"], ids=["dangling", "existing"])
    def test_symbolic_ref_with_the_branch_name_is_treated_as_an_existing_branch(self, sample_repo, tmp_path, target):
        """同じ名前の参照が、シンボリックな参照として存在する（先が無くても）とき、起動の前に断る。"""
        sample_repo.git("symbolic-ref", f"refs/heads/{BRANCH}", target)

        exists = make_git(sample_repo.path).branch_exists(BRANCH)
        outcome = run_fixer(sample_repo, tmp_path)

        assert exists is True
        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "branch_exists")
        assert outcome.sdk.invocation_count == 0
        assert "somewhere-else" not in sample_repo.branches()
        assert sample_repo.git("symbolic-ref", f"refs/heads/{BRANCH}").strip() == target

    @pytest.mark.parametrize(
        "branch",
        [
            "HEAD",
            "main",
            "feature/x",
            "fix/other",
            "fix/diagnosed-",
            "fix/diagnosed-x",
            "fix/diagnosed-exec-2026-abc",
            f"fix/diagnosed-{EXECUTION_ID}/x",
            f"fix/diagnosed-{EXECUTION_ID}.lock",
            f"x/fix/diagnosed-{EXECUTION_ID}",
            f"fix/diagnosed-{EXECUTION_ID.upper()}",
        ],
    )
    def test_commit_only_accepts_branch_names_of_the_fixed_form(self, sample_repo, process_recorder, branch):
        """``HEAD`` などの名前で、``refs/heads/HEAD`` を作れない。"""
        before = len(process_recorder.implementation_calls)

        with pytest.raises(git_error()):
            commit(sample_repo, branch=branch)

        assert len(process_recorder.implementation_calls) == before
        assert sample_repo.branches() == ["main"]

    def test_commit_accepts_a_branch_name_of_the_fixed_form(self, sample_repo):
        other = "fix/diagnosed-exec-20250101000000-0123abcd"

        sha = commit(sample_repo, branch=other)

        assert sample_repo.rev(other) == sha

    def test_keyboard_interrupt_from_the_sdk_cleans_up_and_is_raised_again(self, sample_repo, tmp_path):
        """割り込みでも、セッションを閉じ、一時ディレクトリを消してから、例外を上げ直す。"""
        kept: dict[str, object] = {}

        async def script(agent):
            kept["agent"] = agent
            await good_fix(agent)

        sdk = FakeSdk(script=script, raises=KeyboardInterrupt())

        created, error = run_expecting(KeyboardInterrupt, sample_repo, tmp_path, sdk=sdk)

        try:
            leftovers = [path for path in created if path.exists()]
            call = kept["agent"].call("write_file", {"path": "src/agent/late.py", "content": "X = 1\n"})  # type: ignore[attr-defined]
            late = asyncio.run(call)
            assert isinstance(error, KeyboardInterrupt)
            assert created
            assert leftovers == []
            assert sample_repo.branches() == ["main"]
            assert late.outcome == "internal_error"
            assert not sdk.root.exists()
        finally:
            discard(created)

    @pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
    def test_interrupt_during_the_gates_cleans_up_and_is_raised_again(self, sample_repo, tmp_path, error_type):
        def interrupt(run):
            raise error_type()

        created, error = run_expecting(
            error_type,
            sample_repo,
            tmp_path,
            sdk=FakeSdk(script=good_fix_without_test_runs),
            test_runner=FakeTestRunner(override=interrupt),
        )

        try:
            assert isinstance(error, error_type)
            assert created
            assert [path for path in created if path.exists()] == []
            assert sample_repo.branches() == ["main"]
        finally:
            discard(created)

    def test_interrupt_stops_a_test_process_that_is_running(self, sample_repo, tmp_path):
        """割り込みで、走っているテストのプロセスを止める（実物の pytest）。"""
        from src.fixer.runners import PytestRunner

        pid_file = tmp_path / "pid.txt"
        slow = (
            "import os\nimport time\n\nfrom orchestrator import average\n\n\n"
            "def test_average_of_empty_list_is_zero():\n"
            f"    with open({str(pid_file)!r}, 'w') as file:  # noqa: E501\n        file.write(str(os.getpid()))\n"
            "    time.sleep(60)\n    assert average([]) == 0.0\n"
        )
        real = PytestRunner(timeout_seconds=120.0)
        active = threading.Event()
        active.set()
        timers: list[threading.Timer] = []

        def interrupt_if_still_running() -> None:
            # run_fix の外（pytest 本体）に割り込まないよう、まだ走っているときだけ割り込む
            if active.is_set():
                _raise_in_main_thread()

        class Interrupting:
            def run(self, tree, paths, **kwargs):
                if list(paths) != ["tests"] and not timers:
                    timers.append(threading.Timer(1.5, interrupt_if_still_running))
                    timers[0].start()
                return real.run(tree, paths, **kwargs)

        try:
            created, error = run_expecting(
                KeyboardInterrupt,
                sample_repo,
                tmp_path,
                sdk=FakeSdk(script=write_script(FIXED_SOURCE, slow)),
                test_runner=Interrupting(),
            )
            assert isinstance(error, KeyboardInterrupt)
            assert wait_until_dead(int(pid_file.read_text()))
            assert [path for path in created if path.exists()] == []
        finally:
            active.clear()
            discard(locals().get("created", []))
            for timer in timers:
                timer.cancel()
            if pid_file.is_file():
                with contextlib.suppress(OSError, ValueError):
                    os.kill(int(pid_file.read_text()), 9)


def discard(paths) -> None:
    """テストが落ちたときに、一時ディレクトリを残さない。"""
    from src.fixer.workspace import remove_tree

    for path in paths:
        remove_tree(path)


def _raise_in_main_thread() -> None:
    import _thread

    _thread.interrupt_main()


def run_expecting(error_type, repo, tmp_path, *, sdk, test_runner=None, lint_runner=None):
    """``run_fix`` が ``error_type`` を上げることを確かめ、（その間に作られた一時ディレクトリ, 例外）を返す。"""
    from src.fixer import run_fix

    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    handoff = repo_factory.write_handoff(tmp_path / "handoff.json", handoff_dict())
    with (
        patch("src.fixer.runner._invoke_sdk", sdk),
        patch.object(tempfile, "mkdtemp", recording),
        pytest.raises(error_type) as caught,
    ):
        run_fix(
            handoff,
            repo=repo.path,
            out_dir=tmp_path / "fix-out",
            test_runner=test_runner or FakeTestRunner(),
            lint_runner=lint_runner or FakeLintRunner(),
        )
    return created, caught.value


# ===========================================================================
# F11: 隔離されていない環境での起動を、既定で断る
# ===========================================================================

CREDENTIAL_NAMES = [
    ".aws",
    ".ssh",
    ".netrc",
    ".git-credentials",
    ".config/gh",
    ".docker/config.json",
    ".codex",
    ".kube",
    ".gnupg",
]
FILE_NAMES = (".netrc", ".git-credentials", ".docker/config.json")


@pytest.fixture
def real_home(tmp_path, monkeypatch):
    """「本物のホーム」として差し替える、一時ディレクトリ。実際の開発者のホームは、見ない。"""
    home = tmp_path / "pretend-real-home"
    home.mkdir()
    monkeypatch.setattr(f"src.fixer.runner.{REAL_HOME_SEAM}", lambda: home, raising=False)
    return home


def place_credential(home: Path, name: str) -> None:
    target = home / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if name in FILE_NAMES:
        target.write_text(f"secret {MARK}\n", encoding="utf-8")
    else:
        target.mkdir()
        (target / "credentials").write_text(f"secret {MARK}\n", encoding="utf-8")


class TestF11UnisolatedEnvironment:
    @pytest.mark.parametrize("name", CREDENTIAL_NAMES)
    def test_run_fix_refuses_to_start_when_the_real_home_holds_credentials(
        self, sample_repo, tmp_path, real_home, name, caplog
    ):
        """修正役が書いたテストは、起動した人の権限で走る。認証情報のある環境では、既定で起動しない。"""
        place_credential(real_home, name)

        with caplog.at_level(logging.INFO):
            outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unisolated_environment")
        assert outcome.sdk.invocation_count == 0
        assert outcome.temp_dirs == []
        assert not outcome.out_dir.exists()
        assert outcome.new_branches == []
        # 見つけたものの名前だけを出す（中身は読まない）
        assert name in caplog.text
        assert MARK not in caplog.text
        assert MARK not in repr(outcome.run)

    def test_run_fix_starts_in_an_empty_home(self, sample_repo, tmp_path, real_home):
        outcome = run_fixer(sample_repo, tmp_path)

        assert outcome.run.status == "proposed"

    def test_claude_directory_alone_does_not_block_the_start(self, sample_repo, tmp_path, real_home):
        """``.claude`` は、サブスクリプションの認証で同じ環境に必ず要るので、一覧に入れない。"""
        (real_home / ".claude").mkdir()
        (real_home / ".config").mkdir()
        (real_home / ".docker").mkdir()
        (real_home / "Documents").mkdir()

        outcome = run_fixer(sample_repo, tmp_path)

        assert outcome.run.status == "proposed"

    def test_allow_unisolated_skips_the_check(self, sample_repo, tmp_path, real_home):
        for name in CREDENTIAL_NAMES:
            place_credential(real_home, name)

        outcome = run_fixer(sample_repo, tmp_path, allow_unisolated=True)

        assert outcome.run.status == "proposed"

    def test_explicit_false_behaves_like_the_default(self, sample_repo, tmp_path, real_home):
        place_credential(real_home, ".aws")

        outcome = run_fixer(sample_repo, tmp_path, allow_unisolated=False)

        assert outcome.run.stop_detail == "unisolated_environment"

    def test_home_from_the_environment_variable_is_not_what_is_checked(
        self, sample_repo, tmp_path, real_home, monkeypatch
    ):
        """環境変数の ``HOME`` を空のディレクトリに向けても、本物のホームを確かめる。"""
        place_credential(real_home, ".ssh")
        decoy = tmp_path / "decoy-home"
        decoy.mkdir()
        monkeypatch.setenv("HOME", str(decoy))

        outcome = run_fixer(sample_repo, tmp_path)

        assert outcome.run.stop_detail == "unisolated_environment"


# ===========================================================================
# F12: プロセスを、親とは別のグループで起こす（書き方は、どちらでもよい）
# ===========================================================================


class TestF12ProcessGroup:
    def test_test_and_lint_processes_are_started_in_their_own_group(self, tree, process_recorder):
        """専用のセッション、または専用のプロセスグループ（``process_group=0``）で起こす。"""
        from src.fixer.runners import PytestRunner, RuffRunner

        PytestRunner().run(tree, [EXISTING_TEST])
        RuffRunner().count(tree)

        calls = process_recorder.implementation_calls
        assert len(calls) == 2
        assert all(call.own_group for call in calls)
