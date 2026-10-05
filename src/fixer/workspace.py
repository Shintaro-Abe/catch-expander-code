"""作業用の複製、パスの検査、変更の取り出し、検査用の複製の組み立て。SDK には依存しない。

設計詳細: `.steering/20261004-execution-fixer/design.md` §6

複製は、基準のコミットを `git archive` で書き出した、`.git` の無いディレクトリ。モデルが渡すパスは、
呼び出しのたびに、複製の根からの相対パスとして解決し、複製の外・書き込めない場所を、その場で断る。
複製の中のファイルは、修正役が走らせたテストのコードが書き換えうるので、読むのは通常のファイルだけにする
（FIFO などを開いて、止まらないようにする）。大きさは、開いた後に確かめ、上限を超えるものは読まない。
複製の根そのものが、作ったときのディレクトリでなくなっていたら（リンクや、別のディレクトリに替えられた）、
複製には触れない（設計 §21 の H8・H9）。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from src.fixer.gates import (
    CHANGE_PATH_PATTERN,
    MAX_CHANGE_PATH_CHARS,
    PACKAGE_INIT_NAME,
    PYTHON_SUFFIX,
    TESTS_DIRECTORY,
    WRITABLE_TOP_DIRECTORIES,
    ChangeScope,
    is_test_file_name,
)
from src.fixer.gitio import Git, extract_archive
from src.fixer.models import FixLimits

GIT_DIRECTORY = ".git"
# 変更として数えないキャッシュ（ディレクトリの名前と、ファイルの拡張子）
IGNORED_DIRECTORY_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})
IGNORED_FILE_SUFFIX = ".pyc"
MAX_PATH_CHARS = 400
MAX_BASELINE_CONTENT_BYTES = 1_000_000  # これを超える元のファイルは、中身を持たない（行数を数えられない）
MAX_TOTAL_CHANGED_BYTES = 20_000_000  # 取り出す変更の中身の合計。超えたぶんは oversized にする
DEFAULT_MAX_FILE_BYTES = FixLimits().max_write_bytes

_KIND_FILE = "file"
_KIND_SYMLINK = "symlink"
_KIND_OTHER = "other"
_NEW_FILE_MODE = 0o644
_EXECUTABLE_OR_SPECIAL_BITS = 0o7111
_READ_CHUNK_BYTES = 1 << 16
_DRIVE_PATTERN = re.compile(r"[A-Za-z]:")


class PathDenied(Exception):
    """複製の外、または読み書きを認めていない場所を指すパス。文面は決まった形だけ。"""


class WorkspaceReplaced(PathDenied):
    """複製の根が、作ったときのディレクトリでなくなっている（リンクや、別のディレクトリに替えられた）。"""


class FileTooLarge(Exception):
    """読もうとしたファイルが、上限より大きい。"""


@dataclass(frozen=True)
class BaselineEntry:
    """基準の一覧の 1 件（書き出した直後の記録）。"""

    kind: str  # file / symlink
    mode: int
    size: int
    sha256: str  # ファイルは中身、シンボリックリンクは指す先の文字列
    content: bytes | None  # 元の中身。MAX_BASELINE_CONTENT_BYTES を超えるファイルと、シンボリックリンクは None


@dataclass(frozen=True)
class ChangeSet:
    """複製を基準の一覧と比べた結果。パスは、複製の根からの相対パス。"""

    added: dict[str, bytes]  # 新しい通常のファイル
    modified: dict[str, bytes]  # 中身が変わった通常のファイル
    deleted: tuple[str, ...]
    irregular: tuple[str, ...]  # シンボリックリンク、モードの変化、種類の変化、通常のファイルでないもの
    oversized: tuple[str, ...]  # 上限を超えるもの（中身は読まない）

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted or self.irregular or self.oversized)


def split_relative(rel: object) -> tuple[str, ...]:
    """相対パスを要素に分ける。複製の外を指しうる形（絶対パス、`..` など）なら PathDenied。"""
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_CHARS or "\x00" in rel:
        raise PathDenied("path has an invalid form")
    if rel.startswith("/") or "\\" in rel or _DRIVE_PATTERN.match(rel) is not None:
        raise PathDenied("path must be relative to the workspace")
    parts = tuple(rel.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise PathDenied("path has an empty, '.' or '..' segment")
    if parts[0].casefold() == GIT_DIRECTORY:
        raise PathDenied("path points into .git")
    return parts


def read_regular_file(path: Path, max_bytes: int) -> bytes | None:
    """通常のファイルなら中身を返す。通常のファイルでなければ（ディレクトリ、FIFO など）None。

    Raises:
        FileTooLarge: `max_bytes` を超える。
        OSError: 開けない（シンボリックリンクを含む）。
    """
    # O_NONBLOCK: FIFO を開いても待たない。O_NOFOLLOW: 末尾がシンボリックリンクなら開かない
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        # リンク数が 2 以上のファイル（ハードリンク）は、複製の外のファイルと中身を共有しうるので、読まない
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            return None
        if info.st_size > max_bytes:
            raise FileTooLarge
        data = bytearray()
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            data.extend(chunk)
            if len(data) > max_bytes:
                raise FileTooLarge
        return bytes(data)
    finally:
        os.close(descriptor)


def write_regular_file(path: Path, data: bytes) -> None:
    """ファイルを書く（無ければ作り、あれば全体を書き直す）。末尾がシンボリックリンクなら書かない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    with os.fdopen(os.open(path, flags, 0o644), "wb") as file:
        file.write(data)


