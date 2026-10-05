"""5 つの筋書き（AC-19）の偽の記録。

イベントの形は、実際に emit している箇所に合わせてある:
``src/agent/orchestrator.py``（``workflow_planned``・``subagent_*``・``research_completed``・
``review_completed``・``error``・``execution_completed``）、``src/observability/event_emitter.py``
（``api_call_completed``・``rate_limit_hit``）、``src/trigger/app.py``（``topic_received``）。

このファイルは ``diagnosis`` パッケージに依存しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .fakes import PROMPTS_BUCKET

# 記録に入っている Slack の識別子。ツールの結果・イベント・ログ・受け渡しのどこにも出てはいけない。
SLACK_USER_ID = "U0123ABCDE"
SLACK_CHANNEL = "C0456FGHIJ"
SLACK_THREAD_TS = "1759300000.000100"
USER_ID_HASH = "ab12cd34ef56ab78"
SLACK_IDENTIFIERS = (SLACK_USER_ID, SLACK_CHANNEL, SLACK_THREAD_TS, USER_ID_HASH)


@dataclass
class Scenario:
    name: str
    execution_id: str
    execution: dict | None
    steps: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    prompts: dict[tuple[str, str], dict] = field(default_factory=dict)
    prompts_bucket: str = PROMPTS_BUCKET
    s3_error: Exception | None = None
    # 記録の中身にだけ現れる目印。イベントの payload・ログ・受け渡しに出てはいけない（AC-24・AC-29）。
    record_markers: tuple[str, ...] = ()


def make_execution(execution_id: str, *, status: str, topic: str, **extra: object) -> dict:
    record = {
        "execution_id": execution_id,
        "user_id": SLACK_USER_ID,
        "topic": topic,
        "status": status,
        "slack_channel": SLACK_CHANNEL,
        "slack_thread_ts": SLACK_THREAD_TS,
        "created_at": "2026-10-01T09:00:00.000000+00:00",
        "completed_at": "2026-10-01T09:07:30.000000+00:00",
        "category": "技術",
        "intent": "仕組みを理解したい",
        "total_tokens_used": 12345,
        "total_input_tokens": 9000,
        "total_output_tokens": 3345,
        "total_cost_usd": 0.4321,
        "ttl": 1790000000,
    }
    record.update(extra)
    return record


def make_step(execution_id: str, order: int, *, status: str, result: dict | None = None) -> dict:
    step = {
        "execution_id": execution_id,
        "step_id": f"research-{order}",
        "phase": "research",
        "step_name": f"調査ステップ {order}",
        "step_order": order,
        "status": status,
        "started_at": f"2026-10-01T09:01:0{order}.000000+00:00",
        "completed_at": f"2026-10-01T09:02:0{order}.000000+00:00",
        "ttl": 1790000000,
    }
    if result is not None:
        step["result"] = result
    return step


def make_event(
    execution_id: str,
    sequence: int,
    event_type: str,
    payload: dict,
    *,
    status_at_emit: str = "in_progress",
) -> dict:
    timestamp = f"2026-10-01T09:{sequence // 60:02d}:{sequence % 60:02d}.000Z"
    return {
        "execution_id": execution_id,
        "sk": f"{timestamp}#{sequence:05d}",
        "event_type": event_type,
        "timestamp": timestamp,
        "sequence_number": sequence,
        "status_at_emit": status_at_emit,
        "payload": payload,
        "ttl": 1790000000,
        "gsi_pk": "GLOBAL",
    }


def make_stack_trace(frames: list[tuple[str, int, str]], error_line: str) -> str:
    lines = ["Traceback (most recent call last):"]
    for path, line, function in frames:
        lines.append(f'  File "{path}", line {line}, in {function}')
        lines.append("    do_something()")
    lines.append(error_line)
    return "\n".join(lines) + "\n"


def _topic_received(execution_id: str, topic: str) -> dict:
    return make_event(
        execution_id,
        1,
        "topic_received",
        {"topic": topic, "user_id_hash": USER_ID_HASH, "channel_id": SLACK_CHANNEL, "workflow_run_id": execution_id},
    )


def _workflow_planned(execution_id: str, sequence: int = 2) -> dict:
    return make_event(
        execution_id,
        sequence,
        "workflow_planned",
        {
            "topic_category": "技術",
            "expected_deliverable_type": "report",
            "deliverable_types": ["report"],
            "planned_subagents": ["researcher", "generator", "reviewer"],
            "planning_summary": "仕組みを理解したい",
            "storage_targets": ["notion"],
        },
    )


def _execution_completed(execution_id: str, sequence: int, status: str) -> dict:
    return make_event(
        execution_id,
        sequence,
        "execution_completed",
        {
            "status": status,
            "total_duration_ms": 450000,
            "total_tokens_used": 12345,
            "total_input_tokens": 9000,
            "total_output_tokens": 3345,
            "total_cost_usd": 0.4321,
            "final_deliverable_url": None,
            "github_url": None,
        },
        status_at_emit=status,
    )


# ---------------------------------------------------------------------------
# 1. 調査が全滅
# ---------------------------------------------------------------------------

RESEARCH_FRAMES = [
    ("/app/main.py", 307, "main"),
    ("/app/main.py", 186, "_run_orchestrator"),
    ("/app/orchestrator.py", 1637, "run"),
    ("/app/orchestrator.py", 2095, "_run_researchers"),
]


def research_all_failed() -> Scenario:
    eid = "exec-20261001090000-0a1b2c3d"
    topic = "RESEARCH_TOPIC_MARKER Terraform の state 管理"
    events = [
        _topic_received(eid, topic),
        _workflow_planned(eid),
        make_event(eid, 3, "subagent_started", {"subagent": "researcher", "input_summary": "3 research steps"}),
        make_event(
            eid,
            4,
            "subagent_failed",
            {
                "subagent": "researcher",
                "duration_ms": 61000,
                "error_type": "RuntimeError",
                "error_message": "All research steps failed RESEARCH_ERROR_MARKER",
            },
            status_at_emit="failed",
        ),
        make_event(
            eid,
            5,
            "error",
            {
                "error_type": "RuntimeError",
                "error_message": "All research steps failed RESEARCH_ERROR_MARKER",
                "stack_trace": make_stack_trace(RESEARCH_FRAMES, "RuntimeError: All research steps failed"),
                "stage": "orchestrator",
                "is_recoverable": False,
            },
            status_at_emit="failed",
        ),
        _execution_completed(eid, 6, "failed"),
    ]
    return Scenario(
        name="research_all_failed",
        execution_id=eid,
        execution=make_execution(eid, status="failed", topic=topic),
        steps=[make_step(eid, n, status="failed") for n in (1, 2, 3)],
        events=events,
        record_markers=("RESEARCH_TOPIC_MARKER", "RESEARCH_ERROR_MARKER"),
    )


# ---------------------------------------------------------------------------
# 2. 出力の検証に失敗
# ---------------------------------------------------------------------------

VALIDATION_FRAMES = [
    ("/app/main.py", 307, "main"),
    ("/app/main.py", 186, "_run_orchestrator"),
    ("/app/orchestrator.py", 1711, "run"),
    ("/usr/local/lib/python3.13/site-packages/anyio/_backends/_asyncio.py", 2461, "run"),
    ("/app/orchestrator.py", 2211, "_run_text_generator_with_retries"),
]


def output_validation_failed() -> Scenario:
    eid = "exec-20261001100000-1b2c3d4e"
    topic = "VALIDATION_TOPIC_MARKER DynamoDB の設計"
    message = "All 3 attempts failed: reason=missing_keys, extras={'missing': ['summary']} VALIDATION_ERROR_MARKER"
    events = [
        _topic_received(eid, topic),
        _workflow_planned(eid),
        make_event(eid, 3, "subagent_started", {"subagent": "researcher", "input_summary": "2 research steps"}),
        make_event(
            eid,
            4,
            "subagent_completed",
            {"subagent": "researcher", "duration_ms": 50000, "tokens_used": None, "output_summary": "2 steps"},
        ),
        make_event(
            eid,
            5,
            "research_completed",
            {"source_count": 4, "source_domains": ["docs.aws.amazon.com"], "total_tokens_used": None},
        ),
        make_event(eid, 6, "subagent_started", {"subagent": "generator_text", "input_summary": "category=技術"}),
        make_event(
            eid,
            7,
            "subagent_failed",
            {
                "subagent": "generator_text",
                "stage": "text_generation",
                "duration_ms": 90000,
                "error_type": "NonDictGeneratorResponse",
                "error_message": message,
            },
            status_at_emit="failed",
        ),
        make_event(
            eid,
            8,
            "error",
            {
                "error_type": "NonDictGeneratorResponse",
                "error_message": message,
                "stack_trace": make_stack_trace(
                    VALIDATION_FRAMES, "orchestrator.NonDictGeneratorResponse: missing_keys"
                ),
                "stage": "orchestrator",
                "is_recoverable": False,
            },
            status_at_emit="failed",
        ),
        _execution_completed(eid, 9, "failed"),
    ]
    prompts = {
        ("generator_text", "0"): {
            "subagent": "generator_text",
            "index": "0",
            "prompt": "あなたはジェネレーターです。VALIDATION_PROMPT_MARKER deliverable.json を書いてください。",
            "output": "Wrote: notes.md VALIDATION_OUTPUT_MARKER",
            "recorded_at": "2026-10-01T10:05:00.000Z",
            "output_files": {"notes.md": "FILE_CONTENT_MARKER 本文"},
        }
    }
    return Scenario(
        name="output_validation_failed",
        execution_id=eid,
        execution=make_execution(eid, status="failed", topic=topic),
        steps=[make_step(eid, n, status="completed", result={"summary": "STEP_RESULT_MARKER"}) for n in (1, 2)],
        events=events,
        prompts=prompts,
        record_markers=(
            "VALIDATION_TOPIC_MARKER",
            "VALIDATION_ERROR_MARKER",
            "VALIDATION_PROMPT_MARKER",
            "VALIDATION_OUTPUT_MARKER",
            "FILE_CONTENT_MARKER",
            "STEP_RESULT_MARKER",
        ),
    )


# ---------------------------------------------------------------------------
# 3. レート制限
# ---------------------------------------------------------------------------


RATE_LIMIT_FRAMES = [("/app/main.py", 307, "main"), ("/app/orchestrator.py", 1547, "run")]


def rate_limited() -> Scenario:
    eid = "exec-20261001110000-2c3d4e5f"
    topic = "RATE_TOPIC_MARKER ECS のネットワーク"
    events = [
        _topic_received(eid, topic),
        *[
            make_event(
                eid,
                n,
                "rate_limit_hit",
                {
                    "subtype": "anthropic_429",
                    "endpoint_path": "/v1/messages",
                    "detail": "stderr_snippet=429 Too Many Requests RATE_DETAIL_MARKER",
                },
            )
            for n in (2, 3, 4)
        ],
        make_event(
            eid,
            5,
            "api_call_completed",
            {
                "subtype": "anthropic",
                "success": False,
                "duration_ms": 14000,
                "response_status_code": 1,
                "endpoint_path": "/v1/messages",
            },
        ),
        make_event(
            eid,
            6,
            "error",
            {
                "error_type": "ClaudeInvocationError",
                "error_message": "claude agent sdk call failed after retries: 429 RATE_DETAIL_MARKER",
                "stack_trace": make_stack_trace(
                    RATE_LIMIT_FRAMES,
                    "orchestrator.ClaudeInvocationError: claude agent sdk call failed after retries",
                ),
                "stage": "orchestrator",
                "is_recoverable": False,
            },
            status_at_emit="failed",
        ),
        _execution_completed(eid, 7, "failed"),
    ]
    return Scenario(
        name="rate_limited",
        execution_id=eid,
        execution=make_execution(eid, status="failed", topic=topic),
        events=events,
        record_markers=("RATE_TOPIC_MARKER", "RATE_DETAIL_MARKER"),
    )


# ---------------------------------------------------------------------------
# 4. レビューで不合格が続いた（実行自体は成功）
# ---------------------------------------------------------------------------

REVIEW_ITERATIONS = 3  # MAX_REVIEW_LOOPS = 2 なので、評価は最大 3 回


def review_repeatedly_failed() -> Scenario:
    eid = "exec-20261001120000-3d4e5f6a"
    topic = "REVIEW_TOPIC_MARKER Lambda のコールドスタート"
    events = [
        _topic_received(eid, topic),
        _workflow_planned(eid),
        make_event(eid, 3, "subagent_started", {"subagent": "reviewer"}),
        *[
            make_event(
                eid,
                3 + n,
                "review_completed",
                {
                    "iteration": n,
                    "passed": False,
                    "issues_count": 4,
                    "fixer_notes_count": n - 1,
                    "code_related_unfixed_count": 2,
                    "issues_summary": "出典の不足 REVIEW_ISSUE_MARKER",
                },
            )
            for n in range(1, REVIEW_ITERATIONS + 1)
        ],
        make_event(
            eid,
            7,
            "subagent_completed",
            {"subagent": "reviewer", "duration_ms": 120000, "tokens_used": None, "output_summary": "passed=False"},
        ),
        make_event(eid, 8, "notion_stored", {"url": "https://www.notion.so/page-1", "page_id": "page-1"}),
        make_event(eid, 9, "slack_notified", {"channel_id": SLACK_CHANNEL, "thread_ts": SLACK_THREAD_TS}),
        _execution_completed(eid, 10, "success"),
    ]
    return Scenario(
        name="review_repeatedly_failed",
        execution_id=eid,
        execution=make_execution(eid, status="completed", topic=topic),
        steps=[make_step(eid, n, status="completed", result={"summary": "ok"}) for n in (1, 2)],
        events=events,
        record_markers=("REVIEW_TOPIC_MARKER", "REVIEW_ISSUE_MARKER"),
    )


# ---------------------------------------------------------------------------
# 5. 記録が欠けている
# ---------------------------------------------------------------------------


def records_missing() -> Scenario:
    """イベントが空で、プロンプト記録は保存先が未設定（``unavailable``）。"""
    eid = "exec-20261001130000-4e5f6a7b"
    topic = "MISSING_TOPIC_MARKER S3 のライフサイクル"
    return Scenario(
        name="records_missing",
        execution_id=eid,
        execution=make_execution(eid, status="failed", topic=topic),
        prompts_bucket="",
        record_markers=("MISSING_TOPIC_MARKER",),
    )


# 筋書きのスタックトレースに出てくる、/app/ のフレームのすべて。
# 実際のコードとの突き合わせ（修正仕様 S1）を通すため、テストはこの形のコードを一時ディレクトリに作る
# （``factories.default_code_root()``）。
ALL_FRAMES = [
    frame for frame in (*RESEARCH_FRAMES, *VALIDATION_FRAMES, *RATE_LIMIT_FRAMES) if frame[0].startswith("/app/")
]

ALL_SCENARIOS = {
    "research_all_failed": research_all_failed,
    "output_validation_failed": output_validation_failed,
    "rate_limited": rate_limited,
    "review_repeatedly_failed": review_repeatedly_failed,
    "records_missing": records_missing,
}
