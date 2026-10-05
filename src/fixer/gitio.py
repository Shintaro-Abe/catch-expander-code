"""git の操作。決まったサブコマンドだけを、シェルを介さずに呼ぶ。SDK には依存しない。

設計詳細: `.steering/20261004-execution-fixer/design.md` §5

モデルには git を触らせない。ここが呼ぶのは、読み取りと、作業ツリー・現在のブランチ・インデックスに
触れない低レベルの操作だけで、リモートに触れるものは一覧に無い。環境変数は、親から引き継がず、
決まったものだけを渡す。

修正役が走らせたテストのコードは、元のリポジトリの `.git` に書ける。置かれたフックや設定を、修正役自身が
実行しないよう、すべての呼び出しでフックと fsmonitor を無効にし、全体設定・システムの設定を読まない
（設計 §20 の F2）。リポジトリの中の設定とフックが実行の間に変わっていないかは、`repository_fingerprint` で比べる。
置換参照（`refs/replace/`）を読まず、部分クローンの足りないオブジェクトを取りに行かない（設計 §21 の H2・H3）。
コミットを作るときは、作ったツリーが「基準のツリー＋渡された中身」と一致することを、参照を作る前に確かめる。
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

logger = logging.getLogger("catch-expander-fixer")

# 使ってよいサブコマンド。ここに無いものは、プロセスを起こす前に断る
ALLOWED_SUBCOMMANDS = frozenset(
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
GIT_TIMEOUT_SECONDS = 60.0
FALLBACK_IDENTITY_NAME = "catch-expander-fixer"
FALLBACK_IDENTITY_EMAIL = FALLBACK_IDENTITY_NAME + "@" + "localhost"
BRANCH_REF_PREFIX = "refs/heads/"
FILE_MODE = "100644"

# 数字は [0-9] と書く。検証は fullmatch で行う
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_ZERO_SHA = "0" * 40
NULL_DEVICE = "/dev/null"
# すべての呼び出しで、サブコマンドの前に `-c` で渡す設定。リポジトリの中の設定より優先される。
# フックを起こさない（`update-ref` は reference-transaction フックを起こす）。fsmonitor のコマンドを起こさない。
# 利用者ごとの属性ファイル（`~/.config/git/attributes`）を読まない
_FIXED_CONFIG = (f"core.hooksPath={NULL_DEVICE}", "core.fsmonitor=false", f"core.attributesFile={NULL_DEVICE}")
# 作業用ブランチの名前の形（`fix/diagnosed-<実行 ID>`）。コミットを載せられるのは、この形の名前だけ
_WORK_BRANCH_PATTERN = re.compile(r"fix/diagnosed-exec-[0-9]{14}-[0-9a-f]{8}")
# 実行の間に変わっていないかを確かめる、リポジトリの中の設定とフック（git の共通ディレクトリからの相対）
_FINGERPRINTED_PATHS = ("config", "config.worktree", "hooks", "info/attributes")
_ARCHIVE_ATTRIBUTES = ("export-ignore", "export-subst")
_ATTRIBUTES_FILE_NAME = ".gitattributes"
_MAX_ATTRIBUTES_BYTES = 1_000_000
_INDEX_SIGNATURE = b"DIRC"
_INDEX_VERSION = 2
_INDEX_ENTRY_HEADER = struct.Struct(">10I20sH")  # 時刻など 10 個、オブジェクト名、フラグ
_INDEX_FILE_MODE = 0o100644  # `FILE_MODE` を、インデックスの中の数にしたもの
_INDEX_NAME_LENGTH_MASK = 0x0FFF
_GITLINK_MODE = 0o160000
_READ_CHUNK_BYTES = 1 << 16
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*")
_MAX_BRANCH_CHARS = 200
_FILE_PATH_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
_MAX_REV_CHARS = 200
_MAX_LOGGED_STDERR_CHARS = 500


class GitError(Exception):
    """git の操作に失敗した。文面は決まった形だけ（git の出力や、渡された値を入れない）。"""


class Git:
    """リポジトリ 1 つに対する git の操作。"""

    def __init__(self, repo: Path) -> None:
        self._repo = Path(repo)

    def _run(
        self,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        extra_env: Mapping[str, str] | None = None,
        config: Sequence[str] = (),
        ok_codes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        """git を 1 回呼ぶ。`args` の先頭は、決まった一覧にあるサブコマンドでなければならない。

        Args:
            args: サブコマンドを先頭にした引数の list。
            stdin: 標準入力に渡すバイト列。
            extra_env: 決まった環境変数に足すもの（一時的なインデックス、作者の指定）。
            config: サブコマンドの前に `-c` で渡す設定（コードが決めた値だけ）。
            ok_codes: 成功とみなす終了コード。

        Raises:
            GitError: 一覧にないサブコマンド、起動の失敗、時間切れ、成功でない終了コード。
        """
        if not isinstance(args, list | tuple) or not args or not all(isinstance(item, str) for item in args):
            raise GitError("git arguments must be a non-empty list of strings")
        subcommand = args[0]
        if subcommand not in ALLOWED_SUBCOMMANDS:
            raise GitError("git subcommand is not in the fixed list")
        command = ["git"]
        for item in (*_FIXED_CONFIG, *config):
            command.extend(["-c", item])
        command.extend(args)
        try:
            completed = subprocess.run(  # noqa: S603 - 引数はコードが組み立てた list。シェルは介さない
                command,
                cwd=str(self._repo),
                env=self._environment(extra_env),
                input=stdin,
                stdin=subprocess.DEVNULL if stdin is None else None,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {subcommand} timed out") from e
        except OSError as e:
            raise GitError(f"git {subcommand} could not be started") from e
        if completed.returncode not in ok_codes:
            logger.debug(
                "git %s exited with %d: %s",
                subcommand,
                completed.returncode,
                completed.stderr.decode("utf-8", errors="replace")[:_MAX_LOGGED_STDERR_CHARS],
            )
            raise GitError(f"git {subcommand} exited with {completed.returncode}")
        return completed

    @staticmethod
    def _environment(extra_env: Mapping[str, str] | None) -> dict[str, str]:
        """git に渡す環境変数。親の `GIT_DIR`・`GIT_WORK_TREE` や秘密の値を引き継がない。"""
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            # 開発者の全体設定（core.hooksPath など）と、システムの設定を読ませない
            "GIT_CONFIG_GLOBAL": NULL_DEVICE,
            "GIT_CONFIG_NOSYSTEM": "1",
            # 置換参照（refs/replace/）を読ませない。置かれると、基準のコミットを読むつもりで、別のツリーを読む
            "GIT_NO_REPLACE_OBJECTS": "1",
            # 部分クローンで、足りないオブジェクトを取りに行かせない（通信と、設定のコマンドの実行を避ける）
            "GIT_NO_LAZY_FETCH": "1",
        }
        home = os.environ.get("HOME")
        if home:
            env["HOME"] = home
        if extra_env:
            env.update(extra_env)
        return env

    def toplevel(self) -> Path:
        """リポジトリの作業ツリーの根を返す。リポジトリでなければ GitError。"""
        completed = self._run(["rev-parse", "--show-toplevel"])
        text = completed.stdout.decode("utf-8", errors="replace").strip()
        if not text:
            raise GitError("git rev-parse returned no top-level directory")
        return Path(text)

    def resolve_commit(self, rev: str) -> str:
        """`rev` をコミットの SHA（40 桁）に解決する。解決できなければ GitError。"""
        # `-` で始まる値は、オプションとして読まれないよう、git を起こす前に断る
        if not isinstance(rev, str) or not rev or len(rev) > _MAX_REV_CHARS or rev.startswith("-") or "\x00" in rev:
            raise GitError("revision has an invalid form")
        completed = self._run(["rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"])
        sha = completed.stdout.decode("ascii", errors="replace").strip()
        if _SHA_PATTERN.fullmatch(sha) is None:
            raise GitError("git rev-parse returned an unexpected value")
        return sha

    def common_dir(self) -> Path:
        """git の共通ディレクトリ（ふつうは `<repo>/.git`）を返す。"""
        completed = self._run(["rev-parse", "--git-common-dir"])
        text = completed.stdout.decode("utf-8", errors="surrogateescape").rstrip("\n")
        if not text:
            raise GitError("git rev-parse returned no common directory")
        return self._repo / text

    def branch_exists(self, name: str) -> bool:
        """`refs/heads/<name>` があれば True。先の無いシンボリックな参照でも True。"""
        ref = _branch_ref(name)
        completed = self._run(["show-ref", "--verify", "--quiet", ref], ok_codes=(0, 1))
        if completed.returncode == 0:
            return True
        # `show-ref` は、先の無いシンボリックな参照を「無い」と答える。参照のファイルそのものがあるかも見る
        return os.path.lexists(self.common_dir() / ref)

    def repository_fingerprint(self) -> str:
        """リポジトリの中の設定とフック（一覧・種類・モード・中身）の要約を返す。

        修正役が走らせたテストのコードは、元のリポジトリの `.git` に書ける。実行の前と、git の書き込みの
        直前とで、この値を比べる。読むだけで、何も書かない。

        Raises:
            GitError: 共通ディレクトリを得られない、読めない。
        """
        common = self.common_dir()
        digest = hashlib.sha256()
        try:
            for name in _FINGERPRINTED_PATHS:
                for record in _describe(common / name, name):
                    digest.update(record.encode("utf-8", errors="surrogateescape") + b"\0")
        except OSError as e:
            raise GitError("repository configuration could not be read") from e
        return digest.hexdigest()

    def tree_entries(self, commit: str) -> dict[str, int]:
        """コミットのツリーのすべてのパスを、パス → モードで返す（属性の影響を受けない一覧）。"""
        return {path: mode for path, (mode, _name) in self.tree_objects(commit).items()}

    def tree_objects(self, treeish: str) -> dict[str, tuple[int, str]]:
        """コミット（またはツリー）のすべてのパスを、パス → （モード, オブジェクト名）で返す。

        一時的なインデックスにツリーを読み込み、そのファイルを読む（一覧を出すサブコマンドを、決まった一覧に
        足さずに済ませる）。
        """
        if not isinstance(treeish, str) or _SHA_PATTERN.fullmatch(treeish) is None:
            raise GitError("commit must be a full SHA")
        index_directory = tempfile.mkdtemp(prefix="fixer-index-")
        try:
            index = Path(index_directory, "index")
            self._run(
                ["read-tree", treeish],
                extra_env={"GIT_INDEX_FILE": str(index)},
                config=(f"index.version={_INDEX_VERSION}", "core.splitIndex=false"),
            )
            return _parse_index(index.read_bytes())
        except (OSError, struct.error, ValueError) as e:
            raise GitError("temporary index could not be read") from e
        finally:
            shutil.rmtree(index_directory, ignore_errors=True)

    def has_archive_attributes(self, commit: str) -> bool:
        """書き出しが、コミットの中身と食い違う属性（`export-ignore`・`export-subst`）があれば True。

        `git archive` は、これらの属性に従う。`export-ignore` の付いたファイルは複製に現れず、そのパスへの
        「新しいファイル」が、コミットでは既存のファイルの上書きになる。見るのは、コミットの中の
        `.gitattributes`（どの階層でも）、`$GIT_DIR/info/attributes`、書き出しとツリーの一覧の食い違い。
        作業ツリーの未コミットの `.gitattributes` は見ない。読み切れない大きさ（`_MAX_ATTRIBUTES_BYTES` を
        超える）の `.gitattributes` があるコミットも、扱わない（上限より後ろの行を、見落とさないため）。
        """
        expected = {path for path, mode in self.tree_entries(commit).items() if mode != _GITLINK_MODE}
        exported: set[str] = set()
        try:
            with tarfile.open(fileobj=io.BytesIO(self.archive_bytes(commit)), mode="r:") as archive:
                for member in archive:
                    if member.isdir():
                        continue
                    exported.add(member.name)
                    if member.isfile() and member.name.rsplit("/", 1)[-1] == _ATTRIBUTES_FILE_NAME:
                        if member.size > _MAX_ATTRIBUTES_BYTES:
                            return True
                        file = archive.extractfile(member)
                        if file is not None and _sets_archive_attribute(file.read(_MAX_ATTRIBUTES_BYTES)):
                            return True
            info = self.common_dir() / "info" / "attributes"
            if info.is_file() and _sets_archive_attribute(info.read_bytes()[:_MAX_ATTRIBUTES_BYTES]):
                return True
        except (tarfile.TarError, OSError) as e:
            raise GitError("exported tree could not be inspected") from e
        # 一覧が食い違うのは、属性のファイルそのものが書き出しから外されている場合など
        return exported != expected

    def archive_bytes(self, commit: str) -> bytes:
        """コミットの中身の書き出し（`git archive` の出力。tar）を返す。

        `git archive` は、リポジトリの設定のフィルター（`filter.<名前>.smudge`）のコマンドを実行する。
        呼ぶのは、モデルが書いたコードが走る前だけにする（その後は、ここで得たバイト列から組み立てる）。
        """
        if not isinstance(commit, str) or _SHA_PATTERN.fullmatch(commit) is None:
            raise GitError("commit must be a full SHA")
        return self._run(["archive", "--format=tar", commit]).stdout

    def export_tree(self, commit: str, dest: Path) -> None:
        """コミットの中身を `dest` に書き出す。`.git` は含まれない。"""
        extract_archive(self.archive_bytes(commit), dest)

    def create_branch_commit(
        self,
        *,
        base: str,
        branch: str,
        message: str,
        files: Mapping[str, bytes],
        expected_base: Mapping[str, tuple[int, str]] | None = None,
    ) -> str:
        """`base` を親にしたコミットを 1 つ作り、新しいブランチに載せる。新しいコミットの SHA を返す。

        作業ツリー・現在のブランチ・リポジトリのインデックスには触れない（一時的なインデックスを使う）。
        `commit-tree` と `update-ref` は、コミット時のフックを起こさない。既にあるブランチは上書きしない。
        参照を作る前に、作ったツリーを読み直し、「基準のツリー＋渡された中身」と、オブジェクト名まで一致する
        ことを確かめる（基準を読むつもりで、別のツリーを読まされていないか。設計 §21 の H2）。

        Args:
            base: 親にするコミット。
            branch: 新しいブランチの名前（`refs/heads/` は付けない）。`fix/diagnosed-<実行 ID>` の形だけ。
            message: コミットメッセージ。
            files: 相対パス → 中身。`base` のツリーに、この中身を上書きしたものがコミットに入る。
            expected_base: 基準のツリーの記録（`tree_objects` の結果）。モデルが書いたコードが走る前に取ったものを
                渡す。None なら、ここで読む（コミットの時点で読み直した基準なので、置換には気づけない）。

        Raises:
            GitError: 親を解決できない、ブランチが既にある、パスの形が不正、ツリーの食い違い、git の失敗。
        """
        # `HEAD` や `main` などの名前で参照を作れないよう、プロセスを起こす前に、名前の形を確かめる
        if not isinstance(branch, str) or _WORK_BRANCH_PATTERN.fullmatch(branch) is None:
            raise GitError("branch name is not a work branch name")
        ref = _branch_ref(branch)
        parent = self.resolve_commit(base)
        for path, content in files.items():
            valid_path = isinstance(path, str) and _FILE_PATH_PATTERN.fullmatch(path) is not None
            if not valid_path or not isinstance(content, bytes):
                raise GitError("file path or content has an invalid form")
        if self.branch_exists(branch):
            raise GitError("branch already exists")
        base_objects = dict(expected_base) if expected_base is not None else self.tree_objects(parent)
        # 中身のオブジェクト名は、git の答えに頼らず、ここで計算する
        expected_tree = {**base_objects, **{path: (_INDEX_FILE_MODE, _blob_name(data)) for path, data in files.items()}}

        index_directory = tempfile.mkdtemp(prefix="fixer-index-")
        try:
            # フィルターを通さず、渡された中身そのものをオブジェクトにする（--path は付けない）
            blobs = {
                path: self._sha_of(self._run(["hash-object", "-w", "--stdin"], stdin=content))
                for path, content in files.items()
            }
            index_env = {"GIT_INDEX_FILE": str(Path(index_directory, "index"))}
            self._run(["read-tree", parent], extra_env=index_env)
            for path, blob in blobs.items():
                self._run(["update-index", "--add", "--cacheinfo", f"{FILE_MODE},{blob},{path}"], extra_env=index_env)
            tree = self._sha_of(self._run(["write-tree"], extra_env=index_env))
        finally:
            shutil.rmtree(index_directory, ignore_errors=True)

        # 作ったツリーが、基準＋承認した変更と違えば、コミットも参照も作らない
        if self.tree_objects(tree) != expected_tree:
            logger.warning("Fix was not committed: the written tree differs from the base tree plus the approved files")
            raise GitError("written tree does not match the base tree plus the given files")
        commit = self._commit_tree(tree, parent, message)
        # 古い値に全ゼロを渡す: 参照が既にあれば失敗する（間に作られたブランチも上書きしない）
        self._run(["update-ref", "--no-deref", ref, commit, _ZERO_SHA])
        return commit

    def _commit_tree(self, tree: str, parent: str, message: str) -> str:
        """コミットのオブジェクトを作る。作者は設定のものを使い、無ければ決まった名前にする。"""
        args = ["commit-tree", "--no-gpg-sign", tree, "-p", parent]
        stdin = message.encode("utf-8")
        try:
            # useConfigOnly: 設定に名前・メールが無ければ、ホスト名などから推測せずに失敗する
            completed = self._run(args, stdin=stdin, config=("user.useConfigOnly=true",))
        except GitError:
            identity = {
                "GIT_AUTHOR_NAME": FALLBACK_IDENTITY_NAME,
                "GIT_AUTHOR_EMAIL": FALLBACK_IDENTITY_EMAIL,
                "GIT_COMMITTER_NAME": FALLBACK_IDENTITY_NAME,
                "GIT_COMMITTER_EMAIL": FALLBACK_IDENTITY_EMAIL,
            }
            completed = self._run(args, stdin=stdin, extra_env=identity)
        return self._sha_of(completed)

    @staticmethod
    def _sha_of(completed: subprocess.CompletedProcess[bytes]) -> str:
        sha = completed.stdout.decode("ascii", errors="replace").strip()
        if _SHA_PATTERN.fullmatch(sha) is None:
            raise GitError("git returned an unexpected object name")
        return sha


def extract_archive(data: bytes, dest: Path) -> None:
    """`Git.archive_bytes` で得た書き出しを、`dest` に展開する。git を呼ばない。

    Python の `tarfile` で展開する（`filter="data"`。絶対パス・`..`・書き出し先の外を指すリンク・
    特殊なファイルを通さない）。通せないものがあれば、失敗して終わる。

    Raises:
        GitError: 展開できなかった。
    """
    try:
        Path(dest).mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            # filter="data": 書き出し先の外に出るもの（絶対パス、..、外を指すリンク）と、特殊なファイルを断る
            archive.extractall(dest, filter="data")
    except (tarfile.TarError, OSError, ValueError) as e:
        raise GitError("exported tree could not be extracted") from e


def _blob_name(data: bytes) -> str:
    """中身のオブジェクト名（`git hash-object` が、フィルターなしで返す値）。"""
    # git のオブジェクト名の計算であって、安全のためのハッシュではない
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _branch_ref(name: str) -> str:
    """ブランチ名の形を確かめて、参照の名前にする。"""
    if (
        not isinstance(name, str)
        or len(name) > _MAX_BRANCH_CHARS
        or _BRANCH_PATTERN.fullmatch(name) is None
        or ".." in name
        or name.endswith((".lock", "."))
    ):
        raise GitError("branch name has an invalid form")
    return BRANCH_REF_PREFIX + name


def _describe(path: Path, name: str) -> list[str]:
    """パスの下のすべての項目を、「名前・種類・モード・中身の要約」の行にする（シンボリックリンクはたどらない）。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return [f"{name} missing"]
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return [f"{name} link {os.readlink(path)}"]
    if stat.S_ISDIR(info.st_mode):
        records = [f"{name} dir {mode:o}"]
        for child in sorted(os.listdir(path)):
            records.extend(_describe(path / child, f"{name}/{child}"))
        return records
    if not stat.S_ISREG(info.st_mode):
        return [f"{name} other {mode:o}"]
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
    return [f"{name} file {mode:o} {digest.hexdigest()}"]


