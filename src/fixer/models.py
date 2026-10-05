"""修正役の決まった値とデータの型。

設計詳細: `.steering/20261004-execution-fixer/design.md` §3
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from diagnosis.models import CodeLocation

RUN_STATUSES = ("proposed", "rejected", "declined", "incomplete", "not_eligible", "failed")
TOOL_OUTCOMES = (
    "ok",
    "invalid_input",
    "not_found",
    "denied",
    "call_limit",
    "size_limit",
    "timeout",
    "internal_error",
    "deadline_exceeded",
)
TOOL_NAMES = ("list_files", "read_file", "search_code", "write_file", "replace_in_file", "run_tests", "finish")
GATE_IDS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10")
PATCH_KINDS = ("symptomatic", "structural", "unspecified")


@dataclass(frozen=True)
class FixLimits:
    """修正 1 回の上限。"""

    max_turns: int = 30
    timeout_seconds: float = 1200.0  # 修正役（SDK）の全体
    max_tool_calls: int = 120  # run_tests と finish を除く
    max_test_runs: int = 10
    test_timeout_seconds: float = 300.0  # run_tests 1 回、検査のテスト実行 1 回
    gate_timeout_seconds: float = 900.0  # 合否の検査（テスト 4 回とリント 2 回）の合計
    max_read_bytes: int = 60_000  # read_file 1 回
    max_write_bytes: int = 200_000  # ファイル 1 つ
    max_changed_files: int = 5  # G-9
    max_changed_lines: int = 200  # G-9（足した行＋消した行）
    max_explanation_chars: int = 800
    max_budget_usd: float | None = None  # None なら SDK に渡さない


@dataclass(frozen=True)
class VerifiedLocation:
    """複製のコードと突き合わせて、コードが組み立て直した場所。"""

    path: str  # 複製の根からの相対パス。例: src/agent/orchestrator.py
    function: str
    line: int


@dataclass(frozen=True)
class HandoffInput:
    """形の検証を通った受け渡し。固定項目だけを持つ（自由記述は持たない）。"""

    execution_id: str
    failed_stage: str
    cause_category: str
    cause_nature: str
    confidence: str
    exception_type: str | None
    code_locations: tuple[CodeLocation, ...]  # /app/ で始まるパス。複製との突き合わせは verify_locations
    action_type: str
    valid_evidence_count: int
    fixer_eligible: bool


@dataclass(frozen=True)
class FixToolCall:
    """足取りの 1 件。"""

    seq: int  # 1 始まり
    tool: str  # TOOL_NAMES、または "unknown"
    path: str | None  # 解決できた相対パス。パスの無いツール、検証や解決に落ちた呼び出しは None
    outcome: str  # TOOL_OUTCOMES
    duration_ms: int
    tests_passed: int | None = None  # run_tests が走ったときだけ
    tests_failed: int | None = None


@dataclass(frozen=True)
class GateResult:
    """合否の検査 1 件の結果。"""

    gate: str  # GATE_IDS
    passed: bool
    detail: str  # コードが作る短い文。モデルの文字列は入れない


@dataclass(frozen=True)
class ToolResult:
    """ツールがモデルに返す結果。"""

    outcome: str
    data: dict

    @property
    def is_error(self) -> bool:
        return self.outcome != "ok"

    def to_text(self) -> str:
        text = json.dumps({"outcome": self.outcome, **self.data}, ensure_ascii=False)
        # UTF-8 にできない文字（ファイル名に由来する単独のサロゲートなど）は、置き換える
        return text.encode("utf-8", errors="replace").decode("utf-8")


@dataclass
class FixRun:
    """修正 1 回の結果。"""

    status: str  # RUN_STATUSES
    # committed / G1+G2 など / no_changes / declined_with_changes / no_finish / max_turns / max_budget / timeout /
    # unknown_exception_type / no_verified_location / conditions_not_met / not_a_repository / base_not_found /
    # branch_exists / invalid_out_dir / invalid_handoff / setup_error / tool_isolation_failed / sdk_error /
    # commit_error / internal_error / repository_tampered / unsupported_attributes / unisolated_environment /
    # workspace_tampered
    stop_detail: str
    execution_id: str  # 受け渡しが検証に通らなかったときは空
    base_commit: str  # 解決した SHA。解決できなかったときは空
    branch: str | None  # proposed のときだけ
    commit: str | None
    locations: list[VerifiedLocation]
    changed_files: list[str]
    gates: list[GateResult]  # 走らせた検査だけ
    trace: list[FixToolCall]
    patch_kind: str  # PATCH_KINDS
    explanation: str  # モデルの説明（マスク済み・長さの上限つき・未検証）
    num_turns: int | None
    duration_ms: int
    usage: dict[str, int]  # input_tokens / output_tokens / total_tokens
    total_cost_usd: float | None
    # 元のリポジトリの .git/hooks または .git/config が、実行の間に変わった（終わりの照合の結果）
    repository_changed: bool = False
