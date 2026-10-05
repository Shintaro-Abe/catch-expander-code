"""修正後の再レビュー（Codex）で再現された経路の、修正の仕様のテスト（H1〜H9）。最後の修正。

実装の前に書いたテスト。再現に使われた筋書きを、そのままテストにしている。git・pytest が関わるもの
（H2・H4・H6）は、実物で確かめる（偽物では、この種の問題が隠れる）。

前提: 修正役が書いたテストは、起動した人の権限で、任意のコードとして走る。ここで塞ぐのは、その「テストのコード」が、
修正役自身の処理（検査、コミット、後始末）を騙す・踏み台にする経路である。

テストが決めた呼び方（developer は、これに合わせる）:

- H1: 差し替え口 ``src.fixer.runner._writable_python_locations() -> list[str]``（``sys.prefix`` と、実行中の
  Python の site-packages のうち、``os.access(パス, os.W_OK)`` が真のものの一覧）。``run_fix`` は、モジュールの
  属性として毎回引く。空でなければ ``failed``・``unisolated_environment``。``allow_unisolated=True`` なら飛ばす。
  土台（``conftest.py``）が、既定を「空の一覧」に差し替える。実物は、fixture ``unpatched_python_locations`` で得る。
- H2・H3: git のすべての呼び出しの環境変数に ``GIT_NO_REPLACE_OBJECTS=1`` と ``GIT_NO_LAZY_FETCH=1``。
  コミットを作る処理が、ツリーの食い違いに気づいたときは ``GitError``（``run_fix`` は ``failed``・``commit_error``）。
- H4: モデルの作業が終わった後、最初の git の操作の前に、設定とフックを照合する。変わっていたら ``failed``・
  ``repository_tampered``（照合そのものに要る ``rev-parse`` のほかは、git を呼ばない）。
- H5: import の乗っ取りになる名前に、基準のコミットの ``requirements*.txt`` の配布物の名前を足す。基準の一覧
  （``Workspace.baseline``。``check_static`` に渡すもの）の ``content`` から読む。
- H6: 書き込めるパスの各要素に、ピリオドを含めない（末尾の ``.py`` だけ）。書き込みのツールは ``denied``、G-1 は
  不合格。同じ ID の結果が複数ある XML は、「検査できなかった」（``outcomes`` が空、``exit_code`` が ``None``）。
- H7: 基準のコミットの ``.gitattributes`` が 1,000,000 バイトを超えたら、``Git.has_archive_attributes`` は真
  （``run_fix`` は ``failed``・``unsupported_attributes``）。
- H8: 複製の根が、作ったときのディレクトリでなくなっていたら、読み書きのツール 6 つは ``denied``（結果は空）。
- H9: ``LintRunner.count(tree, *, timeout_seconds=None)``（``TestRunner.run`` と同じ流儀。渡されたら、作るときに
  決めた制限時間より優先する）。検査は、残り時間が 0 以下なら、テストもリントも起こさない。
"""

import contextlib
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

import pytest

from . import fakes, repo_factory
from .conftest import OUTSIDE_MARK, WRITABLE_PYTHON_SEAM
from .fakes import (
    FINISH_FIXED,
    FakeLintRunner,
    FakeTestRunner,
    assert_discarded,
    assert_sdk_not_called,
    good_fix_without_test_runs,
    run_fixer,
    with_tamper,
    write_text,
)
from .repo_factory import (
    BRANCH,
    EXISTING_TEST,
    FILES,
    FIX_NEW,
    FIX_OLD,
    LEGACY,
    MAIN,
    NEW_TEST,
    NEW_TEST_SOURCE,
    SRC,
)
from .test_hardening import CASE, commit, failed, git_error, limits, make_git, make_session, rewriting_test, static
from .test_review_fixes import FIXED_SOURCE, kind_of, stop_gates, write_script

MARK = "FINAL_FIX_MARK_7719"
GIT_WRITING_SUBCOMMANDS = {"archive", "hash-object", "read-tree", "update-index", "write-tree", "commit-tree"} | {
    "update-ref"
}


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    repo_factory.write_files(root, FILES)
    return root


# ===========================================================================
# H1: 書き込める仮想環境を共有すると、以後の検査をすべて偽れる
# ===========================================================================

PRETEND_SITE_PACKAGES = "/opt/pretend-venv/lib/python3.13/site-packages"
PRETEND_PREFIX = "/opt/pretend-venv"


def pretend_writable(monkeypatch, *paths: str) -> list[int]:
    """差し替え口を、「これらが書き込める」と答えるものにする。返すのは、呼ばれた回数を数える箱。"""
    calls = [0]

    def locations() -> list[str]:
        calls[0] += 1
        return list(paths)

    monkeypatch.setattr(f"src.fixer.runner.{WRITABLE_PYTHON_SEAM}", locations, raising=False)
    return calls


def only_writable(monkeypatch, *paths: str) -> None:
    """``os.access(パス, os.W_OK)`` が、``paths`` だけで真になるようにする（実際の環境には、何も書かない）。"""
    real_access = os.access
    targets = {os.path.realpath(path) for path in paths}

    def access(path, mode, **kwargs):
        if mode & os.W_OK and isinstance(path, str | bytes | os.PathLike):
            return os.path.realpath(os.fsdecode(path)) in targets
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(os, "access", access)


class TestH1WritablePythonEnvironment:
    @pytest.mark.parametrize(
        "found",
        [(PRETEND_SITE_PACKAGES,), (PRETEND_PREFIX,), (PRETEND_PREFIX, PRETEND_SITE_PACKAGES)],
        ids=["site-packages", "prefix", "both"],
    )
    def test_run_fix_refuses_to_start_when_the_python_environment_is_writable(
        self, sample_repo, tmp_path, monkeypatch, caplog, found
    ):
        """再現（窓口）: テストのコードが site-packages に ``.pth`` を置くと、以後のリントと実行 A〜D を偽れる。

        検査に使う Python の環境が、いまの利用者から書き込めるなら、既定では起動しない。
        """
        pretend_writable(monkeypatch, *found)

        with caplog.at_level(logging.INFO):
            outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unisolated_environment")
        assert outcome.sdk.invocation_count == 0
        assert outcome.temp_dirs == []
        assert not outcome.out_dir.exists()
        assert outcome.new_branches == []
        # 見つけたものは、名前（パス）だけをログに出す
        assert all(path in caplog.text for path in found)

    def test_default_of_the_test_base_is_that_nothing_is_writable(self, sample_repo, tmp_path):
        """土台が、既定を「書き込めるものは無い」に差し替えている（開発機の仮想環境は、書き込める）。"""
        import src.fixer.runner as runner_module

        assert getattr(runner_module, WRITABLE_PYTHON_SEAM)() == []

        outcome = run_fixer(sample_repo, tmp_path)

        assert outcome.run.status == "proposed"

    def test_check_is_looked_up_on_the_module_for_every_run(self, sample_repo, tmp_path, monkeypatch):
        """``run_fix`` は、差し替え口を、モジュールの属性として毎回引く（起動のたびに、確かめ直す）。"""
        calls = pretend_writable(monkeypatch)

        first = run_fixer(sample_repo, tmp_path, out_dir=tmp_path / "first-out")
        after_first = calls[0]
        pretend_writable(monkeypatch, PRETEND_PREFIX)
        second = run_fixer(
            sample_repo, tmp_path, out_dir=tmp_path / "second-out", handoff=_handoff_with_another_execution()
        )

        assert after_first >= 1
        assert first.run.status == "proposed"
        assert second.run.stop_detail == "unisolated_environment"

    def test_allow_unisolated_skips_the_check(self, sample_repo, tmp_path, monkeypatch):
        pretend_writable(monkeypatch, PRETEND_PREFIX, PRETEND_SITE_PACKAGES)

        outcome = run_fixer(sample_repo, tmp_path, allow_unisolated=True)

        assert outcome.run.status == "proposed"

    def test_explicit_false_behaves_like_the_default(self, sample_repo, tmp_path, monkeypatch):
        pretend_writable(monkeypatch, PRETEND_SITE_PACKAGES)

        outcome = run_fixer(sample_repo, tmp_path, allow_unisolated=False)

        assert outcome.run.stop_detail == "unisolated_environment"

    def test_writable_environment_is_refused_before_anything_else_is_looked_at(
        self, sample_repo, tmp_path, monkeypatch
    ):
        """既にある「隔離されていない環境」の確かめと同じ場所: リポジトリや受け渡しを見る前に断る。"""
        pretend_writable(monkeypatch, PRETEND_PREFIX)
        not_a_repository = tmp_path / "not-a-repository"
        not_a_repository.mkdir()

        outcome = run_fixer(sample_repo, tmp_path, repo_path=not_a_repository, handoff={"broken": True})

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unisolated_environment")
        assert not outcome.out_dir.exists()


