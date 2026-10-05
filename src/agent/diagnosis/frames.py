"""スタックトレースのフレームの取り出しと、実際のコードとの突き合わせ。SDK には依存しない。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §18 の S1・C2・A3

スタックトレースは記録の中身（信頼できない入力）で、末尾の例外メッセージには外から来た文字列が
入りうる。フレームの形をした行を仕込まれても受け渡しに入らないよう、取り出したフレームは
スタックトレースの最初のブロック（1 行目の見出しから、例外の行の手前まで）からだけ取り、さらに
「実際のコードにその場所があるもの」だけを残す。返すパスと関数名は、記録の文字列ではなく、
解決した相対パスと AST の定義の名前から組み立て直したもの。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from diagnosis.models import CodeLocation

APP_PREFIX = "/app/"
MODULE_LEVEL = "<module>"
MAX_PATH_CHARS = 120
MAX_FUNCTION_CHARS = 80
MAX_SOURCE_BYTES = 1_000_000

# 行全体が「空白 2 つ + File "…", line N, in 名前」の形のときだけ拾う（fullmatch で確かめる）。
# 数字は [0-9] と書く（\d は全角の数字にも一致するため）。行末の空白や CR は許さない。
_FRAME_LINE_PATTERN = re.compile(
    r'  File "(/app/[A-Za-z0-9_/]+\.py)", line ([0-9]{1,7}), in ([A-Za-z_<][A-Za-z0-9_<>]*)'
)
_PATH_PATTERN = re.compile(r"/app/[A-Za-z0-9_/]+\.py")
# スタックトレースの 1 行目。これと完全に一致しなければ、フレームは取らない
_TRACEBACK_HEADER = "Traceback (most recent call last):"
# ブロックの中のフレームの行（パスは任意。ライブラリや <frozen …> のフレームでも、ブロックは続く）
_ANY_FRAME_LINE_PATTERN = re.compile(r'  File ".*", line [0-9]+, in .+')
# コードの行と、^ や ~ の印の行は、空白 4 つ以上で始まる
_CODE_LINE_PREFIX = "    "
# ブロックを終える例外の行。「点で区切った識別子」で始まり、その後ろは行末か `:`。型の名前は最後の部分（80 字まで）
_EXCEPTION_LINE_PATTERN = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*([A-Za-z_][A-Za-z0-9_]{0,79})(?::.*)?")

# ファイル 1 つの解析結果: (行数, [(関数名, 開始行, 終了行), ...])
_Source = tuple[int, list[tuple[str, int, int]]]


def _first_block(stack_trace: object) -> tuple[list[str], str | None]:
    """スタックトレースの最初のブロックを読む。(フレームの行の一覧, ブロックを終えた行) を返す。

    読むのは、1 行目の見出しに続く、フレームの行とコードの行の並びだけ。それ以外の行に当たったら、
    そこでやめる（その行が「ブロックを終えた行」）。見出しが 1 行目に無ければ、何も読まない。
    記録が途中で切れていて、終える行まで届かなかったときは、終えた行は None。
    """
    if not isinstance(stack_trace, str):
        return [], None
    # 行の区切りは LF だけ（CR などで「行頭」を作れないようにする）
    lines = stack_trace.split("\n")
    if lines[0] != _TRACEBACK_HEADER:
        return [], None
    frame_lines = []
    for line in lines[1:]:
        if _ANY_FRAME_LINE_PATTERN.fullmatch(line) is not None:
            frame_lines.append(line)
        elif not line.startswith(_CODE_LINE_PREFIX):
            return frame_lines, line
    return frame_lines, None


def exception_line_type(stack_trace: object) -> str | None:
    """スタックトレースの最初のブロックを終える例外の行から、例外の型の名前を返す。読めなければ None。

    `a.b.DeepError: x` なら `DeepError`。連鎖した例外（raise … from e）では、最初のブロックは内側の例外で
    終わるので、記録された型（外側の例外）と、最初のブロックのフレームを組にしないために使う。
    例外の行まで届いていない記録、空行や空白で始まる行で終わるブロックは None。
    """
    _, terminator = _first_block(stack_trace)
    if terminator is None:
        return None
    match = _EXCEPTION_LINE_PATTERN.fullmatch(terminator)
    return match.group(1) if match is not None else None


class FrameVerifier:
    """「コードの根」の下のファイルと突き合わせて、フレームが本物かを確かめる。

    `/app/x/y.py` は `<code_root>/x/y.py` と突き合わせる。解析結果はインスタンスの中で使い回す
    （診断 1 回につき 1 つ作る）。どのメソッドも例外を出さず、確かめられないフレームは捨てる。
    """

    def __init__(self, code_root: str | os.PathLike[str] | None = None) -> None:
        # 既定は diagnosis パッケージの親ディレクトリ（ECS では /app、手元では src/agent）
        root = Path(code_root) if code_root is not None else Path(__file__).resolve().parent.parent
        try:
            # 根そのものがシンボリックリンクの先にあるのは構わないので、先に解決しておく
            self._root: Path | None = root.resolve()
        except (OSError, RuntimeError):
            self._root = None
        self._sources: dict[Path, _Source | None] = {}

    def extract(self, stack_trace: object, limit: int) -> list[CodeLocation]:
        """スタックトレースの最初のブロックから、突き合わせを通ったフレームを末尾から limit 件まで返す。

        例外メッセージ（外から来た文字列を含みうる）に、実在する場所を指す行を仕込まれても拾わないよう、
        読むのは 1 行目の見出しに続く、フレームの行とコードの行の並びだけ。それ以外の行（例外の行、
        空行など）に当たったら、そこでやめる。それより後ろは、どんな形でも拾わない。
        """
        verified = []
        for line in _first_block(stack_trace)[0]:
            # 登録するのは、/app/ のフレームで、実際のコードとの突き合わせを通ったものだけ
            match = _FRAME_LINE_PATTERN.fullmatch(line)
            if match is None:
                continue
            location = self.verify(match.group(1), int(match.group(2)), match.group(3))
            if location is not None:
                verified.append(location)
        return verified[-limit:] if limit > 0 else []

    def verify(self, path: str, line: int, function: str) -> CodeLocation | None:
        """フレームが実際のコードにあれば、組み立て直した CodeLocation を返す。無ければ None。"""
        if len(path) > MAX_PATH_CHARS or len(function) > MAX_FUNCTION_CHARS or line < 1:
            return None
        resolved = self._resolve(path)
        if resolved is None:
            return None
        source = self._source(resolved)
        if source is None:
            return None
        line_count, functions = source
        rebuilt_path = APP_PREFIX + resolved.relative_to(self._root).as_posix()
        # シンボリックリンクをたどった先の名前が、パスの形や長さから外れることがある
        if len(rebuilt_path) > MAX_PATH_CHARS or _PATH_PATTERN.fullmatch(rebuilt_path) is None:
            return None
        if function == MODULE_LEVEL:
            return CodeLocation(rebuilt_path, MODULE_LEVEL, line) if line <= line_count else None
        for name, start, end in functions:
            if name == function and start <= line <= end:
                return CodeLocation(rebuilt_path, name, line)
        return None

    def _resolve(self, path: str) -> Path | None:
        """`/app/` の後ろを根からの相対パスとして解決する。根の外に出るもの、ファイルでないものは None。"""
        if self._root is None or not path.startswith(APP_PREFIX):
            return None
        segments = path[len(APP_PREFIX) :].split("/")
        # 空の区切り（`/app//x.py` は後ろが絶対パスになる）と `.`・`..` は通さない
        if any(segment in ("", ".", "..") for segment in segments):
            return None
        try:
            resolved = self._root.joinpath(*segments).resolve()
            if not resolved.is_relative_to(self._root) or not resolved.is_file():
                return None
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved

    def _source(self, resolved: Path) -> _Source | None:
        if resolved not in self._sources:
            self._sources[resolved] = self._parse(resolved)
        return self._sources[resolved]

    @staticmethod
    def _parse(resolved: Path) -> _Source | None:
        """ファイルを読んで、行数と関数定義の範囲を取り出す。解析できないファイルは None。"""
        try:
            with resolved.open("rb") as file:
                raw = file.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES:
                return None
            text = raw.decode("utf-8")
            tree = ast.parse(text)
        except (OSError, ValueError, SyntaxError, RecursionError, MemoryError):
            return None
        line_count = text.count("\n") + (0 if not text or text.endswith("\n") else 1)
        functions = [
            (node.name, node.lineno, node.end_lineno or node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        return line_count, functions
