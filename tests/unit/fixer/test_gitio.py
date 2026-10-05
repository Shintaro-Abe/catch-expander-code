"""git の操作（design.md §5）。実物の git を、一時ディレクトリの小さなリポジトリに対して使う。

対応: AC-21（コミットの作り方。元のリポジトリを変えない。フックを走らせない）、AC-22（リモートに触れない。
決まったサブコマンドだけ）、AC-28 の部品（基準のコミットの解決、既にあるブランチ）、R6（複製に ``.git`` がない）。

読み方（設計に名前が無い点）: 一覧にないサブコマンドを直接確かめるための入口は、``Git.run(args)`` または
``Git._run(args)``（``args`` は、サブコマンドを先頭にした文字列のリスト）とした。
"""

import os
import re
from pathlib import Path

import pytest

from . import repo_factory
from .repo_factory import AUTHOR_NAME, BRANCH, EXISTING_TEST, FILES, NEW_TEST, NEW_TEST_SOURCE, SRC

SHA = re.compile(r"[0-9a-f]{40}")
MESSAGE = "fix: 診断にもとづく修正案 (exec-20261004120000-abcdef12)\n\n修正役による提案。取り込む前に人が確認する\n"
FIXED_SOURCE = FILES[SRC].replace(repo_factory.FIX_OLD, repo_factory.FIX_NEW)
COMMIT_FILES = {SRC: FIXED_SOURCE.encode(), NEW_TEST: NEW_TEST_SOURCE.encode()}
# design.md §5: git に渡してよい環境変数（一時的なインデックスと、作者・コミッターの指定を含む）
ALLOWED_GIT_ENV = {
    "PATH",
    "HOME",
    "GIT_TERMINAL_PROMPT",
    "GIT_OPTIONAL_LOCKS",
    "LC_ALL",
    "GIT_INDEX_FILE",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    # レビュー対応 F2: 開発者の全体設定・システムの設定を読ませない
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    # 最後のレビュー対応 H2・H3: 置換参照を読ませない。部分クローンで、足りないオブジェクトを取りに行かせない
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_NO_LAZY_FETCH",
}
SECRET_MARK = "GITIO_PARENT_ENV_MARK_3391"


def make_git(repo_path):
    from src.fixer.gitio import Git

    return Git(Path(repo_path))


def git_error():
    from src.fixer.gitio import GitError

    return GitError


def raw_run(git, args):
    runner = getattr(git, "run", None) or git._run
    return runner(list(args))


def commit(repo, **overrides):
    arguments = {"base": repo.head, "branch": BRANCH, "message": MESSAGE, "files": dict(COMMIT_FILES)}
    arguments.update(overrides)
    return make_git(repo.path).create_branch_commit(**arguments)


# ---------------------------------------------------------------------------
# AC-22: 決まったサブコマンドだけ
# ---------------------------------------------------------------------------


class TestSubcommandAllowlist:
    @pytest.mark.parametrize(
        "args",
        [
            ["push", "origin", "HEAD"],
            ["push"],
            ["fetch", "--all"],
            ["pull"],
            ["clone", "https://example.invalid/x.git"],
            ["remote", "add", "origin", "https://example.invalid/x.git"],
            ["ls-remote", "origin"],
            ["submodule", "update", "--init"],
            ["checkout", "-b", "x"],
            ["reset", "--hard"],
            ["stash"],
            ["clean", "-fdx"],
            ["worktree", "add", "../w"],
            ["commit", "-m", "x"],
            ["config", "core.hooksPath", "/elsewhere"],
            ["gc"],
            ["status"],
            ["PUSH"],
            ["push\n"],
            [" rev-parse", "HEAD"],
            ["-c", "core.sshCommand=true", "fetch"],
            ["-C", "/", "push"],
            ["--exec-path=/tmp", "rev-parse", "HEAD"],
            [],
        ],
        ids=lambda args: "_".join(args)[:30].replace("\n", "NL") or "empty",
    )
    def test_git_subcommand_outside_the_fixed_list_raises_git_error_without_running(
        self, sample_repo, process_recorder, args
    ):
        """AC-22: 一覧にないサブコマンド（リモートに触れるものを含む）は、プロセスを起こす前に拒否する。"""
        git = make_git(sample_repo.path)
        before = len(process_recorder.implementation_calls)

        with pytest.raises(git_error()):
            raw_run(git, args)

        assert len(process_recorder.implementation_calls) == before

    def test_gitio_source_has_no_remote_subcommand_literals(self):
        source = (Path(__file__).resolve().parents[3] / "src" / "fixer" / "gitio.py").read_text(encoding="utf-8")

        for name in ("push", "fetch", "pull", "clone", "remote", "ls-remote", "submodule", "worktree", "checkout"):
            assert f'"{name}"' not in source, name
            assert f"'{name}'" not in source, name
        assert "shell=True" not in source


