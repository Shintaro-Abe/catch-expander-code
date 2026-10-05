"""作業用の複製（design.md §6）。実物の git で書き出した複製を使う。

対応: AC-6（複製の外・書き込めない場所を、パスの解決の時点で拒否する。複製に ``.git`` がない）、
AC-20 の部品（複製を消す）、AC-31 の部品（検査用のきれいな複製）、G-1〜G-3・G-10 の材料（変更の取り出し）。

読み方（設計に名前が無い点）: 基準の一覧は ``Workspace.baseline``（複製の根からの相対パスをキーにした
``Mapping``）とした。``changes()`` の ``oversized`` の上限は、``FixLimits`` の既定値（200,000 バイト）。
"""

import contextlib
import os
import threading
import time
from pathlib import Path

import pytest

from .conftest import OUTSIDE_MARK
from .fakes import check_each
from .repo_factory import EXISTING_CONFTEST, EXISTING_TEST, FILES, FIX_NEW, FIX_OLD, NEW_TEST, NEW_TEST_SOURCE, SRC


def denied():
    from src.fixer.workspace import PathDenied

    return PathDenied


# 読み取りでも書き込みでも拒否する、複製の外・形の不正なパス
ESCAPING_PATHS = [
    "",
    "/etc/passwd",
    "/",
    "..",
    "../outside/secret.py",
    "src/../../outside/secret.py",
    "src/agent/../../../outside/secret.py",
    "src/..",
    "src/../src/agent/orchestrator.py",
    "./src/agent/orchestrator.py",
    "src/./agent/orchestrator.py",
    "src//agent/orchestrator.py",
    "src/agent/",
    "src\\agent\\orchestrator.py",
    "..\\outside\\secret.py",
    "C:\\Windows\\win.ini",
    "C:/Windows/win.py",
    "src/agent/orchestrator.py\x00.txt",
    ".git",
    ".git/config",
    ".git/hooks/pre-commit",
    "a/" * 200 + "b.py",  # 401 字以上
]
NON_STRING_PATHS = [
    None,
    3,
    3.5,
    True,
    ["src/agent/orchestrator.py"],
    {"path": SRC},
    b"src/agent/orchestrator.py",
    ("src",),
]


class TestCreate:
    def test_create_exports_base_commit_outside_the_repository_without_dot_git(
        self, workspace, _repo_template, tmp_path
    ):
        """R6・AC-6: 複製はリポジトリの外にあり、``.git`` が無い。"""
        root = Path(workspace.root)

        assert root.is_dir()
        assert not (root / ".git").exists()
        assert not root.resolve().is_relative_to(_repo_template[0].resolve())
        assert root.resolve().is_relative_to((tmp_path / "workspace-parent").resolve())
        for rel, content in FILES.items():
            assert (root / rel).read_text(encoding="utf-8") == content

    def test_create_records_every_path_in_the_baseline(self, workspace):
        assert set(workspace.baseline) == set(FILES)

    def test_create_does_not_change_the_repository(self, sample_repo, tmp_path):
        from src.fixer.gitio import Git
        from src.fixer.workspace import Workspace

        before = sample_repo.state()
        git = Git(sample_repo.path)

        created = Workspace.create(git, sample_repo.head, tmp_path)
        created.destroy()

        assert before.differences(sample_repo.state()) == []

    def test_destroy_removes_the_workspace(self, sample_repo, tmp_path):
        from src.fixer.gitio import Git
        from src.fixer.workspace import Workspace

        created = Workspace.create(Git(sample_repo.path), sample_repo.head, tmp_path)
        root = Path(created.root)
        (root / "src" / "agent" / "extra.py").write_text("X = 1\n", encoding="utf-8")
        os.symlink("/", root / "link-to-root")

        created.destroy()

        assert not root.exists()
        assert Path("/").is_dir()