def _handoff_with_another_execution() -> dict:
    return repo_factory.handoff_dict(execution_id="exec-20261004120001-abcdef34")


class TestH1RealCheck:
    """差し替えを外した、実物の確かめ。実際の仮想環境には何も書かず、``os.access`` の答えだけを差し替える。"""

    def test_real_check_reports_nothing_when_nothing_is_writable(self, unpatched_python_locations, monkeypatch):
        only_writable(monkeypatch)

        assert unpatched_python_locations() == []

    def test_real_check_reports_a_writable_prefix(self, unpatched_python_locations, monkeypatch):
        only_writable(monkeypatch, sys.prefix)

        found = unpatched_python_locations()

        assert isinstance(found, list)
        assert all(isinstance(path, str) for path in found)
        assert os.path.realpath(sys.prefix) in {os.path.realpath(path) for path in found}

    def test_real_check_reports_a_writable_site_packages_directory(self, unpatched_python_locations, monkeypatch):
        """site-packages だけが書き込める場合も、見つける（``.pth`` を置けるのは、ここ）。"""
        site_packages = sysconfig.get_paths()["purelib"]
        only_writable(monkeypatch, site_packages)

        found = unpatched_python_locations()

        assert {os.path.realpath(path) for path in found} == {os.path.realpath(site_packages)}

    def test_real_check_agrees_with_the_actual_permissions_of_this_environment(self, unpatched_python_locations):
        """実際の環境に対しても、答えが ``os.access`` と一致する（読むだけ。何も書かない）。"""
        found = {os.path.realpath(path) for path in unpatched_python_locations()}

        assert (os.path.realpath(sys.prefix) in found) == os.access(sys.prefix, os.W_OK)

    def test_run_fix_with_the_real_check_refuses_a_writable_prefix(
        self, sample_repo, tmp_path, monkeypatch, unpatched_python_locations, caplog
    ):
        """歯止めが、差し替えなしで効く: 実物の確かめを戻し、``sys.prefix`` が書き込める、と答えさせる。"""
        monkeypatch.setattr(f"src.fixer.runner.{WRITABLE_PYTHON_SEAM}", unpatched_python_locations)
        only_writable(monkeypatch, sys.prefix)

        with caplog.at_level(logging.INFO):
            outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unisolated_environment")
        assert outcome.sdk.invocation_count == 0
        assert outcome.temp_dirs == []
        assert not outcome.out_dir.exists()
        assert sys.prefix in caplog.text


# ===========================================================================
# H2: 置換参照を足すと、検査していない禁止ファイルがコミットに入る
# H3: 部分クローンの遅延取得
# ===========================================================================

UNCHECKED_SOURCE = f"def setup_credentials():\n    return {MARK!r}  # unreviewed\n"
REPLACE_PLANTING_TEST = """from pathlib import Path

from orchestrator import average

REFERENCE = Path({reference!r})  # noqa: E501
REPLACEMENT = {replacement!r}


def test_average_of_empty_list_is_zero():
    value = average([])
    REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE.write_text(REPLACEMENT + "\\n", encoding="utf-8")
    assert value == 0.0
"""


def unchecked_commit(repo) -> str:
    """対象外の ``src/agent/main.py`` を書き換えたコミットを、元のリポジトリのオブジェクトとして用意する。

    どの参照からも指されない。``HEAD``・ブランチ・作業ツリー・インデックスは、元に戻す。
    """
    original = (repo.path / MAIN).read_text(encoding="utf-8")
    repo.git("branch", "unchecked", repo.head)
    repo.git("symbolic-ref", "HEAD", "refs/heads/unchecked")
    (repo.path / MAIN).write_text(UNCHECKED_SOURCE, encoding="utf-8")
    repo.git("add", "-A")
    repo.git("commit", "-q", "--no-verify", "-m", "unchecked")
    sha = repo.rev("HEAD")
    repo.git("symbolic-ref", "HEAD", "refs/heads/main")
    (repo.path / MAIN).write_text(original, encoding="utf-8")
    repo.git("add", "-A")
    repo.git("branch", "-D", "unchecked")
    assert repo.rev("HEAD") == repo.head
    assert repo.branches() == ["main"]
    assert repo.file_at(sha, MAIN) == UNCHECKED_SOURCE
    return sha


def replace_reference(repo) -> Path:
    return repo.path / ".git" / "refs" / "replace" / repo.head


def plant_replace_reference(repo, replacement: str) -> None:
    """``refs/replace/<基準>`` を作る（``git replace <基準> <置換先>`` と同じ結果）。"""
    reference = replace_reference(repo)
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text(replacement + "\n", encoding="utf-8")


def remove_replace_reference(repo) -> None:
    with contextlib.suppress(FileNotFoundError):
        replace_reference(repo).unlink()


def differing_paths(before: dict, after: dict) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def assert_no_unchecked_change(repo, outcome, true_base: dict) -> None:
    """検査していない変更が、コミットに入っていない: ブランチが無いか、コミットが「本当の基準＋検査を通った変更」。"""
    run = outcome.run
    if run.commit is None:
        assert run.status == "failed", (run.status, run.stop_detail)
        assert run.stop_detail in ("commit_error", "repository_tampered")
        assert run.branch is None
        assert outcome.new_branches == []
        return
    assert run.status == "proposed"
    files = repo.tree_files(run.commit)
    assert files[MAIN] == true_base[MAIN], "the commit carries an unchecked change to a forbidden file"
    assert differing_paths(true_base, files) == sorted([SRC, NEW_TEST])
    assert MARK not in repo.file_at(run.commit, MAIN)
    assert repo.parents(run.commit) == [repo.head]