class TestGitEnvironment:
    def test_git_calls_receive_only_the_fixed_environment(self, sample_repo, process_recorder, monkeypatch):
        """design.md §5: 環境変数は決まったものだけ。親の秘密の値は渡らない。"""
        for name in (
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "GITHUB_TOKEN",
            "ANTHROPIC_API_KEY",
            "GIT_SSH_COMMAND",
        ):
            monkeypatch.setenv(name, SECRET_MARK)
        git = make_git(sample_repo.path)
        base = git.resolve_commit("HEAD")
        git.branch_exists(BRANCH)
        git.export_tree(base, sample_repo.path.parent / "exported")
        git.create_branch_commit(base=base, branch=BRANCH, message=MESSAGE, files=dict(COMMIT_FILES))

        calls = process_recorder.implementation_git_calls
        assert calls, "no git process was recorded"
        for call in calls:
            assert call.env is not None
            assert set(call.env) <= ALLOWED_GIT_ENV, sorted(set(call.env) - ALLOWED_GIT_ENV)
            assert SECRET_MARK not in call.env.values()
            assert call.env["GIT_TERMINAL_PROMPT"] == "0"
            assert call.env["LC_ALL"] == "C"
            assert call.shell is False

    def test_git_uses_a_temporary_index_and_never_the_repository_index(self, sample_repo, process_recorder):
        commit(sample_repo)

        index_files = {
            call.env["GIT_INDEX_FILE"]
            for call in process_recorder.implementation_git_calls
            if "GIT_INDEX_FILE" in call.env
        }
        writers = [
            call
            for call in process_recorder.implementation_git_calls
            if call.git_subcommand in ("read-tree", "update-index", "write-tree")
        ]
        assert writers
        assert all("GIT_INDEX_FILE" in call.env for call in writers)
        for path in index_files:
            assert not Path(path).resolve().is_relative_to(sample_repo.path.resolve())

    def test_git_ignores_git_dir_of_the_parent_environment(self, sample_repo, tmp_path, monkeypatch):
        """親の ``GIT_DIR``・``GIT_WORK_TREE`` を引き継がない（別のリポジトリを取り違えない）。"""
        decoy = tmp_path / "decoy"
        repo_factory.init_repo(decoy, sample_repo.home, files={"a.txt": "decoy\n"})
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

        assert make_git(sample_repo.path).resolve_commit("HEAD") == sample_repo.head


# ---------------------------------------------------------------------------
# AC-28 の部品: 基準のコミットとブランチ
# ---------------------------------------------------------------------------