class TestResolveRead:
    @pytest.mark.parametrize(
        "rel", [SRC, "src", "tests/unit", "README.md", ".github/workflows/ci.yml", "src/agent/main.py"]
    )
    def test_resolve_read_path_inside_the_workspace_returns_the_real_path(self, workspace, rel):
        resolved = workspace.resolve_read(rel)

        assert Path(resolved).resolve() == (Path(workspace.root) / rel).resolve()

    def test_resolve_read_path_outside_or_malformed_raises_path_denied(self, workspace, outside):
        """AC-6: 絶対パス・``..``・``.git`` などは、解決の時点で拒否する。"""

        def check(rel):
            with pytest.raises(denied()):
                workspace.resolve_read(rel)

        denied()
        check_each(ESCAPING_PATHS, check)

    def test_resolve_read_non_string_raises_path_denied(self, workspace):
        def check(rel):
            with pytest.raises(denied()):
                workspace.resolve_read(rel)

        denied()
        check_each(NON_STRING_PATHS, check)

    def test_resolve_read_absolute_path_of_a_file_inside_the_workspace_raises_path_denied(self, workspace):
        with pytest.raises(denied()):
            workspace.resolve_read(str(Path(workspace.root) / SRC))

    def test_resolve_read_symlink_to_a_file_outside_raises_path_denied(self, workspace, outside):
        """AC-6: 複製の外を指すシンボリックリンク。"""
        os.symlink(outside / "secret.py", Path(workspace.root) / "src" / "agent" / "leak.py")

        with pytest.raises(denied()):
            workspace.resolve_read("src/agent/leak.py")

    def test_resolve_read_path_through_a_symlinked_directory_outside_raises_path_denied(self, workspace, outside):
        os.symlink(outside, Path(workspace.root) / "src" / "agent" / "linkdir")

        for rel in ("src/agent/linkdir", "src/agent/linkdir/secret.py"):
            with pytest.raises(denied()):
                workspace.resolve_read(rel)

    def test_resolve_read_relative_symlink_climbing_out_raises_path_denied(self, workspace, outside):
        root = Path(workspace.root)
        target = os.path.relpath(outside / "secret.py", root / "src")
        os.symlink(target, root / "src" / "climb.py")

        with pytest.raises(denied()):
            workspace.resolve_read("src/climb.py")

    def test_resolve_read_chain_of_symlinks_ending_outside_raises_path_denied(self, workspace, outside):
        root = Path(workspace.root)
        os.symlink(outside / "secret.py", root / "src" / "hop2.py")
        os.symlink("hop2.py", root / "src" / "hop1.py")

        with pytest.raises(denied()):
            workspace.resolve_read("src/hop1.py")

    def test_resolve_read_symlink_staying_inside_the_workspace_is_allowed(self, workspace):
        root = Path(workspace.root)
        os.symlink("orchestrator.py", root / "src" / "agent" / "alias.py")

        assert Path(workspace.resolve_read("src/agent/alias.py")).resolve() == (root / SRC).resolve()


WRITABLE = [
    SRC,
    "src/agent/new_module.py",
    "src/agent/storage/notion_client.py",
    "src/agent/storage/new_helper.py",
    "src/observability/emitter.py",
    "src/dashboard_api/executions/app.py",
    "src/agent/diagnosis_notes.py",  # 「src/agent/diagnosis/」の前方一致には当たらない
    "tests/unit/test_new.py",
    NEW_TEST,
    "tests/unit/new_package/test_new.py",
    "tests/unit/new_package/__init__.py",
    "tests/integration/test_new.py",
]
NOT_WRITABLE = {
    "outside src and tests": [
        "README.md",
        "pyproject.toml",
        "template.yaml",
        "CLAUDE.md",
        ".gitleaks.toml",
        ".github/workflows/ci.yml",
        ".github/workflows/new.yml",
        ".steering/x/requirements.md",
        "docs/guide.md",
        "new_top_level.py",
        "scripts/tool.py",
        "conftest.py",
        "source/agent/x.py",
        "srcx/agent/x.py",
        "test/unit/test_x.py",
    ],
    "not a python file": [
        "src",
        "src/agent",
        "src/agent/data.json",
        "src/agent/notes.txt",
        "src/agent/module.py.txt",
        "src/agent/no_extension",
        "src/agent/script.sh",
        "src/agent/module.pyc",
        "src/agent/.py/x",
        "tests/unit/pytest.ini",
        "tests/unit/data.json",
    ],
    "forbidden prefix": [
        "src/fixer/gates.py",
        "src/fixer/new_module.py",
        "src/fixer/sub/new_module.py",
        "src/agent/diagnosis/tools.py",
        "src/agent/diagnosis/new_module.py",
        "src/agent/prompts/builder.py",
        "src/agent/prompts/new_prompt.py",
        "src/token_monitor/handler.py",
        "src/token_monitor/new_module.py",
        "src/dashboard_api/authorizer/app.py",
        "src/dashboard_api/auth_login/app.py",
        "src/dashboard_api/auth_callback/app.py",
        "src/dashboard_api/auth_logout/app.py",
        "src/dashboard_api/auth_me/app.py",
        "tests/unit/fixer/test_new.py",
        "tests/unit/fixer/test_gates.py",
        "tests/unit/agent/diagnosis/test_new.py",
    ],
    "forbidden exact path": [
        "src/agent/main.py",
        "src/trigger/slack_verify.py",
        "src/trigger/app.py",
        "src/agent/Dockerfile",
        "src/agent/requirements.txt",
    ],
    "forbidden file name": [
        "src/agent/conftest.py",
        "src/conftest.py",
        "tests/conftest.py",
        "tests/unit/new_package/conftest.py",
        "src/agent/sitecustomize.py",
        "src/sitecustomize.py",
        "tests/unit/sitecustomize.py",
        "src/agent/usercustomize.py",
        "src/trigger/requirements.txt",
        "src/trigger/requirements-dev.txt",
        "src/trigger/Dockerfile",
    ],
    "existing or non-test file under tests": [
        EXISTING_TEST,
        EXISTING_CONFTEST,
        "tests/__init__.py",
        "tests/unit/__init__.py",
        "tests/unit/helper.py",
        "tests/unit/new_package/helper.py",
        "tests/unit/testing.py",
        "tests/unit/Test_new.py",
        "tests/unit/test_new.txt",
    ],
}


