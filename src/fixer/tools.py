"""修正役の 7 つのツールの中身。SDK には依存しない。

設計詳細: `.steering/20261004-execution-fixer/design.md` §7

モデルが決めるのは「どのツールを、どの引数で呼ぶか」だけ。どこを読み書きできるか・どのコマンドが
走るか・上限・結果の形は、すべて FixToolSession が握る。入力はモデルが作った信頼できない値として、
SDK の検証に頼らずここで検証する。検証に落ちた呼び出しも、足取りと回数に残す。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from diagnosis.redaction import mask_secrets

from src.fixer.gates import PYTHON_SUFFIX, TESTS_DIRECTORY, has_hidden_characters
from src.fixer.models import PATCH_KINDS, TOOL_NAMES, FixLimits, FixToolCall, ToolResult
from src.fixer.runners import OUTPUT_TAIL_CHARS, TestRunner
from src.fixer.workspace import (
    GIT_DIRECTORY,
    IGNORED_DIRECTORY_NAMES,
    FileTooLarge,
    PathDenied,
    Workspace,
    read_regular_file,
    write_regular_file,
)

logger = logging.getLogger("catch-expander-fixer")

FINISH_TOOL = "finish"
RUN_TESTS_TOOL = "run_tests"
UNKNOWN_TOOL = "unknown"
FINISH_OUTCOMES = ("fixed", "declined")
# モデルが申告できる分類（unspecified は、申告が無いときにコードが入れる値）
CLAIMABLE_PATCH_KINDS = tuple(kind for kind in PATCH_KINDS if kind != "unspecified")

ROOT_PATHS = (".", "")  # list_files で、複製の根を指す書き方
MAX_FINISH_ATTEMPTS = 5  # 終了の申告の試行（受理・不受理を問わない）。ほかのツールの上限とは別に数える
MAX_LIST_ENTRIES = 200
DEFAULT_READ_LINES = 400
MAX_READ_LINES = 2_000
MAX_LINE_NUMBER = 10_000_000
MAX_READABLE_FILE_BYTES = 2_000_000  # read_file・replace_in_file が開くファイルの大きさ
MAX_SEARCH_PATTERN_CHARS = 200
MAX_SEARCH_MATCHES = 100
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_SEARCHED_FILES = 5_000
MAX_SEARCH_LINE_CHARS = 200
MAX_TEST_PATHS = 5
MAX_FAILED_TESTS = 20
MAX_TEST_NAME_CHARS = 200
MAX_ENTRY_NAME_CHARS = 200
MAX_RESULT_CHARS = 16_000  # 一覧と検索の結果の、おおよその上限
MAX_REPORTED_FIELDS = 20
MAX_FIELD_NAME_CHARS = 64

# 足取りに残すパスの形。決まった文字だけのパスを残す（検証は fullmatch で行う）
_TRACE_PATH_PATTERN = re.compile(r"[A-Za-z0-9_./ -]{1,400}")
_HIDDEN_NAMES = IGNORED_DIRECTORY_NAMES | {GIT_DIRECTORY}
_TEST_ID_SEPARATOR = "::"
_FAILING_OUTCOMES = ("failed", "error")


def _object_schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_PATH_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 400}

# 入力の厳しいスキーマ（引数の形の定義）。SDK には、これを緩めたものを渡す（sdk_server.py）。
# 検証の正本は、この下の _validate_* の側。
TOOL_SCHEMAS: dict[str, dict] = {
    "list_files": _object_schema({"path": _PATH_SCHEMA}, required=("path",)),
    "read_file": _object_schema(
        {
            "path": _PATH_SCHEMA,
            "start_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LINES},
        },
        required=("path",),
    ),
    "search_code": _object_schema(
        {"pattern": {"type": "string", "minLength": 1, "maxLength": MAX_SEARCH_PATTERN_CHARS}, "path": _PATH_SCHEMA},
        required=("pattern",),
    ),
    "write_file": _object_schema({"path": _PATH_SCHEMA, "content": {"type": "string"}}, required=("path", "content")),
    "replace_in_file": _object_schema(
        {"path": _PATH_SCHEMA, "old": {"type": "string", "minLength": 1}, "new": {"type": "string"}},
        required=("path", "old", "new"),
    ),
    RUN_TESTS_TOOL: _object_schema(
        {"paths": {"type": "array", "minItems": 1, "maxItems": MAX_TEST_PATHS, "items": _PATH_SCHEMA}},
        required=("paths",),
    ),
    FINISH_TOOL: _object_schema(
        {
            "outcome": {"type": "string", "enum": list(FINISH_OUTCOMES)},
            "patch_kind": {"type": "string", "enum": list(CLAIMABLE_PATCH_KINDS)},
            "explanation": {"type": "string"},
        },
        required=("outcome", "patch_kind", "explanation"),
    ),
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_files": "ディレクトリの直下の名前と種類（file / dir）を返す。200 件まで。",
    "read_file": (
        "ファイルを、行番号つきで返す。start_line と max_lines で読む範囲を選べる（既定は先頭から 400 行）。"
        "続きがあるときは truncated が true になる。"
    ),
    "search_code": (
        "固定の文字列（正規表現ではない）を含む行を、.py のファイルから探す。path でディレクトリを絞り込める。"
        "100 件まで。"
    ),
    "write_file": (
        "ファイルを新しく作る、または全体を書き直す。書けるのは src/ と tests/ の下の .py だけ。"
        "既存のテスト・conftest.py・設定・依存の定義には書けない。"
    ),
    "replace_in_file": "ファイルの中の old を new に置き換える。old が、ちょうど 1 か所に現れるときだけ置き換える。",
    RUN_TESTS_TOOL: (
        "tests/ の下のファイルかディレクトリを指定して、テストを走らせる。成功・失敗・エラーの件数、"
        "落ちたテストの名前、出力の末尾を返す。走らせられる回数には上限がある。"
    ),
    FINISH_TOOL: (
        "作業の終わりを申告する。最後に必ず呼ぶ。直したなら outcome を fixed、直せないと判断したなら declined にする。"
    ),
}


@dataclass(frozen=True)
class FinishClaim:
    """受理した、終了の申告。"""

    outcome: str  # fixed / declined
    patch_kind: str  # symptomatic / structural
    explanation: str  # モデルが書いたまま（マスクと切り詰めは、結果に入れる側で行う）


@dataclass(frozen=True)
class _Done:
    """ツール 1 回の結果と、足取りに残すもの。"""

    result: ToolResult
    path: str | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None


def _field_names(keys: list) -> list[str]:
    """不正な項目の名前を返す。値は返さない。"""
    names = [key[:MAX_FIELD_NAME_CHARS] if isinstance(key, str) else "(non-string key)" for key in keys]
    return names[:MAX_REPORTED_FIELDS]


def _invalid(*fields: object, **extra: Any) -> ToolResult:
    return ToolResult("invalid_input", {"invalid_fields": _field_names(list(fields)), **extra})


def _unknown_fields(args: dict, allowed: tuple[str, ...]) -> list:
    return [key for key in args if key not in allowed]


def _is_int(value: object, minimum: int, maximum: int) -> bool:
    # 真偽値（int の派生）と小数は通さない
    return type(value) is int and minimum <= value <= maximum


def _validate_list_files(args: dict, _limits: FixLimits) -> tuple[dict, list]:
    invalid = _unknown_fields(args, ("path",))
    if not isinstance(args.get("path"), str):
        invalid.append("path")
    return {"path": args.get("path")}, invalid


def _validate_read_file(args: dict, _limits: FixLimits) -> tuple[dict, list]:
    invalid = _unknown_fields(args, ("path", "start_line", "max_lines"))
    if not isinstance(args.get("path"), str):
        invalid.append("path")
    validated = {"path": args.get("path"), "start_line": 1, "max_lines": DEFAULT_READ_LINES}
    if "start_line" in args:
        if _is_int(args["start_line"], 1, MAX_LINE_NUMBER):
            validated["start_line"] = args["start_line"]
        else:
            invalid.append("start_line")
    if "max_lines" in args:
        if _is_int(args["max_lines"], 1, MAX_READ_LINES):
            validated["max_lines"] = args["max_lines"]
        else:
            invalid.append("max_lines")
    return validated, invalid


def _validate_search_code(args: dict, _limits: FixLimits) -> tuple[dict, list]:
    invalid = _unknown_fields(args, ("pattern", "path"))
    pattern = args.get("pattern")
    if not (isinstance(pattern, str) and 1 <= len(pattern) <= MAX_SEARCH_PATTERN_CHARS):
        invalid.append("pattern")
    if "path" in args and not isinstance(args["path"], str):
        invalid.append("path")
    return {"pattern": pattern, "path": args.get("path")}, invalid


def _validate_write_file(args: dict, _limits: FixLimits) -> tuple[dict, list]:
    invalid = _unknown_fields(args, ("path", "content"))
    for name in ("path", "content"):
        if not isinstance(args.get(name), str):
            invalid.append(name)
    return {"path": args.get("path"), "content": args.get("content")}, invalid


def _validate_replace_in_file(args: dict, limits: FixLimits) -> tuple[dict, list]:
    invalid = _unknown_fields(args, ("path", "old", "new"))
    if not isinstance(args.get("path"), str):
        invalid.append("path")
    old, new = args.get("old"), args.get("new")
    # 1 字は 1 バイト以上なので、字数が上限を超えるものは、書き込みの上限も必ず超える
    if not (isinstance(old, str) and 1 <= len(old) <= limits.max_write_bytes):
        invalid.append("old")
    if not (isinstance(new, str) and len(new) <= limits.max_write_bytes):
        invalid.append("new")
    return {"path": args.get("path"), "old": old, "new": new}, invalid


def _validate_run_tests(args: dict, _limits: FixLimits) -> tuple[dict, list]:
    invalid = _unknown_fields(args, ("paths",))
    paths = args.get("paths")
    valid = (
        isinstance(paths, list)
        and 1 <= len(paths) <= MAX_TEST_PATHS
        # `-` で始まる値は pytest のオプションに、`::` を含む値はテストの ID の指定になる
        and all(
            isinstance(path, str) and path and not path.startswith("-") and _TEST_ID_SEPARATOR not in path
            for path in paths
        )
    )
    if not valid:
        invalid.append("paths")
    return {"paths": paths}, invalid


def _validate_finish(args: dict, limits: FixLimits) -> tuple[dict, list]:
    # tests_passed・status など、スキーマにない項目を含む申告は受理しない（合否はコードが決める）
    invalid = _unknown_fields(args, ("outcome", "patch_kind", "explanation"))
    outcome, patch_kind, explanation = args.get("outcome"), args.get("patch_kind"), args.get("explanation")
    if not (isinstance(outcome, str) and outcome in FINISH_OUTCOMES):
        invalid.append("outcome")
    if not (isinstance(patch_kind, str) and patch_kind in CLAIMABLE_PATCH_KINDS):
        invalid.append("patch_kind")
    if not (isinstance(explanation, str) and len(explanation) <= limits.max_explanation_chars):
        invalid.append("explanation")
    return {"outcome": outcome, "patch_kind": patch_kind, "explanation": explanation}, invalid


_VALIDATORS: dict[str, Callable[[dict, FixLimits], tuple[dict, list]]] = {
    "list_files": _validate_list_files,
    "read_file": _validate_read_file,
    "search_code": _validate_search_code,
    "write_file": _validate_write_file,
    "replace_in_file": _validate_replace_in_file,
    RUN_TESTS_TOOL: _validate_run_tests,
    FINISH_TOOL: _validate_finish,
}


def _trace_path(relative: str | None) -> str | None:
    """足取りに残すパス。決まった文字だけで、秘密情報らしい部分のないものだけを残す。"""
    if relative is None or _TRACE_PATH_PATTERN.fullmatch(relative) is None or mask_secrets(relative) != relative:
        return None
    return relative


def _split_lines(text: str) -> list[str]:
    """LF で行に分ける（末尾の改行のあとの空の行は数えない）。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _cut_to_bytes(text: str, max_bytes: int) -> str:
    """UTF-8 で `max_bytes` に収まるところまでを返す（文字の途中では切らない）。"""
    return text.encode("utf-8")[: max(max_bytes, 0)].decode("utf-8", errors="ignore")


