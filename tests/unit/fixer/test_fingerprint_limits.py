"""設定とフックの照合の上限（main に入れる前のレビュー、5 巡目の修正仕様 ③）。

背景: ``Git.repository_fingerprint()`` は、``.git/config`` や ``.git/hooks`` などを全部読んでハッシュしていた。

- 巨大な疎なファイルを置かれると、最後まで読む（検査の制限時間は、ここには効かない）。
- 深いディレクトリを置かれると ``RecursionError`` が上がり、``run_fix()`` から漏れて、結果のファイルが書かれない。
- ``.git/info/attributes`` は、全体を読んでから切っていた。

仕様:

- 照合で読むファイルは、開いた後に大きさを確かめる。1MB を超えるファイルは、中身を読まない。そのファイルは
  「大きすぎる」という印と大きさで表し、照合の値に含める（置かれる前と後で、値が変わる）。
- たどる深さは 8 段まで、数えるファイルとディレクトリは合わせて 2,000 個まで。超えたら、照合は失敗。
- 照合が失敗したとき、例外の種類に関わらず、``run_fix()`` から例外を漏らさない。``failed``・
  ``repository_tampered`` とし、git の書き込みをしない。起動の前の照合が失敗したら、複製を作らず、SDK を呼ばない。
- ``.git/info/attributes`` は、読む前に大きさを確かめる。1MB を超えていたら、起動の前の確かめで ``failed``・
  ``unsupported_attributes``。

テストが決めた呼び方（developer は、これに合わせる）:

- 1MB は 1,000,000 バイト（H7 の属性ファイルの上限と同じ）。ちょうど 1,000,000 バイトは読み、1 バイト超えたら読まない。
- 上限（深さ・個数）を超えたとき、``Git.repository_fingerprint()`` は ``GitError`` を上げる（今までの、読めなかった
  ときと同じ）。``RecursionError`` などを、そのまま外に出さない。
- 深さは、``.git/hooks`` の下のディレクトリの入れ子で数える。8 段の入れ子の中のファイルまでは読み、9 段目の
  ディレクトリがあれば失敗。
- 起動の前の照合が失敗したときの ``stop_detail`` は ``setup_error``（ブランチを確かめられなかったときなど、起動の
  前の git の失敗と同じ値）。
- ``run_fix`` は、照合を ``Git.repository_fingerprint`` という名前のメソッドとして呼ぶ（テストは、クラスの属性を
  差し替えて、失敗を起こす）。

読んだ量は、時間ではなく、読み取りとハッシュの呼び出しを数えて確かめる（H9 と同じやり方）。
"""

import contextlib
import hashlib
import json
import os
import shutil
import sys
import tracemalloc
from pathlib import Path

import pytest

from . import fakes
from .fakes import (
    FINISH_FIXED,
    FakeTestRunner,
    assert_sdk_not_called,
    good_fix_without_test_runs,
    run_fixer,
)
from .test_hardening import git_error, make_git
from .test_review_fixes import kind_of

SIZE_LIMIT = 1_000_000  # 照合で、中身を読むファイルの上限
MAX_DEPTH = 8
MAX_ENTRIES = 2_000
SPARSE_BYTES = 1 << 30  # 1GB（疎なファイル。実際のディスクは使わない）
ATTRIBUTES_SPARSE_BYTES = 300_000_000
FINGERPRINT_READ_BYTES = 4_000_000  # 見本のリポジトリの設定とフックを読み切っても、これには届かない
RUN_READ_BYTES = 64_000_000  # run_fix 全体（git の出力の読み取りを含む）でも、これには届かない
PEAK_MEMORY_BYTES = 128_000_000  # 巨大なファイルを丸ごとメモリに読めば、これを超える
GIT_WRITING_SUBCOMMANDS = {"archive", "hash-object", "read-tree", "update-index", "write-tree", "commit-tree"} | {
    "update-ref"
}
_HASH_NAMES = ("sha256", "sha1", "sha224", "sha384", "sha512", "md5", "blake2b", "blake2s", "sha3_256", "sha3_512")


# ---------------------------------------------------------------------------
# 土台: 読んだ量を数える。疎なファイルと、深いディレクトリを作り、必ず消す
# ---------------------------------------------------------------------------