def _parse_index(data: bytes) -> dict[str, tuple[int, str]]:
    """インデックスのファイル（版 2）から、パス → （モード, オブジェクト名）を読む。形が合わなければ ValueError。"""
    signature, version, count = struct.unpack_from(">4sII", data, 0)
    if signature != _INDEX_SIGNATURE or version != _INDEX_VERSION:
        raise ValueError("unexpected index format")
    entries: dict[str, tuple[int, str]] = {}
    offset = 12
    for _ in range(count):
        fields = _INDEX_ENTRY_HEADER.unpack_from(data, offset)
        mode, name, flags = fields[6], fields[10], fields[11]
        name_start = offset + _INDEX_ENTRY_HEADER.size
        length = flags & _INDEX_NAME_LENGTH_MASK
        # 名前が長いとき（4095 バイト以上）は、長さが入っていないので、NUL までを読む
        name_end = name_start + length if length < _INDEX_NAME_LENGTH_MASK else data.index(b"\0", name_start)
        entries[data[name_start:name_end].decode("utf-8", errors="surrogateescape")] = (mode, name.hex())
        # 各項目は、NUL を 1〜8 個足して、8 バイトの倍数にそろえてある
        offset = (
            name_start + ((name_end - name_start + _INDEX_ENTRY_HEADER.size) // 8 + 1) * 8 - _INDEX_ENTRY_HEADER.size
        )
    return entries


def _sets_archive_attribute(data: bytes) -> bool:
    """属性のファイルの中身に、`export-ignore`・`export-subst` を付ける行があれば True。コメント行は見ない。"""
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 先頭はパターン（引用符で囲めば、空白を含められる）。その後ろが属性
        closing = line.find('"', 1) if line.startswith('"') else -1
        attributes = line[closing + 1 :].split() if closing > 0 else line.split()[1:]
        for attribute in attributes:
            # `-名前`・`!名前` は、属性を外す指定
            if any(attribute == name or attribute.startswith(name + "=") for name in _ARCHIVE_ATTRIBUTES):
                return True
    return False