class TestResolveWrite:
    def test_resolve_write_allowed_place_returns_a_path_inside_the_workspace(self, workspace):
        def check(rel):
            resolved = Path(workspace.resolve_write(rel))
            assert (
                resolved == Path(workspace.root) / rel or resolved.resolve() == (Path(workspace.root) / rel).resolve()
            )

        denied()
        check_each(WRITABLE, check)

    @pytest.mark.parametrize("group", list(NOT_WRITABLE))
    def test_resolve_write_forbidden_place_raises_path_denied(self, workspace, group):
        """R6a・AC-6: ``src/``・``tests/`` の外、禁止の場所、既存のテストファイルへの書き込みを拒否する。"""

        def check(rel):
            with pytest.raises(denied()):
                workspace.resolve_write(rel)

        denied()
        check_each(NOT_WRITABLE[group], check)

    def test_resolve_write_path_outside_or_malformed_raises_path_denied(self, workspace, outside):
        def check(rel):
            with pytest.raises(denied()):
                workspace.resolve_write(rel)

        denied()
        check_each([*ESCAPING_PATHS, *NON_STRING_PATHS], check)

    def test_resolve_write_through_a_symlinked_directory_outside_raises_path_denied(self, workspace, outside):
        os.symlink(outside, Path(workspace.root) / "src" / "agent" / "linkdir")

        with pytest.raises(denied()):
            workspace.resolve_write("src/agent/linkdir/new_module.py")

    def test_resolve_write_through_a_symlinked_directory_inside_raises_path_denied(self, workspace):
        """design.md §6.2: パスの途中の要素がシンボリックリンクなら、先が複製の中でも書かない。"""
        os.symlink("diagnosis", Path(workspace.root) / "src" / "agent" / "harmless")

        with pytest.raises(denied()):
            workspace.resolve_write("src/agent/harmless/tools.py")

    @pytest.mark.parametrize("target", ["main.py", "orchestrator.py", "OUTSIDE"])
    def test_resolve_write_onto_a_symlink_raises_path_denied(self, workspace, outside, target):
        link_target = str(outside / "secret.py") if target == "OUTSIDE" else target
        os.symlink(link_target, Path(workspace.root) / "src" / "agent" / "alias.py")

        with pytest.raises(denied()):
            workspace.resolve_write("src/agent/alias.py")

    def test_resolve_write_dangling_symlink_raises_path_denied(self, workspace, outside):
        os.symlink(outside / "not-yet.py", Path(workspace.root) / "src" / "agent" / "dangling.py")

        with pytest.raises(denied()):
            workspace.resolve_write("src/agent/dangling.py")

    def test_resolve_write_top_level_src_replaced_by_a_symlink_raises_path_denied(self, workspace, outside):
        root = Path(workspace.root)
        (root / "src").rename(root / "src-moved")
        os.symlink(outside, root / "src")

        with pytest.raises(denied()):
            workspace.resolve_write("src/new_module.py")

    def test_resolve_write_new_test_file_created_earlier_can_be_rewritten(self, workspace):
        """基準の一覧に無いテストファイルは、書き直せる（既存のテストだけが対象外）。"""
        first = Path(workspace.resolve_write(NEW_TEST))
        first.write_text(NEW_TEST_SOURCE, encoding="utf-8")

        assert Path(workspace.resolve_write(NEW_TEST)).name == Path(NEW_TEST).name