class ReadGuard:
    """親のプロセスが読んだ・ハッシュに掛けたバイト数と、メモリの最大の使用量。"""

    def __init__(self) -> None:
        self.read_bytes = 0
        self.hashed_bytes = 0

    @property
    def worst(self) -> int:
        return max(self.read_bytes, self.hashed_bytes)

    @property
    def peak_memory(self) -> int:
        return tracemalloc.get_traced_memory()[1]

    def describe(self) -> str:
        return f"read {self.read_bytes:,} / hashed {self.hashed_bytes:,} / peak memory {self.peak_memory:,} bytes"


@pytest.fixture
def read_guard(monkeypatch):
    """``os.read`` で読んだ量、``hashlib`` に掛けた量、メモリの最大の使用量を数える。"""
    guard = ReadGuard()
    real_read = os.read
    real_new = hashlib.new

    def counting_read(descriptor, length):
        data = real_read(descriptor, length)
        guard.read_bytes += len(data)
        return data

    class CountingHash:
        def __init__(self, inner, data=b""):
            guard.hashed_bytes += len(data)
            self._inner = inner

        def update(self, data):
            guard.hashed_bytes += len(data)
            self._inner.update(data)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def counting_constructor(real):
        def construct(data=b"", **kwargs):
            return CountingHash(real(data, **kwargs), data)

        return construct

    def counting_new(name, data=b"", **kwargs):
        return CountingHash(real_new(name, data, **kwargs), data)

    def counting_file_digest(fileobj, digest, /, *, _bufsize=2**18):
        hasher = real_new(digest) if isinstance(digest, str) else digest()
        while chunk := fileobj.read(_bufsize):
            if not isinstance(hasher, CountingHash):
                guard.hashed_bytes += len(chunk)
            hasher.update(chunk)
        return hasher

    monkeypatch.setattr(os, "read", counting_read)
    for name in _HASH_NAMES:
        monkeypatch.setattr(hashlib, name, counting_constructor(getattr(hashlib, name)))
    monkeypatch.setattr(hashlib, "new", counting_new)
    monkeypatch.setattr(hashlib, "file_digest", counting_file_digest)
    started_here = not tracemalloc.is_tracing()
    if started_here:
        tracemalloc.start()
    tracemalloc.reset_peak()
    yield guard
    if started_here:
        tracemalloc.stop()


