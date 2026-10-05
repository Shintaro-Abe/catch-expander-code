"""テストとリントの実行、環境変数の絞り込み。SDK には依存しない。

設計詳細: `.steering/20261004-execution-fixer/design.md` §8

コマンドはコードが決める。モデルが指定できるのは、`--` の後ろに置くテストのパスだけ（検証は tools.py）。
親の環境変数は引き継がず、決まった一覧だけを渡す。ホームと一時ディレクトリは、実行のたびに作って消す。
これは緩和であって、隔離ではない（走らせるテストのコードは、手元のファイルに触れられる）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.fixer.models import FixLimits
from src.fixer.workspace import remove_tree

logger = logging.getLogger("catch-expander-fixer")

LINT_TIMEOUT_SECONDS = 60.0
OUTPUT_TAIL_CHARS = 4_000
MAX_JUNIT_BYTES = 20_000_000
MAX_LINT_OUTPUT_BYTES = 50_000_000
MAX_TEST_CASES = 100_000
# テストのプロセスが書ける出力の大きさ。超えたら、実行を止める（テストから差し替えるので、使うたびに引く）
MAX_OUTPUT_BYTES = 50_000_000

# macOS の既定の一時ディレクトリはパスが長く、UNIX ソケットを作る既存のテストが落ちるため、/tmp の下に作る
_SHORT_TEMP_ROOT = "/tmp"  # noqa: S108 - 中に、実行ごとの専用ディレクトリを mkdtemp で作る
_KILL_WAIT_SECONDS = 10.0
_POLL_MIN_SECONDS = 0.005
_POLL_MAX_SECONDS = 0.05
_OUTPUT_TAIL_BYTES = OUTPUT_TAIL_CHARS * 4
_READ_CHUNK_BYTES = 1 << 16
_RUFF_FINDINGS_EXIT_CODES = (0, 1)


@dataclass(frozen=True)
class TestReport:
    """テスト実行 1 回の結果。"""

    __test__ = False  # pytest に、テストのクラスとして集めさせない

    outcomes: dict[str, str]  # テストの ID（classname::name の全文）→ passed / failed / error / skipped
    # 起動できなかったとき、時間切れのとき、「検査できなかった」とき（出力や結果の件数が上限を超えた）は None
    exit_code: int | None
    timed_out: bool
    output_tail: str
    output_overflow: bool = False  # 出力が上限を超えたので、実行を止めた


class TestRunner(Protocol):
    __test__ = False

    def run(
        self, tree: Path, paths: Sequence[str], *, ignore: Sequence[str] = (), timeout_seconds: float | None = None
    ) -> TestReport: ...


class LintRunner(Protocol):
    def count(self, tree: Path, *, timeout_seconds: float | None = None) -> int | None: ...


def safe_env(home: Path, tmp: Path) -> dict[str, str]:
    """テストとリントのプロセスに渡す環境変数。親からは `PATH` だけを引き継ぐ。

    AWS の値は、既存のテストが要るダミーの値だけを入れる（本物の値を引き継がない）。
    """
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "AWS_DEFAULT_REGION": "ap-northeast-1",
        "AWS_ACCESS_KEY_ID": "dummy",
        "AWS_SECRET_ACCESS_KEY": "dummy",
        "AWS_EC2_METADATA_DISABLED": "true",
        # 存在しないパス（ホームは空のディレクトリ）
        "AWS_SHARED_CREDENTIALS_FILE": str(Path(home, "no-aws-credentials")),
        "AWS_CONFIG_FILE": str(Path(home, "no-aws-config")),
    }


@dataclass(frozen=True)
class _Scratch:
    """プロセス 1 回ぶんの、複製の外の作業場所。"""

    base: Path
    home: Path
    tmp: Path

    @property
    def env(self) -> dict[str, str]:
        return safe_env(self.home, self.tmp)


@dataclass(frozen=True)
class _Finished:
    """プロセス 1 回の終わり方と、標準出力（親が開いたままのファイルから読んだもの）。"""

    exit_code: int | None
    timed_out: bool
    overflowed: bool
    output: bytes


@contextlib.contextmanager
def _scratch() -> Iterator[_Scratch]:
    """空のホームと一時ディレクトリを作り、終わったら消す。"""
    root = _SHORT_TEMP_ROOT if os.path.isdir(_SHORT_TEMP_ROOT) else None
    base = Path(tempfile.mkdtemp(prefix="fixer-run-", dir=root))
    try:
        home, tmp = base / "home", base / "tmp"
        home.mkdir()
        tmp.mkdir()
        yield _Scratch(base=base, home=home, tmp=tmp)
    finally:
        # テストのコードが権限を落としたディレクトリを残していても、消し切る
        remove_tree(base)


def _wait_without_reaping(
    process: subprocess.Popen, timeout_seconds: float, output_descriptor: int
) -> tuple[bool, bool]:
    """プロセスの終了を、回収せずに待つ。（制限時間の中で終わったか, 出力が上限を超えたか）を返す。

    回収しない（WNOWAIT）のは、この後でプロセスのグループを止めるため。回収する前なら、グループの番号が
    別のプロセスに使い回されていることはない。
    """
    deadline = time.monotonic() + timeout_seconds
    delay = _POLL_MIN_SECONDS
    while True:
        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            return True, False
        if os.fstat(output_descriptor).st_size > MAX_OUTPUT_BYTES:
            return False, True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, False
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, _POLL_MAX_SECONDS)


def _run_process(
    command: list[str], tree: Path, scratch: _Scratch, timeout_seconds: float, *, merge_stderr: bool, tail_only: bool
) -> _Finished:
    """コマンドを `tree` で走らせる。

    出力をメモリに溜めないよう、ファイルに書かせる。終わったとき（時間切れ・割り込みを含む）に、プロセスの
    グループごと止める（テストのコードが起こした子プロセスを、残さないようにする。自分でグループを抜けた子には
    効かない）。出力は、親が最初に開いたファイル記述子から読む。テストのコードが、同じ名前を FIFO などに
    すり替えても、名前で開き直さないので、止まらない。

    Args:
        tail_only: 真なら、出力の末尾だけを返す。偽なら、全体を返す。
    """
    with (scratch.base / "output.txt").open("w+b") as output:
        process = subprocess.Popen(  # noqa: S603 - 引数はコードが組み立てた list。シェルは介さない
            command,
            cwd=str(tree),
            env=scratch.env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
            process_group=0,  # 子プロセスごと止められるよう、専用のグループにする
        )
        exited = overflowed = False
        try:
            exited, overflowed = _wait_without_reaping(process, timeout_seconds, output.fileno())
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_KILL_WAIT_SECONDS)
        size = os.fstat(output.fileno()).st_size
        overflowed = overflowed or size > MAX_OUTPUT_BYTES
        output.seek(max(0, size - _OUTPUT_TAIL_BYTES) if tail_only or overflowed else 0)
        data = output.read(_OUTPUT_TAIL_BYTES if tail_only or overflowed else MAX_OUTPUT_BYTES)
    return _Finished(
        exit_code=process.returncode if exited else None,
        timed_out=not exited and not overflowed,
        overflowed=overflowed,
        output=data,
    )


def _read_regular(path: Path, max_bytes: int) -> bytes | None:
    """通常のファイルなら中身を返す。無い、通常のファイルでない（FIFO・リンクにすり替えられた）、大きすぎる → None。"""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        data = bytearray()
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            data.extend(chunk)
            if len(data) > max_bytes:
                return None
        return bytes(data)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _parse_junit(path: Path) -> dict[str, str] | None:
    """JUnit の XML から、テストごとの成否を読む。無い・読めないときは空。

    テストの件数が上限を超えたときは None（超えたぶんを黙って捨てると、落ちたテストを見落とす）。
    同じ ID の結果が複数あるときも None（pytest は、パスの `/` を `.` に変えて ID を作るので、別のファイルの
    テストが同じ ID になりうる。合流させると、落ちたほうを隠せる）。
    """
    raw = _read_regular(path, MAX_JUNIT_BYTES)
    if raw is None:
        return {}
    try:
        # 読むのは、自分で起こした pytest が書いた XML（大きさに上限を付けている）
        root = ET.fromstring(raw)  # noqa: S314
    except (ET.ParseError, ValueError, RecursionError, MemoryError):
        return {}
    outcomes: dict[str, str] = {}
    for case in root.iter("testcase"):
        # 比べるときのキーは、ID の全文（途中で切ると、先頭が同じ 2 つのテストが合流する）
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        children = {child.tag for child in case}
        if "error" in children:
            outcome = "error"
        elif "failure" in children:
            outcome = "failed"
        elif "skipped" in children:
            outcome = "skipped"
        else:
            outcome = "passed"
        if test_id in outcomes:
            return None
        outcomes[test_id] = outcome
        if len(outcomes) > MAX_TEST_CASES:
            return None
    return outcomes


class PytestRunner:
    """実物の pytest を、コードが決めたコマンドで走らせる。"""

    def __init__(self, timeout_seconds: float = FixLimits().test_timeout_seconds) -> None:
        self._timeout_seconds = timeout_seconds

    def run(
        self, tree: Path, paths: Sequence[str], *, ignore: Sequence[str] = (), timeout_seconds: float | None = None
    ) -> TestReport:
        """`tree` を作業ディレクトリにして、`paths` のテストを走らせる。例外を出さない（割り込みを除く）。

        成否は、標準出力ではなく JUnit の XML から読む（XML は `tree` の外に置く）。XML が無い・読めないときは
        `outcomes` を空にする（呼ぶ側は `exit_code` で判断する）。出力か、結果の件数が上限を超えたとき、
        同じ ID の結果が複数あるときは、「検査できなかった」として、`outcomes` を空に、`exit_code` を None にする。

        Args:
            timeout_seconds: この 1 回の制限時間。渡されたら、作るときに決めた制限時間より優先する。
        """
        limit = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            with _scratch() as scratch:
                junit = scratch.base / "report" / "junit.xml"
                junit.parent.mkdir()
                command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml", str(junit)]
                for path in ignore:
                    command.extend(["--ignore", path])
                command.extend(["--", *paths])
                finished = _run_process(command, Path(tree), scratch, limit, merge_stderr=True, tail_only=True)
                # 時間切れ・打ち切りの実行の XML は、途中までの結果なので使わない
                outcomes = {} if finished.exit_code is None else _parse_junit(junit)
        except (OSError, ValueError):
            logger.warning("Test run could not be started", exc_info=True)
            return TestReport(outcomes={}, exit_code=None, timed_out=False, output_tail="")
        tail = finished.output.decode("utf-8", errors="replace")[-OUTPUT_TAIL_CHARS:]
        if outcomes is None or finished.overflowed:
            return TestReport(
                outcomes={}, exit_code=None, timed_out=False, output_tail=tail, output_overflow=finished.overflowed
            )
        return TestReport(
            outcomes=outcomes, exit_code=finished.exit_code, timed_out=finished.timed_out, output_tail=tail
        )


class RuffRunner:
    """実物の ruff で、指摘の数を数える。"""

    def __init__(self, timeout_seconds: float = LINT_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def count(self, tree: Path, *, timeout_seconds: float | None = None) -> int | None:
        """`tree` の `src` と `tests` の指摘の数を返す。走らせられなければ None。例外を出さない。

        Args:
            timeout_seconds: この 1 回の制限時間。渡されたら、作るときに決めた制限時間より優先する。
        """
        limit = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        command = [sys.executable, "-m", "ruff", "check", "--output-format", "json", "--no-cache", "src", "tests"]
        try:
            with _scratch() as scratch:
                finished = _run_process(command, Path(tree), scratch, limit, merge_stderr=False, tail_only=False)
            # 0: 指摘なし、1: 指摘あり。それ以外（2: 実行の失敗）は、数えられなかったものとする
            if finished.overflowed or finished.exit_code not in _RUFF_FINDINGS_EXIT_CODES:
                return None
            if len(finished.output) > MAX_LINT_OUTPUT_BYTES:
                return None
            findings = json.loads(finished.output)
        except (OSError, ValueError, RecursionError):
            return None
        return len(findings) if isinstance(findings, list) else None