class FixToolSession:
    """修正 1 回ぶんの、上限・足取り・終了の申告を持つ。

    ハンドラーは別スレッドから呼ぶので、呼び出しは 1 件ずつロックの中で処理する。
    """

    def __init__(
        self,
        workspace: Workspace,
        limits: FixLimits,
        test_runner: TestRunner,
        *,
        clock: Callable[[], float] = time.monotonic,
        deadline: float | None = None,
    ) -> None:
        """
        Args:
            deadline: 修正役の全体の期限（`clock` と同じ時計の時刻）。過ぎてから処理を始める呼び出しは、
                何もせずに `deadline_exceeded` を返す。None なら、期限なし。
        """
        self._workspace = workspace
        self._deadline = deadline
        self._limits = limits
        self._test_runner = test_runner
        self._clock = clock
        self._lock = threading.Lock()
        self.trace: list[FixToolCall] = []
        self.finished: FinishClaim | None = None
        self._call_count = 0
        self._test_runs = 0
        self._finish_attempts = 0
        self._closed = False
        self._handlers: dict[str, Callable[[dict], _Done]] = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "search_code": self._search_code,
            "write_file": self._write_file,
            "replace_in_file": self._replace_in_file,
            RUN_TESTS_TOOL: self._run_tests,
            FINISH_TOOL: self._finish,
        }

    def call(self, name: str, args: object) -> ToolResult:
        """ツールを 1 回呼ぶ。例外を出さず、失敗は固定の分類で返す。失敗した呼び出しも足取りに残す。"""
        with self._lock:
            # 閉じた後は、ロックを待っていた呼び出しも、処理を始めない（足取りにも残さない）
            if self._closed:
                return ToolResult("internal_error", {})
            started = self._clock()
            # 足取りに残す名前は、決まった 7 つのどれか、または unknown
            tool = name if isinstance(name, str) and name in TOOL_NAMES else UNKNOWN_TOOL
            try:
                if self._deadline is not None and started >= self._deadline:
                    # 全体の期限を過ぎてから始まる呼び出しは、複製に触れず、テストも走らせない
                    done = _Done(ToolResult("deadline_exceeded", {}))
                else:
                    done = self._run(tool, args)
            except Exception:
                # 例外の文面は結果に入れない（複製の中身や内部の情報がモデルに渡るのを防ぐ）
                logger.exception("Fixer tool failed unexpectedly: tool=%s", tool)
                done = _Done(ToolResult("internal_error", {}))
            self.trace.append(
                FixToolCall(
                    seq=len(self.trace) + 1,
                    tool=tool,
                    path=_trace_path(done.path),
                    outcome=done.result.outcome,
                    duration_ms=round((self._clock() - started) * 1000),
                    tests_passed=done.tests_passed,
                    tests_failed=done.tests_failed,
                )
            )
            return done.result

    def close(self) -> None:
        """これ以降の呼び出しを受け付けない。処理の途中の呼び出しがあれば、終わるまで待つ。

        修正役の実行が終わった後（時間切れ・割り込みを含む）に呼ぶ。閉じる印は、ロックの外で先に立てる
        （ロックを待っていた呼び出しが、時間切れの後に順に実行されるのを防ぐ）。変更を取り出す間や、
        複製を消した後に、遅れて届いた呼び出しが複製に触れないようにする。
        """
        self.stop_accepting()
        with self._lock:
            pass

    def stop_accepting(self) -> None:
        """閉じる印だけを立てる（待たない）。処理の途中の呼び出しは、そのまま続く。"""
        self._closed = True

    def _run(self, tool: str, args: object) -> _Done:
        if tool == UNKNOWN_TOOL:
            return _Done(_invalid("tool"))
        # 回数は、検証の前に数える（形の不正な呼び出しの繰り返しにも、上限を効かせる）
        if not self._take(tool):
            return _Done(ToolResult("call_limit", {}))
        if not isinstance(args, dict):
            return _Done(_invalid("arguments"))
        validated, invalid = _VALIDATORS[tool](args, self._limits)
        if invalid:
            return _Done(_invalid(*invalid))
        # 複製の根が、作ったときのディレクトリでなくなっていたら（リンクや、別のディレクトリに替えられた）、
        # 複製に触れるツールは、何も読まず、何も書かず、テストも走らせない。終了の申告は、複製に触れない
        if tool != FINISH_TOOL and not self._workspace.is_intact():
            return _Done(ToolResult("denied", {}))
        try:
            return self._handlers[tool](validated)
        except PathDenied:
            return _Done(ToolResult("denied", {}))
        except FileNotFoundError:
            return _Done(ToolResult("not_found", {}))
        except FileTooLarge:
            return _Done(ToolResult("size_limit", {}))

    def _take(self, tool: str) -> bool:
        """上限の中なら、1 回ぶんを数えて True。テスト実行と終了の申告は、ほかのツールとは別に数える。"""
        if tool == RUN_TESTS_TOOL:
            if self._test_runs >= self._limits.max_test_runs:
                return False
            self._test_runs += 1
        elif tool == FINISH_TOOL:
            if self._finish_attempts >= MAX_FINISH_ATTEMPTS:
                return False
            self._finish_attempts += 1
        else:
            if self._call_count >= self._limits.max_tool_calls:
                return False
            self._call_count += 1
        return True

    def _relative(self, resolved: Path) -> str | None:
        return self._workspace.relative_to_root(resolved)

    # ------------------------------------------------------------------
    # 読むツール
    # ------------------------------------------------------------------

    def _list_files(self, args: dict) -> _Done:
        # 複製の根は、`.` または空文字で一覧できる（それ以外の `.` を含むパスは、ほかのツールと同じく断る）
        at_root = args["path"] in ROOT_PATHS
        resolved = self._workspace.root.resolve() if at_root else self._workspace.resolve_read(args["path"])
        if not resolved.is_dir():
            return _Done(_invalid("path"))
        entries: list[dict[str, str]] = []
        truncated = False
        used = 0
        for name in sorted(os.listdir(resolved)):
            if name in _HIDDEN_NAMES:
                continue
            if len(entries) >= MAX_LIST_ENTRIES or used > MAX_RESULT_CHARS:
                truncated = True
                break
            # 種類は lstat で見る（シンボリックリンクの先を、ここでは調べない）
            mode = os.lstat(resolved / name).st_mode
            kind = "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"
            entries.append({"name": name[:MAX_ENTRY_NAME_CHARS], "type": kind})
            used += min(len(name), MAX_ENTRY_NAME_CHARS)
        return _Done(ToolResult("ok", {"entries": entries, "truncated": truncated}), self._relative(resolved))

    def _read_text(self, path: Path) -> str | None:
        """通常のファイルを UTF-8 で読む。通常のファイルでない、UTF-8 で読めない → None。"""
        raw = read_regular_file(path, MAX_READABLE_FILE_BYTES)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _read_file(self, args: dict) -> _Done:
        resolved = self._workspace.resolve_read(args["path"])
        text = self._read_text(resolved)
        if text is None:
            return _Done(_invalid("path"))
        lines = _split_lines(text)
        start, budget = args["start_line"], self._limits.max_read_bytes
        window = lines[start - 1 : start - 1 + args["max_lines"]]
        truncated = start - 1 + len(window) < len(lines)
        shown: list[str] = []
        for number, line in enumerate(window, start=start):
            numbered = f"{number}\t{line}\n"
            size = len(numbered.encode("utf-8"))
            if size > budget:
                # 1 回の読み取りの上限。行の途中でも切る
                shown.append(_cut_to_bytes(numbered, budget))
                truncated = True
                break
            shown.append(numbered)
            budget -= size
        data = {"content": "".join(shown), "total_lines": len(lines), "truncated": truncated}
        return _Done(ToolResult("ok", data), self._relative(resolved))

    def _search_code(self, args: dict) -> _Done:
        pattern = args["pattern"]
        start = self._workspace.resolve_read(args["path"]) if args["path"] is not None else self._workspace.root
        if not start.is_dir():
            return _Done(_invalid("path"))
        base = self._workspace.root.resolve()
        matches: list[dict[str, Any]] = []
        truncated = False
        used = searched = 0
        # 名前の順に歩く（上限に当たったとき、どこまで探したかが環境で変わらないようにする）
        pending = [start.resolve()]
        while pending and not truncated:
            directory = pending.pop()
            files, subdirectories = self._python_files_and_directories(directory)
            pending.extend(reversed(subdirectories))
            for path in files:
                if searched >= MAX_SEARCHED_FILES:
                    truncated = True
                    break
                searched += 1
                relative = path.relative_to(base).as_posix()
                for number, line in self._matching_lines(path, pattern):
                    if len(matches) >= MAX_SEARCH_MATCHES or used > MAX_RESULT_CHARS:
                        truncated = True
                        break
                    text = line[:MAX_SEARCH_LINE_CHARS]
                    matches.append({"path": relative, "line": number, "text": text})
                    used += len(relative) + len(text)
                if truncated:
                    break
        searched_path = self._relative(start) if args["path"] is not None else None
        return _Done(ToolResult("ok", {"matches": matches, "truncated": truncated}), searched_path)

    @staticmethod
    def _python_files_and_directories(directory: Path) -> tuple[list[Path], list[Path]]:
        """ディレクトリの直下の、（`.py` の通常のファイル, ディレクトリ）。どちらも名前の順。"""
        files: list[Path] = []
        subdirectories: list[Path] = []
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return files, subdirectories
        for name in names:
            if name in _HIDDEN_NAMES:
                continue
            path = directory / name
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue
            # シンボリックリンクはたどらない。読むのは、通常のファイルだけ
            if stat.S_ISDIR(mode):
                subdirectories.append(path)
            elif stat.S_ISREG(mode) and name.endswith(PYTHON_SUFFIX):
                files.append(path)
        return files, subdirectories

    @staticmethod
    def _matching_lines(path: Path, pattern: str) -> list[tuple[int, str]]:
        """ファイルの中の、`pattern` を含む行。読めないファイル（大きすぎる、UTF-8 でない）は飛ばす。"""
        try:
            raw = read_regular_file(path, MAX_SEARCH_FILE_BYTES)
            text = raw.decode("utf-8") if raw is not None else ""
        except (OSError, FileTooLarge, UnicodeDecodeError):
            return []
        if pattern not in text:
            return []
        return [(number, line) for number, line in enumerate(_split_lines(text), start=1) if pattern in line]

    # ------------------------------------------------------------------
    # 書くツール
    # ------------------------------------------------------------------

    def _write_file(self, args: dict) -> _Done:
        relative, content = args["path"], args["content"]
        target = self._workspace.resolve_write(relative)
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            return _Done(_invalid("content"))
        # G-10 で落ちる文字（双方向の制御文字、見えない文字、CR）は、その場で断る
        if has_hidden_characters(content):
            return _Done(_invalid("content"))
        if len(data) > self._limits.max_write_bytes:
            return _Done(ToolResult("size_limit", {}))
        # 中身に依る規則（新しい __init__.py は、空のものだけ）
        if self._workspace.forbids_content(relative, data):
            return _Done(ToolResult("denied", {}))
        write_regular_file(target, data)
        return _Done(ToolResult("ok", {"bytes_written": len(data)}), relative)

    def _replace_in_file(self, args: dict) -> _Done:
        relative, old, new = args["path"], args["old"], args["new"]
        target = self._workspace.resolve_write(relative)
        if not target.exists():
            return _Done(ToolResult("not_found", {"missing": "file"}))
        text = self._read_text(target)
        if text is None:
            return _Done(_invalid("path"))
        if has_hidden_characters(new):
            return _Done(_invalid("new"))
        occurrences = text.count(old)
        if occurrences == 0:
            return _Done(ToolResult("not_found", {"missing": "old"}))
        if occurrences > 1:
            return _Done(_invalid("old", occurrences=occurrences))
        position = text.index(old)
        try:
            data = (text[:position] + new + text[position + len(old) :]).encode("utf-8")
        except UnicodeEncodeError:
            return _Done(_invalid("new"))
        if len(data) > self._limits.max_write_bytes:
            return _Done(ToolResult("size_limit", {}))
        write_regular_file(target, data)
        return _Done(ToolResult("ok", {"line": text.count("\n", 0, position) + 1}), relative)

    # ------------------------------------------------------------------
    # テスト実行と、終了の申告
    # ------------------------------------------------------------------

    def _run_tests(self, args: dict) -> _Done:
        paths: list[str] = args["paths"]
        tests_root = self._workspace.root.resolve() / TESTS_DIRECTORY
        relatives = []
        for path in paths:
            resolved = self._workspace.resolve_read(path)
            # たどった先も含めて、tests/ の下であること
            if path.split("/")[0] != TESTS_DIRECTORY or not resolved.is_relative_to(tests_root):
                raise PathDenied("test path is outside tests/")
            if not (resolved.is_dir() or (resolved.is_file() and resolved.name.endswith(PYTHON_SUFFIX))):
                return _Done(_invalid("paths"))
            relatives.append(self._relative(resolved) or "")
        # 1 回の制限時間と、全体の期限までの残りの、短いほうで止める
        timeout_seconds = self._limits.test_timeout_seconds
        if self._deadline is not None:
            timeout_seconds = min(timeout_seconds, max(self._deadline - self._clock(), 0.0))
        report = self._test_runner.run(self._workspace.root, list(paths), timeout_seconds=timeout_seconds)
        trace_path = " ".join(relatives)
        if getattr(report, "output_overflow", False):
            return _Done(ToolResult("size_limit", {}), trace_path)
        if report.timed_out:
            return _Done(ToolResult("timeout", {}), trace_path)
        values = list(report.outcomes.values())
        passed, failed, errors = values.count("passed"), values.count("failed"), values.count("error")
        failed_tests = [name for name, outcome in report.outcomes.items() if outcome in _FAILING_OUTCOMES]
        data = {
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": values.count("skipped"),
            "exit_code": report.exit_code,
            "failed_tests": [str(name)[:MAX_TEST_NAME_CHARS] for name in failed_tests[:MAX_FAILED_TESTS]],
            "output_tail": str(report.output_tail)[-OUTPUT_TAIL_CHARS:],
        }
        return _Done(ToolResult("ok", data), trace_path, tests_passed=passed, tests_failed=failed + errors)

    def _finish(self, args: dict) -> _Done:
        """形の正しい申告を受理する。受理のたびに上書きし、最後に受理したものが有効。"""
        self.finished = FinishClaim(
            outcome=args["outcome"], patch_kind=args["patch_kind"], explanation=args["explanation"]
        )
        return _Done(ToolResult("ok", {"accepted": True}))