class TestChanges:
    def test_changes_untouched_workspace_reports_nothing(self, workspace):
        changes = workspace.changes()

        assert dict(changes.added) == {}
        assert dict(changes.modified) == {}
        assert tuple(changes.deleted) == ()
        assert tuple(changes.irregular) == ()
        assert tuple(changes.oversized) == ()

    def test_changes_reports_added_modified_and_deleted_files_with_contents(self, workspace):
        root = Path(workspace.root)
        (root / NEW_TEST).write_text(NEW_TEST_SOURCE, encoding="utf-8")
        fixed = FILES[SRC].replace(FIX_OLD, FIX_NEW)
        (root / SRC).write_text(fixed, encoding="utf-8")
        (root / "README.md").unlink()

        changes = workspace.changes()

        assert dict(changes.added) == {NEW_TEST: NEW_TEST_SOURCE.encode()}
        assert dict(changes.modified) == {SRC: fixed.encode()}
        assert tuple(changes.deleted) == ("README.md",)
        assert tuple(changes.irregular) == ()

    def test_changes_file_rewritten_with_identical_content_is_not_a_change(self, workspace):
        (Path(workspace.root) / SRC).write_text(FILES[SRC], encoding="utf-8")

        assert dict(workspace.changes().modified) == {}

    @pytest.mark.parametrize(
        "rel",
        [
            "__pycache__/top.cpython-313.pyc",
            "src/agent/__pycache__/orchestrator.cpython-313.pyc",
            "src/agent/__pycache__/notes.txt",
            "tests/unit/__pycache__/test_orchestrator.cpython-313-pytest-8.3.0.pyc",
            "src/agent/stray.pyc",
            ".pytest_cache/v/cache/lastfailed",
            ".pytest_cache/README.md",
            ".ruff_cache/0.8.0/123",
        ],
    )
    def test_changes_cache_files_are_ignored(self, workspace, rel):
        """design.md §6.3・AC-31: キャッシュは変更として数えない。"""
        target = Path(workspace.root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"cache")

        changes = workspace.changes()

        assert dict(changes.added) == {}
        assert tuple(changes.irregular) == ()

    @pytest.mark.parametrize(
        "rel",
        [
            "notes.txt",
            ".env",
            ".git/config",
            ".git/hooks/pre-commit",
            ".github/workflows/new.yml",
            ".hidden/module.py",
            "src/agent/pycache/module.py",
            "src/agent/module.pyc.py",
            "conftest.py",
            "pytest_cache/x",
        ],
    )
    def test_changes_files_outside_src_and_tests_and_hidden_files_are_reported(self, workspace, rel):
        """無視するのは、決まったキャッシュだけ。隠しファイルや ``.git`` を作っても、変更として現れる。"""
        target = Path(workspace.root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")

        assert rel in dict(workspace.changes().added)

    def test_changes_added_symlink_is_irregular_and_not_followed(self, workspace, outside):
        os.symlink(outside / "secret.py", Path(workspace.root) / "src" / "agent" / "leak.py")

        changes = workspace.changes()

        assert "src/agent/leak.py" in tuple(changes.irregular)
        assert "src/agent/leak.py" not in dict(changes.added)
        assert OUTSIDE_MARK.encode() not in b"".join(dict(changes.added).values())

    def test_changes_symlinked_directory_is_irregular_and_its_contents_are_not_walked(self, workspace, outside):
        os.symlink(outside, Path(workspace.root) / "src" / "agent" / "linkdir")

        changes = workspace.changes()

        assert "src/agent/linkdir" in tuple(changes.irregular)
        assert not any(path.startswith("src/agent/linkdir/") for path in dict(changes.added))

    def test_changes_existing_file_replaced_by_symlink_is_irregular(self, workspace):
        target = Path(workspace.root) / SRC
        target.unlink()
        os.symlink("legacy.py", target)

        assert SRC in tuple(workspace.changes().irregular)

    @pytest.mark.parametrize("mode", [0o755, 0o744, 0o4644])
    def test_changes_mode_change_is_irregular(self, workspace, mode):
        """G-10 の材料: 実行権限などのモードの変化。"""
        (Path(workspace.root) / SRC).chmod(mode)

        assert SRC in tuple(workspace.changes().irregular)

    def test_changes_new_executable_file_is_irregular(self, workspace):
        target = Path(workspace.root) / "src" / "agent" / "tool.py"
        target.write_text("X = 1\n", encoding="utf-8")
        target.chmod(0o755)

        assert "src/agent/tool.py" in tuple(workspace.changes().irregular)

    def test_changes_file_replaced_by_directory_is_irregular(self, workspace):
        target = Path(workspace.root) / SRC
        target.unlink()
        target.mkdir()
        (target / "inner.py").write_text("X = 1\n", encoding="utf-8")

        changes = workspace.changes()

        assert SRC in tuple(changes.irregular)
        assert SRC not in tuple(changes.deleted)
        assert dict(changes.added) == {f"{SRC}/inner.py": b"X = 1\n"}

    def test_changes_fifo_is_irregular_and_is_not_opened(self, workspace):
        fifo = Path(workspace.root) / "src" / "agent" / "pipe.py"
        os.mkfifo(fifo)

        def unblock():
            # 実装が FIFO を開いて待ってしまった場合に、テストが止まり続けないようにする
            with contextlib.suppress(OSError):
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))

        watchdog = threading.Timer(5.0, unblock)
        watchdog.start()
        started = time.monotonic()
        try:
            changes = workspace.changes()
        finally:
            watchdog.cancel()

        assert "src/agent/pipe.py" in tuple(changes.irregular)
        assert time.monotonic() - started < 4.0

    def test_changes_file_over_the_write_limit_is_oversized(self, workspace):
        (Path(workspace.root) / "src" / "agent" / "big.py").write_bytes(b"#" * 200_001)

        assert "src/agent/big.py" in tuple(workspace.changes().oversized)

    def test_changes_binary_content_is_reported_as_bytes(self, workspace):
        payload = b"\xff\xfe\x00binary"
        (Path(workspace.root) / "src" / "agent" / "blob.py").write_bytes(payload)

        assert dict(workspace.changes().added)["src/agent/blob.py"] == payload