class TestH2ReplaceReferences:
    def test_create_branch_commit_does_not_take_the_tree_of_a_replacement(self, sample_repo):
        """再現（窓口）: ``refs/replace/<基準>`` があると、``read-tree <基準>`` が、置換先のツリーを読む。"""
        true_base = sample_repo.tree_files(sample_repo.head)
        plant_replace_reference(sample_repo, unchecked_commit(sample_repo))

        try:
            sha = commit(sample_repo)
        except git_error():
            sha = None
        finally:
            remove_replace_reference(sample_repo)

        if sha is None:
            assert BRANCH not in sample_repo.branches()
        else:
            files = sample_repo.tree_files(sha)
            assert files[MAIN] == true_base[MAIN], "the commit carries the tree of the replacement"
            assert differing_paths(true_base, files) == [SRC]
            assert sample_repo.parents(sha) == [sample_repo.head]

    def test_export_tree_writes_the_real_base_when_a_replacement_exists(self, sample_repo, tmp_path):
        """書き出し（検査用の複製）も、置換先ではなく、本当の基準の中身になる。"""
        plant_replace_reference(sample_repo, unchecked_commit(sample_repo))
        git = make_git(sample_repo.path)

        try:
            git.export_tree(sample_repo.head, tmp_path / "exported")
            entries = git.tree_entries(sample_repo.head)
        finally:
            remove_replace_reference(sample_repo)

        assert (tmp_path / "exported" / MAIN).read_text(encoding="utf-8") == FILES[MAIN]
        assert set(entries) == set(FILES)

    def test_replacement_planted_during_the_last_gate_run_does_not_reach_the_commit(self, sample_repo, tmp_path):
        """検査がすべて終わる間際（実行 D の中）に、置換参照が作られる。設定とフックの照合の対象は、変わらない。"""
        true_base = sample_repo.tree_files(sample_repo.head)
        replacement = unchecked_commit(sample_repo)

        def plant(run):
            if kind_of(run) == "D":
                plant_replace_reference(sample_repo, replacement)

        try:
            outcome = run_fixer(
                sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=FakeTestRunner(override=plant)
            )
            planted = replace_reference(sample_repo).exists()
        finally:
            remove_replace_reference(sample_repo)

        assert planted, "the scenario did not run (the replace reference was never planted)"
        assert_no_unchecked_change(sample_repo, outcome, true_base)

    def test_new_test_that_plants_a_replacement_with_real_tools(self, sample_repo, tmp_path):
        """再現そのもの（実物の git・pytest・ruff）: 新しいテストが、修正の入った複製で走るときに、置換参照を作る。"""
        true_base = sample_repo.tree_files(sample_repo.head)
        replacement = unchecked_commit(sample_repo)
        test_source = REPLACE_PLANTING_TEST.format(
            reference=str(replace_reference(sample_repo)), replacement=replacement
        )

        try:
            outcome = run_fixer(
                sample_repo,
                tmp_path,
                script=write_script(FIXED_SOURCE, test_source),
                test_runner=None,
                lint_runner=None,
            )
            planted = replace_reference(sample_repo).exists()
        finally:
            remove_replace_reference(sample_repo)

        assert planted, f"the scenario did not run: {outcome.run.status}/{outcome.run.stop_detail}"
        assert_no_unchecked_change(sample_repo, outcome, true_base)


class TestH2H3GitEnvironment:
    def test_every_git_call_ignores_replacements_and_never_fetches_missing_objects(self, sample_repo, process_recorder):
        """H2: 置換参照を読ませない。H3: 部分クローンで、足りないオブジェクトを取りに行かせない（通信はしない）。"""
        git = make_git(sample_repo.path)
        base = git.resolve_commit("HEAD")
        git.toplevel()
        git.branch_exists(BRANCH)
        git.repository_fingerprint()
        git.has_archive_attributes(base)
        git.export_tree(base, sample_repo.path.parent / "exported")
        commit(sample_repo)

        calls = process_recorder.implementation_git_calls
        assert {call.git_subcommand for call in calls} == set(GIT_WRITING_SUBCOMMANDS) | {"rev-parse", "show-ref"}
        for call in calls:
            assert call.env["GIT_NO_REPLACE_OBJECTS"] == "1", call.argv
            assert call.env["GIT_NO_LAZY_FETCH"] == "1", call.argv

    def test_every_git_call_of_a_whole_run_carries_both_variables(self, sample_repo, tmp_path, process_recorder):
        outcome = run_fixer(sample_repo, tmp_path)

        calls = process_recorder.implementation_git_calls
        assert outcome.run.status == "proposed"
        assert len(calls) >= 9
        assert [call.argv for call in calls if call.env.get("GIT_NO_REPLACE_OBJECTS") != "1"] == []
        assert [call.argv for call in calls if call.env.get("GIT_NO_LAZY_FETCH") != "1"] == []


# ===========================================================================
# H4: 修正役自身の ``git archive`` が、設定のフィルターのコマンドを実行する
# ===========================================================================

FILTER_ATTRIBUTES = "*.py filter=planted\n"
FILTER_PLANTING_TEST = """from pathlib import Path

from orchestrator import average

GIT_DIRECTORY = Path({git_directory!r})  # noqa: E501
CONFIG = {config!r}  # noqa: E501
ATTRIBUTES = {attributes!r}


def test_average_of_empty_list_is_zero():
    with (GIT_DIRECTORY / "config").open("a", encoding="utf-8") as file:
        file.write(CONFIG)
    (GIT_DIRECTORY / "info").mkdir(exist_ok=True)
    (GIT_DIRECTORY / "info" / "attributes").write_text(ATTRIBUTES, encoding="utf-8")
    assert average([]) == 0.0
"""


def filter_config(mark: Path) -> str:
    """目印のファイルを作るだけの、無害なフィルター（中身は、そのまま通す）。"""
    return f'[filter "planted"]\n\tsmudge = "touch {mark}; cat"\n'


def plant_filter(repo_path: Path, mark: Path) -> None:
    with (repo_path / ".git" / "config").open("a", encoding="utf-8") as file:
        file.write(filter_config(mark))
    (repo_path / ".git" / "info").mkdir(exist_ok=True)
    (repo_path / ".git" / "info" / "attributes").write_text(FILTER_ATTRIBUTES, encoding="utf-8")


def assert_filter_never_ran(outcome, mark: Path) -> None:
    assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "repository_tampered")
    assert not mark.exists(), "the planted filter command was executed by the fixer's own git"
    assert outcome.run.branch is None
    assert outcome.run.commit is None
    assert outcome.new_branches == []
    assert outcome.leftover_temp_dirs == []