@pytest.fixture
def sparse(tmp_path_factory):
    """疎なファイルを作る関数。テストの後、作ったものを必ず消す。疎にできない環境では、テストを飛ばす。"""
    directory = tmp_path_factory.mktemp("sparse-probe")
    try:
        probe = directory / "probe"
        probe.touch()
        os.truncate(probe, 64_000_000)
        allocated = probe.stat().st_blocks * 512
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if allocated > 1_000_000:
        pytest.skip("this file system does not create sparse files")
    made: list[Path] = []

    def make(path: Path, size: int = SPARSE_BYTES) -> Path:
        """``path`` を、疎なまま ``size`` バイトにする（既にあれば、中身の後ろを伸ばす）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        made.append(path)
        with path.open("ab"):
            pass
        os.truncate(path, size)
        assert path.stat().st_size == size
        return path

    yield make
    for path in made:
        with contextlib.suppress(OSError):
            os.truncate(path, 0)
        with contextlib.suppress(OSError):
            path.unlink()
    assert [str(path) for path in made if path.exists()] == []


@pytest.fixture
def planted_trees():
    """深い・数の多いディレクトリを作る関数。テストの後、必ず消す。"""
    made: list[Path] = []

    def remember(top: Path) -> Path:
        made.append(top)
        return top

    yield remember
    for top in made:
        shutil.rmtree(top, ignore_errors=True)
    assert [str(top) for top in made if top.exists()] == []


def hooks_of(repo) -> Path:
    hooks = repo.path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    return hooks


def nest(top: Path, depth: int, *, leaf: str | None = "leaf", content: str = "leaf\n") -> Path:
    """``top`` を 1 段目として、``depth`` 段の入れ子のディレクトリを作る。いちばん奥に ``leaf`` のファイルを置く。

    返すのは、いちばん奥のファイル（``leaf`` が ``None`` なら、いちばん奥のディレクトリ）。
    """
    deepest = top
    for _ in range(depth - 1):
        deepest = deepest / "d"
    deepest.mkdir(parents=True)
    if leaf is None:
        return deepest
    target = deepest / leaf
    target.write_text(content, encoding="utf-8")
    return target


def many_files(top: Path, count: int) -> Path:
    top.mkdir()
    for number in range(count):
        (top / f"f{number:05d}").write_bytes(b"x")
    return top


def many_directories(top: Path, count: int) -> Path:
    top.mkdir()
    for number in range(count):
        (top / f"d{number:05d}").mkdir()
    return top


def stack_depth() -> int:
    depth = 0
    frame = sys._getframe()
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def fingerprint(repo):
    return make_git(repo.path).repository_fingerprint()


def result_of(outcome) -> dict:
    return json.loads((outcome.out_dir / "result.json").read_text(encoding="utf-8"))


def assert_tampered(outcome) -> None:
    """``failed``・``repository_tampered`` で終わり、結果のファイルがあり、ブランチもコミットも無い。"""
    assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "repository_tampered")
    assert outcome.run.branch is None
    assert outcome.run.commit is None
    assert outcome.new_branches == []
    assert outcome.leftover_temp_dirs == []
    assert result_of(outcome)["stop_detail"] == "repository_tampered"
    assert not (outcome.out_dir / "pr-draft.md").exists()


def assert_refused_before_start(outcome, process_recorder) -> None:
    """起動の前に断っている: 複製を作らず（``git archive`` を呼ばず）、SDK を呼ばない。"""
    assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "setup_error")
    assert_sdk_not_called(outcome)
    assert set(process_recorder.subcommands()) & GIT_WRITING_SUBCOMMANDS == set()
    assert outcome.test_runner.calls == []
    assert outcome.lint_runner.trees == []


def after_the_work(tamper):
    """正しい修正を書いた後、終了の申告の前に ``tamper()`` を行う台本。"""

    async def script(agent):
        await fakes.good_fix_without_finish(agent)
        tamper()
        await agent.call("finish", dict(FINISH_FIXED))

    return script


def during_gate_run(tamper, kind: str = "B") -> FakeTestRunner:
    """検査の実行（既定は B）の間に ``tamper()`` を行う、偽の ``TestRunner``。"""

    def override(run):
        if kind_of(run) == kind:
            tamper()

    return FakeTestRunner(override=override)


# ===========================================================================
# 1MB を超えるファイルは、中身を読まない
# ===========================================================================


class TestOversizedFilesAreNotRead:
    def test_reviewer_reproduction_sparse_file_in_hooks_is_not_read(self, sample_repo, sparse, read_guard):
        """レビューの再現: ``.git/hooks`` の下の巨大な疎なファイルを、最後まで読んでハッシュした。"""
        before = fingerprint(sample_repo)
        sparse(hooks_of(sample_repo) / "huge")

        after = fingerprint(sample_repo)

        assert after != before  # 置かれたことには、気づく
        assert read_guard.worst < FINGERPRINT_READ_BYTES, read_guard.describe()
        assert read_guard.peak_memory < PEAK_MEMORY_BYTES, read_guard.describe()

    def test_existing_hook_stretched_into_a_sparse_file_is_not_read(self, sample_repo, sparse, read_guard):
        sample_repo.install_hooks()
        before = fingerprint(sample_repo)
        sparse(hooks_of(sample_repo) / "pre-commit")

        after = fingerprint(sample_repo)

        assert after != before
        assert read_guard.worst < FINGERPRINT_READ_BYTES, read_guard.describe()

    def test_sparse_file_in_a_subdirectory_of_hooks_is_not_read(self, sample_repo, sparse, read_guard):
        before = fingerprint(sample_repo)
        sparse(hooks_of(sample_repo) / "sub" / "dir" / "huge")

        after = fingerprint(sample_repo)

        assert after != before
        assert read_guard.worst < FINGERPRINT_READ_BYTES, read_guard.describe()

    def test_oversized_files_of_different_sizes_give_different_values(self, sample_repo, sparse, read_guard):
        """大きすぎるファイルは、印と大きさで表す（大きさが変われば、値が変わる）。"""
        target = hooks_of(sample_repo) / "huge"
        values = []
        for size in (5_000_000, 6_000_000, SPARSE_BYTES):
            sparse(target, size)
            values.append(fingerprint(sample_repo))

        assert len(set(values)) == 3
        assert read_guard.worst < FINGERPRINT_READ_BYTES, read_guard.describe()

    def test_unchanged_oversized_file_gives_the_same_value_and_removing_it_restores_the_first_value(
        self, sample_repo, sparse
    ):
        before = fingerprint(sample_repo)
        target = sparse(hooks_of(sample_repo) / "huge")

        first, second = fingerprint(sample_repo), fingerprint(sample_repo)
        target.unlink()

        assert first == second
        assert first != before
        assert fingerprint(sample_repo) == before

    def test_oversized_file_is_told_apart_from_a_small_file_with_the_same_name(self, sample_repo, sparse):
        target = hooks_of(sample_repo) / "note"
        target.write_bytes(b"")
        small = fingerprint(sample_repo)
        sparse(target, SIZE_LIMIT + 1)

        assert fingerprint(sample_repo) != small

    def test_file_exactly_at_the_limit_is_still_read_in_full(self, sample_repo):
        """境界: ちょうど 1,000,000 バイトは、今までどおり中身を読む（同じ大きさの書き換えに、気づく）。"""
        target = hooks_of(sample_repo) / "at-limit"
        target.write_bytes(b"a" * SIZE_LIMIT)
        first = fingerprint(sample_repo)
        target.write_bytes(b"a" * (SIZE_LIMIT - 1) + b"b")  # 最後の 1 バイトだけ違う

        assert fingerprint(sample_repo) != first

    def test_file_one_byte_over_the_limit_is_not_read(self, sample_repo, read_guard):
        """境界: 1,000,001 バイトは、読まない。"""
        fingerprint(sample_repo)
        cost_without = read_guard.hashed_bytes, read_guard.read_bytes
        target = hooks_of(sample_repo) / "over-limit"
        target.write_bytes(b"a" * (SIZE_LIMIT + 1))
        before = read_guard.hashed_bytes, read_guard.read_bytes

        fingerprint(sample_repo)

        hashed = read_guard.hashed_bytes - before[0] - cost_without[0]
        read = read_guard.read_bytes - before[1] - cost_without[1]
        assert hashed < SIZE_LIMIT, f"{hashed:,} more bytes were hashed"
        assert read < SIZE_LIMIT, f"{read:,} more bytes were read"

    def test_change_within_the_limit_is_still_noticed(self, sample_repo):
        """上限の中のファイルは、今までどおり: 置いたこと、書き換えたこと、権限を変えたことに気づく。"""
        hook = hooks_of(sample_repo) / "pre-push"
        values = [fingerprint(sample_repo)]
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        values.append(fingerprint(sample_repo))
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        values.append(fingerprint(sample_repo))
        hook.chmod(0o755)
        values.append(fingerprint(sample_repo))
        with (sample_repo.path / ".git" / "config").open("a", encoding="utf-8") as file:
            file.write("[core]\n\tfsmonitor = /bin/false\n")
        values.append(fingerprint(sample_repo))

        assert len(set(values)) == 5
        assert fingerprint(sample_repo) == values[-1]

    def test_size_is_checked_after_the_file_is_opened(self, sample_repo, sparse, read_guard, monkeypatch):
        """開く前に見た大きさを信じない: 開く前は小さく見え、開いたら巨大なファイル（確かめの後の入れ替え）。

        ``os.stat``・``os.lstat`` が、そのファイルだけ小さい大きさを答えるようにする。開いた後の ``os.fstat`` は、
        本当の大きさを答える。照合が失敗（``GitError``）で終わってもよいが、中身を読んではならない。
        """
        target = sparse(hooks_of(sample_repo) / "swapped")
        real_stat, real_lstat = os.stat, os.lstat

        def shrink(result):
            fields = list(result[:10])
            fields[6] = 10  # st_size
            return os.stat_result(fields)

        def is_target(path) -> bool:
            return isinstance(path, str | bytes | os.PathLike) and os.path.abspath(os.fsdecode(path)) == str(target)

        def lying_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            return shrink(result) if is_target(path) else result

        def lying_lstat(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            return shrink(result) if is_target(path) else result

        monkeypatch.setattr(os, "stat", lying_stat)
        monkeypatch.setattr(os, "lstat", lying_lstat)
        assert os.stat(target).st_size == 10
        assert target.stat().st_size == 10

        with contextlib.suppress(git_error()):
            fingerprint(sample_repo)

        assert read_guard.worst < FINGERPRINT_READ_BYTES, read_guard.describe()
        assert read_guard.peak_memory < PEAK_MEMORY_BYTES, read_guard.describe()


# ===========================================================================
# たどる深さは 8 段まで、数えるファイルとディレクトリは合わせて 2,000 個まで
# ===========================================================================


class TestTraversalLimits:
    def test_eight_levels_of_directories_are_still_followed(self, sample_repo, planted_trees):
        """境界: 8 段の入れ子の中のファイルまでは、今までどおり読む（書き換えに、気づく）。"""
        before = fingerprint(sample_repo)
        leaf = nest(planted_trees(hooks_of(sample_repo) / "d"), MAX_DEPTH)

        with_tree = fingerprint(sample_repo)
        leaf.write_text("changed\n", encoding="utf-8")

        assert with_tree != before
        assert fingerprint(sample_repo) != with_tree

    @pytest.mark.parametrize("depth", [MAX_DEPTH + 1, 12, 40])
    def test_deeper_directories_make_the_check_fail(self, sample_repo, planted_trees, depth):
        nest(planted_trees(hooks_of(sample_repo) / "d"), depth)

        with pytest.raises(git_error()):
            fingerprint(sample_repo)

    def test_reviewer_reproduction_deep_directories_do_not_raise_recursion_error(self, sample_repo, planted_trees):
        """レビューの再現: 深さ 300 のディレクトリで、再帰の上限を下げると ``RecursionError`` が漏れた。"""
        nest(planted_trees(hooks_of(sample_repo) / "d"), 300, leaf=None)
        git = make_git(sample_repo.path)
        error_type = git_error()
        raised: list[BaseException] = []
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(stack_depth() + 150)
        try:
            try:
                git.repository_fingerprint()
            except BaseException as error:  # noqa: BLE001
                raised.append(error)
        finally:
            sys.setrecursionlimit(limit)

        assert [type(error).__name__ for error in raised] == [error_type.__name__]

    def test_a_thousand_files_are_still_read(self, sample_repo, planted_trees):
        """上限の中の数なら、今までどおり読む（1 つの書き換えに、気づく）。"""
        top = many_files(planted_trees(hooks_of(sample_repo) / "many"), 1_000)

        with_files = fingerprint(sample_repo)
        (top / "f00999").write_bytes(b"y")

        assert fingerprint(sample_repo) != with_files

    @pytest.mark.parametrize(
        "plant",
        [
            lambda top: many_files(top, 2_500),
            lambda top: many_directories(top, 2_500),
            lambda top: (top.mkdir(), many_files(top / "files", 1_300), many_directories(top / "dirs", 1_300)),
            lambda top: (top.mkdir(), [many_files(top / f"part{n}", 300) for n in range(8)]),
        ],
        ids=["files", "directories", "files_and_directories_together", "spread_over_subdirectories"],
    )
    def test_more_than_two_thousand_entries_make_the_check_fail(self, sample_repo, planted_trees, plant):
        assert MAX_ENTRIES < 2_400
        plant(planted_trees(hooks_of(sample_repo) / "many"))

        with pytest.raises(git_error()):
            fingerprint(sample_repo)

    def test_check_works_again_after_the_planted_things_are_removed(self, sample_repo, planted_trees):
        before = fingerprint(sample_repo)
        top = nest(planted_trees(hooks_of(sample_repo) / "d"), 12, leaf=None)
        with pytest.raises(git_error()):
            fingerprint(sample_repo)
        shutil.rmtree(hooks_of(sample_repo) / "d")

        assert not top.exists()
        assert fingerprint(sample_repo) == before


# ===========================================================================
# 照合が失敗しても、run_fix から例外を漏らさない
# ===========================================================================

FAILURES = {
    "RecursionError": lambda: RecursionError("maximum recursion depth exceeded"),
    "RuntimeError": lambda: RuntimeError("unexpected"),
    "OSError": lambda: OSError(63, "File name too long"),
    "ValueError": lambda: ValueError("unexpected"),
    "MemoryError": lambda: MemoryError(),
    "GitError": lambda: git_error()("cannot read"),
}


class Switch:
    """``Git.repository_fingerprint`` を、入れた後は例外を上げるものに差し替える。"""

    def __init__(self, monkeypatch, failure: str) -> None:
        from src.fixer.gitio import Git

        self.on = False
        self.calls_while_on = 0
        real = Git.repository_fingerprint
        switch = self

        def fingerprint_or_fail(git, *args, **kwargs):
            if switch.on:
                switch.calls_while_on += 1
                raise FAILURES[failure]()
            return real(git, *args, **kwargs)

        monkeypatch.setattr(Git, "repository_fingerprint", fingerprint_or_fail)

    def turn_on(self) -> None:
        self.on = True


class TestRunFixNeverLeaksAFailedCheck:
    @pytest.mark.parametrize("failure", sorted(FAILURES))
    def test_check_that_fails_after_the_work_ends_as_tampered(
        self, sample_repo, tmp_path, monkeypatch, process_recorder, failure
    ):
        """モデルの作業が終わった後の照合が、どの種類の例外で失敗しても、``failed``・``repository_tampered``。"""
        switch = Switch(monkeypatch, failure)
        seen: dict[str, int] = {}

        def fail_from_now_on():
            switch.turn_on()
            seen["git_calls"] = len(process_recorder.implementation_git_calls)

        outcome = run_fixer(sample_repo, tmp_path, script=after_the_work(fail_from_now_on))

        assert switch.calls_while_on >= 1
        assert_tampered(outcome)
        # git の書き込みをしない（照合そのものに要る ``rev-parse`` のほかは、呼ばない）
        later = process_recorder.subcommands()[seen["git_calls"] :]
        assert set(later) <= {"rev-parse"}, later
        assert outcome.test_runner.calls == []
        assert outcome.lint_runner.trees == []
        assert outcome.before.differences(outcome.after) == []

    @pytest.mark.parametrize("failure", ["RecursionError", "RuntimeError", "GitError"])
    @pytest.mark.parametrize("kind", ["A", "B", "C", "D"])
    def test_check_that_fails_during_the_gates_ends_as_tampered(
        self, sample_repo, tmp_path, monkeypatch, failure, kind
    ):
        """検査の実行の間に、照合が失敗するようになる（次の照合も、終わりの照合も失敗する）。"""
        switch = Switch(monkeypatch, failure)

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=during_gate_run(switch.turn_on, kind),
        )

        assert switch.calls_while_on >= 1
        assert_tampered(outcome)
        assert outcome.before.differences(outcome.after) == []

    @pytest.mark.parametrize("failure", ["RecursionError", "RuntimeError", "OSError", "GitError"])
    def test_check_that_fails_only_at_the_very_end_does_not_leak(self, sample_repo, tmp_path, monkeypatch, failure):
        """レビューの再現の場所: コミットの後の、終わりの照合。例外を漏らさず、結果のファイルを書く。"""
        from src.fixer.gitio import Git

        switch = Switch(monkeypatch, failure)
        real_commit = Git.create_branch_commit

        def commit_then_fail(git, *args, **kwargs):
            sha = real_commit(git, *args, **kwargs)
            switch.turn_on()
            return sha

        monkeypatch.setattr(Git, "create_branch_commit", commit_then_fail)

        outcome = run_fixer(sample_repo, tmp_path)

        assert switch.calls_while_on >= 1
        assert outcome.run.repository_changed is True  # 確かめられなかったものは、「変わった」として扱う
        result = result_of(outcome)
        assert result["repository_changed"] is True
        assert (result["status"], result["stop_detail"]) == (outcome.run.status, outcome.run.stop_detail)
        assert outcome.leftover_temp_dirs == []

    @pytest.mark.parametrize("failure", sorted(FAILURES))
    def test_check_that_fails_before_the_start_refuses_to_start(
        self, sample_repo, tmp_path, monkeypatch, process_recorder, failure
    ):
        """起動の前の照合が失敗したら、複製を作らず、SDK を呼ばない。"""
        switch = Switch(monkeypatch, failure)
        switch.turn_on()

        outcome = run_fixer(sample_repo, tmp_path)

        assert switch.calls_while_on >= 1
        assert_refused_before_start(outcome, process_recorder)


# ===========================================================================
# 再現: 巨大な疎なファイルと、深い・数の多いディレクトリを置く
# ===========================================================================


class TestRunFixWithPlantedFilesAndDirectories:
    def test_reviewer_reproduction_sparse_hook_planted_while_the_fixer_works(
        self, sample_repo, tmp_path, sparse, read_guard
    ):
        """作業の間に、``.git/hooks`` に巨大な疎なファイルが置かれる。読まずに、置かれたことに気づく。"""
        hooks = hooks_of(sample_repo)

        outcome = run_fixer(sample_repo, tmp_path, script=after_the_work(lambda: sparse(hooks / "huge")))

        assert_tampered(outcome)
        assert outcome.run.repository_changed is True
        assert read_guard.worst < RUN_READ_BYTES, read_guard.describe()
        assert read_guard.peak_memory < PEAK_MEMORY_BYTES, read_guard.describe()

    def test_sparse_hook_planted_during_a_gate_run(self, sample_repo, tmp_path, sparse, read_guard):
        hooks = hooks_of(sample_repo)

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=during_gate_run(lambda: sparse(hooks / "huge")),
        )

        assert_tampered(outcome)
        assert read_guard.worst < RUN_READ_BYTES, read_guard.describe()

    def test_config_stretched_into_a_sparse_file_while_the_fixer_works(self, sample_repo, tmp_path, sparse, read_guard):
        config = sample_repo.path / ".git" / "config"

        outcome = run_fixer(sample_repo, tmp_path, script=after_the_work(lambda: sparse(config)))

        assert_tampered(outcome)
        assert read_guard.worst < RUN_READ_BYTES, read_guard.describe()
        assert read_guard.peak_memory < PEAK_MEMORY_BYTES, read_guard.describe()

    def test_sparse_hook_that_was_there_from_the_start_is_not_tampering(
        self, sample_repo, tmp_path, sparse, read_guard
    ):
        """最初からある巨大なファイルは、改ざんではない（印と大きさが、前後で同じ）。読まずに、最後まで進む。"""
        sparse(hooks_of(sample_repo) / "huge")

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("proposed", "committed")
        assert outcome.run.repository_changed is False
        assert read_guard.worst < RUN_READ_BYTES, read_guard.describe()
        assert read_guard.peak_memory < PEAK_MEMORY_BYTES, read_guard.describe()

    @pytest.mark.parametrize("depth", [MAX_DEPTH + 1, 12, 300])
    def test_deep_directories_planted_while_the_fixer_works(self, sample_repo, tmp_path, planted_trees, depth):
        """作業の間に、深いディレクトリが置かれる。例外を漏らさず、結果のファイルを書く。"""
        top = planted_trees(hooks_of(sample_repo) / "d")

        outcome = run_fixer(sample_repo, tmp_path, script=after_the_work(lambda: nest(top, depth)))

        assert_tampered(outcome)
        assert outcome.run.repository_changed is True

    def test_deep_directories_planted_during_a_gate_run(self, sample_repo, tmp_path, planted_trees):
        top = planted_trees(hooks_of(sample_repo) / "d")

        outcome = run_fixer(
            sample_repo,
            tmp_path,
            script=good_fix_without_test_runs,
            test_runner=during_gate_run(lambda: nest(top, 12)),
        )

        assert_tampered(outcome)

    def test_thousands_of_files_planted_while_the_fixer_works(self, sample_repo, tmp_path, planted_trees):
        top = planted_trees(hooks_of(sample_repo) / "many")

        outcome = run_fixer(sample_repo, tmp_path, script=after_the_work(lambda: many_files(top, 2_500)))

        assert_tampered(outcome)

    @pytest.mark.parametrize("depth", [MAX_DEPTH + 1, 12])
    def test_deep_directories_that_were_there_from_the_start_refuse_the_start(
        self, sample_repo, tmp_path, planted_trees, process_recorder, depth
    ):
        """起動の前の照合が、上限を超えて失敗する。複製を作らず、SDK を呼ばない。"""
        nest(planted_trees(hooks_of(sample_repo) / "d"), depth)

        outcome = run_fixer(sample_repo, tmp_path)

        assert_refused_before_start(outcome, process_recorder)

    def test_thousands_of_files_that_were_there_from_the_start_refuse_the_start(
        self, sample_repo, tmp_path, planted_trees, process_recorder
    ):
        many_files(planted_trees(hooks_of(sample_repo) / "many"), 2_500)

        outcome = run_fixer(sample_repo, tmp_path)

        assert_refused_before_start(outcome, process_recorder)

    def test_repository_within_the_limits_is_handled_as_before(self, sample_repo, tmp_path, planted_trees):
        """上限の中（8 段の入れ子、1,000 個のファイル、1MB ちょうどのファイル）なら、今までどおり最後まで進む。"""
        hooks = hooks_of(sample_repo)
        nest(planted_trees(hooks / "d"), MAX_DEPTH)
        many_files(planted_trees(hooks / "many"), 1_000)
        (hooks / "at-limit").write_bytes(b"a" * SIZE_LIMIT)

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("proposed", "committed")
        assert outcome.run.repository_changed is False


# ===========================================================================
# ``.git/info/attributes`` は、読む前に大きさを確かめる
# ===========================================================================


def comment_padding(size: int) -> bytes:
    """ちょうど ``size`` バイトの、コメントだけの属性ファイル。"""
    line = b"# pad\n"
    return (line * (size // len(line) + 1))[: size - 1] + b"\n"


def info_attributes(repo) -> Path:
    info = repo.path / ".git" / "info"
    info.mkdir(exist_ok=True)
    return info / "attributes"


class TestInfoAttributesSize:
    def test_sparse_info_attributes_is_refused_without_being_read(self, sample_repo, tmp_path, sparse, read_guard):
        """再現: ``.git/info/attributes`` が巨大な疎なファイル。全体を読んでから切っていた。"""
        sparse(info_attributes(sample_repo), ATTRIBUTES_SPARSE_BYTES)

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unsupported_attributes")
        assert_sdk_not_called(outcome)
        assert read_guard.worst < RUN_READ_BYTES, read_guard.describe()
        assert read_guard.peak_memory < PEAK_MEMORY_BYTES, read_guard.describe()

    def test_info_attributes_one_byte_over_the_limit_is_not_supported(self, sample_repo, tmp_path):
        """境界: コメントだけでも、1,000,001 バイトなら扱わない（後ろを読まずに「属性は無い」と決めない）。"""
        content = comment_padding(SIZE_LIMIT + 1)
        assert len(content) == SIZE_LIMIT + 1
        info_attributes(sample_repo).write_bytes(content)

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unsupported_attributes")
        assert_sdk_not_called(outcome)

    def test_info_attributes_exactly_at_the_limit_is_read_as_before(self, sample_repo, tmp_path):
        """境界: ちょうど 1,000,000 バイトで、書き出しを変える属性が無ければ、今までどおり最後まで進む。"""
        content = comment_padding(SIZE_LIMIT)
        assert len(content) == SIZE_LIMIT
        info_attributes(sample_repo).write_bytes(content)

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("proposed", "committed")

    def test_attribute_on_the_last_line_of_a_file_at_the_limit_is_still_found(self, sample_repo, tmp_path):
        tail = b"*.py export-subst\n"
        content = comment_padding(SIZE_LIMIT - len(tail)) + tail
        assert len(content) == SIZE_LIMIT
        info_attributes(sample_repo).write_bytes(content)

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("failed", "unsupported_attributes")
        assert_sdk_not_called(outcome)

    def test_small_info_attributes_without_archive_attributes_is_handled_as_before(self, sample_repo, tmp_path):
        info_attributes(sample_repo).write_text("# nothing here\n*.md text\n", encoding="utf-8")

        outcome = run_fixer(sample_repo, tmp_path)

        assert (outcome.run.status, outcome.run.stop_detail) == ("proposed", "committed")
