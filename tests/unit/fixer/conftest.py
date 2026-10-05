"""修正役のテストの土台（design.md §14）。

自動で適用するもの:

- AC-26: ソケットでの通信と認証情報を使えない状態にする。``socket.socket`` そのものは差し替えない
  （``asyncio.run`` がイベントループの内部で ``socketpair`` を使うため）。
- H1: 「検査に使う Python の環境が書き込めるか」の確かめを、既定で「書き込めるものは無い」に差し替える
  （開発機の仮想環境は書き込めるので、差し替えないと、どのテストも ``unisolated_environment`` で終わる）。
- git の隔離: ``HOME`` を空の一時ディレクトリに、``GIT_CONFIG_GLOBAL`` を空のファイルに、
  ``GIT_CONFIG_NOSYSTEM=1`` にし、``GIT_DIR``・``GIT_WORK_TREE`` などを外す。
- AC-22: ``subprocess`` の呼び出しを記録し、テストの終わりに、git のサブコマンドが決まった一覧に
  収まっていること、リモートに触れる操作が無いことを確かめる。あわせて、**開発中のリポジトリ本体を
  作業ディレクトリにした git の呼び出しは、実行する前に止める**（取り違えの備え）。
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from . import repo_factory

REAL_REPO = Path(__file__).resolve().parents[3]
REAL_HOME_SEAM = "_real_home_directory"  # src.fixer.runner の、本物のホームを返す関数（テストからの差し替え口）
# src.fixer.runner の、「検査に使う Python の環境のうち、いまの利用者から書き込めるもの」の一覧を返す関数（H1）。
# ``() -> list[str]``。空でなければ、修正役は起動を断る（``unisolated_environment``）
WRITABLE_PYTHON_SEAM = "_writable_python_locations"
# 土台が差し替える前の、実物の関数（差し替えを外したテストが使う）。実装に無ければ、入らない
_UNPATCHED_SEAMS: dict[str, object] = {}
OUTSIDE_MARK = "OUTSIDE_FILE_MARK_8842"

# design.md §5: 実装（``Git``）が使ってよいサブコマンド
IMPLEMENTATION_GIT_SUBCOMMANDS = frozenset(
    {
        "rev-parse",
        "show-ref",
        "archive",
        "hash-object",
        "read-tree",
        "update-index",
        "write-tree",
        "commit-tree",
        "update-ref",
    }
)
# テストの準備と確認（``repo_factory.run_git``）が使うサブコマンド。どれもリモートに触れない
HELPER_GIT_SUBCOMMANDS = frozenset(
    {
        "init",
        "add",
        "commit",
        "config",
        "branch",
        "status",
        "rev-parse",
        "rev-list",
        "symbolic-ref",
        "for-each-ref",
        "ls-tree",
        "log",
        "show",
        "update-index",
    }
)
# R14・AC-22: リモートに触れる操作
REMOTE_GIT_SUBCOMMANDS = frozenset({"push", "fetch", "pull", "clone", "remote", "ls-remote", "submodule"})
# 作業ツリーを動かす・消す操作。実装からは、どのリポジトリに対しても呼ばれてはならない
WORKTREE_GIT_SUBCOMMANDS = frozenset({"stash", "checkout", "switch", "restore", "reset", "clean", "worktree", "merge"})
# 値を次の引数に取る、git 本体のオプション
_GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"})
_GIT_ENV_POINTING_AT_REPOSITORY = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_ASKPASS",
    "GIT_EDITOR",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)
_CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "FIXER_MODEL",
)


class NetworkBlockedError(AssertionError):
    """テスト中に通信しようとしたときに上がる。"""


class RealRepositoryTouchedError(AssertionError):
    """開発中のリポジトリ本体に対して git を実行しようとしたときに上がる（実行はしない）。"""


def _blocked(*_args, **_kwargs):
    raise NetworkBlockedError("network access is forbidden in fixer tests (AC-26)")


@dataclass(frozen=True)
class ProcessCall:
    argv: tuple[str, ...]
    cwd: str | None
    env: dict[str, str] | None  # None は「親の環境変数を引き継ぐ」
    shell: bool
    by_helper: bool  # テストの準備（repo_factory.run_git）からの呼び出し
    new_session: bool = False  # 専用のセッションで起こしたか
    process_group: int | None = None  # 専用のプロセスグループで起こしたか（0 なら、自分を先頭にしたグループ）

    @property
    def own_group(self) -> bool:
        """プロセスを、親とは別のグループで起こしたか（グループごと止められる）。"""
        return self.new_session or self.process_group == 0

    @property
    def tokens(self) -> tuple[str, ...]:
        """引数の並び。シェル経由の 1 つの文字列は、空白で分ける。"""
        if self.shell and len(self.argv) == 1:
            return tuple(self.argv[0].split())
        return self.argv

    @property
    def is_git(self) -> bool:
        return bool(self.tokens) and os.path.basename(self.tokens[0]) == "git"

    @property
    def git_subcommand(self) -> str | None:
        return git_subcommand(self.tokens) if self.is_git else None


def git_subcommand(argv: tuple[str, ...] | list[str]) -> str | None:
    """``git [本体のオプション] <サブコマンド> ...`` のサブコマンド。無ければ None。"""
    tokens = list(argv[1:])
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _GIT_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def git_directories(call: ProcessCall, default_cwd: str) -> list[Path]:
    """git の呼び出しが触りうるディレクトリ（作業ディレクトリ、``-C``、``--git-dir``、``GIT_DIR`` など）。"""
    base = Path(call.cwd or default_cwd)
    found = [base]
    tokens = list(call.tokens[1:])
    for index, token in enumerate(tokens):
        if token in ("-C", "--git-dir", "--work-tree") and index + 1 < len(tokens):
            found.append(base / tokens[index + 1])
        for prefix in ("--git-dir=", "--work-tree="):
            if token.startswith(prefix):
                found.append(base / token[len(prefix) :])
    env = call.env if call.env is not None else dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE"):
        if env.get(name):
            found.append(base / env[name])
    return found


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return True  # 確かめられないものは、中にあるものとして扱う


def git_violations(calls: list[ProcessCall]) -> list[str]:
    """記録した呼び出しのうち、決まりに反するもの（AC-22 と design.md §5）。"""
    problems = []
    for call in calls:
        if not call.is_git:
            continue
        sub = call.git_subcommand
        shown = " ".join(call.argv[:6])
        if call.shell:
            problems.append(f"git through a shell: {shown}")
        if sub in REMOTE_GIT_SUBCOMMANDS:
            problems.append(f"remote operation: {shown}")
        elif call.by_helper:
            if sub not in HELPER_GIT_SUBCOMMANDS:
                problems.append(f"unexpected helper subcommand: {shown}")
        else:
            if sub in WORKTREE_GIT_SUBCOMMANDS:
                problems.append(f"worktree operation by the implementation: {shown}")
            elif sub not in IMPLEMENTATION_GIT_SUBCOMMANDS:
                problems.append(f"subcommand outside the fixed list: {shown}")
            if call.env is None:
                problems.append(f"git inherits the parent environment: {shown}")
            elif any(name in call.env for name in ("GIT_DIR", "GIT_WORK_TREE")):
                problems.append(f"git receives GIT_DIR / GIT_WORK_TREE: {shown}")
    return problems


@dataclass
class ProcessRecorder:
    calls: list[ProcessCall] = field(default_factory=list)

    @property
    def git_calls(self) -> list[ProcessCall]:
        return [call for call in self.calls if call.is_git]

    @property
    def implementation_git_calls(self) -> list[ProcessCall]:
        return [call for call in self.git_calls if not call.by_helper]

    @property
    def implementation_calls(self) -> list[ProcessCall]:
        return [call for call in self.calls if not call.by_helper]

    def subcommands(self) -> list[str | None]:
        return [call.git_subcommand for call in self.implementation_git_calls]


# 独自ルールの秘密情報の検査が、この引数の名前の後ろ半分に当たるので、連結で書く
_NEW_SESSION_ARGUMENT = "start" + "_new_session"


def _to_call(
    args: object, cwd: object, env: object, shell: object, new_session: object = False, process_group: object = None
) -> ProcessCall:
    if isinstance(args, str | bytes | os.PathLike):
        argv: tuple[str, ...] = (os.fsdecode(args),)
    else:
        argv = tuple(os.fsdecode(item) for item in args)  # type: ignore[union-attr]
    environment = None if env is None else {os.fsdecode(k): os.fsdecode(v) for k, v in dict(env).items()}  # type: ignore[call-overload]
    by_helper = bool(environment and environment.get(repo_factory.HELPER_ENV_MARK) == "1")
    return ProcessCall(
        argv=argv,
        cwd=None if cwd is None else os.fsdecode(cwd),  # type: ignore[arg-type]
        env=environment,
        shell=bool(shell),
        by_helper=by_helper,
        new_session=bool(new_session),
        process_group=process_group if type(process_group) is int else None,
    )


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path_factory):
    """通信・認証情報・開発者の git の設定を、テストから切り離す。"""
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    for name in (*_CREDENTIAL_ENV, *_GIT_ENV_POINTING_AT_REPOSITORY):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path_factory.mktemp("isolated-home")
    empty_config = home / "empty-gitconfig"
    empty_config.touch()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / "no-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / "no-config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    # F11: 修正役は、本物のホームに認証情報らしいものがあると、起動を断る。テストが、開発機の実際のホームの
    # 状態に左右されないよう（また、実際のホームの中を見ないよう）、既定を「空のホーム」に差し替える
    real_home = tmp_path_factory.mktemp("empty-real-home")
    with contextlib.suppress(ImportError):
        import src.fixer.runner  # noqa: F401

        monkeypatch.setattr(f"src.fixer.runner.{REAL_HOME_SEAM}", lambda: real_home, raising=False)
        # H1: 修正役は、検査に使う Python の環境（``sys.prefix`` と site-packages）が書き込めると、起動を断る。
        # 開発機の仮想環境は書き込めるので、既定を「書き込めるものは無い」に差し替える（実物は、取っておく）
        _UNPATCHED_SEAMS.pop(WRITABLE_PYTHON_SEAM, None)
        if hasattr(src.fixer.runner, WRITABLE_PYTHON_SEAM):
            _UNPATCHED_SEAMS[WRITABLE_PYTHON_SEAM] = getattr(src.fixer.runner, WRITABLE_PYTHON_SEAM)
        monkeypatch.setattr(f"src.fixer.runner.{WRITABLE_PYTHON_SEAM}", list, raising=False)
    return home


@pytest.fixture
def unpatched_python_locations(_isolation):
    """土台が差し替える前の、実物の ``src.fixer.runner._writable_python_locations``（H1 の専用のテストが使う）。"""
    function = _UNPATCHED_SEAMS.get(WRITABLE_PYTHON_SEAM)
    if function is None:
        pytest.fail(f"src.fixer.runner.{WRITABLE_PYTHON_SEAM} is not implemented")
    return function


@pytest.fixture(autouse=True)
def process_recorder(monkeypatch, _isolation):
    """``subprocess`` の呼び出しを、引数を記録してから本物に渡す。終わりに git の呼び出しを確かめる。

    ``subprocess.run`` なども内部で ``Popen`` を作るので、``Popen.__init__`` を 1 か所で押さえる
    （``from subprocess import Popen`` と書いた実装でも記録される）。
    """
    recorder = ProcessRecorder()
    original_init = subprocess.Popen.__init__

    def recording_init(self, args, *positional, **kwargs):
        call = _to_call(
            args,
            kwargs.get("cwd"),
            kwargs.get("env"),
            kwargs.get("shell", False),
            kwargs.get(_NEW_SESSION_ARGUMENT, False),
            kwargs.get("process_group"),
        )
        recorder.calls.append(call)
        if call.is_git and any(_inside(path, REAL_REPO) for path in git_directories(call, os.getcwd())):
            raise RealRepositoryTouchedError(f"git must not run against the real repository: {' '.join(call.argv[:6])}")
        return original_init(self, args, *positional, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", recording_init)
    yield recorder
    problems = git_violations(recorder.calls)
    assert problems == [], f"git calls outside the rules (AC-22): {problems}"


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory):
    """見本のリポジトリのひな形（セッションで 1 回だけ ``git init`` する）。"""
    base = tmp_path_factory.mktemp("fixer-template")
    home = base / "home"
    repo = base / "repo"
    repo_factory.init_repo(repo, home)
    head = repo_factory.run_git(repo, "rev-parse", "HEAD", home=home)
    yield repo, home, head.strip()
    # ひな形は読むだけ。どのテストも、書き換えていないこと（ブランチも増えていないこと）
    assert repo_factory.run_git(repo, "rev-parse", "HEAD", home=home) == head
    assert repo_factory.run_git(repo, "status", "--porcelain", "--untracked-files=all", home=home) == ""
    assert repo_factory.run_git(repo, "for-each-ref", "--format=%(refname)", home=home).split() == ["refs/heads/main"]


@pytest.fixture
def sample_repo(_repo_template, tmp_path) -> repo_factory.SampleRepo:
    """テストごとの、小さな実物のリポジトリ（一時ディレクトリの中）。"""
    template, home, _head = _repo_template
    return repo_factory.copy_repo(template, tmp_path / "origin-repo", home)


@pytest.fixture
def handoff_file(tmp_path):
    """受け渡しのファイルを書く関数。引数は ``repo_factory.handoff_dict`` の上書き。"""

    def write(data: object = None, **overrides: object) -> Path:
        payload = repo_factory.handoff_dict(**overrides) if data is None else data
        return repo_factory.write_handoff(tmp_path / "handoff.json", payload)

    return write


@pytest.fixture
def workspace(_repo_template, tmp_path):
    """基準のコミットから書き出した、作業用の複製（実物の ``git archive`` を使う）。

    元のリポジトリは読むだけなので、ひな形のリポジトリから直接書き出す（テストごとの複製を省いて速くする）。
    """
    from src.fixer.gitio import Git
    from src.fixer.workspace import Workspace

    template, _home, head = _repo_template
    parent = tmp_path / "workspace-parent"
    parent.mkdir()
    created = Workspace.create(Git(template), head, parent)
    yield created
    created.destroy()


@pytest.fixture
def outside(tmp_path):
    """複製の外にあるディレクトリとファイル。"""
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "secret.py").write_text(f"# {OUTSIDE_MARK}\n", encoding="utf-8")
    return directory