class TestResolveCommit:
    @pytest.mark.parametrize("rev", ["HEAD", "main", "HEAD^{commit}"])
    def test_resolve_commit_existing_revision_returns_full_sha(self, sample_repo, rev):
        sha = make_git(sample_repo.path).resolve_commit(rev)

        assert SHA.fullmatch(sha)
        assert sha == sample_repo.head

    def test_resolve_commit_full_sha_returns_itself(self, sample_repo):
        assert make_git(sample_repo.path).resolve_commit(sample_repo.head) == sample_repo.head

    @pytest.mark.parametrize("rev", ["no-such-ref", "", "HEAD~5", "0" * 40, "HEAD..main", "HEAD main", "HEAD\nmain"])
    def test_resolve_commit_unknown_revision_raises_git_error(self, sample_repo, rev):
        with pytest.raises(git_error()):
            make_git(sample_repo.path).resolve_commit(rev)

    @pytest.mark.parametrize("rev", ["-h", "--help", "--all", "-", "--output=/tmp/x", "--upload-pack=true"])
    def test_resolve_commit_revision_starting_with_dash_raises_git_error_without_running(
        self, sample_repo, process_recorder, rev
    ):
        git = make_git(sample_repo.path)
        before = len(process_recorder.implementation_calls)

        with pytest.raises(git_error()):
            git.resolve_commit(rev)

        assert len(process_recorder.implementation_calls) == before

    def test_resolve_commit_object_that_is_not_a_commit_raises_git_error(self, sample_repo):
        blob = sample_repo.tree_files(sample_repo.head)[SRC][1]

        with pytest.raises(git_error()):
            make_git(sample_repo.path).resolve_commit(blob)

    def test_resolve_commit_directory_that_is_not_a_repository_raises_git_error(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()

        with pytest.raises(git_error()):
            make_git(plain).resolve_commit("HEAD")


class TestBranchExists:
    def test_branch_exists_missing_branch_returns_false(self, sample_repo):
        assert make_git(sample_repo.path).branch_exists(BRANCH) is False

    def test_branch_exists_existing_branch_returns_true(self, sample_repo):
        sample_repo.create_branch(BRANCH)

        git = make_git(sample_repo.path)
        assert git.branch_exists(BRANCH) is True
        assert git.branch_exists("main") is True

    def test_branch_exists_prefix_of_an_existing_branch_returns_false(self, sample_repo):
        sample_repo.create_branch(BRANCH)

        assert make_git(sample_repo.path).branch_exists("fix/diagnosed-exec") is False


# ---------------------------------------------------------------------------
# R6: 複製の書き出し
# ---------------------------------------------------------------------------


class TestExportTree:
    def test_export_tree_writes_committed_files_without_dot_git(self, sample_repo, tmp_path):
        dest = tmp_path / "exported"

        make_git(sample_repo.path).export_tree(sample_repo.head, dest)

        assert not (dest / ".git").exists()
        exported = {path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()}
        assert exported == set(FILES)
        for rel, content in FILES.items():
            assert (dest / rel).read_text(encoding="utf-8") == content

    def test_export_tree_ignores_uncommitted_changes_and_leaves_the_repository_untouched(self, sample_repo, tmp_path):
        (sample_repo.path / SRC).write_text("UNCOMMITTED = 1\n", encoding="utf-8")
        (sample_repo.path / "untracked.txt").write_text("x\n", encoding="utf-8")
        before = sample_repo.state()
        dest = tmp_path / "exported"

        make_git(sample_repo.path).export_tree(sample_repo.head, dest)

        assert (dest / SRC).read_text(encoding="utf-8") == FILES[SRC]
        assert not (dest / "untracked.txt").exists()
        assert before.differences(sample_repo.state()) == []

    @pytest.mark.parametrize("target", ["ABSOLUTE", "../../outside-secret.txt"])
    def test_export_tree_never_materialises_a_symlink_that_leaves_the_destination(self, sample_repo, tmp_path, target):
        """design.md §5.1: 絶対パス・``..``・特殊なファイルを通さない（通せないものがあれば、失敗して終わる）。"""
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("OUTSIDE\n", encoding="utf-8")
        os.symlink(str(outside) if target == "ABSOLUTE" else target, sample_repo.path / "src" / "escape.py")
        commit_sha = sample_repo.commit_all("add a symlink that leaves the tree")
        dest = tmp_path / "exported"

        git = make_git(sample_repo.path)
        with pytest.raises(git_error()):
            git.export_tree(commit_sha, dest)

        if dest.exists():
            for path in dest.rglob("*"):
                assert not path.is_symlink(), path
                assert path.resolve().is_relative_to(dest.resolve()), path


# ---------------------------------------------------------------------------
# AC-21: コミット
# ---------------------------------------------------------------------------


class TestCreateBranchCommit:
    def test_create_branch_commit_creates_one_commit_on_the_new_branch_with_base_as_parent(self, sample_repo):
        sha = commit(sample_repo)

        assert SHA.fullmatch(sha)
        assert sample_repo.rev(BRANCH) == sha
        assert sample_repo.parents(sha) == [sample_repo.head]
        assert sample_repo.message(sha).strip() == MESSAGE.strip()

    def test_create_branch_commit_contains_exactly_the_given_files(self, sample_repo):
        sha = commit(sample_repo)

        assert sample_repo.changed_paths(sample_repo.head, sha) == sorted(COMMIT_FILES)
        assert sample_repo.file_at(sha, SRC) == FIXED_SOURCE
        assert sample_repo.file_at(sha, NEW_TEST) == NEW_TEST_SOURCE
        tree = sample_repo.tree_files(sha)
        assert set(tree) == set(FILES) | {NEW_TEST}
        assert tree[SRC][0] == "100644"
        assert tree[NEW_TEST][0] == "100644"
        assert sample_repo.file_at(sha, EXISTING_TEST) == FILES[EXISTING_TEST]

    def test_create_branch_commit_file_in_a_new_directory_is_committed(self, sample_repo):
        files = {
            "tests/unit/new_package/__init__.py": b"",
            "tests/unit/new_package/test_new.py": b"def test_x():\n    assert True\n",
        }

        sha = commit(sample_repo, files=files)

        assert sample_repo.changed_paths(sample_repo.head, sha) == sorted(files)
        assert sample_repo.file_at(sha, "tests/unit/new_package/__init__.py") == ""

    def test_create_branch_commit_leaves_head_branch_worktree_and_index_unchanged(self, sample_repo):
        """AC-21: 元のリポジトリの現在のブランチ・作業ツリー・インデックスは変わらない。"""
        before = sample_repo.state()

        commit(sample_repo)

        after = sample_repo.state()
        assert before.differences(after) == []
        assert after.branch == "main"
        assert after.status == ""
        assert (sample_repo.path / SRC).read_text(encoding="utf-8") == FILES[SRC]
        assert not (sample_repo.path / NEW_TEST).exists()

    def test_create_branch_commit_does_not_pick_up_staged_or_dirty_changes(self, sample_repo):
        """実物のインデックスを使っていれば、ステージ済みの変更がコミットに混ざる。"""
        (sample_repo.path / "README.md").write_text("# staged change\n", encoding="utf-8")
        sample_repo.git("add", "README.md")
        (sample_repo.path / "docs" / "guide.md").write_text("# dirty change\n", encoding="utf-8")
        (sample_repo.path / "untracked.py").write_text("X = 1\n", encoding="utf-8")
        before = sample_repo.state()

        sha = commit(sample_repo)

        assert sample_repo.changed_paths(sample_repo.head, sha) == sorted(COMMIT_FILES)
        assert before.differences(sample_repo.state()) == []
        assert "README.md" in before.status

    def test_create_branch_commit_does_not_run_commit_hooks(self, sample_repo):
        """AC-21: リポジトリに仕込んだコミット時のフックは実行されない。"""
        sample_repo.install_hooks()

        commit(sample_repo)

        assert sorted(path.name for path in sample_repo.hook_mark_dir.glob("*")) == []

    def test_create_branch_commit_existing_branch_raises_git_error_and_keeps_the_branch(self, sample_repo):
        """design.md §5.2 の 5: 既にあるブランチは上書きできない。"""
        sample_repo.create_branch(BRANCH)
        before = sample_repo.state()

        with pytest.raises(git_error()):
            commit(sample_repo)

        assert sample_repo.rev(BRANCH) == sample_repo.head
        assert before.differences(sample_repo.state()) == []

    def test_create_branch_commit_onto_the_current_branch_raises_git_error(self, sample_repo):
        with pytest.raises(git_error()):
            commit(sample_repo, branch="main")

        assert sample_repo.rev("main") == sample_repo.head
        assert sample_repo.state().status == ""

    def test_create_branch_commit_uses_author_from_the_repository_config(self, sample_repo):
        sha = commit(sample_repo)

        assert sample_repo.author(sha) == AUTHOR_NAME

    def test_create_branch_commit_without_configured_identity_uses_the_fixed_name(self, sample_repo):
        """design.md §5.2 の 4: 設定が無いときは、決まった名前（``catch-expander-fixer``）を使う。"""
        sample_repo.git("config", "--unset", "user.name")
        sample_repo.git("config", "--unset", "user.email")

        sha = commit(sample_repo)

        assert sample_repo.author(sha) == "catch-expander-fixer"

    def test_create_branch_commit_unknown_base_raises_git_error_and_creates_no_branch(self, sample_repo):
        with pytest.raises(git_error()):
            commit(sample_repo, base="0" * 40)

        assert BRANCH not in sample_repo.branches()

    def test_create_branch_commit_twice_second_call_fails_and_first_commit_stays(self, sample_repo):
        first = commit(sample_repo)

        with pytest.raises(git_error()):
            commit(sample_repo, files={SRC: b"OVERWRITTEN = 1\n"})

        assert sample_repo.rev(BRANCH) == first