def remove_tree(path: Path) -> None:
    """ディレクトリを中身ごと消す。シンボリックリンクはたどらない。例外を出さない。

    対象そのものがシンボリックリンク（や、ディレクトリでないもの）なら、それだけを消す。リンクの先の、
    権限も中身も変えない（テストのコードが、消す対象をリンクにすり替えていても、外に触れない）。
    """
    path = Path(path)
    if _remove_unless_directory(path):
        return
    try:
        shutil.rmtree(path)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    # テストのコードが権限を落としたディレクトリでも消せるよう、ディレクトリの権限を戻してやり直す
    pending = [path]
    while pending:
        directory = pending.pop()
        with contextlib.suppress(OSError):
            _restore_directory_permissions(directory)
            with os.scandir(directory) as entries:
                pending.extend(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))
    shutil.rmtree(path, ignore_errors=True)
    # 途中で、対象がリンクにすり替えられた場合（rmtree は、リンクを消さない）
    _remove_unless_directory(path)


def _remove_unless_directory(path: Path) -> bool:
    """本物のディレクトリなら False。それ以外（無い、リンク、ファイルなど）は、それだけを消して True。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISDIR(info.st_mode):
        return False
    with contextlib.suppress(OSError):
        os.unlink(path)  # リンクは、リンクそのものを消す（先はたどらない）
    return True


def _restore_directory_permissions(directory: Path) -> None:
    """本物のディレクトリ（リンクでない）であることを確かめてから、所有者の権限を戻す。

    Raises:
        OSError: ディレクトリでない（リンクを含む）、または権限を変えられない。
    """
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise NotADirectoryError("not a real directory")
    if os.chmod in os.supports_follow_symlinks:
        # 確かめた後でリンクにすり替えられても、リンクの先の権限は変えない
        os.chmod(directory, stat.S_IRWXU, follow_symlinks=False)
    else:
        os.chmod(directory, stat.S_IRWXU)


def _walk(root: Path, skip_directory: Callable[[str, str], bool]) -> Iterator[tuple[str, Path, os.stat_result]]:
    """根の下のすべての項目を、名前の順に返す（ディレクトリも返す。シンボリックリンクはたどらない）。"""
    pending = [("", root)]
    while pending:
        prefix, directory = pending.pop()
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            rel, path = prefix + name, directory / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            yield rel, path, info
            if stat.S_ISDIR(info.st_mode) and not skip_directory(rel, name):
                pending.append((rel + "/", path))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_file(path: Path, keep_bytes: int, expected_size: int | None = None) -> tuple[str, bytes | None] | None:
    """（中身の SHA-256, 中身）を返す。`keep_bytes` を超えるファイルは、中身を持たずに読み進める。

    通常のファイルでなければ None。`expected_size` を渡すと、開いた後の大きさがそれと違うファイルは、
    読まずに None を返す。読む量も、その大きさまでにする（読む間に伸ばされても、読み続けない）。
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        if expected_size is not None and info.st_size != expected_size:
            return None
        digest = hashlib.sha256()
        kept: bytearray | None = bytearray()
        total = 0
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            total += len(chunk)
            if expected_size is not None and total > expected_size:
                return None
            digest.update(chunk)
            if kept is not None:
                kept.extend(chunk)
                if len(kept) > keep_bytes:
                    kept = None
        return digest.hexdigest(), None if kept is None else bytes(kept)
    finally:
        os.close(descriptor)


