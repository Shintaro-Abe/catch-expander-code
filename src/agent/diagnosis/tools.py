"""実行診断役の 5 つのツールの中身。SDK には依存しない。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §5

モデルが決めるのは「どのツールを、どの引数で呼ぶか」だけ。どの実行を読めるか・上限・
結果の形・根拠が本物かは、すべて ToolSession が握る。入力はモデルが作った信頼できない値
として、SDK の検証に頼らずここで検証する。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from diagnosis.exception_types import ExceptionTypeAllowlist
from diagnosis.frames import FrameVerifier, exception_line_type
from diagnosis.models import (
    ACTION_TYPES,
    CAUSE_CATEGORIES,
    CAUSE_NATURES,
    CONFIDENCES,
    FAILED_STAGES,
    PROMPT_INDEX_PATTERN,
    PROMPT_SUBAGENTS,
    CodeLocation,
    Diagnosis,
    DiagnosisLimits,
    EvidenceRef,
    ToolCall,
    ToolResult,
)
from diagnosis.records import RecordNotFound, RecordReader, RecordTooLarge, RecordUnavailable
from diagnosis.redaction import mask_secrets, redact, truncate_middle, truncate_strings

logger = logging.getLogger("catch-expander-agent")

SUBMIT_TOOL = "submit_diagnosis"
TOOL_NAMES = ("get_execution_overview", "list_steps", "list_events", "get_prompt_record", SUBMIT_TOOL)
UNKNOWN_TOOL = "unknown"

MAX_EVIDENCE_ITEMS = 10
MAX_EVIDENCE_ID_CHARS = 96  # プロンプト記録の根拠 ID（PR-<役>-<番号>、最長 82 字）が収まる長さ
MAX_SUBMISSION_ATTEMPTS = 5  # 提出の試行（受理・不受理を問わない）。読み取りツールの上限とは別に数える
MAX_EVIDENCE_NOTE_CHARS = 200
MAX_ACTION_NOTE_CHARS = 300
MAX_SUMMARY_CHARS = 500
MAX_CODE_LOCATION_IDS = 5
MAX_FRAMES_PER_EVENT = 15
MAX_OFFSET = 100_000
MAX_OUTPUT_FILES = 20
MAX_REPORTED_FIELDS = 20
MAX_FIELD_NAME_CHARS = 64

_TOPIC_HEAD, _TOPIC_TAIL = 120, 80  # 合わせて 200 字
_PROMPT_HEAD, _PROMPT_TAIL = 1500, 1500
_OUTPUT_HEAD, _OUTPUT_TAIL = 2000, 3000
_FILE_NAME_CHARS = 120

# 数字は [0-9] と書く（\d は全角の数字にも一致するため）。検証は必ず fullmatch で行う。
_EVENT_TYPE_PATTERN = re.compile(r"[A-Za-z0-9_]{1,64}")
_EVENT_ID_PATTERN = re.compile(r"EV-[0-9]{1,6}")
_FRAME_ID_PATTERN = re.compile(r"FR-[0-9]{1,6}")
# 許可一覧に無い例外の型を、モデルに返す結果の中で置き換える文字列（記録の文字列をそのまま出さない）
UNLISTED_EXCEPTION_TYPE = "<unlisted>"
# イベントの外側の項目が、文字列でも数でもないときに置き換える文字列
OMITTED_VALUE = "<omitted>"

_OVERVIEW_FIELDS = (
    "status",
    "created_at",
    "completed_at",
    "topic",
    "category",
    "total_tokens_used",
    "total_input_tokens",
    "total_output_tokens",
    "total_cost_usd",
)
_STEP_FIELDS = ("step_id", "phase", "step_name", "step_order", "status", "started_at", "completed_at")
_EVENT_FIELDS = ("event_type", "timestamp", "status_at_emit")
_ENUM_FIELDS = (
    ("failed_stage", FAILED_STAGES),
    ("cause_category", CAUSE_CATEGORIES),
    ("cause_nature", CAUSE_NATURES),
    ("confidence", CONFIDENCES),
    ("action_type", ACTION_TYPES),
)
_TEXT_FIELDS = (("action_note", MAX_ACTION_NOTE_CHARS), ("summary", MAX_SUMMARY_CHARS))
_SUBMISSION_FIELDS = (
    *(name for name, _ in _ENUM_FIELDS),
    "evidence",
    *(name for name, _ in _TEXT_FIELDS),
    "exception_evidence_id",
    "code_location_ids",
)
# 根拠が無くても受理する分類（「分からない」は正当な結果）
_CATEGORIES_WITHOUT_EVIDENCE = ("unknown", "insufficient_records")
# 有効な根拠が無くて格下げしたときに、モデルが書いた要約・対処の説明の代わりに入れる文面
_DOWNGRADED_SUMMARY = "有効な根拠が示されなかったため、原因を特定できませんでした。"
_DOWNGRADED_ACTION_NOTE = "記録を人が確認してください。"


def _object_schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


# 入力の厳しいスキーマ（引数の形の定義）。SDK には、これを緩めたものを渡す（sdk_server.py）。
# 検証の正本は、この下の _validate_* の側。
TOOL_SCHEMAS: dict[str, dict] = {
    "get_execution_overview": _object_schema({}),
    "list_steps": _object_schema({}),
    "list_events": _object_schema(
        {
            "event_type": {"type": "string", "pattern": "^[A-Za-z0-9_]{1,64}$"},
            "offset": {"type": "integer", "minimum": 0},
        }
    ),
    "get_prompt_record": _object_schema(
        {
            "subagent": {"type": "string", "enum": list(PROMPT_SUBAGENTS)},
            "index": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
        },
        required=("subagent", "index"),
    ),
    SUBMIT_TOOL: _object_schema(
        {
            **{name: {"type": "string", "enum": list(values)} for name, values in _ENUM_FIELDS},
            "evidence": {
                "type": "array",
                "maxItems": MAX_EVIDENCE_ITEMS,
                "items": _object_schema(
                    {
                        "id": {"type": "string", "maxLength": MAX_EVIDENCE_ID_CHARS},
                        "note": {"type": "string", "maxLength": MAX_EVIDENCE_NOTE_CHARS},
                    },
                    required=("id", "note"),
                ),
            },
            **{name: {"type": "string", "maxLength": limit} for name, limit in _TEXT_FIELDS},
            "exception_evidence_id": {"type": "string", "pattern": "^EV-[0-9]+$"},
            "code_location_ids": {
                "type": "array",
                "maxItems": MAX_CODE_LOCATION_IDS,
                "items": {"type": "string", "pattern": "^FR-[0-9]+$"},
            },
        },
        required=(*(name for name, _ in _ENUM_FIELDS), "evidence", *(name for name, _ in _TEXT_FIELDS)),
    ),
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_execution_overview": (
        "診断する実行の概要（状態、開始・完了時刻、トピック、カテゴリ、トークン・費用の合計）を返す。"
    ),
    "list_steps": "診断する実行の段階の一覧（ID、状態、開始・完了時刻、結果の有無）を返す。",
    "list_events": (
        "診断する実行のイベントを時系列順に返す。event_type で種別を絞り込める。"
        "1 回の件数には上限があり、続きは next_offset を offset に渡して読む。"
        "スタックトレースを持つイベントには、コードの場所の一覧（frames）が付く。"
    ),
    "get_prompt_record": (
        "指定した役（subagent）と番号（index）の、プロンプトと出力を返す。長い本文は先頭と末尾だけを残す。"
        "index は、researcher なら段階の step_id、generator_text なら 0、generator_code ならコードの種別、"
        "reviewer_eval・reviewer_fix ならループの番号。"
    ),
    SUBMIT_TOOL: (
        "診断結果を提出する。最後に必ず 1 回呼ぶ。evidence の id には、ほかのツールが返した evidence_id だけを使う。"
        "exception_evidence_id には例外を記録したイベントの evidence_id を、"
        "code_location_ids には frames の frame_id を渡す。"
    ),
}


class _ResultTooLargeError(Exception):
    """1 回の結果が、末尾を落としても上限に収まらない。"""


@dataclass(frozen=True)
class _Event:
    event_type: str | None  # 絞り込みに使う、記録どおりの種別（文字列でなければ None）
    item: dict  # モデルに返す形（マスク・切り詰め済み）


def _field_names(keys: list) -> list[str]:
    """不正な項目の名前を返す。値は返さない。"""
    names = [key[:MAX_FIELD_NAME_CHARS] if isinstance(key, str) else "(non-string key)" for key in keys]
    return names[:MAX_REPORTED_FIELDS]


def _unknown_fields(args: dict, allowed: tuple[str, ...]) -> list:
    return [key for key in args if key not in allowed]


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _validate_no_arguments(args: dict) -> tuple[dict, list]:
    return {}, _unknown_fields(args, ())


def _validate_list_events(args: dict) -> tuple[dict, list]:
    invalid = _unknown_fields(args, ("event_type", "offset"))
    validated: dict[str, Any] = {}
    if "event_type" in args:
        if _matches(_EVENT_TYPE_PATTERN, args["event_type"]):
            validated["event_type"] = args["event_type"]
        else:
            invalid.append("event_type")
    if "offset" in args:
        offset = args["offset"]
        if isinstance(offset, int) and not isinstance(offset, bool) and 0 <= offset <= MAX_OFFSET:
            validated["offset"] = offset
        else:
            invalid.append("offset")
    return validated, invalid


def _validate_get_prompt_record(args: dict) -> tuple[dict, list]:
    invalid = _unknown_fields(args, ("subagent", "index"))
    subagent, index = args.get("subagent"), args.get("index")
    if not (isinstance(subagent, str) and subagent in PROMPT_SUBAGENTS):
        invalid.append("subagent")
    # S3 のキーに区切り文字が入り込まないよう、英数字・ハイフン・下線だけを通す
    if not _matches(PROMPT_INDEX_PATTERN, index):
        invalid.append("index")
    return {"subagent": subagent, "index": index}, invalid


def _valid_evidence(value: object) -> bool:
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_ITEMS:
        return False
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "note"}:
            return False
        evidence_id, note = item["id"], item["note"]
        if not isinstance(evidence_id, str) or not 1 <= len(evidence_id) <= MAX_EVIDENCE_ID_CHARS:
            return False
        if not isinstance(note, str) or len(note) > MAX_EVIDENCE_NOTE_CHARS:
            return False
    return True


def _valid_code_location_ids(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_CODE_LOCATION_IDS
        and all(_matches(_FRAME_ID_PATTERN, item) for item in value)
    )


def _validate_submit_diagnosis(args: dict) -> tuple[dict, list]:
    # fixer_eligible など、スキーマにない項目を含む提出は受理しない（判定はコードが行う）
    invalid = _unknown_fields(args, _SUBMISSION_FIELDS)
    for name, values in _ENUM_FIELDS:
        if not (isinstance(args.get(name), str) and args[name] in values):
            invalid.append(name)
    for name, limit in _TEXT_FIELDS:
        if not (isinstance(args.get(name), str) and len(args[name]) <= limit):
            invalid.append(name)
    if not _valid_evidence(args.get("evidence")):
        invalid.append("evidence")
    if "exception_evidence_id" in args and not _matches(_EVENT_ID_PATTERN, args["exception_evidence_id"]):
        invalid.append("exception_evidence_id")
    if "code_location_ids" in args and not _valid_code_location_ids(args["code_location_ids"]):
        invalid.append("code_location_ids")
    return dict(args), invalid


_VALIDATORS: dict[str, Callable[[dict], tuple[dict, list]]] = {
    "get_execution_overview": _validate_no_arguments,
    "list_steps": _validate_no_arguments,
    "list_events": _validate_list_events,
    "get_prompt_record": _validate_get_prompt_record,
    SUBMIT_TOOL: _validate_submit_diagnosis,
}


def _submission_trace_args(submission: dict) -> dict:
    """足取りに残す提出の引数。自由記述（要約・説明・根拠の note）は残さない。"""
    kept = (*(name for name, _ in _ENUM_FIELDS), "exception_evidence_id", "code_location_ids")
    return {name: submission[name] for name in kept if name in submission}


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def _scalar(value: Any) -> Any:
    """文字列か数（と None）だけを通す。それ以外の型は、中身を出さずに固定の文字列にする。"""
    return value if value is None or isinstance(value, str | int | float) else OMITTED_VALUE


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


class ToolSession:
    """診断 1 回ぶんの、上限・根拠 ID・足取り・提出を持つ。

    ハンドラーは別スレッドから呼ぶので、呼び出しは 1 件ずつロックの中で処理する。
    """

    def __init__(
        self,
        reader: RecordReader,
        limits: DiagnosisLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
        code_root: str | os.PathLike[str] | None = None,
        deadline: float | None = None,
    ) -> None:
        self._reader = reader
        self._limits = limits
        self._clock = clock
        # deadline は、診断全体の期限（clock と同じ時計の時刻）。None なら期限なし
        self._deadline = deadline
        # code_root は、フレームと例外の型を突き合わせる「コードの根」。None なら diagnosis パッケージの親
        self._frame_verifier = FrameVerifier(code_root)
        self._exception_types = ExceptionTypeAllowlist(code_root)
        self._lock = threading.Lock()
        self.trace: list[ToolCall] = []
        self.issued_evidence_ids: set[str] = set()
        self._issued_frame_ids: set[str] = set()
        self._call_count = 0
        self._submit_attempts = 0
        self._total_bytes = 0
        self._events: list[_Event] | None = None
        self._events_truncated = False
        self._frames: dict[str, CodeLocation] = {}
        self._frame_events: dict[str, str] = {}  # フレーム ID → そのフレームが由来するイベントの根拠 ID
        self._error_types: dict[str, str] = {}
        self._submission: dict | None = None
        self._readers: dict[str, Callable[[dict], tuple[dict, list[str], list[str]]]] = {
            "get_execution_overview": self._get_execution_overview,
            "list_steps": self._list_steps,
            "list_events": self._list_events,
            "get_prompt_record": self._get_prompt_record,
        }

    @property
    def has_submission(self) -> bool:
        return self._submission is not None

    def call(self, name: str, args: object) -> ToolResult:
        """ツールを 1 回呼ぶ。例外を出さず、失敗は固定の分類で返す。失敗した呼び出しも足取りに残す。"""
        with self._lock:
            started = self._clock()
            # 足取りに残す名前は、決まった 5 つのどれか、または unknown
            tool = name if isinstance(name, str) and name in TOOL_NAMES else UNKNOWN_TOOL
            # 期限は、ロックを得てから確かめる（待っている間に期限を過ぎた呼び出しも断る）。
            # 期限の前に始めた処理は、最後まで行う。断った呼び出しは、上限にも提出の試行にも数えない
            if self._deadline is not None and started >= self._deadline:
                self.trace.append(ToolCall(len(self.trace) + 1, tool, {}, "deadline_exceeded", 0, 0))
                return ToolResult("deadline_exceeded", {})
            if tool == SUBMIT_TOOL:
                # 提出の試行の上限を超えたぶんは、足取りにも積まない（受理済みの提出も置き換えない）
                if self._submit_attempts >= MAX_SUBMISSION_ATTEMPTS:
                    return ToolResult("call_limit", {})
                self._submit_attempts += 1
            try:
                result, validated, evidence_count = self._run(tool, args)
            except Exception:
                logger.exception("Diagnosis tool failed unexpectedly: tool=%s", tool)
                result, validated, evidence_count = ToolResult("internal_error", {}), {}, 0
            self.trace.append(
                ToolCall(
                    seq=len(self.trace) + 1,
                    tool=tool,
                    args=validated,
                    outcome=result.outcome,
                    evidence_count=evidence_count,
                    duration_ms=round((self._clock() - started) * 1000),
                )
            )
            return result

    def _run(self, tool: str, args: object) -> tuple[ToolResult, dict, int]:
        if tool == UNKNOWN_TOOL:
            return ToolResult("invalid_input", {"invalid_fields": ["tool"]}), {}, 0
        is_submit = tool == SUBMIT_TOOL
        # 提出は、読み取りツールの上限に数えない（上限に達した後でも「分からない」を提出できるようにする）
        if not is_submit:
            if self._call_count >= self._limits.max_tool_calls:
                return ToolResult("call_limit", {}), {}, 0
            self._call_count += 1
        if not isinstance(args, dict):
            return ToolResult("invalid_input", {"invalid_fields": ["arguments"]}), {}, 0
        validated, invalid = _VALIDATORS[tool](args)
        if invalid:
            return ToolResult("invalid_input", {"invalid_fields": _field_names(invalid)}), {}, 0
        if is_submit:
            return self._submit(validated), _submission_trace_args(validated), 0
        if self._total_bytes >= self._limits.max_total_bytes:
            return ToolResult("size_limit", {}), validated, 0

        evidence_ids: list[str] = []
        try:
            data, evidence_ids, frame_ids = self._readers[tool](validated)
        except RecordNotFound:
            result = ToolResult("not_found", {})
        except RecordUnavailable:
            result = ToolResult("unavailable", {})
        except (RecordTooLarge, _ResultTooLargeError):
            result = ToolResult("size_limit", {})
        except Exception:
            # 例外の文面は結果に入れない（記録の中身や内部の情報がモデルに渡るのを防ぐ）
            logger.exception("Diagnosis tool failed: tool=%s", tool)
            result = ToolResult("internal_error", {})
        else:
            result = ToolResult("ok", data)
            # 根拠 ID は、実際に返したものにだけ発行する
            self.issued_evidence_ids.update(evidence_ids)
            self._issued_frame_ids.update(frame_ids)
        self._total_bytes += self._size(result.data)
        return result, validated, len(evidence_ids)

    # ------------------------------------------------------------------
    # 結果の整形
    # ------------------------------------------------------------------

    def _clean(self, obj: Any) -> Any:
        """識別子の除去 → マスク → 切り詰め。"""
        return truncate_strings(redact(obj), self._limits.max_string_chars)

    @staticmethod
    def _size(data: dict) -> int:
        return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _fits(self, data: dict) -> bool:
        return self._size(data) <= self._limits.max_result_bytes

    def _drop_tail(self, data: dict, key: str, *, keep: int = 0) -> None:
        """1 回の大きさの上限に収まるまで、一覧の末尾から落とす。"""
        while len(data[key]) > keep and not self._fits(data):
            data[key].pop()
            data["truncated"] = True

    # ------------------------------------------------------------------
    # 読み取りツール
    # ------------------------------------------------------------------

    def _get_execution_overview(self, _args: dict) -> tuple[dict, list[str], list[str]]:
        record = self._reader.get_execution()
        if record is None:
            raise RecordNotFound("execution record does not exist")
        # 返す項目を選んで取り出す（Slack の識別子などは、ここで落ちる）
        fields = {name: record.get(name) for name in _OVERVIEW_FIELDS}
        if isinstance(fields["topic"], str):
            fields["topic"] = truncate_middle(mask_secrets(fields["topic"]), _TOPIC_HEAD, _TOPIC_TAIL)[0]
        data = {"evidence_id": "EX-1", **self._clean(fields)}
        if not self._fits(data):
            raise _ResultTooLargeError
        return data, ["EX-1"], []

    def _list_steps(self, _args: dict) -> tuple[dict, list[str], list[str]]:
        records = self._reader.list_steps()
        # result は Web から取った内容を含み大きいので、本体は読まず、有無だけを示す
        with_result = self._reader.list_step_ids_with_result()
        results_truncated = bool(self._reader.step_results_truncated)
        steps = []
        for number, step in enumerate(records, start=1):
            fields = self._clean({name: step.get(name) for name in _STEP_FIELDS})
            has_result: bool | None = step.get("step_id") in with_result
            if not has_result and results_truncated:
                has_result = None  # 有無を調べる読み取りを上限で読み止めたので、分からない
            steps.append({"evidence_id": f"ST-{number}", **fields, "has_result": has_result})
        # 読み込みを上限で読み止めたときも、1 回の結果の上限で末尾を落としたときと同じ印を使う
        data = {"steps": steps, "total": len(steps), "truncated": bool(self._reader.steps_truncated)}
        self._drop_tail(data, "steps")
        return data, [step["evidence_id"] for step in data["steps"]], []

    def _load_events(self) -> list[_Event]:
        """イベントを全件読み、根拠 ID・フレーム ID を振る。ID は絞り込みや offset で変わらない。"""
        events = []
        frames: dict[str, CodeLocation] = {}
        frame_events: dict[str, str] = {}
        error_types: dict[str, str] = {}
        records = self._reader.list_events()
        for number, record in enumerate(records, start=1):
            evidence_id = f"EV-{number}"
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            # フレームは、切り詰める前の全文から取り、実際のコードにある場所だけを残す。
            # フレーム ID は、突き合わせを通ったものにだけ、読み込んだ順に振る
            event_frames = []
            for location in self._frame_verifier.extract(payload.get("stack_trace"), MAX_FRAMES_PER_EVENT):
                frame_id = f"FR-{len(frames) + 1}"
                frames[frame_id] = location
                frame_events[frame_id] = evidence_id
                event_frames.append(
                    {"frame_id": frame_id, "path": location.path, "function": location.function, "line": location.line}
                )
            # 例外の型は、コード側の許可一覧にある名前だけを通す。一覧に無い値は、モデルにもそのまま返さない
            error_type = payload.get("error_type")
            if self._exception_types.allows(error_type):
                # 受け渡しに使えるのは、トレースの最初のブロックを終える例外の行の型と一致するときだけ
                # （連鎖した例外で、内側の例外の場所と、外側の例外の型を組にしない）。
                # 一致しなくても、モデルに返す結果には、今までどおり見せる
                if exception_line_type(payload.get("stack_trace")) == error_type:
                    error_types[evidence_id] = error_type
            elif error_type is not None:
                payload = {**payload, "error_type": UNLISTED_EXCEPTION_TYPE}
            # 外側の項目は、文字列か数だけを通す（巨大な配列などで、payload を落としても収まらなくなるのを防ぐ）
            outer = {name: _scalar(record.get(name)) for name in _EVENT_FIELDS}
            fields = self._clean({**outer, "payload": payload})
            item = {"evidence_id": evidence_id, **fields, "frames": event_frames}
            # 絞り込みは、記録どおりの種別で行う（文字列でない種別は、どの絞り込みにも一致しない）
            event_type = record.get("event_type")
            events.append(_Event(event_type=event_type if isinstance(event_type, str) else None, item=item))
        self._frames = frames
        self._frame_events = frame_events
        self._error_types = error_types
        # 件数または合計の大きさの上限で読み止めたか（reader が持たない場合は、読み止めていないとみなす）
        self._events_truncated = bool(getattr(self._reader, "events_truncated", False))
        return events

    def _list_events(self, args: dict) -> tuple[dict, list[str], list[str]]:
        if self._events is None:
            self._events = self._load_events()
        selected = self._events
        if "event_type" in args:
            selected = [event for event in selected if event.event_type == args["event_type"]]
        offset = args.get("offset", 0)
        page = [event.item for event in selected[offset : offset + self._limits.events_page_size]]
        data: dict[str, Any] = {
            "events": page,
            "total": len(selected),
            "next_offset": None,
            "has_more": False,
            # 読み込みを上限で読み止めたときも、1 回の結果の上限で末尾を落としたときと同じ印を使う
            "truncated": self._events_truncated,
        }
        self._drop_tail(data, "events", keep=1)
        if page and not self._fits(data):
            # 1 件でも収まらないイベントは、payload を落として返す（次の offset へ進めるようにする）
            page[0] = {**page[0], "payload": {}, "payload_omitted": True}
            data["truncated"] = True
        if page and not self._fits(data):
            # それでも収まらなければ、根拠 ID と種別だけの最小の形にする（フレームも返さない）
            page[0] = {"evidence_id": page[0]["evidence_id"], "event_type": page[0]["event_type"]}
        if page and not self._fits(data):
            page[0] = {**page[0], "event_type": OMITTED_VALUE}
        if not self._fits(data):
            raise _ResultTooLargeError
        # next_offset は「返さなかった最初のイベント」を指す
        next_offset = offset + len(page)
        if next_offset < len(selected):
            data["next_offset"], data["has_more"] = next_offset, True
        evidence_ids = [item["evidence_id"] for item in page]
        frame_ids = [frame["frame_id"] for item in page for frame in item.get("frames", [])]
        return data, evidence_ids, frame_ids

    def _get_prompt_record(self, args: dict) -> tuple[dict, list[str], list[str]]:
        subagent, index = args["subagent"], args["index"]
        record = self._reader.get_prompt_record(subagent, index)
        prompt, output = _as_text(record.get("prompt")), _as_text(record.get("output"))
        files = record.get("output_files") if isinstance(record.get("output_files"), dict) else {}
        # ファイルは名前と大きさだけを返す
        output_files = [
            {"name": truncate_strings(mask_secrets(_as_text(name)), _FILE_NAME_CHARS), "chars": len(_as_text(content))}
            for name, content in list(files.items())[:MAX_OUTPUT_FILES]
        ]
        evidence_id = f"PR-{subagent}-{index}"
        masked_prompt, masked_output = mask_secrets(prompt), mask_secrets(output)
        # 1 回の大きさの上限に収まらないとき（マルチバイトの本文など）は、残す長さを半分ずつ縮める
        for divisor in (1, 2, 4, 8, 16):
            data = {
                "evidence_id": evidence_id,
                "subagent": subagent,
                "index": index,
                "recorded_at": self._clean(record.get("recorded_at")),
                "prompt": truncate_middle(masked_prompt, _PROMPT_HEAD // divisor, _PROMPT_TAIL // divisor)[0],
                "prompt_chars": len(prompt),
                "output": truncate_middle(masked_output, _OUTPUT_HEAD // divisor, _OUTPUT_TAIL // divisor)[0],
                "output_chars": len(output),
                "output_files": output_files,
                "output_file_count": len(files),
            }
            if divisor > 1:
                data["truncated"] = True
            if self._fits(data):
                return data, [evidence_id], []
        raise _ResultTooLargeError

    # ------------------------------------------------------------------
    # 提出と照合
    # ------------------------------------------------------------------

    def _submit(self, submission: dict) -> ToolResult:
        """形の正しい提出を受理する。受理のたびに上書きし、最後に受理したものが正本。"""
        evidence = [EvidenceRef(id=item["id"], note=mask_secrets(item["note"])) for item in submission["evidence"]]
        code_location_ids = list(submission.get("code_location_ids", []))
        self._submission = {
            **submission,
            "evidence": evidence,
            "action_note": mask_secrets(submission["action_note"]),
            "summary": mask_secrets(submission["summary"]),
            "code_location_ids": code_location_ids,
        }
        # 使えない ID は、ここで知らせる（モデルが出し直せるように）。取り除くのは finalize
        invalid_ids = _unique([ref.id for ref in evidence if ref.id not in self.issued_evidence_ids])
        # 場所は、発行していない ID と、例外の根拠にしたイベント以外に由来する ID を知らせる
        # （同じ場所の重複をまとめたぶんは、取り除いたものに数えない）
        event = self._exception_event(submission.get("exception_evidence_id"))
        dropped = _unique(
            [
                frame_id
                for frame_id in code_location_ids
                if frame_id not in self._issued_frame_ids
                or (event is not None and self._frame_events.get(frame_id) != event)
            ]
        )
        return ToolResult(
            "ok",
            {"accepted": True, "invalid_evidence_ids": invalid_ids, "dropped_code_location_ids": dropped},
        )

    def _exception_event(self, exception_evidence_id: object) -> str | None:
        """例外の根拠として使えるイベントの根拠 ID を返す。使えなければ None。"""
        if exception_evidence_id in self.issued_evidence_ids and exception_evidence_id in self._error_types:
            return exception_evidence_id
        return None

    def _usable_frame_ids(self, exception_evidence_id: object, selected: list[str]) -> list[str]:
        """選ばれたフレームのうち、受け渡しに使えるものを、スタックトレースに現れた順で返す。

        使えるのは、例外の型を取ったイベントに由来し、発行済みのものだけ。並びは、モデルが挙げた順ではなく
        フレーム ID を振った順（= トレースに現れた順）。同じ場所（パス・関数・行）は 1 つにする。
        例外の根拠が使えないときは、空。
        """
        event = self._exception_event(exception_evidence_id)
        if event is None:
            return []
        chosen = set(selected)
        usable: list[str] = []
        seen: set[CodeLocation] = set()
        for frame_id, location in self._frames.items():
            if self._frame_events[frame_id] != event or frame_id not in self._issued_frame_ids:
                continue
            if frame_id in chosen and location not in seen:
                seen.add(location)
                usable.append(frame_id)
        return usable

    def finalize(self) -> Diagnosis:
        """受理した提出を、発行した根拠 ID と照合して診断結果にする（コードが正本を作る）。"""
        with self._lock:
            submission = self._submission
            if submission is None:
                return Diagnosis.unknown()

            valid: dict[str, EvidenceRef] = {}
            invalid_count = 0
            for ref in submission["evidence"]:
                if ref.id not in self.issued_evidence_ids:
                    invalid_count += 1
                else:
                    valid.setdefault(ref.id, ref)
            evidence = list(valid.values())

            cause_category = submission["cause_category"]
            cause_nature = submission["cause_nature"]
            confidence = submission["confidence"]
            action_type = submission["action_type"]
            action_note = submission["action_note"]
            summary = submission["summary"]
            if cause_category not in _CATEGORIES_WITHOUT_EVIDENCE and not evidence:
                # 根拠のない診断は、分類だけでなく、対処とモデルが書いた自由記述も出さない
                cause_category, cause_nature, confidence = "unknown", "unknown", "low"
                action_type = "human_investigation"
                action_note, summary = _DOWNGRADED_ACTION_NOTE, _DOWNGRADED_SUMMARY

            # 例外の型とコードの場所は、モデルが選んだ ID からコードが登録簿を引く。
            # 場所は、例外の型を取ったイベントのものだけ。順序と重複は、コードが決める
            exception_evidence_id = self._exception_event(submission.get("exception_evidence_id"))
            exception_type = self._error_types[exception_evidence_id] if exception_evidence_id is not None else None
            frame_ids = self._usable_frame_ids(exception_evidence_id, submission["code_location_ids"])

            return Diagnosis(
                failed_stage=submission["failed_stage"],
                cause_category=cause_category,
                cause_nature=cause_nature,
                confidence=confidence,
                evidence=evidence,
                invalid_evidence_count=invalid_count,
                action_type=action_type,
                action_note=action_note,
                summary=summary,
                exception_type=exception_type,
                code_locations=[self._frames[frame_id] for frame_id in frame_ids],
                exception_evidence_id=exception_evidence_id,
                code_location_evidence_ids=[self._frame_events[frame_id] for frame_id in frame_ids],
            )