class TestH4FilterPlantedWhileTheFixerWorks:
    def test_planted_filter_really_runs_when_git_archive_is_called(self, sample_repo, tmp_path):
        """筋書きの確認: 置かれたフィルターは、``git archive`` で実際に走る（だから、呼ぶ前に照合する）。"""
        mark = tmp_path / "filter-ran"
        plant_filter(sample_repo.path, mark)

        make_git(sample_repo.path).export_tree(sample_repo.head, tmp_path / "exported")

        assert mark.exists()

    def test_filter_planted_while_the_fixer_works_is_noticed_before_any_git_operation(
        self, sample_repo, tmp_path, process_recorder
    ):
        """モデルの作業が終わった後、検査用の複製を作る前に、設定とフックを照合する。"""
        mark = tmp_path / "filter-ran"
        seen: dict[str, int] = {}

        async def script(agent):
            await fakes.good_fix_without_finish(agent)
            plant_filter(sample_repo.path, mark)
            seen["git_calls"] = len(process_recorder.implementation_git_calls)
            await agent.call("finish", dict(FINISH_FIXED))

        outcome = run_fixer(sample_repo, tmp_path, script=script)

        assert_filter_never_ran(outcome, mark)
        # 変わっていたら、git の操作をしない（照合そのものに要る ``rev-parse`` を除く）
        later = process_recorder.subcommands()[seen["git_calls"] :]
        assert set(later) <= {"rev-parse"}, later
        assert outcome.test_runner.calls == []
        assert outcome.lint_runner.trees == []
        assert outcome.run.repository_changed is True
        assert json.loads((outcome.out_dir / "result.json").read_text(encoding="utf-8"))["stop_detail"] == (
            "repository_tampered"
        )

    def test_new_test_that_plants_a_filter_with_real_tools(self, sample_repo, tmp_path):
        """再現そのもの（実物の git・pytest・ruff）: 作業中のテスト実行が、フィルターの設定と属性を書く。"""
        mark = tmp_path / "filter-ran"
        test_source = FILTER_PLANTING_TEST.format(
            git_directory=str(sample_repo.path / ".git"), config=filter_config(mark), attributes=FILTER_ATTRIBUTES
        )

        async def script(agent):
            await agent.call("write_file", {"path": NEW_TEST, "content": test_source})
            ran = await agent.call("run_tests", {"paths": [NEW_TEST]})
            assert ran.outcome == "ok"
            await agent.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})
            await agent.call("finish", dict(FINISH_FIXED))

        outcome = run_fixer(sample_repo, tmp_path, script=script, test_runner=None, lint_runner=None)

        assert "planted" in (sample_repo.path / ".git" / "config").read_text(encoding="utf-8")
        assert_filter_never_ran(outcome, mark)

    def test_untouched_repository_with_its_own_filter_is_not_treated_as_tampered(self, sample_repo, tmp_path):
        """最初からある設定は、改ざんではない（照合は、実行の前との比較）。"""
        (sample_repo.path / ".git" / "config").open("a", encoding="utf-8").write('[filter "own"]\n\tsmudge = cat\n')

        outcome = run_fixer(sample_repo, tmp_path)

        assert outcome.run.status == "proposed"
        assert outcome.run.repository_changed is False


class TestH4FilterPlantedDuringTheGates:
    """仕様の文面（「モデルの作業が終わった後、最初の git の呼び出しの前」）の外。司令塔の判断を仰ぐ。

    実行 B（モデルが書いた新しいテスト）の間に置かれたフィルターは、次の検査用の複製（実行 C・D）を作る
    ``git archive`` で走る。H4 と同じ穴なので、テストにしておく。
    """

    def test_filter_planted_during_a_gate_run_is_noticed_before_the_next_tree_is_built(self, sample_repo, tmp_path):
        mark = tmp_path / "filter-ran"

        def plant(run):
            if kind_of(run) == "B":
                plant_filter(sample_repo.path, mark)

        outcome = run_fixer(
            sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=FakeTestRunner(override=plant)
        )

        assert_filter_never_ran(outcome, mark)


# ===========================================================================
# H5: 実際に使っている依存の名前が、差し替えの一覧に無い
# ===========================================================================

POWERTOOLS_PATHS = [
    "src/trigger/aws_lambda_powertools.py",
    "src/agent/aws_lambda_powertools.py",
    "src/aws_lambda_powertools.py",
    "src/trigger/AWS_Lambda_Powertools.py",
    "src/trigger/aws_lambda_powertools/__init__.py",
    "src/agent/aws_lambda_powertools/logging.py",
]
REQUIREMENT_FILES = {
    "src/agent/requirements.txt": "requests\nSome-Dist==1.0\n",
    "requirements-dev.txt": (
        "# 開発用の依存\n"
        'other.pkg>=2 ; python_version > "3.8"\n'
        "Extra_Thing[socks,security]~=3.1  # 固定\n"
        "-r base.txt\n"
        "--no-binary :all:\n"
        "-e ./vendored\n"
        "\n"
        "UPPER-Case\n"
    ),
    "src/trigger/requirements.txt": "deep-dist<3\n",
}
# 基準のコミットの requirements*.txt（どの階層でも）の配布物の名前から作った、import の名前に当たるもの
REQUIREMENT_SHADOWING_PATHS = [
    "src/agent/some_dist.py",
    "src/trigger/some_dist.py",
    "src/some_dist.py",
    "src/agent/Some_Dist.py",
    "src/agent/some_dist/__init__.py",
    "src/agent/some_dist/helper.py",
    "src/agent/other_pkg.py",
    "src/agent/extra_thing.py",
    "src/observability/upper_case.py",
    "src/agent/deep_dist.py",
]
# 一覧に当たらない名前（コメント、オプションの行、extras、版の指定からは、名前を作らない）
REQUIREMENT_FREE_PATHS = [
    "src/agent/some_dist_helper.py",
    "src/agent/storage/some_dist.py",  # 直下ではない
    "src/agent/vendored.py",
    "src/agent/base.py",
    "src/agent/socks.py",
    "src/agent/unlisted_name.py",
]


def init_content(path: str) -> str:
    return "" if path.endswith("__init__.py") else "VALUE = 1\n"


@pytest.fixture
def dependency_workspace(sample_repo, tmp_path):
    """``requirements*.txt`` に、手書きの一覧に無い配布物を足して、コミットしたリポジトリの複製。"""
    from src.fixer.workspace import Workspace

    repo_factory.write_files(sample_repo.path, REQUIREMENT_FILES)
    sample_repo.commit_all("dependencies")
    parent = tmp_path / "dependency-workspace-parent"
    parent.mkdir()
    created = Workspace.create(make_git(sample_repo.path), sample_repo.head, parent)
    yield created
    created.destroy()


