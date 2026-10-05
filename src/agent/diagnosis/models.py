"""実行診断役の決まった値とデータの型。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §3
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

FAILED_STAGES = (
    "analyzing",
    "planning",
    "researching",
    "generating",
    "reviewing",
    "storing",
    "notifying",
    "none",
    "unknown",
)
CAUSE_CATEGORIES = (
    "rate_limit",
    "external_service_error",
    "research_failed",
    "output_validation_failed",
    "review_not_passed",
    "auth_or_environment",
    "insufficient_records",
    "unknown",
)
CAUSE_NATURES = ("code_defect", "transient", "input_or_prompt", "environment_or_config", "unknown")
CONFIDENCES = ("high", "medium", "low")
ACTION_TYPES = ("retry_later", "retry_now", "review_config", "fix_code", "no_action", "human_investigation")
RUN_STATUSES = ("completed", "incomplete", "failed")
TOOL_OUTCOMES = (
    "ok",
    "invalid_input",
    "not_found",
    "unavailable",
    "call_limit",
    "size_limit",
    "internal_error",
    "deadline_exceeded",
)
PROMPT_SUBAGENTS = ("researcher", "generator_text", "generator_code", "reviewer_eval", "reviewer_fix", "generator")

# プロンプト記録の番号の形。S3 のキーに区切り文字が入り込まないよう、英数字・ハイフン・下線だけを通す。
# 検証は fullmatch で行う（^...$ と match では、末尾に改行のある値が通る）。
PROMPT_INDEX_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

HANDOFF_SCHEMA_VERSION = 1
_UNKNOWN_SUMMARY = "原因を特定できませんでした。"


@dataclass(frozen=True)
class DiagnosisLimits:
    """診断 1 回の上限。"""

    max_turns: int = 15
    timeout_seconds: float = 180.0
    max_tool_calls: int = 20
    max_total_bytes: int = 200_000  # ツール結果の累計
    max_result_bytes: int = 20_000  # ツール結果 1 回
    max_string_chars: int = 800  # 文字列項目 1 つ
    events_page_size: int = 30
    max_budget_usd: float | None = None  # None なら SDK に渡さない


@dataclass(frozen=True)
class CodeLocation:
    """スタックトレースからコードが取り出した場所。"""

    path: str
    function: str
    line: int


@dataclass(frozen=True)
class EvidenceRef:
    """診断結果が指す根拠。"""

    id: str
    note: str


@dataclass(frozen=True)
class ToolCall:
    """足取りの 1 件。"""

    seq: int  # 1 始まり
    tool: str
    args: dict  # 検証後の引数。検証に落ちたときは {}
    outcome: str  # TOOL_OUTCOMES
    evidence_count: int
    duration_ms: int


@dataclass(frozen=True)
class ToolResult:
    """ツールがモデルに返す結果。"""

    outcome: str
    data: dict

    @property
    def is_error(self) -> bool:
        return self.outcome != "ok"

    def to_text(self) -> str:
        return json.dumps({"outcome": self.outcome, **self.data}, ensure_ascii=False)


@dataclass
class Diagnosis:
    """照合を終えた診断結果。"""

    failed_stage: str
    cause_category: str
    cause_nature: str
    confidence: str
    evidence: list[EvidenceRef]  # 照合後に残った有効なものだけ
    invalid_evidence_count: int
    action_type: str
    action_note: str
    summary: str
    exception_type: str | None
    code_locations: list[CodeLocation]
    # 「修正役に渡せる」の判定に使う、つながりの記録（受け渡しには入れない）
    exception_evidence_id: str | None = None  # 例外の型を取ったイベントの根拠 ID
    code_location_evidence_ids: list[str] = field(default_factory=list)  # code_locations と同じ並び

    @classmethod
    def unknown(cls) -> Diagnosis:
        """「不明」の既定値を返す。"""
        return cls(
            failed_stage="unknown",
            cause_category="unknown",
            cause_nature="unknown",
            confidence="low",
            evidence=[],
            invalid_evidence_count=0,
            action_type="human_investigation",
            action_note="",
            summary=_UNKNOWN_SUMMARY,
            exception_type=None,
            code_locations=[],
        )


@dataclass(frozen=True)
class DiagnosisHandoff:
    """受け渡し用の診断結果。決まった値と、形を検証した識別子だけを持つ。"""

    schema_version: int
    execution_id: str
    failed_stage: str
    cause_category: str
    cause_nature: str
    confidence: str
    exception_type: str | None
    code_locations: tuple[CodeLocation, ...]
    action_type: str
    valid_evidence_count: int
    fixer_eligible: bool

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["code_locations"] = [asdict(location) for location in self.code_locations]
        return data


@dataclass
class DiagnosisRun:
    """診断 1 回の結果。"""

    execution_id: str
    status: str  # RUN_STATUSES
    # submitted / max_turns / max_budget / timeout / no_submission / sdk_error / setup_error /
    # tool_isolation_failed / invalid_execution_id
    stop_detail: str
    diagnosis: Diagnosis
    handoff: DiagnosisHandoff
    trace: list[ToolCall]
    observed_tool_uses: list[str]  # 受信ループが見たツール名（照らし合わせ用）
    num_turns: int | None
    duration_ms: int
    usage: dict[str, int]  # input_tokens / output_tokens / total_tokens
    total_cost_usd: float | None