def _opened_size(path: Path) -> int | None:
    """通常のファイルを開いた後の大きさ。通常のファイルでなければ None。中身は読まない。"""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        return info.st_size if stat.S_ISREG(info.st_mode) else None
    finally:
        os.close(descriptor)


def _is_cache_path(rel: str) -> bool:
    parts = rel.split("/")
    return rel.endswith(IGNORED_FILE_SUFFIX) or any(part in IGNORED_DIRECTORY_NAMES for part in parts[:-1])


def _snapshot(root: Path, keep_bytes: int = MAX_BASELINE_CONTENT_BYTES) -> dict[str, BaselineEntry]:
    """複製のすべてのパスについて（種類、モード、大きさ、SHA-256、中身）を記録する。

    通常のファイルでもシンボリックリンクでもないもの（FIFO など）は、種類を other として記録する。
    """
    baseline: dict[str, BaselineEntry] = {}
    for rel, path, info in _walk(root, lambda _rel, _name: False):
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISLNK(info.st_mode) and not stat.S_ISREG(info.st_mode):
            baseline[rel] = BaselineEntry(_KIND_OTHER, 0, 0, "", None)
        elif stat.S_ISLNK(info.st_mode):
            target = os.fsencode(os.readlink(path))
            baseline[rel] = BaselineEntry(_KIND_SYMLINK, 0, len(target), _digest(target), None)
        elif stat.S_ISREG(info.st_mode):
            digested = _digest_file(path, keep_bytes)
            if digested is not None:
                baseline[rel] = BaselineEntry(
                    _KIND_FILE, stat.S_IMODE(info.st_mode), info.st_size, digested[0], digested[1]
                )
    return baseline


def _directory_identity(path: Path) -> tuple[int, int] | None:
    """本物のディレクトリ（リンクでない）の（デバイス, inode）。それ以外は None。"""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None


def _deny_hard_link(info: os.stat_result) -> None:
    """リンク数が 2 以上の通常のファイルは、複製の外のファイルと中身を共有しうるので、読み書きを断る。"""
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise PathDenied("path is a hard link")


# 検査用の複製の中身: 相対パス → （種類, モード, 大きさ, SHA-256）
Manifest = dict[str, tuple[str, int, int, str]]


def expected_manifest(baseline: Mapping[str, BaselineEntry], files: Mapping[str, bytes]) -> Manifest:
    """検査用の複製が持つはずの中身（基準の一覧に、渡したファイルを上書きしたもの）。"""
    manifest: Manifest = {rel: (entry.kind, entry.mode, entry.size, entry.sha256) for rel, entry in baseline.items()}
    for rel, content in files.items():
        mode = baseline[rel].mode if rel in baseline else _NEW_FILE_MODE
        manifest[rel] = (_KIND_FILE, mode, len(content), _digest(content))
    return manifest


def tree_matches(root: Path, expected: Manifest) -> bool:
    """複製の中身（ファイルの一覧・種類・モード・中身と、ディレクトリの一覧）が、`expected` と一致するか。

    複製は、テストのコードが書き換えうる。違いを 1 つ見つけた時点でやめ、期待の大きさと違うファイルは、
    読まずに不一致とする（巨大なファイルに伸ばされても、全体を読まない）。
    """
    expected_directories = {
        "/".join(rel.split("/")[:depth]) for rel in expected for depth in range(1, rel.count("/") + 1)
    }
    seen = 0
    directories = 0
    try:
        for rel, path, info in _walk(Path(root), lambda _rel, _name: False):
            if stat.S_ISDIR(info.st_mode):
                if rel not in expected_directories:
                    return False
                directories += 1
                continue
            entry = expected.get(rel)
            if entry is None or not _entry_matches(path, info, entry):
                return False
            seen += 1
    except OSError:
        return False
    return seen == len(expected) and directories == len(expected_directories)