class TestH5DependencyNames:
    def test_write_file_refuses_the_name_of_a_dependency_that_other_code_imports(self, workspace):
        """再現（窓口）: ``src/trigger/aws_lambda_powertools.py`` を足すと、``app.py`` を変えずに介入できる。"""

        def check(path):
            session = make_session(workspace)
            result = session.call("write_file", {"path": path, "content": init_content(path)})
            assert result.outcome == "denied"
            assert workspace.changes().is_empty

        fakes.check_each(POWERTOOLS_PATHS, check)

    @pytest.mark.parametrize("path", POWERTOOLS_PATHS)
    def test_file_with_that_name_placed_in_the_workspace_fails_g2(self, workspace, path):
        results = static(workspace, write_text(path, init_content(path)))

        assert "G2" in failed(results)

    def test_run_fix_with_a_module_named_like_that_dependency_is_rejected(self, sample_repo, tmp_path):
        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=with_tamper(write_text("src/trigger/aws_lambda_powertools.py", "VALUE = 1\n")),
        )

        assert outcome.run.status == "rejected"
        assert "G2" in stop_gates(outcome)
        assert_discarded(outcome)

    def test_write_file_refuses_names_made_from_the_requirements_of_the_base_commit(self, dependency_workspace):
        """配布物の名前を小文字にし、``-`` と ``.`` を ``_`` にしたものが、import の名前。"""

        def check(path):
            session = make_session(dependency_workspace)
            result = session.call("write_file", {"path": path, "content": init_content(path)})
            assert result.outcome == "denied"
            assert dependency_workspace.changes().is_empty

        fakes.check_each(REQUIREMENT_SHADOWING_PATHS, check)

    @pytest.mark.parametrize("path", REQUIREMENT_SHADOWING_PATHS)
    def test_file_named_like_a_requirement_placed_in_the_workspace_fails_g2(self, dependency_workspace, path):
        results = static(dependency_workspace, write_text(path, init_content(path)))

        assert "G2" in failed(results)

    def test_names_that_are_in_no_list_can_still_be_written(self, dependency_workspace):
        def check(path):
            result = make_session(dependency_workspace).call("write_file", {"path": path, "content": "VALUE = 1\n"})
            assert result.outcome == "ok"

        fakes.check_each(REQUIREMENT_FREE_PATHS, check)

    @pytest.mark.parametrize("path", REQUIREMENT_FREE_PATHS)
    def test_file_with_a_name_in_no_list_passes_g2(self, dependency_workspace, path):
        results = static(dependency_workspace, write_text(path, "VALUE = 1\n"))

        assert "G2" not in failed(results)

    def test_name_that_is_not_in_the_requirements_of_this_repository_can_be_written(self, workspace):
        """見本のリポジトリの ``requirements.txt`` は ``requests`` だけ。``some_dist`` は、どの一覧にも当たらない。"""
        result = make_session(workspace).call("write_file", {"path": "src/agent/some_dist.py", "content": "V = 1\n"})

        assert result.outcome == "ok"
        assert "G2" not in failed(static(workspace, write_text("src/agent/deep_dist.py", "V = 1\n")))

    def test_run_fix_with_a_module_named_like_a_requirement_is_rejected(self, sample_repo, tmp_path):
        repo_factory.write_files(sample_repo.path, REQUIREMENT_FILES)
        sample_repo.commit_all("dependencies")

        outcome = run_fixer(
            sample_repo, tmp_path, script=with_tamper(write_text("src/agent/some_dist.py", "VALUE = 1\n"))
        )

        assert outcome.run.status == "rejected"
        assert "G2" in stop_gates(outcome)


# ===========================================================================
# H6: JUnit の ID が、別のファイルで衝突する
# ===========================================================================

DOTTED_PATHS = [
    "tests/unit/foo.bar/test_x.py",
    "src/agent/a.b/module.py",
    "src/v1.2/module.py",
    "src/agent/module.v2.py",
    "tests/unit/test_x.y.py",
    "src/agent/a-b.c_d.py",
    "src/agent/module.py.py",
    "tests/unit/pkg.sub/__init__.py",
]
UNDOTTED_PATHS = ["src/agent/a-b_c.py", "src/agent/sub_dir/module.py", "tests/unit/sub_dir/test_x.py"]


class TestH6DottedPaths:
    def test_write_file_refuses_paths_with_a_period_inside_an_element(self, workspace):
        """pytest は、パスの ``/`` を ``.`` に変えて ID を作る。ピリオドを含む要素は、別のファイルと同じ ID になる。"""

        def check(path):
            session = make_session(workspace)
            result = session.call("write_file", {"path": path, "content": init_content(path)})
            assert result.outcome == "denied"
            assert session.trace[0].path is None
            assert workspace.changes().is_empty

        fakes.check_each(DOTTED_PATHS, check)

    def test_write_file_still_accepts_paths_whose_only_period_is_the_suffix(self, workspace):
        def check(path):
            assert make_session(workspace).call("write_file", {"path": path, "content": "VALUE = 1\n"}).outcome == "ok"

        fakes.check_each(UNDOTTED_PATHS, check)

    @pytest.mark.parametrize("path", DOTTED_PATHS)
    def test_file_at_a_dotted_path_placed_in_the_workspace_fails_g1(self, workspace, path):
        results = static(workspace, write_text(path, init_content(path)))

        assert "G1" in failed(results)

    @pytest.mark.parametrize("path", UNDOTTED_PATHS)
    def test_file_at_an_undotted_path_passes_g1(self, workspace, path):
        results = static(workspace, write_text(path, "VALUE = 1\n"))

        assert "G1" not in failed(results)

    def test_run_fix_with_a_test_file_in_a_dotted_directory_is_rejected(self, sample_repo, tmp_path):
        outcome = run_fixer(
            sample_repo, tmp_path, script=with_tamper(write_text("tests/unit/foo.bar/test_extra.py", NEW_TEST_SOURCE))
        )

        assert outcome.run.status == "rejected"
        assert "G1" in stop_gates(outcome)
        assert_discarded(outcome)