class TestBuildTree:
    def test_build_tree_writes_base_commit_plus_the_given_files_only(self, sample_repo, tmp_path):
        """AC-31: 検査用の複製は、基準のコミットと、渡したファイルだけからなる。"""
        from src.fixer.gitio import Git
        from src.fixer.workspace import build_tree

        dest = tmp_path / "clean-tree"
        fixed = FILES[SRC].replace(FIX_OLD, FIX_NEW)

        build_tree(
            Git(sample_repo.path), sample_repo.head, dest, {SRC: fixed.encode(), NEW_TEST: NEW_TEST_SOURCE.encode()}
        )

        files = {path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()}
        assert files == set(FILES) | {NEW_TEST}
        assert (dest / SRC).read_text(encoding="utf-8") == fixed
        assert (dest / NEW_TEST).read_text(encoding="utf-8") == NEW_TEST_SOURCE
        assert (dest / EXISTING_TEST).read_text(encoding="utf-8") == FILES[EXISTING_TEST]
        assert not (dest / ".git").exists()

    def test_build_tree_creates_missing_directories_for_new_files(self, sample_repo, tmp_path):
        from src.fixer.gitio import Git
        from src.fixer.workspace import build_tree

        dest = tmp_path / "clean-tree"
        files = {
            "tests/unit/new_package/__init__.py": b"",
            "tests/unit/new_package/test_new.py": b"def test_x():\n    assert True\n",
        }

        build_tree(Git(sample_repo.path), sample_repo.head, dest, files)

        assert (dest / "tests/unit/new_package/__init__.py").read_bytes() == b""
        assert (dest / "tests/unit/new_package/test_new.py").is_file()

    def test_build_tree_with_no_files_equals_the_base_commit(self, sample_repo, tmp_path):
        from src.fixer.gitio import Git
        from src.fixer.workspace import build_tree

        dest = tmp_path / "clean-tree"

        build_tree(Git(sample_repo.path), sample_repo.head, dest, {})

        assert {path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()} == set(FILES)