def _entry_matches(path: Path, info: os.stat_result, entry: tuple[str, int, int, str]) -> bool:
    """複製の中の 1 件（ディレクトリでないもの）が、期待の（種類, モード, 大きさ, SHA-256）と一致するか。"""
    kind, mode, size, sha256 = entry
    if stat.S_ISLNK(info.st_mode):
        return kind == _KIND_SYMLINK and _digest(os.fsencode(os.readlink(path))) == sha256
    if not stat.S_ISREG(info.st_mode) or kind != _KIND_FILE or stat.S_IMODE(info.st_mode) != mode:
        return False
    digested = _digest_file(path, 0, expected_size=size)
    return digested is not None and digested[0] == sha256


class Workspace:
    """基準のコミットから書き出した、作業用の複製。"""

    def __init__(self, root: Path, baseline: dict[str, BaselineEntry], archive: bytes | None = None) -> None:
        self.root = root
        self._resolved_root = root.resolve()
        # 根の実体（デバイスと inode）。根が、リンクや別のディレクトリに替えられていないかを、これで確かめる
        self._root_identity = _directory_identity(root)
        self._archive = archive
        self._baseline = baseline
        self._folded_baseline = frozenset(rel.casefold() for rel in baseline)
        self._scope = ChangeScope(baseline)
        # 基準の一覧にあるパスの、親ディレクトリ（キャッシュの名前でも、基準にあるものは歩く）
        self._baseline_directories = frozenset(
            "/".join(rel.split("/")[:depth]) for rel in baseline for depth in range(1, rel.count("/") + 1)
        )

    @classmethod
    def create(cls, git: Git, base: str, parent: Path) -> Workspace:
        """`parent` の下に新しいディレクトリを作り、基準のコミットを書き出して、基準の一覧を記録する。"""
        root = Path(tempfile.mkdtemp(prefix="workspace-", dir=parent))
        try:
            # 書き出しは、メモリに持っておく（検査用の複製は、git を呼ばずに、ここから組み立てる）
            archive = git.archive_bytes(base)
            extract_archive(archive, root)
            return cls(root, _snapshot(root), archive)
        except BaseException:
            remove_tree(root)
            raise

    @property
    def baseline(self) -> Mapping[str, BaselineEntry]:
        """基準の一覧（相対パス → 記録）。キーの集合は、基準のコミットの全ファイル。"""
        return MappingProxyType(self._baseline)

    @property
    def archive(self) -> bytes | None:
        """基準のコミットの書き出し（モデルが書いたコードが走る前に得たもの）。持っていなければ None。"""
        return self._archive

    def is_intact(self) -> bool:
        """複製の根が、作ったときのディレクトリのままなら True（リンクや、別のディレクトリに替えられていない）。"""
        return self._root_identity is not None and _directory_identity(self.root) == self._root_identity

    def _require_intact(self) -> None:
        if not self.is_intact():
            raise WorkspaceReplaced("workspace root is no longer the created directory")

    def resolve_read(self, rel: object) -> Path:
        """読んでよいパスを、シンボリックリンクをたどった先の実際のパスにして返す。

        Raises:
            PathDenied: 形が不正、または複製の外を指す（たどった先を含む）。
            FileNotFoundError: 複製の中に、その綴りのパスが無い。
        """
        self._require_intact()
        parts = split_relative(rel)
        resolved = self._resolve_inside(parts)
        if not self._exists_as_spelled(parts) or not resolved.exists():
            raise FileNotFoundError("path does not exist in the workspace")
        _deny_hard_link(os.lstat(resolved))
        return resolved

    def resolve_write(self, rel: object) -> Path:
        """書いてよいパスを返す。それ以外は PathDenied。

        書けるのは `src/`・`tests/` の下の `.py` だけ。対象外の場所、`tests/` の既存のファイル、
        シンボリックリンク越しのパスには書けない。
        """
        self._require_intact()
        parts = split_relative(rel)
        relative = "/".join(parts)
        name = parts[-1]
        if parts[0] not in WRITABLE_TOP_DIRECTORIES or len(parts) < 2:
            raise PathDenied("path is outside src/ and tests/")
        # G-1 と同じ形（決まった文字だけ、長さの上限つき）
        if len(relative) > MAX_CHANGE_PATH_CHARS or CHANGE_PATH_PATTERN.fullmatch(relative) is None:
            raise PathDenied("path is too long or has characters that are not allowed")
        if not name.endswith(PYTHON_SUFFIX) or name == PYTHON_SUFFIX:
            raise PathDenied("path is not a .py file")
        # 中身に依る規則（新しい __init__.py は空だけ）は、空の中身として確かめ、書くときに確かめ直す
        if self._scope.forbids(relative, b""):
            raise PathDenied("path is in a forbidden place")
        if parts[0] == TESTS_DIRECTORY:
            # 綴りの違い（大文字と小文字）で、既存のテストに届かないよう、畳んで比べる
            if relative.casefold() in self._folded_baseline:
                raise PathDenied("existing files under tests/ cannot be changed")
            if not (is_test_file_name(name) or name == PACKAGE_INIT_NAME):
                raise PathDenied("only test_*.py and __init__.py can be added under tests/")
        self._deny_links_and_aliases(parts)
        self._resolve_inside(parts)
        return self.root.joinpath(*parts)

    def forbids_content(self, relative: str, content: bytes) -> bool:
        """その中身を、そのパスに書くことを認めないなら True（G-2・G-3 で落ちる変更を、その場で断る）。"""
        if self._scope.forbids(relative, content):
            return True
        # tests/ の下に足せる __init__.py は、空のものだけ（テストの集め方や結果を変えるコードを足せないようにする）
        return (
            relative.startswith(TESTS_DIRECTORY + "/") and relative.endswith("/" + PACKAGE_INIT_NAME) and bool(content)
        )

    def relative_to_root(self, resolved: Path) -> str | None:
        """実際のパスを、複製の根からの相対パスにする。複製の外なら None。"""
        try:
            return resolved.relative_to(self._resolved_root).as_posix()
        except ValueError:
            return None

    def _resolve_inside(self, parts: tuple[str, ...]) -> Path:
        try:
            resolved = self.root.joinpath(*parts).resolve()
        except (OSError, RuntimeError) as e:
            raise PathDenied("path cannot be resolved") from e
        if not resolved.is_relative_to(self._resolved_root):
            raise PathDenied("path leads outside the workspace")
        return resolved

    def _exists_as_spelled(self, parts: tuple[str, ...]) -> bool:
        """各要素が、その綴りのまま親ディレクトリの一覧にあるか（大文字と小文字の違いを、別のパスとして扱う）。"""
        current = self.root
        for part in parts:
            try:
                if part not in os.listdir(current):
                    return False
            except OSError:
                return False
            current = current / part
        return True

    def _deny_links_and_aliases(self, parts: tuple[str, ...]) -> None:
        """パスの途中の要素と、書き込み先そのものを `lstat` で確かめる。"""
        current = self.root
        for index, part in enumerate(parts):
            path = current / part
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                return  # ここから先は、まだ無い（新しく作る）
            except OSError as e:
                raise PathDenied("path cannot be inspected") from e
            # 大文字と小文字を区別しないファイルシステムで、別の綴りが既存のファイルに届くのを断る
            try:
                spelled = part in os.listdir(current)
            except OSError as e:
                raise PathDenied("path cannot be inspected") from e
            if not spelled:
                raise PathDenied("path is another spelling of an existing path")
            if stat.S_ISLNK(info.st_mode):
                raise PathDenied("path goes through a symbolic link")
            is_last = index == len(parts) - 1
            if is_last and not stat.S_ISREG(info.st_mode):
                raise PathDenied("target is not a regular file")
            if is_last:
                _deny_hard_link(info)
            if not is_last and not stat.S_ISDIR(info.st_mode):
                raise PathDenied("path goes through something that is not a directory")
            current = path

    def _skip_directory(self, rel: str, name: str) -> bool:
        return name in IGNORED_DIRECTORY_NAMES and rel not in self._baseline_directories

    def changes(self, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> ChangeSet:
        """複製を歩いて、基準の一覧と比べる。キャッシュ（`__pycache__/` など）は無視する。

        Args:
            max_file_bytes: これを超える新しいファイル・変わったファイルは、中身を読まずに oversized にする。

        Raises:
            WorkspaceReplaced: 複製の根が、作ったときのディレクトリでなくなっている（外をたどらない）。
        """
        self._require_intact()
        added: dict[str, bytes] = {}
        modified: dict[str, bytes] = {}
        irregular: list[str] = []
        oversized: list[str] = []
        seen: set[str] = set()
        directories: set[str] = set()
        remaining = MAX_TOTAL_CHANGED_BYTES
        for rel, path, info in _walk(self.root, self._skip_directory):
            if stat.S_ISDIR(info.st_mode):
                directories.add(rel)
                continue
            entry = self._baseline.get(rel)
            if entry is None and _is_cache_path(rel):
                continue
            seen.add(rel)
            kind = self._classify(rel, path, info, entry, min(max_file_bytes, remaining))
            if isinstance(kind, bytes):
                (modified if entry is not None else added)[rel] = kind
                remaining -= len(kind)
            elif kind == "irregular":
                irregular.append(rel)
            elif kind == "oversized":
                oversized.append(rel)
        deleted = []
        for rel in self._baseline:
            if rel in seen:
                continue
            # ファイルがディレクトリに置き換わったものは、種類の変化として扱う
            (irregular if rel in directories else deleted).append(rel)
        return ChangeSet(added, modified, tuple(sorted(deleted)), tuple(sorted(irregular)), tuple(sorted(oversized)))

    @staticmethod
    def _classify(
        rel: str, path: Path, info: os.stat_result, entry: BaselineEntry | None, max_bytes: int
    ) -> bytes | str | None:
        """変わった中身（bytes）、"irregular"、"oversized"、または None（変わっていない）を返す。"""
        if stat.S_ISLNK(info.st_mode):
            with contextlib.suppress(OSError):
                target = os.fsencode(os.readlink(path))
                if entry is not None and entry.kind == _KIND_SYMLINK and entry.sha256 == _digest(target):
                    return None
            return "irregular"
        # 通常のファイルでないもの（FIFO など）は、開かない
        if not stat.S_ISREG(info.st_mode):
            return "irregular"
        mode = stat.S_IMODE(info.st_mode)
        if entry is None:
            if mode & _EXECUTABLE_OR_SPECIAL_BITS:
                return "irregular"
            if info.st_size > max_bytes:
                return "oversized"
            try:
                content = read_regular_file(path, max_bytes)
            except FileTooLarge:
                return "oversized"
            except OSError:
                return "irregular"
            return "irregular" if content is None else content
        if entry.kind != _KIND_FILE or mode != entry.mode:
            return "irregular"
        try:
            size = _opened_size(path)
            if size is None:
                return "irregular"
            if size != entry.size:
                # 大きさが違えば、変わっている。上限を超えるものは、読まない（巨大なファイルに伸ばされても）
                if size > max_bytes:
                    return "oversized"
                content = read_regular_file(path, max_bytes)
                return "irregular" if content is None else content
            # 大きさが同じなら、中身を比べる。読む量は、基準の大きさまで
            digested = _digest_file(path, max_bytes, expected_size=entry.size)
        except FileTooLarge:
            return "oversized"
        except OSError:
            return "irregular"
        if digested is None:
            # 開き直す間に、大きさや種類が変わった
            return "irregular"
        digest, content = digested
        if digest == entry.sha256:
            return None
        return "oversized" if content is None else content

    def destroy(self) -> None:
        """複製を消す。"""
        remove_tree(self.root)


def build_tree(git: Git, base: str, dest: Path, files: Mapping[str, bytes], *, archive: bytes | None = None) -> None:
    """検査用のきれいな複製を組み立てる: 基準のコミットを書き出し、渡されたファイルだけを上書きする。

    Args:
        archive: 基準のコミットの書き出し（`Workspace.archive`）。渡されたら、git を呼ばずに、ここから展開する。
            モデルが書いたコードが走った後は、必ず渡す（`git archive` は、設定のフィルターのコマンドを実行する）。

    Raises:
        GitError: 書き出しに失敗した。
        PathDenied: 渡されたパスが、複製の外やシンボリックリンク越しの場所を指す。
    """
    dest = Path(dest)
    if archive is None:
        git.export_tree(base, dest)
    else:
        extract_archive(archive, dest)
    resolved_root = dest.resolve()
    for rel, content in files.items():
        parts = split_relative(rel)
        current = dest
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise PathDenied("path goes through a symbolic link")
        if not current.parent.resolve().is_relative_to(resolved_root):
            raise PathDenied("path leads outside the tree")
        is_new = not current.exists()
        write_regular_file(current, content)
        if is_new:
            # 新しいファイルのモードを、umask に依らず決める（`expected_manifest` と合わせる）
            current.chmod(_NEW_FILE_MODE)