class TestH6CollidingIds:
    def test_two_test_files_whose_ids_collide_make_the_run_unverifiable(self, tree):
        """再現そのもの（実物の pytest）: ``foo.bar/test_x.py`` と ``foo/bar/test_x.py`` の同名のテスト。

        1 件が失敗、1 件が成功。合流させて 1 件の結果にしない（落ちるほうを消すと、同じ ID が「成功」になる）。
        """
        from src.fixer.runners import PytestRunner

        dotted = tree / "tests" / "unit" / "foo.bar"
        nested = tree / "tests" / "unit" / "foo" / "bar"
        dotted.mkdir(parents=True)
        nested.mkdir(parents=True)
        (dotted / "test_x.py").write_text("def test_a():\n    assert False\n", encoding="utf-8")
        (nested / "test_x.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
        (nested / "__init__.py").write_text("", encoding="utf-8")
        (nested.parent / "__init__.py").write_text("", encoding="utf-8")

        report = PytestRunner().run(tree, ["tests/unit/foo.bar", "tests/unit/foo"])

        assert "1 failed, 1 passed" in report.output_tail, report.output_tail
        assert report.outcomes == {}
        assert report.exit_code is None
        assert report.timed_out is False

    def test_run_without_colliding_ids_is_read_as_before(self, tree):
        from src.fixer.runners import PytestRunner

        report = PytestRunner().run(tree, [EXISTING_TEST])

        assert len(report.outcomes) == 3
        assert report.exit_code == 1

    def test_xml_with_a_repeated_id_among_many_makes_the_run_unverifiable(self, tree):
        """XML の中のテストの件数と、ID でまとめた後の件数が違えば、「検査できなかった」。"""
        from src.fixer.runners import PytestRunner

        cases = "".join(CASE.format(name=f"test_{number}", child="") for number in range(50))
        repeated = CASE.format(name="test_7", child="<failure/>")
        source = rewriting_test(f"file.write(b'<testsuites>{cases}{repeated}</testsuites>')")
        (tree / "tests" / "unit" / "test_rewrite.py").write_text(source, encoding="utf-8")

        report = PytestRunner().run(tree, ["tests/unit/test_rewrite.py"])

        assert report.outcomes == {}
        assert report.exit_code is None

    def test_unverifiable_run_fails_the_gate_it_belongs_to(self):
        """「検査できなかった」実行は、G-5・G-6・G-7 のどれも通さない（既にある表し方と同じ）。"""
        from .test_hardening import dynamic

        unusable = fakes.make_report({}, exit_code=None)

        assert "G5" in failed(dynamic(pre_fix_new=unusable))
        assert "G6" in failed(dynamic(post_fix_new=unusable))
        assert "G7" in failed(dynamic(base_existing=unusable, post_fix_existing=unusable))


# ===========================================================================
# H7: 属性ファイルの 1MB より後ろの ``export-subst`` を見落とす
# ===========================================================================

ATTRIBUTES_LIMIT = 1_000_000
SUBST_SOURCE = "# $Format:%H$\nimport os\n\n\ndef legacy():\n    return 1\n"


def comment_padding(size: int) -> bytes:
    """ちょうど ``size`` バイトの、コメントだけの属性ファイル。"""
    line = b"# pad\n"
    return (line * (size // len(line) + 1))[: size - 1] + b"\n"


def commit_attributes(repo, content: bytes, rel: str = ".gitattributes") -> None:
    target = repo.path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    repo.commit_all("attributes")


class TestH7LargeAttributesFile:
    def test_export_subst_after_a_megabyte_of_comments_is_not_missed(self, sample_repo, tmp_path):
        """再現そのもの: コメントを 108 万バイト並べた後に ``export-subst`` を置く。"""
        (sample_repo.path / LEGACY).write_text(SUBST_SOURCE, encoding="utf-8")
        content = b"# pad\n" * 180_000 + f"{LEGACY} export-subst\n".encode()
        assert len(content) > ATTRIBUTES_LIMIT
        commit_attributes(sample_repo, content)
        git = make_git(sample_repo.path)
        git.export_tree(sample_repo.head, tmp_path / "exported")
        # 筋書きの確認: 書き出した中身は、コミットの中身と違う
        assert (tmp_path / "exported" / LEGACY).read_text(encoding="utf-8").splitlines()[0] == f"# {sample_repo.head}"

        assert git.has_archive_attributes(sample_repo.head) is True

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unsupported_attributes")
        assert_sdk_not_called(outcome)
        assert not outcome.out_dir.exists()

    @pytest.mark.parametrize("rel", [".gitattributes", "src/agent/.gitattributes", "tests/unit/.gitattributes"])
    def test_attributes_file_over_the_limit_is_not_supported_at_any_level(self, sample_repo, tmp_path, rel):
        """読む上限を超える属性ファイルは、中身に関わらず、扱わない（どの階層でも）。"""
        commit_attributes(sample_repo, comment_padding(ATTRIBUTES_LIMIT + 1), rel)

        assert make_git(sample_repo.path).has_archive_attributes(sample_repo.head) is True

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unsupported_attributes")
        assert_sdk_not_called(outcome)

    def test_attributes_file_exactly_at_the_limit_is_still_read_in_full(self, sample_repo, tmp_path):
        """境界: ちょうど 1,000,000 バイトで、書き出しを変える属性が無ければ、今までどおり扱う。"""
        content = comment_padding(ATTRIBUTES_LIMIT)
        assert len(content) == ATTRIBUTES_LIMIT
        commit_attributes(sample_repo, content)

        assert make_git(sample_repo.path).has_archive_attributes(sample_repo.head) is False

    def test_export_subst_on_the_last_line_of_a_file_at_the_limit_is_found(self, sample_repo):
        tail = f"{LEGACY} export-subst\n".encode()
        content = comment_padding(ATTRIBUTES_LIMIT - len(tail)) + tail
        assert len(content) == ATTRIBUTES_LIMIT
        (sample_repo.path / LEGACY).write_text(SUBST_SOURCE, encoding="utf-8")
        commit_attributes(sample_repo, content)

        assert make_git(sample_repo.path).has_archive_attributes(sample_repo.head) is True


# ===========================================================================
# H8: 後始末が、リンクの先の権限を変える。複製の根をリンクに替えると、外を一覧できる
# ===========================================================================


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def locked_outside(tmp_path):
    """複製の外の、権限を落としたディレクトリ（500）と、その下（500）。終わったら、消せるように権限を戻す。"""
    directory = tmp_path / "locked-outside"
    (directory / "sub").mkdir(parents=True)
    (directory / "sub" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (directory / "sub").chmod(0o500)
    directory.chmod(0o500)
    yield directory
    directory.chmod(0o700)
    (directory / "sub").chmod(0o700)


def assert_untouched(directory: Path) -> None:
    assert (mode_of(directory), mode_of(directory / "sub")) == (0o500, 0o500)
    directory.chmod(0o700)
    (directory / "sub").chmod(0o700)
    try:
        assert (directory / "sub" / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    finally:
        (directory / "sub").chmod(0o500)
        directory.chmod(0o500)


class TestH8RemoveTreeAndLinks:
    def test_remove_tree_on_a_link_removes_only_the_link(self, tmp_path, locked_outside):
        """再現そのもの: 消す対象がリンクに替えられていると、リンクの先の権限が 500 から 700 に変わった。"""
        from src.fixer.workspace import remove_tree

        link = tmp_path / "gate-parent"
        os.symlink(locked_outside, link)

        remove_tree(link)

        assert not os.path.lexists(link)
        assert_untouched(locked_outside)

    def test_remove_tree_on_a_link_to_a_file_removes_only_the_link(self, tmp_path):
        from src.fixer.workspace import remove_tree

        target = tmp_path / "outside-file.txt"
        target.write_text("keep\n", encoding="utf-8")
        target.chmod(0o400)
        link = tmp_path / "gate-parent"
        os.symlink(target, link)

        remove_tree(link)

        assert not os.path.lexists(link)
        assert target.read_text(encoding="utf-8") == "keep\n"
        assert mode_of(target) == 0o400

    def test_remove_tree_on_a_link_to_nothing_removes_the_link(self, tmp_path):
        from src.fixer.workspace import remove_tree

        link = tmp_path / "gate-parent"
        os.symlink(tmp_path / "never-existed", link)

        remove_tree(link)

        assert not os.path.lexists(link)

    def test_remove_tree_does_not_follow_a_link_inside_a_locked_directory(self, tmp_path, locked_outside):
        """途中で出会うリンクも、たどらない（権限を戻してやり直す処理の中でも）。"""
        from src.fixer.workspace import remove_tree

        root = tmp_path / "victim"
        locked = root / "locked"
        locked.mkdir(parents=True)
        os.symlink(locked_outside, locked / "link")
        os.symlink(locked_outside / "sub", root / "second-link")
        locked.chmod(0o000)

        remove_tree(root)

        assert not os.path.lexists(root)
        assert_untouched(locked_outside)

    def test_gate_tree_parent_swapped_for_a_link_does_not_get_its_target_changed(
        self, sample_repo, tmp_path, locked_outside
    ):
        """検査の実行の間に、検査用の複製の親が、外を指すリンクに替えられる（``runner`` の後始末の経路）。"""
        moved: list[Path] = []
        links: list[Path] = []

        def swap(run):
            if kind_of(run) != "B":
                return
            parent = run.tree.parent
            aside = parent.with_name(parent.name + "-moved")
            os.rename(parent, aside)
            os.symlink(locked_outside, parent)
            moved.append(aside)
            links.append(parent)

        try:
            outcome = run_fixer(
                sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=FakeTestRunner(override=swap)
            )
            remaining_links = [str(link) for link in links if os.path.lexists(link)]

            assert len(links) == 1, "the scenario did not run"
            assert outcome.run.status == "rejected"
            assert "G5" in stop_gates(outcome)
            assert_untouched(locked_outside)
            assert remaining_links == []
            assert outcome.new_branches == []
        finally:
            # 筋書きが残したもの（リンクと、脇へ動かした本物の複製）を、必ず消す
            for link in links:
                if os.path.islink(link):
                    os.unlink(link)
            for aside in moved:
                shutil.rmtree(aside, ignore_errors=True)


SWAPPED_ROOT_CALLS = {
    "list-root": ("list_files", {"path": "."}),
    "list-root-empty": ("list_files", {"path": ""}),
    "list-src": ("list_files", {"path": "src"}),
    "read-top": ("read_file", {"path": "secret.py"}),
    "read-src": ("read_file", {"path": SRC}),
    "search-all": ("search_code", {"pattern": OUTSIDE_MARK}),
    "search-src": ("search_code", {"pattern": OUTSIDE_MARK, "path": "src"}),
    "write": ("write_file", {"path": "src/agent/planted.py", "content": "PLANTED = 1\n"}),
    "replace": ("replace_in_file", {"path": SRC, "old": "OUTSIDE = 1", "new": "CHANGED = 1"}),
    "run-tests": ("run_tests", {"paths": ["tests"]}),
}


def summary_of(directory: Path) -> dict[str, str]:
    """ディレクトリの下のすべてのファイルの、相対パス → 中身。"""
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def foreign_directory(outside):
    """複製ではない、別のディレクトリ。ツールのパスの形に合う場所に、目印つきのファイルがある。"""
    repo_factory.write_files(
        outside,
        {
            SRC: f"# {OUTSIDE_MARK}\nOUTSIDE = 1\n",
            "tests/unit/test_outside.py": f"# {OUTSIDE_MARK}\n\n\ndef test_outside():\n    assert True\n",
        },
    )
    return outside


def swap_root(workspace, foreign: Path, tmp_path: Path, kind: str) -> Path:
    """複製の根を、作ったときのディレクトリでないものに替える。返すのは、いま根の場所にある、外の中身の実体。"""
    root = Path(workspace.root)
    os.rename(root, tmp_path / "moved-workspace")
    if kind == "link":
        os.symlink(foreign, root)
        return foreign
    os.rename(foreign, root)
    return root


class TestH8SwappedWorkspaceRoot:
    def test_list_files_of_the_root_does_not_list_the_target_of_a_link(self, workspace, foreign_directory, tmp_path):
        """未確認だった経路: 複製の根をリンクに替えると、``list_files(".")`` が、外の一覧を返す。"""
        swap_root(workspace, foreign_directory, tmp_path, "link")
        session = make_session(workspace)

        result = session.call("list_files", {"path": "."})

        assert (result.outcome, result.data) == ("denied", {})
        assert session.trace[0].path is None

    @pytest.mark.parametrize("kind", ["link", "directory"])
    def test_tools_touch_nothing_when_the_root_is_no_longer_the_created_directory(
        self, workspace, foreign_directory, tmp_path, kind
    ):
        """根が、リンクや別のディレクトリに替えられたら、読み書きのツール 6 つは、外に触れずに ``denied``。"""
        runner = FakeTestRunner()
        content = swap_root(workspace, foreign_directory, tmp_path, kind)
        before = summary_of(content)
        session = make_session(workspace, runner=runner)

        results = {name: session.call(tool, dict(args)) for name, (tool, args) in SWAPPED_ROOT_CALLS.items()}

        assert {name: result.outcome for name, result in results.items()} == dict.fromkeys(SWAPPED_ROOT_CALLS, "denied")
        assert all(result.data == {} for result in results.values())
        assert OUTSIDE_MARK not in json.dumps([result.data for result in results.values()])
        assert [call.path for call in session.trace] == [None] * len(SWAPPED_ROOT_CALLS)
        assert runner.calls == []
        assert summary_of(content) == before

    def test_tools_work_as_before_while_the_root_is_the_created_directory(self, workspace):
        session = make_session(workspace)

        listed = session.call("list_files", {"path": "."})
        read = session.call("read_file", {"path": SRC})
        written = session.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
        ran = session.call("run_tests", {"paths": [NEW_TEST]})

        assert [result.outcome for result in (listed, read, written, ran)] == ["ok"] * 4

    def test_finish_is_still_accepted_after_the_root_was_swapped(self, workspace, foreign_directory, tmp_path):
        """終了の申告は、複製に触れないので、今までどおり受ける（合否は、コードが決める）。"""
        swap_root(workspace, foreign_directory, tmp_path, "link")

        result = make_session(workspace).call("finish", dict(FINISH_FIXED))

        assert result.outcome == "ok"


# ===========================================================================
# H9: 親が行うハッシュ計算に、上限が無い
# ===========================================================================

SPARSE_BYTES = 1 << 30  # 1GB（疎なファイル。実際のディスクは使わない）
SMALL_READ_BYTES = 8_000_000  # 見本のリポジトリを読み切っても、これには届かない
RUN_READ_BYTES = 64_000_000  # run_fix 全体（git の出力の読み取りを含む）でも、これには届かない


class ReadMeter:
    """親のプロセスが読んだ・ハッシュに掛けたバイト数。"""

    def __init__(self) -> None:
        self.read_bytes = 0
        self.hashed_bytes = 0

    @property
    def worst(self) -> int:
        return max(self.read_bytes, self.hashed_bytes)


@pytest.fixture
def read_meter(monkeypatch):
    """``os.read`` で読んだバイト数と、``hashlib.sha256`` に掛けたバイト数を数える。"""
    meter = ReadMeter()
    real_read = os.read
    real_sha256 = hashlib.sha256

    def counting_read(descriptor, length):
        data = real_read(descriptor, length)
        meter.read_bytes += len(data)
        return data

    class CountingSha256:
        def __init__(self, data=b"", **kwargs):
            meter.hashed_bytes += len(data)
            self._inner = real_sha256(data, **kwargs)

        def update(self, data):
            meter.hashed_bytes += len(data)
            self._inner.update(data)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(os, "read", counting_read)
    monkeypatch.setattr(hashlib, "sha256", CountingSha256)
    return meter


@pytest.fixture
def sparse_files():
    """疎なファイルを作れる環境かを確かめる（作れなければ、実際のディスクを使うので、テストを飛ばす）。"""
    directory = Path(tempfile.mkdtemp(prefix="sparse-probe-"))
    try:
        probe = directory / "probe"
        probe.touch()
        os.truncate(probe, 64_000_000)
        allocated = probe.stat().st_blocks * 512
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if allocated > 1_000_000:
        pytest.skip("this file system does not create sparse files")


def stretch(path: Path, size: int = SPARSE_BYTES) -> None:
    """既にあるファイルを、疎なまま ``size`` バイトに伸ばす。"""
    os.truncate(path, size)


class TestH9SizeBeforeHashing:
    def test_changes_reports_a_stretched_existing_file_as_oversized_without_reading_it(
        self, workspace, sparse_files, read_meter
    ):
        """再現そのもの: 既にあるファイルを巨大な疎なファイルに伸ばされると、変更の取り出しが、全体を読んだ。"""
        target = Path(workspace.root) / SRC
        stretch(target)
        try:
            started = time.monotonic()
            changes = workspace.changes(200_000)
            elapsed = time.monotonic() - started
        finally:
            target.unlink()

        assert SRC in tuple(changes.oversized)
        assert SRC not in dict(changes.modified)
        assert read_meter.worst < SMALL_READ_BYTES, f"{read_meter.worst} bytes were read ({elapsed:.2f} seconds)"

    def test_stretched_existing_file_fails_g9(self, workspace, sparse_files, read_meter):
        """今までの「大きすぎるファイル」と同じ扱い（G-9 の不合格）。"""
        target = Path(workspace.root) / SRC
        try:
            results = static(workspace, lambda root: stretch(root / SRC))
        finally:
            target.unlink()

        assert "G9" in failed(results)
        assert read_meter.worst < SMALL_READ_BYTES

    def test_file_changed_within_the_limit_is_still_read_and_reported(self, workspace, read_meter):
        """上限の中の変更は、今までどおり取り出す。"""
        (Path(workspace.root) / SRC).write_text(FIXED_SOURCE, encoding="utf-8")

        changes = workspace.changes(200_000)

        assert dict(changes.modified) == {SRC: FIXED_SOURCE.encode()}
        assert tuple(changes.oversized) == ()

    def test_existing_file_one_byte_over_the_limit_is_oversized(self, workspace):
        """境界: 上限ちょうどは取り出し、1 バイト超えたら ``oversized``。"""
        target = Path(workspace.root) / SRC
        target.write_bytes(b"#" * 1_000)
        at_limit = workspace.changes(1_000)
        target.write_bytes(b"#" * 1_001)
        over_limit = workspace.changes(1_000)

        assert (SRC in dict(at_limit.modified), tuple(at_limit.oversized)) == (True, ())
        assert (SRC in dict(over_limit.modified), tuple(over_limit.oversized)) == (False, (SRC,))

    def test_tree_matches_rejects_a_stretched_file_without_reading_it(self, workspace, sparse_files, read_meter):
        """検査用の複製の照合: 期待の大きさと違う時点で、読まずに不一致。"""
        from src.fixer.workspace import expected_manifest, tree_matches

        root = Path(workspace.root)
        expected = expected_manifest(workspace.baseline, {})
        assert tree_matches(root, expected) is True
        already = read_meter.worst
        stretch(root / LEGACY)
        try:
            started = time.monotonic()
            matches = tree_matches(root, expected)
            elapsed = time.monotonic() - started
        finally:
            (root / LEGACY).unlink()

        assert matches is False
        assert read_meter.worst - already < SMALL_READ_BYTES, f"{read_meter.worst} bytes ({elapsed:.2f} seconds)"

    def test_tree_matches_rejects_a_stretched_approved_file_without_reading_it(
        self, workspace, sparse_files, read_meter
    ):
        """検査を通ったファイル（上書きしたもの）も、期待の大きさと違えば、読まずに不一致。"""
        from src.fixer.workspace import expected_manifest, tree_matches

        root = Path(workspace.root)
        (root / SRC).write_text(FIXED_SOURCE, encoding="utf-8")
        expected = expected_manifest(workspace.baseline, {SRC: FIXED_SOURCE.encode()})
        assert tree_matches(root, expected) is True
        already = read_meter.worst
        stretch(root / SRC)
        try:
            matches = tree_matches(root, expected)
        finally:
            (root / SRC).unlink()

        assert matches is False
        assert read_meter.worst - already < SMALL_READ_BYTES

    def test_tree_matches_still_notices_a_change_that_keeps_the_size(self, workspace):
        from src.fixer.workspace import expected_manifest, tree_matches

        root = Path(workspace.root)
        expected = expected_manifest(workspace.baseline, {})
        original = (root / LEGACY).read_bytes()
        (root / LEGACY).write_bytes(original.replace(b"return 1", b"return 2"))

        assert tree_matches(root, expected) is False

    def test_run_fix_with_a_file_stretched_while_the_fixer_works_is_rejected_quickly(
        self, sample_repo, tmp_path, sparse_files, read_meter
    ):
        outcome = run_fixer(sample_repo, tmp_path, script=with_tamper(lambda root: stretch(root / LEGACY)))

        assert outcome.run.status == "rejected"
        assert "G9" in stop_gates(outcome)
        assert read_meter.worst < RUN_READ_BYTES, f"{read_meter.worst} bytes were read"
        assert_discarded(outcome)

    def test_run_fix_with_a_gate_tree_file_stretched_during_a_run_is_rejected_quickly(
        self, sample_repo, tmp_path, sparse_files, read_meter
    ):
        """検査の実行（B）の間に、検査用の複製のファイルが伸ばされる。実行の後の照合が、全体を読まない。"""

        def meddle(run):
            if kind_of(run) == "B":
                stretch(run.tree / LEGACY)

        outcome = run_fixer(
            sample_repo, tmp_path, script=good_fix_without_test_runs, test_runner=FakeTestRunner(override=meddle)
        )

        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", "G5")
        assert read_meter.worst < RUN_READ_BYTES, f"{read_meter.worst} bytes were read"
        assert_discarded(outcome)


class TestH9GateBudget:
    def test_lint_runs_are_limited_by_the_time_left_for_the_gates(self, sample_repo, tmp_path):
        """リントにも、検査の残り時間を渡す（``count(tree, timeout_seconds=...)``）。"""
        lint = FakeLintRunner()

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            lint_runner=lint,
            limits=limits(gate_timeout_seconds=50.0, test_timeout_seconds=300.0),
        )

        assert outcome.run.status == "proposed"
        assert len(lint.timeouts) == 2
        assert all(value is not None and 0 < value <= 50.0 for value in lint.timeouts), lint.timeouts

    def test_no_run_is_started_once_the_time_for_the_gates_is_used_up(self, sample_repo, tmp_path):
        """残り時間が 0 以下になったら、複製を作ってあっても、テストもリントも起こさない。

        合計の上限を 1 ミリ秒にする。最初の複製を作る（実物の ``git archive``）間に、使い切る。
        """
        lint = FakeLintRunner()
        runner = FakeTestRunner()

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=runner,
            lint_runner=lint,
            limits=limits(gate_timeout_seconds=0.001),
        )

        started = [*lint.timeouts, *(run.timeout_seconds for run in runner.calls)]
        assert outcome.run.status == "rejected"
        assert {"G5", "G6", "G7", "G8"} <= stop_gates(outcome)
        assert all(value is not None and value > 0 for value in started), started
        assert_discarded(outcome)

    def test_no_tree_is_built_when_no_time_is_left_at_all(self, sample_repo, tmp_path):
        """残り時間が 0 なら、次の複製を作らず、残りの検査を不合格にする。"""
        lint = FakeLintRunner()
        runner = FakeTestRunner()

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=runner,
            lint_runner=lint,
            limits=limits(gate_timeout_seconds=0.0),
        )

        assert outcome.run.status == "rejected"
        assert {"G5", "G6", "G7", "G8"} <= stop_gates(outcome)
        assert (runner.calls, lint.trees) == ([], [])
        assert [path.name for path in outcome.temp_dirs if path.name.startswith("fixer-gate-")] == []

    def test_slow_lint_uses_up_the_time_of_the_test_runs(self, sample_repo, tmp_path):
        class SlowLint(FakeLintRunner):
            def count(self, tree, **kwargs):
                time.sleep(0.4)
                return super().count(tree, **kwargs)

        runner = FakeTestRunner()

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=runner,
            lint_runner=SlowLint(),
            limits=limits(gate_timeout_seconds=0.3),
        )

        assert outcome.run.status == "rejected"
        assert runner.calls == []
        assert {"G5", "G6", "G7"} <= stop_gates(outcome)

    def test_ruff_runner_takes_the_limit_of_a_single_call(self, tree):
        """``RuffRunner.count`` に渡した制限時間は、作るときに決めた制限時間より優先する。"""
        from src.fixer.runners import RuffRunner

        assert RuffRunner().count(tree, timeout_seconds=0.0) is None
        assert RuffRunner(timeout_seconds=0.0).count(tree, timeout_seconds=30.0) == 1
        assert RuffRunner().count(tree) == 1
        assert RuffRunner().count(tree, timeout_seconds=None) == 1
