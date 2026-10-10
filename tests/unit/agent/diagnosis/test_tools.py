"""``ToolSession``（design.md §5）: 5 つのツールの中身、入力の検証、上限、根拠 ID、足取り、提出、照合。

対応: AC-1〜AC-15、AC-29、AC-30。テスト名か節の見出しで、どの条件に当たるかを示している。
``RecordReader`` は本物を使い、その下の DynamoDB・S3 だけを偽物にしている。
"""

import json
import logging

import pytest

from . import fakes, scenarios
from .factories import (
    EVENT_ID,
    SCENARIO_CLASSES,
    STEP_ID,
    bearer_value,
    code_root_for,
    default_code_root,
    fake_tokens,
    frame_ids,
    parse,
    valid_submission,
)

READ_TOOLS = ("get_execution_overview", "list_steps", "list_events", "get_prompt_record")
ALL_TOOLS = (*READ_TOOLS, "submit_diagnosis")
FORBIDDEN_SCHEMA_KEYS = {"execution_id", "table", "table_name", "bucket", "bucket_name", "key", "prefix"}
VALID_PROMPT_ARGS = {"subagent": "generator_text", "index": "0"}


def make_session(scenario=None, *, limits=None, clock=None, code_root=None, **reader_kwargs):
    from diagnosis.models import DiagnosisLimits
    from diagnosis.tools import ToolSession

    scenario = scenario or scenarios.output_validation_failed()
    bundle = fakes.build_reader(scenario, **reader_kwargs)
    kwargs = {} if clock is None else {"clock": clock}
    if code_root is not None:
        kwargs["code_root"] = code_root
    session = ToolSession(bundle.reader, limits or DiagnosisLimits(), **kwargs)
    return session, bundle


def make_frame_session(scenario=None, *, frames=None, classes=(), **kwargs):
    """フレームと例外の型を確かめるテスト用。それらが「本物」になるコードの根を渡す（修正仕様 S1・C1）。

    ``frames`` を渡さなければ、筋書き（``scenarios.py``）のフレームが本物になる根を使う。
    ``classes`` は、コードの根に定義しておくクラスの名前（例外の型の許可一覧に入る）。
    筋書きに出てくる例外クラス（``SCENARIO_CLASSES``）は、いつも定義しておく。
    """
    if frames is None and not classes:
        code_root = default_code_root()
    else:
        code_root = code_root_for(scenarios.ALL_FRAMES if frames is None else frames, (*SCENARIO_CLASSES, *classes))
    return make_session(scenario, code_root=code_root, **kwargs)


def tool_schemas() -> dict:
    """SDK に渡す入力スキーマ。置き場所は ``tools.py`` と読んでいる（無ければ ``sdk_server.py``）。"""
    import diagnosis.tools as tools

    if hasattr(tools, "TOOL_SCHEMAS"):
        return tools.TOOL_SCHEMAS
    import diagnosis.sdk_server as sdk_server

    return sdk_server.TOOL_SCHEMAS


def events_of(data: dict, event_type: str) -> list[dict]:
    return [e for e in data["events"] if e["event_type"] == event_type]


def read_all_events(session) -> list[dict]:
    collected: list[dict] = []
    offset = 0
    while True:
        data = parse(session.call("list_events", {"offset": offset}))
        collected.extend(data["events"])
        if not data["has_more"]:
            return collected
        offset = data["next_offset"]


# ---------------------------------------------------------------------------
# AC-1: 概要
# ---------------------------------------------------------------------------


class TestExecutionOverview:
    def test_overview_with_execution_record_returns_status_times_topic_category_totals(self):
        scenario = scenarios.rate_limited()
        session, _ = make_session(scenario)

        result = session.call("get_execution_overview", {})
        data = parse(result)

        assert result.outcome == "ok"
        assert result.is_error is False
        assert data["outcome"] == "ok"
        assert data["evidence_id"] == "EX-1"
        assert data["status"] == "failed"
        assert data["created_at"] == "2026-10-01T09:00:00.000000+00:00"
        assert data["completed_at"] == "2026-10-01T09:07:30.000000+00:00"
        assert "ECS のネットワーク" in data["topic"]
        assert data["category"] == "技術"
        assert data["total_tokens_used"] == 12345
        assert data["total_input_tokens"] == 9000
        assert data["total_output_tokens"] == 3345
        assert data["total_cost_usd"] == pytest.approx(0.4321)

    def test_overview_does_not_return_slack_identifiers(self):
        session, _ = make_session(scenarios.rate_limited())

        text = session.call("get_execution_overview", {}).to_text()

        for identifier in scenarios.SLACK_IDENTIFIERS:
            assert identifier not in text
        for key in ("user_id", "slack_channel", "slack_thread_ts", "ttl"):
            assert key not in json.loads(text)

    def test_overview_with_long_topic_shortens_it(self):
        scenario = scenarios.rate_limited()
        scenario.execution["topic"] = "あ" * 150 + "TOPIC_MIDDLE" + "い" * 400
        session, _ = make_session(scenario)

        data = parse(session.call("get_execution_overview", {}))

        # 先頭 120 字 + 省略の印 + 末尾 80 字（合わせて 200 字。design.md §5.2）
        assert data["topic"] == "あ" * 120 + "…[362 字省略]…" + "い" * 80
        assert "TOPIC_MIDDLE" not in data["topic"]

    def test_overview_registers_evidence_id_as_issued(self):
        session, _ = make_session(scenarios.rate_limited())

        session.call("get_execution_overview", {})

        assert "EX-1" in session.issued_evidence_ids


# ---------------------------------------------------------------------------
# AC-2: 段階の一覧
# ---------------------------------------------------------------------------


class TestListSteps:
    def test_list_steps_with_three_steps_returns_three_with_id_status_times_and_evidence_ids(self):
        session, _ = make_session(scenarios.research_all_failed())

        result = session.call("list_steps", {})
        steps = parse(result)["steps"]

        assert result.outcome == "ok"
        assert [s["step_id"] for s in steps] == ["research-1", "research-2", "research-3"]
        assert [s["step_order"] for s in steps] == [1, 2, 3]
        assert {s["status"] for s in steps} == {"failed"}
        for step in steps:
            assert step["phase"] == "research"
            assert step["step_name"]
            assert step["started_at"].startswith("2026-10-01T09:01:")
            assert step["completed_at"].startswith("2026-10-01T09:02:")
            assert STEP_ID.match(step["evidence_id"])
        ids = [s["evidence_id"] for s in steps]
        assert len(set(ids)) == 3
        assert set(ids) <= session.issued_evidence_ids

    def test_list_steps_reports_result_presence_without_returning_result_body(self):
        scenario = scenarios.output_validation_failed()
        scenario.steps.append(scenarios.make_step(scenario.execution_id, 3, status="failed"))
        session, _ = make_session(scenario)

        result = session.call("list_steps", {})
        steps = parse(result)["steps"]

        assert [s["has_result"] for s in steps] == [True, True, False]
        assert "STEP_RESULT_MARKER" not in result.to_text()
        assert all("result" not in s for s in steps)


# ---------------------------------------------------------------------------
# AC-3: イベントの一覧
# ---------------------------------------------------------------------------


class TestListEvents:
    def test_list_events_returns_events_in_chronological_order_with_evidence_ids(self):
        scenario = scenarios.output_validation_failed()
        scenario.events.reverse()
        session, _ = make_session(scenario)

        result = session.call("list_events", {})
        data = parse(result)

        assert result.outcome == "ok"
        timestamps = [e["timestamp"] for e in data["events"]]
        assert timestamps == sorted(timestamps)
        assert [e["event_type"] for e in data["events"]][:2] == ["topic_received", "workflow_planned"]
        assert data["total"] == 9
        assert data["has_more"] is False
        ids = [e["evidence_id"] for e in data["events"]]
        assert all(EVENT_ID.match(i) for i in ids)
        assert len(set(ids)) == 9
        assert set(ids) <= session.issued_evidence_ids
        for event in data["events"]:
            assert "status_at_emit" in event
            assert isinstance(event["payload"], dict)

    def test_list_events_with_event_type_filter_returns_only_that_type(self):
        session, _ = make_session(scenarios.review_repeatedly_failed())

        data = parse(session.call("list_events", {"event_type": "review_completed"}))

        assert [e["event_type"] for e in data["events"]] == ["review_completed"] * scenarios.REVIEW_ITERATIONS
        assert [e["payload"]["iteration"] for e in data["events"]] == [1, 2, 3]
        assert all(e["payload"]["passed"] is False for e in data["events"])

    def test_list_events_with_filter_matching_nothing_returns_no_events(self):
        session, _ = make_session(scenarios.review_repeatedly_failed())

        result = session.call("list_events", {"event_type": "rate_limit_hit"})

        # design.md §17 の 2: 絞り込みの結果が 0 件でも ok で空の一覧（not_found にしない）
        assert result.outcome == "ok"
        assert result.is_error is False
        assert parse(result) == {
            "outcome": "ok",
            "events": [],
            "total": 0,
            "next_offset": None,
            "has_more": False,
            "truncated": False,
        }
        assert session.issued_evidence_ids == set()
        assert session.trace[-1].evidence_count == 0

    def test_list_events_evidence_ids_do_not_change_with_filter_or_offset(self):
        from diagnosis.models import DiagnosisLimits

        scenario = scenarios.review_repeatedly_failed()
        session, _ = make_session(scenario, limits=DiagnosisLimits(events_page_size=4))

        everything = {e["timestamp"]: e["evidence_id"] for e in read_all_events(session)}
        filtered = parse(session.call("list_events", {"event_type": "review_completed"}))["events"]
        paged = parse(session.call("list_events", {"offset": 6}))["events"]

        assert len(everything) == 10
        assert filtered
        assert paged
        for event in (*filtered, *paged):
            assert event["evidence_id"] == everything[event["timestamp"]]

    def test_list_events_over_page_size_returns_one_page_and_signals_more(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(scenarios.review_repeatedly_failed(), limits=DiagnosisLimits(events_page_size=4))

        first = parse(session.call("list_events", {}))
        second = parse(session.call("list_events", {"offset": first["next_offset"]}))
        third = parse(session.call("list_events", {"offset": second["next_offset"]}))

        assert len(first["events"]) == 4
        assert first["has_more"] is True
        assert first["next_offset"] == 4
        assert first["total"] == 10
        assert len(second["events"]) == 4
        assert second["has_more"] is True
        assert second["next_offset"] == 8
        assert len(third["events"]) == 2
        assert third["has_more"] is False
        timestamps = [e["timestamp"] for page in (first, second, third) for e in page["events"]]
        assert timestamps == sorted(timestamps)
        assert len(set(timestamps)) == 10

    def test_list_events_with_offset_past_the_end_returns_no_events(self):
        session, _ = make_session(scenarios.rate_limited())

        scenario = scenarios.rate_limited()

        result = session.call("list_events", {"offset": 100})

        # design.md §17 の 2: offset の結果が 0 件でも ok で空の一覧
        assert result.outcome == "ok"
        assert parse(result) == {
            "outcome": "ok",
            "events": [],
            "total": len(scenario.events),
            "next_offset": None,
            "has_more": False,
            "truncated": False,
        }
        assert session.issued_evidence_ids == set()

    def test_list_events_with_offset_exactly_at_the_end_returns_no_events(self):
        scenario = scenarios.rate_limited()
        session, _ = make_session(scenario)

        data = parse(session.call("list_events", {"offset": len(scenario.events)}))

        assert data["outcome"] == "ok"
        assert data["events"] == []
        assert data["has_more"] is False
        assert data["next_offset"] is None

    def test_list_events_with_offset_at_last_event_returns_only_it(self):
        scenario = scenarios.rate_limited()
        session, _ = make_session(scenario)

        data = parse(session.call("list_events", {"offset": len(scenario.events) - 1}))

        assert [e["event_type"] for e in data["events"]] == ["execution_completed"]
        assert [e["evidence_id"] for e in data["events"]] == [f"EV-{len(scenario.events)}"]
        assert data["has_more"] is False
        assert data["next_offset"] is None

    def test_list_events_reads_events_from_dynamodb_only_once_per_session(self):
        from diagnosis.models import DiagnosisLimits

        session, bundle = make_session(scenarios.review_repeatedly_failed(), limits=DiagnosisLimits(events_page_size=4))

        session.call("list_events", {})
        queries_after_first = len(bundle.dynamodb.ops("query", fakes.EVENTS_TABLE))
        session.call("list_events", {"offset": 4})
        session.call("list_events", {"event_type": "review_completed"})

        assert queries_after_first >= 1
        assert len(bundle.dynamodb.ops("query", fakes.EVENTS_TABLE)) == queries_after_first

    def test_list_events_with_long_stack_trace_truncates_it_with_marker(self):
        scenario = scenarios.research_all_failed()
        error = next(e for e in scenario.events if e["event_type"] == "error")
        error["payload"]["stack_trace"] = "HEAD_OF_TRACE " + "x" * 5000 + " TAIL_OF_TRACE"
        session, _ = make_session(scenario)

        data = parse(session.call("list_events", {"event_type": "error"}))
        trace = data["events"][0]["payload"]["stack_trace"]

        assert len(trace) < 1000  # 先頭 300 字 + 印 + 末尾 500 字
        assert "字省略" in trace
        assert trace.startswith("HEAD_OF_TRACE")
        assert trace.endswith("TAIL_OF_TRACE")

    def test_list_events_with_string_exactly_at_limit_keeps_it(self):
        scenario = scenarios.research_all_failed()
        error = next(e for e in scenario.events if e["event_type"] == "error")
        error["payload"]["error_message"] = "m" * 800
        session, _ = make_session(scenario)

        data = parse(session.call("list_events", {"event_type": "error"}))

        assert data["events"][0]["payload"]["error_message"] == "m" * 800

    def test_list_events_does_not_return_identifier_fields(self):
        session, _ = make_session(scenarios.review_repeatedly_failed())

        text = "".join(json.dumps(e, ensure_ascii=False) for e in read_all_events(session))

        for identifier in scenarios.SLACK_IDENTIFIERS:
            assert identifier not in text
        for key in ("user_id_hash", "channel_id", "thread_ts", "gsi_pk", '"ttl"'):
            assert key not in text

    @pytest.mark.parametrize(
        "args",
        [
            {"offset": -1},
            {"offset": "3"},
            {"offset": 1.5},
            {"event_type": 5},
            {"limit": 500},
        ],
    )
    def test_list_events_with_invalid_arguments_returns_invalid_input_without_reading(self, args):
        session, bundle = make_session(scenarios.rate_limited())

        result = session.call("list_events", args)

        assert result.outcome == "invalid_input"
        assert result.is_error is True
        assert bundle.all_operations() == []

    def test_list_events_with_result_over_size_limit_drops_tail_and_marks_truncated(self):
        from diagnosis.models import DiagnosisLimits

        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(scenario.execution_id, n, "api_call_completed", {"endpoint_path": "/v1/" + "p" * 200})
            for n in range(1, 31)
        ]
        session, _ = make_session(scenario, limits=DiagnosisLimits(max_result_bytes=3000))

        result = session.call("list_events", {})
        data = parse(result)

        assert result.outcome == "ok"
        assert data["truncated"] is True
        assert 0 < len(data["events"]) < 30
        # 上限は、ツールが返す項目（outcome を除いた部分）の JSON の大きさにかかる
        body = {key: value for key, value in data.items() if key != "outcome"}
        assert len(json.dumps(body, ensure_ascii=False).encode("utf-8")) <= 3000
        assert len(result.to_text().encode("utf-8")) <= 3000 + len('"outcome": "ok", ')


# ---------------------------------------------------------------------------
# AC-3・AC-29: スタックトレースのフレームと例外の型
# ---------------------------------------------------------------------------


class TestFrames:
    def test_list_events_extracts_app_frames_with_frame_ids(self):
        session, _ = make_frame_session(scenarios.output_validation_failed())

        data = parse(session.call("list_events", {"event_type": "error"}))
        event = data["events"][0]

        ids = frame_ids(event)
        assert len(ids) == 4  # /app/ の 4 件。site-packages の 1 件は入らない
        assert len(set(ids)) == 4
        assert "site-packages" not in json.dumps(event["frames"])

    def test_list_events_extracts_frames_from_full_trace_before_truncation(self):
        scenario = scenarios.research_all_failed()
        error = next(e for e in scenario.events if e["event_type"] == "error")
        frames = [("/app/orchestrator.py", 100 + n, f"step_{n}") for n in range(40)]
        error["payload"]["stack_trace"] = scenarios.make_stack_trace(frames, "RuntimeError: boom")
        session, _ = make_frame_session(scenario, frames=frames)

        data = parse(session.call("list_events", {"event_type": "error"}))
        event = data["events"][0]

        assert "字省略" in event["payload"]["stack_trace"]
        assert len(frame_ids(event)) == 15  # 末尾から 15 件まで
        text = json.dumps(event["frames"])
        assert "step_39" in text
        assert "step_25" in text
        assert "step_24" not in text

    def test_list_events_ignores_frames_outside_app_or_with_path_traversal(self):
        scenario = scenarios.research_all_failed()
        error = next(e for e in scenario.events if e["event_type"] == "error")
        error["payload"]["stack_trace"] = scenarios.make_stack_trace(
            [
                ("/app/../etc/passwd.py", 1, "evil"),
                ("/etc/shadow.py", 2, "evil"),
                ("/app/.github/workflows/x.py", 3, "evil"),
                ("/app/ok.py; rm -rf /.py", 4, "evil"),
                ("/app/orchestrator.py", 5, "good"),
            ],
            "RuntimeError: boom",
        )
        session, _ = make_frame_session(scenario, frames=[("/app/orchestrator.py", 5, "good")])

        event = parse(session.call("list_events", {"event_type": "error"}))["events"][0]

        assert len(frame_ids(event)) == 1
        assert "evil" not in json.dumps(event["frames"])

    def test_list_events_without_stack_trace_returns_no_frames(self):
        session, _ = make_session(scenarios.rate_limited())

        data = parse(session.call("list_events", {"event_type": "rate_limit_hit"}))

        for event in data["events"]:
            assert frame_ids(event) == []


# ---------------------------------------------------------------------------
# AC-4: プロンプト記録
# ---------------------------------------------------------------------------


class TestPromptRecord:
    def _scenario_with_long_record(self):
        scenario = scenarios.output_validation_failed()
        scenario.prompts[("researcher", "research-1")] = {
            "subagent": "researcher",
            "index": "research-1",
            "prompt": "P" * 1500 + "m" * 7000 + "Q" * 1500,
            "output": "O" * 2000 + "n" * 9000 + "R" * 3000,
            "recorded_at": "2026-10-01T10:02:00.000Z",
            "output_files": {"deliverable.json": "FILE_BODY_MARKER" * 10, "notes.md": "abc"},
        }
        return scenario

    def test_get_prompt_record_truncates_prompt_and_output_keeping_head_and_tail_with_original_size(self):
        session, _ = make_session(self._scenario_with_long_record())

        result = session.call("get_prompt_record", {"subagent": "researcher", "index": "research-1"})
        data = parse(result)

        assert result.outcome == "ok"
        assert data["subagent"] == "researcher"
        assert data["index"] == "research-1"
        assert data["recorded_at"] == "2026-10-01T10:02:00.000Z"
        assert data["prompt"].startswith("P" * 1500)
        assert data["prompt"].endswith("Q" * 1500)
        assert "m" * 50 not in data["prompt"]
        assert data["prompt_chars"] == 10000
        assert data["output"].startswith("O" * 2000)
        assert data["output"].endswith("R" * 3000)
        assert "n" * 50 not in data["output"]
        assert data["output_chars"] == 14000
        assert len(result.to_text().encode("utf-8")) <= 20_000

    def test_get_prompt_record_returns_evidence_id_built_from_subagent_and_index(self):
        session, _ = make_session(self._scenario_with_long_record())

        data = parse(session.call("get_prompt_record", {"subagent": "researcher", "index": "research-1"}))

        assert data["evidence_id"] == "PR-researcher-research-1"
        assert "PR-researcher-research-1" in session.issued_evidence_ids

    def test_get_prompt_record_returns_output_file_names_and_sizes_without_contents(self):
        session, _ = make_session(self._scenario_with_long_record())

        result = session.call("get_prompt_record", {"subagent": "researcher", "index": "research-1"})
        text = result.to_text()

        assert "deliverable.json" in text
        assert "notes.md" in text
        assert "FILE_BODY_MARKER" not in text
        assert str(len("FILE_BODY_MARKER" * 10)) in json.dumps(parse(result)["output_files"])

    def test_get_prompt_record_with_short_record_returns_it_whole(self):
        session, _ = make_session(scenarios.output_validation_failed())

        data = parse(session.call("get_prompt_record", dict(VALID_PROMPT_ARGS)))

        assert "VALIDATION_PROMPT_MARKER" in data["prompt"]
        assert "VALIDATION_OUTPUT_MARKER" in data["output"]
        original = scenarios.output_validation_failed().prompts[("generator_text", "0")]
        assert data["prompt_chars"] == len(original["prompt"])

    @pytest.mark.parametrize(
        "subagent",
        ["researcher", "generator_text", "generator_code", "reviewer_eval", "reviewer_fix", "generator"],
    )
    def test_get_prompt_record_accepts_every_defined_subagent(self, subagent):
        scenario = scenarios.output_validation_failed()
        scenario.prompts[(subagent, "A_b-1")] = {
            "subagent": subagent,
            "index": "A_b-1",
            "prompt": "p",
            "output": "o",
            "recorded_at": "2026-10-01T10:02:00.000Z",
        }
        session, bundle = make_session(scenario)

        result = session.call("get_prompt_record", {"subagent": subagent, "index": "A_b-1"})

        assert result.outcome == "ok"
        assert bundle.s3.operations[0].kwargs["Key"] == f"prompts/{scenario.execution_id}/{subagent}_A_b-1.json"


# ---------------------------------------------------------------------------
# AC-5: 範囲の縛り
# ---------------------------------------------------------------------------


class TestScope:
    def test_tool_schemas_define_exactly_the_five_tools(self):
        assert set(tool_schemas()) == set(ALL_TOOLS)

    @pytest.mark.parametrize(
        ("tool", "properties"),
        [
            ("get_execution_overview", set()),
            ("list_steps", set()),
            ("list_events", {"event_type", "offset"}),
            ("get_prompt_record", {"subagent", "index"}),
        ],
    )
    def test_read_tool_schema_has_no_execution_table_bucket_or_key_property(self, tool, properties):
        schema = tool_schemas()[tool]

        assert set(schema.get("properties", {})) == properties
        assert not set(schema.get("properties", {})) & FORBIDDEN_SCHEMA_KEYS
        assert schema["additionalProperties"] is False

    def test_submit_schema_rejects_additional_properties_and_has_no_fixer_eligible(self):
        schema = tool_schemas()["submit_diagnosis"]

        assert schema["additionalProperties"] is False
        assert "fixer_eligible" not in schema["properties"]
        assert not set(schema["properties"]) & FORBIDDEN_SCHEMA_KEYS

    @pytest.mark.parametrize("tool", READ_TOOLS)
    @pytest.mark.parametrize("extra", ["execution_id", "table", "bucket", "key"])
    def test_read_tool_with_extra_scope_argument_returns_invalid_input_without_reading(self, tool, extra):
        session, bundle = make_session()
        args = dict(VALID_PROMPT_ARGS) if tool == "get_prompt_record" else {}
        args[extra] = "exec-20250101000000-ffffffff"

        result = session.call(tool, args)

        assert result.outcome == "invalid_input"
        assert result.is_error is True
        assert bundle.all_operations() == []
        assert "exec-20250101000000-ffffffff" not in result.to_text()  # 値は返さない
        assert extra in result.to_text()  # 項目の名前は返す

    @pytest.mark.parametrize("args", [None, "x", ["a"], 3])
    def test_tool_with_arguments_that_are_not_a_dict_returns_invalid_input(self, args):
        session, bundle = make_session()

        result = session.call("list_steps", args)

        assert result.outcome == "invalid_input"
        assert bundle.all_operations() == []

    @pytest.mark.parametrize("name", ["delete_execution", "Bash", "Write", ""])
    def test_call_with_unknown_tool_name_returns_invalid_input_without_reading(self, name):
        session, bundle = make_session()

        result = session.call(name, {})

        assert result.outcome == "invalid_input"
        assert bundle.all_operations() == []

    def test_session_after_using_every_tool_touched_only_bound_execution_and_fixed_targets(self):
        scenario = scenarios.output_validation_failed()
        scenario.events.append(scenarios.make_event("exec-20250101000000-ffffffff", 1, "error", {"error_type": "X"}))
        session, bundle = make_session(scenario)

        session.call("get_execution_overview", {})
        session.call("list_steps", {})
        session.call("list_events", {})
        session.call("get_prompt_record", dict(VALID_PROMPT_ARGS))
        session.call("submit_diagnosis", valid_submission())

        assert {op.name for op in bundle.all_operations()} <= fakes.READ_OPERATIONS
        for op in bundle.dynamodb.operations:
            assert op.target in (fakes.EXECUTIONS_TABLE, fakes.STEPS_TABLE, fakes.EVENTS_TABLE)
            if op.name == "get_item":
                assert op.kwargs["Key"] == {"execution_id": scenario.execution_id}
            else:
                assert fakes.partition_values(op.kwargs) == {scenario.execution_id}
        assert len(bundle.s3.operations) == 1
        assert bundle.s3.operations[0].kwargs["Bucket"] == fakes.PROMPTS_BUCKET
        assert bundle.s3.operations[0].kwargs["Key"].startswith(f"prompts/{scenario.execution_id}/")


# ---------------------------------------------------------------------------
# AC-6: 番号と役の検証
# ---------------------------------------------------------------------------


class TestPromptRecordValidation:
    @pytest.mark.parametrize(
        "index",
        ["../secret", "a/b", "/", "..", "a.b", "a b", "", "x" * 65, "0\n", "é", "0%2F1", 0, None, ["0"]],
    )
    def test_get_prompt_record_with_invalid_index_returns_invalid_input_without_calling_s3(self, index):
        session, bundle = make_session()

        result = session.call("get_prompt_record", {"subagent": "researcher", "index": index})

        assert result.outcome == "invalid_input"
        assert result.is_error is True
        assert bundle.s3.operations == []

    @pytest.mark.parametrize(
        "subagent",
        ["orchestrator", "", "Researcher", "researcher/../x", "../researcher", "researcher\n", None, 1, ["researcher"]],
    )
    def test_get_prompt_record_with_invalid_subagent_returns_invalid_input_without_calling_s3(self, subagent):
        session, bundle = make_session()

        result = session.call("get_prompt_record", {"subagent": subagent, "index": "0"})

        assert result.outcome == "invalid_input"
        assert bundle.s3.operations == []

    @pytest.mark.parametrize("args", [{}, {"subagent": "researcher"}, {"index": "0"}])
    def test_get_prompt_record_with_missing_argument_returns_invalid_input(self, args):
        session, bundle = make_session()

        result = session.call("get_prompt_record", args)

        assert result.outcome == "invalid_input"
        assert bundle.s3.operations == []

    def test_get_prompt_record_with_longest_allowed_index_is_accepted(self):
        scenario = scenarios.output_validation_failed()
        index = "a" * 64
        record = {"subagent": "researcher", "index": index, "prompt": "p", "output": "o"}
        scenario.prompts[("researcher", index)] = record
        session, _ = make_session(scenario)

        assert session.call("get_prompt_record", {"subagent": "researcher", "index": index}).outcome == "ok"

    def test_invalid_input_result_names_the_field_but_not_the_value(self):
        session, _ = make_session()

        text = session.call("get_prompt_record", {"subagent": "researcher", "index": "../SECRET_VALUE"}).to_text()

        assert "index" in text
        assert "SECRET_VALUE" not in text


# ---------------------------------------------------------------------------
# AC-7: 記録がない・読めない
# ---------------------------------------------------------------------------


class TestMissingOrUnreadable:
    def test_overview_with_no_execution_record_returns_not_found(self):
        scenario = scenarios.rate_limited()
        scenario.execution = None
        session, _ = make_session(scenario)

        result = session.call("get_execution_overview", {})

        assert result.outcome == "not_found"
        assert result.is_error is True
        assert "EX-1" not in session.issued_evidence_ids

    def test_get_prompt_record_with_s3_access_denied_returns_unavailable(self):
        session, _ = make_session(s3_error=fakes.client_error("AccessDenied", 403))

        result = session.call("get_prompt_record", dict(VALID_PROMPT_ARGS))

        assert result.outcome == "unavailable"
        assert result.is_error is True

    def test_get_prompt_record_with_bucket_not_configured_returns_unavailable(self):
        session, bundle = make_session(scenarios.records_missing())

        result = session.call("get_prompt_record", {"subagent": "researcher", "index": "research-1"})

        assert result.outcome == "unavailable"
        assert bundle.s3.operations == []

    def test_get_prompt_record_with_missing_key_returns_not_found(self):
        session, _ = make_session()

        result = session.call("get_prompt_record", {"subagent": "reviewer_eval", "index": "1"})

        assert result.outcome == "not_found"
        assert session.issued_evidence_ids == set()

    def test_list_events_with_events_table_not_configured_returns_unavailable(self):
        session, bundle = make_session(events_table="")

        result = session.call("list_events", {})

        assert result.outcome == "unavailable"
        assert bundle.dynamodb.operations == []

    def test_list_events_with_no_events_returns_no_evidence(self):
        session, _ = make_session(scenarios.records_missing())

        result = session.call("list_events", {})

        # design.md §17 の 2: 「記録が無い」ことも診断の材料であり、失敗ではない
        assert result.outcome == "ok"
        assert result.is_error is False
        assert parse(result) == {
            "outcome": "ok",
            "events": [],
            "total": 0,
            "next_offset": None,
            "has_more": False,
            "truncated": False,
        }
        assert session.issued_evidence_ids == set()
        assert [(c.tool, c.outcome, c.evidence_count) for c in session.trace] == [("list_events", "ok", 0)]


# ---------------------------------------------------------------------------
# AC-8: 想定外の例外
# ---------------------------------------------------------------------------


class TestInternalError:
    @pytest.mark.parametrize("tool", ["get_execution_overview", "list_steps", "list_events"])
    def test_tool_with_unexpected_dynamodb_exception_returns_internal_error_and_logs_it(self, tool, caplog):
        session, _ = make_session(dynamodb_error=RuntimeError("BOOM_DETAIL_9731 connection reset"))

        with caplog.at_level(logging.ERROR):
            result = session.call(tool, {})

        assert result.outcome == "internal_error"
        assert result.is_error is True
        assert "BOOM_DETAIL_9731" not in result.to_text()
        assert "connection reset" not in result.to_text()
        assert "BOOM_DETAIL_9731" in caplog.text

    def test_get_prompt_record_with_unexpected_s3_exception_returns_internal_error_and_logs_it(self, caplog):
        session, _ = make_session(s3_error=ValueError("S3_DETAIL_4420"))

        with caplog.at_level(logging.ERROR):
            result = session.call("get_prompt_record", dict(VALID_PROMPT_ARGS))

        assert result.outcome == "internal_error"
        assert "S3_DETAIL_4420" not in result.to_text()
        assert "S3_DETAIL_4420" in caplog.text

    def test_get_prompt_record_with_other_client_error_returns_internal_error(self):
        session, _ = make_session(s3_error=fakes.client_error("InternalError", 500))

        result = session.call("get_prompt_record", dict(VALID_PROMPT_ARGS))

        assert result.outcome == "internal_error"
        assert "fake InternalError" not in result.to_text()

    def test_session_after_internal_error_keeps_working(self):
        session, bundle = make_session(dynamodb_error=RuntimeError("temporary"))
        assert session.call("list_steps", {}).outcome == "internal_error"

        bundle.dynamodb.raise_on_read = None

        assert session.call("list_steps", {}).outcome == "ok"


# ---------------------------------------------------------------------------
# AC-9: 上限
# ---------------------------------------------------------------------------


class TestLimits:
    def test_call_over_tool_call_limit_returns_call_limit_without_reading(self):
        from diagnosis.models import DiagnosisLimits

        session, bundle = make_session(limits=DiagnosisLimits(max_tool_calls=2))
        assert session.call("get_execution_overview", {}).outcome == "ok"
        assert session.call("list_steps", {}).outcome == "ok"
        operations_before = len(bundle.all_operations())

        result = session.call("list_events", {})

        assert result.outcome == "call_limit"
        assert result.is_error is True
        assert len(bundle.all_operations()) == operations_before

    def test_submit_after_tool_call_limit_is_still_accepted(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=1))
        session.call("get_execution_overview", {})
        assert session.call("list_steps", {}).outcome == "call_limit"

        result = session.call("submit_diagnosis", valid_submission(cause_category="unknown", confidence="low"))

        assert result.outcome == "ok"
        assert session.has_submission is True

    def test_submissions_do_not_count_toward_tool_call_limit(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=1))
        for _ in range(3):
            assert session.call("submit_diagnosis", valid_submission()).outcome == "ok"

        assert session.call("get_execution_overview", {}).outcome == "ok"

    def test_call_over_total_size_limit_returns_size_limit_without_reading(self):
        from diagnosis.models import DiagnosisLimits

        session, bundle = make_session(limits=DiagnosisLimits(max_total_bytes=50))
        assert session.call("list_events", {}).outcome == "ok"  # 1 回で累計が上限を超える
        operations_before = len(bundle.all_operations())

        result = session.call("list_steps", {})

        assert result.outcome == "size_limit"
        assert result.is_error is True
        assert len(bundle.all_operations()) == operations_before

    def test_submit_after_total_size_limit_is_still_accepted(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_total_bytes=50))
        session.call("list_events", {})
        assert session.call("list_steps", {}).outcome == "size_limit"

        result = session.call("submit_diagnosis", valid_submission(cause_category="insufficient_records"))

        assert result.outcome == "ok"
        assert session.has_submission is True

    def test_limit_refusals_are_recorded_in_trace(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=1))
        session.call("get_execution_overview", {})
        session.call("list_steps", {})

        assert [(c.tool, c.outcome) for c in session.trace] == [
            ("get_execution_overview", "ok"),
            ("list_steps", "call_limit"),
        ]


# ---------------------------------------------------------------------------
# AC-10: マスク
# ---------------------------------------------------------------------------


class TestMasking:
    def _secrets(self) -> list[str]:
        return [*fake_tokens().values(), bearer_value()]

    def test_overview_with_token_in_topic_masks_it(self):
        scenario = scenarios.rate_limited()
        token = fake_tokens()["slack"]
        scenario.execution["topic"] = f"調べたい {token}"
        session, _ = make_session(scenario)

        text = session.call("get_execution_overview", {}).to_text()

        assert token not in text
        assert "[MASKED]" in text

    def test_list_events_with_tokens_in_payload_masks_them(self):
        scenario = scenarios.rate_limited()
        tokens = fake_tokens()
        scenario.events = [
            scenarios.make_event(
                scenario.execution_id,
                n,
                "error",
                {
                    "error_type": "HTTPError",
                    "error_message": f"request failed: {token}",
                    "nested": {"headers": [f"Authorization: Bearer {bearer_value()}"]},
                },
            )
            for n, token in enumerate(tokens.values(), start=1)
        ]
        session, _ = make_session(scenario)

        text = session.call("list_events", {}).to_text()

        for secret in self._secrets():
            assert secret not in text
        assert text.count("[MASKED]") >= len(tokens)

    def test_get_prompt_record_with_tokens_in_prompt_and_output_masks_them(self):
        scenario = scenarios.output_validation_failed()
        tokens = fake_tokens()
        record = scenario.prompts[("generator_text", "0")]
        record["prompt"] = f"GITHUB_TOKEN={tokens['github']} を使う"
        record["output"] = (
            f"export AWS_ACCESS_KEY_ID={tokens['aws_akia']}\n"
            f"curl -H 'Authorization: Bearer {bearer_value()}'"
        )
        session, _ = make_session(scenario)

        text = session.call("get_prompt_record", dict(VALID_PROMPT_ARGS)).to_text()

        assert tokens["github"] not in text
        assert tokens["aws_akia"] not in text
        assert bearer_value() not in text
        assert text.count("[MASKED]") >= 3

    def test_list_events_masks_before_truncating_so_token_at_boundary_does_not_leak(self):
        scenario = scenarios.rate_limited()
        token = fake_tokens()["github"]
        # 先頭 300 字の境目をトークンがまたぐ位置に置く
        message = "a" * 290 + " " + token + " " + "b" * 2000
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", {"error_message": message})]
        session, _ = make_session(scenario)

        text = session.call("list_events", {}).to_text()

        assert "字省略" in text
        assert token[:10] not in text


# ---------------------------------------------------------------------------
# AC-18・R19: 足取り
# ---------------------------------------------------------------------------


class TestTrace:
    def test_trace_records_every_call_in_order_with_outcome_evidence_count_and_duration(self):
        ticks = iter(range(0, 10_000, 5))

        session, _ = make_session(clock=lambda: next(ticks) / 1000)

        session.call("get_execution_overview", {})
        session.call("list_events", {"event_type": "subagent_failed"})
        session.call("get_prompt_record", {"subagent": "researcher", "index": "../x"})
        session.call("submit_diagnosis", valid_submission())

        trace = session.trace
        assert [c.seq for c in trace] == [1, 2, 3, 4]
        assert [c.tool for c in trace] == [
            "get_execution_overview",
            "list_events",
            "get_prompt_record",
            "submit_diagnosis",
        ]
        assert [c.outcome for c in trace] == ["ok", "ok", "invalid_input", "ok"]
        assert [c.evidence_count for c in trace] == [1, 1, 0, 0]
        for call in trace:
            assert isinstance(call.duration_ms, int)
            assert call.duration_ms > 0  # 時計は呼ぶたびに 5ms 進む

    def test_trace_keeps_validated_arguments_and_empties_them_when_validation_fails(self):
        session, _ = make_session()

        session.call("list_events", {"event_type": "error", "offset": 0})
        session.call("list_events", {"event_type": "error", "execution_id": "exec-20250101000000-ffffffff"})

        assert session.trace[0].args == {"event_type": "error", "offset": 0}
        assert session.trace[1].args == {}

    def test_trace_does_not_hold_submission_free_text_for_rejected_submission(self):
        session, _ = make_session()

        session.call("submit_diagnosis", valid_submission(cause_category="made_up", summary="REJECTED_TEXT"))

        assert session.trace[0].outcome == "invalid_input"
        assert session.trace[0].args == {}


# ---------------------------------------------------------------------------
# AC-12: 提出の検証
# ---------------------------------------------------------------------------

REQUIRED_SUBMISSION_FIELDS = (
    "failed_stage",
    "cause_category",
    "cause_nature",
    "confidence",
    "evidence",
    "action_type",
    "action_note",
    "summary",
)


class TestSubmissionValidation:
    def test_submit_with_valid_shape_is_accepted(self):
        session, _ = make_session()

        result = session.call("submit_diagnosis", valid_submission())
        data = parse(result)

        assert result.outcome == "ok"
        assert result.is_error is False
        assert data["accepted"] is True
        assert data["invalid_evidence_ids"] == []
        assert data["dropped_code_location_ids"] == []
        assert session.has_submission is True

    def test_session_without_submission_reports_no_submission(self):
        session, _ = make_session()

        assert session.has_submission is False

    @pytest.mark.parametrize("field", REQUIRED_SUBMISSION_FIELDS)
    def test_submit_with_missing_required_field_is_rejected(self, field):
        session, _ = make_session()
        submission = valid_submission()
        del submission[field]

        result = session.call("submit_diagnosis", submission)

        assert result.outcome == "invalid_input"
        assert result.is_error is True
        assert field in result.to_text()
        assert session.has_submission is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("failed_stage", "deploying"),
            ("failed_stage", "調査"),
            ("cause_category", "cosmic_rays"),
            ("cause_nature", "bug"),
            ("confidence", "HIGH"),
            ("confidence", 1),
            ("action_type", "delete_everything"),
            ("action_type", None),
        ],
    )
    def test_submit_with_value_outside_fixed_list_is_rejected(self, field, value):
        session, _ = make_session()

        result = session.call("submit_diagnosis", valid_submission(**{field: value}))

        assert result.outcome == "invalid_input"
        assert result.is_error is True
        assert session.has_submission is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("summary", "s" * 501),
            ("action_note", "n" * 301),
            ("summary", 123),
            ("action_note", None),
            ("evidence", [{"id": "EV-1", "note": "n" * 201}]),
            ("evidence", [{"id": "E" * 97, "note": "x"}]),  # 根拠 ID は 96 字まで（修正仕様 L1）
            ("evidence", [{"id": "EV-1", "note": "x"}] * 11),
            ("evidence", "EV-1"),
            ("evidence", ["EV-1"]),
            ("evidence", [{"id": "EV-1"}]),
            ("evidence", [{"note": "x"}]),
            ("evidence", [{"id": 1, "note": "x"}]),
            ("evidence", [{"id": "EV-1", "note": "x", "quote": "raw record text"}]),
            ("exception_evidence_id", "EX-1"),
            ("exception_evidence_id", "EV-1; drop"),
            ("exception_evidence_id", "EV-1\n"),
            ("exception_evidence_id", 7),
            ("code_location_ids", ["FR-1", "FR-2", "FR-3", "FR-4", "FR-5", "FR-6"]),
            ("code_location_ids", ["/app/orchestrator.py"]),
            ("code_location_ids", "FR-1"),
            ("code_location_ids", [1]),
        ],
    )
    def test_submit_with_malformed_or_oversized_field_is_rejected(self, field, value):
        session, _ = make_session()

        result = session.call("submit_diagnosis", valid_submission(**{field: value}))

        assert result.outcome == "invalid_input"
        assert result.is_error is True
        assert session.has_submission is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("summary", "s" * 500),
            ("action_note", "n" * 300),
            ("evidence", [{"id": "E" * 96, "note": "n" * 200}] * 10),
            ("code_location_ids", ["FR-1", "FR-2", "FR-3", "FR-4", "FR-5"]),
            ("code_location_ids", []),
            ("exception_evidence_id", "EV-12"),
        ],
    )
    def test_submit_with_field_exactly_at_limit_is_accepted(self, field, value):
        session, _ = make_session()

        result = session.call("submit_diagnosis", valid_submission(**{field: value}))

        assert result.outcome == "ok"
        assert session.has_submission is True

    @pytest.mark.parametrize("extra", ["fixer_eligible", "execution_id", "exception_type", "code_locations", "notes"])
    def test_submit_with_field_outside_schema_is_rejected(self, extra):
        """AC-30: 提出に ``fixer_eligible`` などがあれば、その提出は受理しない。"""
        session, _ = make_session()

        result = session.call("submit_diagnosis", valid_submission(**{extra: True}))

        assert result.outcome == "invalid_input"
        assert extra in result.to_text()
        assert session.has_submission is False

    def test_submit_after_rejection_can_be_resubmitted(self):
        session, _ = make_session()
        assert session.call("submit_diagnosis", valid_submission(confidence="certain")).outcome == "invalid_input"

        result = session.call("submit_diagnosis", valid_submission(cause_category="unknown", confidence="low"))

        assert result.outcome == "ok"
        assert session.finalize().confidence == "low"

    def test_submit_twice_makes_the_last_accepted_one_authoritative(self):
        session, _ = make_session()
        session.call("submit_diagnosis", valid_submission(cause_category="unknown", summary="最初の提出"))
        session.call("submit_diagnosis", valid_submission(cause_category="insufficient_records", summary="出し直し"))

        diagnosis = session.finalize()

        assert diagnosis.cause_category == "insufficient_records"
        assert diagnosis.summary == "出し直し"

    def test_rejected_submission_after_accepted_one_does_not_replace_it(self):
        session, _ = make_session()
        session.call("submit_diagnosis", valid_submission(cause_category="unknown", summary="受理ずみ"))
        session.call("submit_diagnosis", valid_submission(cause_category="nonsense", summary="不正"))

        diagnosis = session.finalize()

        assert session.has_submission is True
        assert diagnosis.summary == "受理ずみ"

    def test_accepted_free_text_is_masked(self):
        tokens = fake_tokens()
        session, _ = make_session()
        session.call("get_execution_overview", {})
        session.call(
            "submit_diagnosis",
            valid_submission(
                summary=f"トークン {tokens['slack']} が漏れていた",
                action_note=f"Authorization: Bearer {bearer_value()} を無効にする",
                evidence=[{"id": "EX-1", "note": f"鍵 {tokens['aws_akia']}"}],
            ),
        )

        diagnosis = session.finalize()

        assert tokens["slack"] not in diagnosis.summary
        assert "[MASKED]" in diagnosis.summary
        assert bearer_value() not in diagnosis.action_note
        assert "[MASKED]" in diagnosis.action_note
        assert tokens["aws_akia"] not in diagnosis.evidence[0].note
        assert "[MASKED]" in diagnosis.evidence[0].note

    def test_accepted_submission_keeps_fixed_values(self):
        session, _ = make_session()
        session.call("get_execution_overview", {})
        session.call("submit_diagnosis", valid_submission(evidence=[{"id": "EX-1", "note": "状態が failed"}]))

        diagnosis = session.finalize()

        assert diagnosis.failed_stage == "researching"
        assert diagnosis.cause_category == "rate_limit"
        assert diagnosis.cause_nature == "transient"
        assert diagnosis.confidence == "high"
        assert diagnosis.action_type == "retry_later"
        assert diagnosis.action_note == "時間を置いて再投入してください"
        assert diagnosis.summary == "レート制限に達して失敗しました"

    def test_finalize_without_submission_returns_unknown(self):
        from diagnosis.models import Diagnosis

        session, _ = make_session()
        session.call("get_execution_overview", {})

        assert session.finalize() == Diagnosis.unknown()


# ---------------------------------------------------------------------------
# AC-13: 根拠の照合
# ---------------------------------------------------------------------------


class TestEvidenceMatching:
    def test_finalize_keeps_issued_evidence_and_drops_unissued_with_count(self):
        from diagnosis.models import EvidenceRef

        session, _ = make_session(scenarios.rate_limited())
        events = parse(session.call("list_events", {"event_type": "rate_limit_hit"}))["events"]
        issued = events[0]["evidence_id"]

        result = session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[
                    {"id": issued, "note": "429 を受けた"},
                    {"id": "EV-999", "note": "でっちあげ"},
                    {"id": "ZZ-1", "note": "でっちあげ"},
                ]
            ),
        )
        diagnosis = session.finalize()

        assert result.outcome == "ok"
        assert sorted(parse(result)["invalid_evidence_ids"]) == ["EV-999", "ZZ-1"]
        assert diagnosis.evidence == [EvidenceRef(id=issued, note="429 を受けた")]
        assert diagnosis.invalid_evidence_count == 2
        assert diagnosis.cause_category == "rate_limit"

    def test_finalize_treats_evidence_from_every_read_tool_as_valid(self):
        session, _ = make_session(scenarios.output_validation_failed())
        overview = parse(session.call("get_execution_overview", {}))
        steps = parse(session.call("list_steps", {}))["steps"]
        events = parse(session.call("list_events", {}))["events"]
        prompt = parse(session.call("get_prompt_record", dict(VALID_PROMPT_ARGS)))
        ids = [overview["evidence_id"], steps[0]["evidence_id"], events[6]["evidence_id"], prompt["evidence_id"]]

        session.call(
            "submit_diagnosis",
            valid_submission(cause_category="output_validation_failed", evidence=[{"id": i, "note": "x"} for i in ids]),
        )
        diagnosis = session.finalize()

        assert [e.id for e in diagnosis.evidence] == ids
        assert diagnosis.invalid_evidence_count == 0

    def test_finalize_rejects_id_of_record_that_exists_but_was_not_returned_yet(self):
        from diagnosis.models import DiagnosisLimits

        limits = DiagnosisLimits(events_page_size=4)
        probe, _ = make_session(scenarios.review_repeatedly_failed(), limits=limits)
        later_id = read_all_events(probe)[7]["evidence_id"]  # 2 ページ目にある記録

        session, _ = make_session(scenarios.review_repeatedly_failed(), limits=limits)
        session.call("list_events", {})  # 1 ページ目だけ
        session.call(
            "submit_diagnosis",
            valid_submission(cause_category="review_not_passed", evidence=[{"id": later_id, "note": "見ていない"}]),
        )
        diagnosis = session.finalize()

        assert later_id not in session.issued_evidence_ids
        assert diagnosis.evidence == []
        assert diagnosis.invalid_evidence_count == 1

    def test_failed_read_does_not_issue_evidence(self):
        session, _ = make_session(scenarios.records_missing())
        session.call("get_prompt_record", {"subagent": "researcher", "index": "research-1"})
        session.call(
            "submit_diagnosis",
            valid_submission(evidence=[{"id": "PR-researcher-research-1", "note": "読めなかった"}]),
        )

        diagnosis = session.finalize()

        assert diagnosis.evidence == []
        assert diagnosis.invalid_evidence_count == 1


# ---------------------------------------------------------------------------
# AC-14: 根拠ゼロ / AC-15: 「分からない」
# ---------------------------------------------------------------------------


class TestDowngrade:
    def test_finalize_with_rate_limit_and_no_evidence_downgrades_to_unknown_low(self):
        session, _ = make_session(scenarios.rate_limited())
        session.call("list_events", {})
        session.call("submit_diagnosis", valid_submission(cause_category="rate_limit", confidence="high", evidence=[]))

        diagnosis = session.finalize()

        assert diagnosis.cause_category == "unknown"
        assert diagnosis.cause_nature == "unknown"
        assert diagnosis.confidence == "low"
        assert diagnosis.evidence == []

    @pytest.mark.parametrize("action_type", ["retry_later", "retry_now", "review_config", "fix_code", "no_action"])
    def test_downgrade_replaces_action_and_free_text_written_by_the_model(self, action_type):
        """修正仕様 M2: 根拠のない診断の「対処」と自由記述を、そのまま出さない。"""
        session, _ = make_session(scenarios.rate_limited())
        session.call("list_events", {})
        session.call(
            "submit_diagnosis",
            valid_submission(
                cause_category="rate_limit",
                confidence="high",
                evidence=[{"id": "EV-999", "note": "でっちあげ"}],
                action_type=action_type,
                action_note="MODEL_ACTION_NOTE_7741 すぐ再投入してください",
                summary="MODEL_SUMMARY_7742 レート制限です",
            ),
        )

        diagnosis = session.finalize()

        assert diagnosis.cause_category == "unknown"
        assert diagnosis.action_type == "human_investigation"
        assert "MODEL_SUMMARY_7742" not in diagnosis.summary
        assert "レート制限です" not in diagnosis.summary
        assert "MODEL_ACTION_NOTE_7741" not in diagnosis.action_note
        assert "再投入" not in diagnosis.action_note
        assert diagnosis.summary.strip()
        assert diagnosis.action_note.strip()
        assert diagnosis.invalid_evidence_count == 1

    def test_downgrade_drops_exception_type_and_code_locations_from_handoff_eligibility(self):
        from diagnosis.handoff import build_handoff

        session, _ = make_session(scenarios.rate_limited())
        session.call(
            "submit_diagnosis",
            valid_submission(
                cause_category="output_validation_failed",
                cause_nature="code_defect",
                confidence="high",
                action_type="fix_code",
                evidence=[],
            ),
        )

        handoff = build_handoff(scenarios.rate_limited().execution_id, session.finalize())

        assert handoff.action_type == "human_investigation"
        assert handoff.fixer_eligible is False

    def test_diagnosis_with_valid_evidence_keeps_action_and_free_text(self):
        session, _ = make_session(scenarios.rate_limited())
        session.call("get_execution_overview", {})
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": "EX-1", "note": "x"}],
                action_type="retry_later",
                action_note="MODEL_ACTION_NOTE_7741",
                summary="MODEL_SUMMARY_7742",
            ),
        )

        diagnosis = session.finalize()

        assert diagnosis.action_type == "retry_later"
        assert diagnosis.action_note == "MODEL_ACTION_NOTE_7741"
        assert diagnosis.summary == "MODEL_SUMMARY_7742"

    def test_finalize_with_only_unissued_evidence_downgrades_to_unknown_low(self):
        session, _ = make_session(scenarios.rate_limited())
        session.call(
            "submit_diagnosis",
            valid_submission(cause_category="rate_limit", confidence="high", evidence=[{"id": "EV-1", "note": "未読"}]),
        )

        diagnosis = session.finalize()

        assert diagnosis.cause_category == "unknown"
        assert diagnosis.confidence == "low"
        assert diagnosis.invalid_evidence_count == 1

    @pytest.mark.parametrize(
        "category",
        [
            "external_service_error",
            "research_failed",
            "output_validation_failed",
            "review_not_passed",
            "auth_or_environment",
        ],
    )
    def test_finalize_with_any_specific_category_and_no_evidence_downgrades(self, category):
        session, _ = make_session()
        session.call("submit_diagnosis", valid_submission(cause_category=category, evidence=[]))

        diagnosis = session.finalize()

        assert diagnosis.cause_category == "unknown"
        assert diagnosis.confidence == "low"

    def test_finalize_with_one_valid_evidence_keeps_category_and_confidence(self):
        session, _ = make_session(scenarios.rate_limited())
        events = parse(session.call("list_events", {"event_type": "rate_limit_hit"}))["events"]
        session.call(
            "submit_diagnosis",
            valid_submission(evidence=[{"id": events[0]["evidence_id"], "note": "429"}]),
        )

        diagnosis = session.finalize()

        assert diagnosis.cause_category == "rate_limit"
        assert diagnosis.cause_nature == "transient"
        assert diagnosis.confidence == "high"

    @pytest.mark.parametrize("category", ["unknown", "insufficient_records"])
    def test_finalize_with_unknown_or_insufficient_records_and_no_evidence_is_kept(self, category):
        session, _ = make_session(scenarios.records_missing())
        result = session.call(
            "submit_diagnosis",
            valid_submission(
                failed_stage="unknown",
                cause_category=category,
                cause_nature="unknown",
                confidence="medium",
                action_type="human_investigation",
                evidence=[],
            ),
        )

        diagnosis = session.finalize()

        assert result.outcome == "ok"
        assert diagnosis.cause_category == category
        assert diagnosis.confidence == "medium"  # 格下げの対象外
        assert diagnosis.action_type == "human_investigation"
        assert diagnosis.invalid_evidence_count == 0


# ---------------------------------------------------------------------------
# AC-29: 例外の型とコードの場所は、コードが記録から取り出す
# ---------------------------------------------------------------------------


class TestExceptionTypeAndCodeLocations:
    def _read_error_event(self, session) -> dict:
        return parse(session.call("list_events", {"event_type": "error"}))["events"][0]

    def test_finalize_resolves_exception_type_and_code_locations_from_issued_ids(self):
        from diagnosis.models import CodeLocation

        session, _ = make_frame_session(scenarios.output_validation_failed())
        event = self._read_error_event(session)
        ids = frame_ids(event)

        result = session.call(
            "submit_diagnosis",
            valid_submission(
                failed_stage="generating",
                cause_category="output_validation_failed",
                cause_nature="code_defect",
                action_type="fix_code",
                evidence=[{"id": event["evidence_id"], "note": "検証に失敗"}],
                exception_evidence_id=event["evidence_id"],
                code_location_ids=ids,
            ),
        )
        diagnosis = session.finalize()

        assert parse(result)["dropped_code_location_ids"] == []
        assert diagnosis.exception_type == "NonDictGeneratorResponse"
        assert sorted(diagnosis.code_locations, key=lambda c: (c.path, c.line)) == [
            CodeLocation(path="/app/main.py", function="_run_orchestrator", line=186),
            CodeLocation(path="/app/main.py", function="main", line=307),
            CodeLocation(path="/app/orchestrator.py", function="run", line=1711),
            CodeLocation(path="/app/orchestrator.py", function="_run_text_generator_with_retries", line=2211),
        ]

    def test_finalize_with_unissued_exception_evidence_id_leaves_exception_type_empty(self):
        session, _ = make_session(scenarios.output_validation_failed())
        session.call("get_execution_overview", {})  # イベントは読んでいない
        session.call(
            "submit_diagnosis",
            valid_submission(evidence=[{"id": "EX-1", "note": "x"}], exception_evidence_id="EV-8"),
        )

        assert session.finalize().exception_type is None

    def test_finalize_with_event_that_has_no_error_type_leaves_exception_type_empty(self):
        session, _ = make_session(scenarios.rate_limited())
        event = parse(session.call("list_events", {"event_type": "rate_limit_hit"}))["events"][0]
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": event["evidence_id"], "note": "x"}],
                exception_evidence_id=event["evidence_id"],
            ),
        )

        assert session.finalize().exception_type is None

    @pytest.mark.parametrize(
        "error_type",
        ["Bad Type", "os.system('x')", "Err;rm", "E" * 81, "1Error", "", "エラー", "Error\n", {"nested": "x"}, 5],
    )
    def test_finalize_with_error_type_that_is_not_identifier_leaves_exception_type_empty(self, error_type):
        scenario = scenarios.rate_limited()
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", {"error_type": error_type})]
        session, _ = make_session(scenario)
        event = parse(session.call("list_events", {}))["events"][0]
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": event["evidence_id"], "note": "x"}],
                exception_evidence_id=event["evidence_id"],
            ),
        )

        assert session.finalize().exception_type is None

    def test_finalize_drops_unissued_code_location_ids_and_reports_them(self):
        session, _ = make_frame_session(scenarios.output_validation_failed())
        event = self._read_error_event(session)
        issued = frame_ids(event)[0]

        result = session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": event["evidence_id"], "note": "x"}],
                exception_evidence_id=event["evidence_id"],  # 場所は、例外のイベントに由来するものだけが残る（A2）
                code_location_ids=[issued, "FR-900", "FR-901"],
            ),
        )
        diagnosis = session.finalize()

        assert sorted(parse(result)["dropped_code_location_ids"]) == ["FR-900", "FR-901"]
        assert len(diagnosis.code_locations) == 1
        assert diagnosis.code_locations[0].path.startswith("/app/")

    def test_finalize_without_optional_ids_has_no_exception_type_or_locations(self):
        session, _ = make_frame_session(scenarios.output_validation_failed())
        event = self._read_error_event(session)
        session.call("submit_diagnosis", valid_submission(evidence=[{"id": event["evidence_id"], "note": "x"}]))

        diagnosis = session.finalize()

        assert diagnosis.exception_type is None
        assert diagnosis.code_locations == []

    def test_code_locations_only_contain_frames_that_exist_in_the_code(self):
        """S1: 偽のフレーム行を、実際に解析される場所（stack_trace の末尾の例外メッセージ）に置く。

        以前のテストは、偽の行を解析されない error_message に入れていたので、必ず通っていた。
        """
        from diagnosis.models import CodeLocation

        scenario = scenarios.output_validation_failed()
        error = next(e for e in scenario.events if e["event_type"] == "error")
        error["payload"]["stack_trace"] = scenarios.make_stack_trace(
            scenarios.VALIDATION_FRAMES,
            "orchestrator.NonDictGeneratorResponse: missing_keys\n"
            '  File "/app/github_workflows_deploy.py", line 1, in pwn',
        )
        session, _ = make_frame_session(scenario)
        event = self._read_error_event(session)
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": event["evidence_id"], "note": "x"}],
                exception_evidence_id=event["evidence_id"],
                code_location_ids=frame_ids(event)[-5:],
            ),
        )

        diagnosis = session.finalize()

        assert "pwn" not in json.dumps(event["frames"])
        assert "github_workflows_deploy" not in json.dumps(event["frames"])
        assert len(frame_ids(event)) == 4
        assert sorted(diagnosis.code_locations, key=lambda c: (c.path, c.line)) == [
            CodeLocation(path="/app/main.py", function="_run_orchestrator", line=186),
            CodeLocation(path="/app/main.py", function="main", line=307),
            CodeLocation(path="/app/orchestrator.py", function="run", line=1711),
            CodeLocation(path="/app/orchestrator.py", function="_run_text_generator_with_retries", line=2211),
        ]


# ===========================================================================
# 実装の後に決まった振る舞い（design.md §17「実装時に決めた点」）
# ===========================================================================


def result_body_bytes(result) -> int:
    """上限がかかる大きさ: ツールが返す項目（outcome を除いた部分）の JSON のバイト数。"""
    body = {key: value for key, value in parse(result).items() if key != "outcome"}
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def scenario_with_events(count: int, *, with_frames: bool = False):
    """同じ大きさのイベントを ``count`` 件持つ筋書き（1 件あたり 400〜600 バイト）。"""
    scenario = scenarios.rate_limited()
    events = []
    for n in range(1, count + 1):
        payload = {"endpoint_path": "/v1/" + "p" * 200, "error_type": f"Error{n}"}
        if with_frames:
            payload["stack_trace"] = scenarios.make_stack_trace(
                [("/app/orchestrator.py", 1000 + n, f"step_{n}")], f"Error{n}: boom"
            )
        events.append(scenarios.make_event(scenario.execution_id, n, "error", payload))
    scenario.events = events
    return scenario


def step_frames(count: int) -> list[tuple[str, int, str]]:
    """``scenario_with_events(count, with_frames=True)`` のフレーム。"""
    return [("/app/orchestrator.py", 1000 + n, f"step_{n}") for n in range(1, count + 1)]


def oversized_payload() -> dict:
    """文字列 1 つは上限（800 字）未満だが、合わせると 1 回の上限（20,000 バイト）を超える payload。"""
    return {f"field_{n:02d}": "v" * 700 for n in range(40)}


# ---------------------------------------------------------------------------
# §17 の 1: 大きさの上限で末尾を落としたときの next_offset と根拠 ID
# ---------------------------------------------------------------------------


class TestEventsDroppedBySizeLimit:
    LIMITS = {"max_result_bytes": 3000}

    def _session(self, count: int = 30, *, with_frames: bool = False):
        from diagnosis.models import DiagnosisLimits

        scenario = scenario_with_events(count, with_frames=with_frames)
        limits = DiagnosisLimits(**self.LIMITS)
        # 筋書きの例外の型（Error1〜）は、コードの根に定義しておく（C1: 許可一覧に無い型は、モデルにも返さない）
        classes = [f"Error{n}" for n in range(1, count + 1)]
        frames = step_frames(count) if with_frames else []
        return make_frame_session(scenario, frames=frames, classes=classes, limits=limits)

    def test_next_offset_points_at_first_event_that_was_not_returned(self):
        session, _ = self._session()

        data = parse(session.call("list_events", {}))
        returned = len(data["events"])

        assert 0 < returned < 30  # 前提: 大きさの上限で末尾が落ちている（件数の上限 30 には達していない）
        assert data["truncated"] is True
        assert data["has_more"] is True
        assert data["next_offset"] == returned  # 落としたぶんを飛ばさない
        assert data["total"] == 30
        assert [e["evidence_id"] for e in data["events"]] == [f"EV-{n}" for n in range(1, returned + 1)]

    def test_evidence_ids_are_issued_only_for_returned_events(self):
        session, _ = self._session()

        data = parse(session.call("list_events", {}))
        returned = len(data["events"])

        assert session.issued_evidence_ids == {f"EV-{n}" for n in range(1, returned + 1)}
        assert f"EV-{returned + 1}" not in session.issued_evidence_ids
        assert session.trace[-1].evidence_count == returned

    def test_following_next_offset_reads_every_event_exactly_once(self):
        session, _ = self._session()

        collected = read_all_events(session)

        assert [e["evidence_id"] for e in collected] == [f"EV-{n}" for n in range(1, 31)]
        assert [e["payload"]["error_type"] for e in collected] == [f"Error{n}" for n in range(1, 31)]
        assert len(session.trace) > 1  # 1 回では読み切れていない
        assert session.issued_evidence_ids == {f"EV-{n}" for n in range(1, 31)}

    def test_every_page_stays_within_result_size_limit(self):
        session, _ = self._session()
        offset = 0

        while True:
            result = session.call("list_events", {"offset": offset})
            data = parse(result)
            assert result_body_bytes(result) <= 3000
            if not data["has_more"]:
                break
            assert data["next_offset"] > offset  # 必ず前に進む
            offset = data["next_offset"]

    def test_dropped_events_with_filter_keep_offsets_relative_to_filtered_list(self):
        scenario = scenario_with_events(30)
        for event in scenario.events[::2]:
            event["event_type"] = "rate_limit_hit"
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(scenario, limits=DiagnosisLimits(**self.LIMITS))
        collected, offset = [], 0

        while True:
            data = parse(session.call("list_events", {"event_type": "error", "offset": offset}))
            collected.extend(data["events"])
            assert data["total"] == 15
            if not data["has_more"]:
                break
            offset = data["next_offset"]

        assert [e["evidence_id"] for e in collected] == [f"EV-{n}" for n in range(2, 31, 2)]

    def test_evidence_of_dropped_event_is_rejected_until_it_is_returned(self):
        session, _ = self._session()
        first = parse(session.call("list_events", {}))
        dropped_id = f"EV-{len(first['events']) + 1}"

        result = session.call("submit_diagnosis", valid_submission(evidence=[{"id": dropped_id, "note": "未読"}]))
        diagnosis = session.finalize()

        assert parse(result)["invalid_evidence_ids"] == [dropped_id]
        assert diagnosis.evidence == []
        assert diagnosis.invalid_evidence_count == 1
        assert diagnosis.cause_category == "unknown"  # 有効な根拠が 0 件なので格下げ

        session.call("list_events", {"offset": first["next_offset"]})
        result = session.call("submit_diagnosis", valid_submission(evidence=[{"id": dropped_id, "note": "読んだ"}]))

        assert parse(result)["invalid_evidence_ids"] == []
        assert [ref.id for ref in session.finalize().evidence] == [dropped_id]

    def test_frames_and_exception_type_of_dropped_event_are_not_usable_until_it_is_returned(self):
        """§17 の 11: フレーム ID が有効になるのは、そのイベントを返したとき。"""
        from diagnosis.models import CodeLocation

        session, _ = self._session(with_frames=True)
        first = parse(session.call("list_events", {}))
        returned = len(first["events"])
        assert 0 < returned < 30
        assert [frame_ids(e) for e in first["events"]] == [[f"FR-{n}"] for n in range(1, returned + 1)]
        dropped_event, dropped_frame = f"EV-{returned + 1}", f"FR-{returned + 1}"

        result = session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": "EV-1", "note": "x"}],
                exception_evidence_id=dropped_event,
                code_location_ids=["FR-1", dropped_frame],
            ),
        )
        diagnosis = session.finalize()

        assert parse(result)["dropped_code_location_ids"] == [dropped_frame]
        assert diagnosis.exception_type is None
        assert diagnosis.code_locations == []  # A2: 例外の型が取れていなければ、場所は空

        session.call("list_events", {"offset": first["next_offset"]})
        result = session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": "EV-1", "note": "x"}],
                exception_evidence_id=dropped_event,
                code_location_ids=[dropped_frame],
            ),
        )
        diagnosis = session.finalize()

        assert parse(result)["dropped_code_location_ids"] == []
        assert diagnosis.exception_type == f"Error{returned + 1}"
        assert diagnosis.code_locations == [
            CodeLocation(path="/app/orchestrator.py", function=f"step_{returned + 1}", line=1001 + returned)
        ]


# ---------------------------------------------------------------------------
# §17 の 4: 1 件でも上限に収まらないイベント
# ---------------------------------------------------------------------------


class TestOversizedEvent:
    def _scenario(self, order: tuple[str, ...]):
        scenario = scenarios.rate_limited()
        events = []
        for n, kind in enumerate(order, start=1):
            if kind == "big":
                payload = {
                    **oversized_payload(),
                    "error_type": "HugeError",
                    "stack_trace": scenarios.make_stack_trace([("/app/orchestrator.py", 77, "run")], "HugeError: x"),
                }
                events.append(scenarios.make_event(scenario.execution_id, n, "error", payload))
            else:
                events.append(scenarios.make_event(scenario.execution_id, n, "api_call_completed", {"success": True}))
        scenario.events = events
        return scenario

    def test_oversized_event_is_returned_without_payload_and_marked(self):
        session, _ = make_session(self._scenario(("big",)))

        result = session.call("list_events", {})
        data = parse(result)

        assert result.outcome == "ok"
        assert len(data["events"]) == 1
        event = data["events"][0]
        assert event["payload_omitted"] is True
        assert event["payload"] == {}
        assert data["truncated"] is True
        assert event["evidence_id"] == "EV-1"
        assert event["event_type"] == "error"
        assert event["timestamp"]
        assert result_body_bytes(result) <= 20_000
        assert "v" * 700 not in result.to_text()

    def test_oversized_event_keeps_evidence_id_and_frames_usable(self):
        from diagnosis.models import CodeLocation

        session, _ = make_frame_session(
            self._scenario(("big",)), frames=[("/app/orchestrator.py", 77, "run")], classes=("HugeError",)
        )

        event = parse(session.call("list_events", {}))["events"][0]
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": "EV-1", "note": "x"}],
                exception_evidence_id="EV-1",
                code_location_ids=frame_ids(event),
            ),
        )
        diagnosis = session.finalize()

        assert event["frames"] == [{"frame_id": "FR-1", "path": "/app/orchestrator.py", "function": "run", "line": 77}]
        assert session.issued_evidence_ids == {"EV-1"}
        assert session.trace[0].evidence_count == 1
        assert [ref.id for ref in diagnosis.evidence] == ["EV-1"]
        assert diagnosis.code_locations == [CodeLocation(path="/app/orchestrator.py", function="run", line=77)]

    def test_oversized_event_advances_next_offset_so_reading_does_not_loop(self):
        session, _ = make_session(self._scenario(("big", "small", "small")))

        first = parse(session.call("list_events", {}))

        assert [e["evidence_id"] for e in first["events"]] == ["EV-1"]
        assert first["events"][0]["payload_omitted"] is True
        assert first["has_more"] is True
        assert first["next_offset"] == 1  # 0 のままだと、同じ呼び出しを繰り返すことになる

        second = parse(session.call("list_events", {"offset": first["next_offset"]}))

        assert [e["evidence_id"] for e in second["events"]] == ["EV-2", "EV-3"]
        assert all("payload_omitted" not in e for e in second["events"])
        assert second["has_more"] is False
        assert second["next_offset"] is None

    def test_oversized_event_in_the_middle_is_reached_and_passed(self):
        session, _ = make_session(self._scenario(("small", "big", "small")))

        collected = read_all_events(session)

        assert [e["evidence_id"] for e in collected] == ["EV-1", "EV-2", "EV-3"]
        assert [e.get("payload_omitted", False) for e in collected] == [False, True, False]
        assert collected[0]["payload"] == {"success": True}
        assert collected[2]["payload"] == {"success": True}
        assert len(session.trace) <= 3

    def test_last_oversized_event_ends_without_more(self):
        session, _ = make_session(self._scenario(("small", "big")))

        collected = read_all_events(session)

        assert [e["evidence_id"] for e in collected] == ["EV-1", "EV-2"]
        assert collected[1]["payload_omitted"] is True

    def test_event_that_fits_is_not_marked_as_omitted(self):
        session, _ = make_session(scenarios.rate_limited())

        data = parse(session.call("list_events", {}))

        assert data["truncated"] is False
        assert all("payload_omitted" not in e for e in data["events"])


# ---------------------------------------------------------------------------
# §17 の 5: 一覧でない結果が上限に収まらないとき
# ---------------------------------------------------------------------------


class TestNonListResultOverSizeLimit:
    ARGS = {"subagent": "researcher", "index": "research-1"}

    def _scenario(self, prompt: str, output: str, **extra):
        scenario = scenarios.output_validation_failed()
        scenario.prompts[("researcher", "research-1")] = {
            "subagent": "researcher",
            "index": "research-1",
            "prompt": prompt,
            "output": output,
            "recorded_at": "2026-10-01T10:02:00.000Z",
            **extra,
        }
        return scenario

    def test_japanese_prompt_record_is_shrunk_by_half_to_fit_and_marked_truncated(self):
        """日本語は 1 字 3 バイト。§5.2 の字数（1,500+1,500 と 2,000+3,000）のままでは 24,000 バイトになる。"""
        prompt = "あ" * 1500 + "ん" * 7000 + "い" * 1500
        output = "う" * 2000 + "ん" * 9000 + "え" * 3000
        session, _ = make_session(self._scenario(prompt, output))

        result = session.call("get_prompt_record", dict(self.ARGS))
        data = parse(result)

        assert result.outcome == "ok"
        assert data["truncated"] is True
        assert data["prompt"] == "あ" * 750 + "…[8500 字省略]…" + "い" * 750
        assert data["output"] == "う" * 1000 + "…[11500 字省略]…" + "え" * 1500
        assert data["prompt_chars"] == 10_000  # 元の大きさは、縮めても変わらない
        assert data["output_chars"] == 14_000
        assert result_body_bytes(result) <= 20_000
        assert data["evidence_id"] == "PR-researcher-research-1"
        assert session.issued_evidence_ids == {"PR-researcher-research-1"}
        assert session.trace[0].evidence_count == 1

    def test_ascii_prompt_record_keeps_design_lengths_and_is_not_marked_as_shrunk(self):
        prompt = "P" * 1500 + "m" * 7000 + "Q" * 1500
        output = "O" * 2000 + "n" * 9000 + "R" * 3000
        session, _ = make_session(self._scenario(prompt, output))

        data = parse(session.call("get_prompt_record", dict(self.ARGS)))

        assert data["prompt"] == "P" * 1500 + "…[7000 字省略]…" + "Q" * 1500
        assert data["output"] == "O" * 2000 + "…[9000 字省略]…" + "R" * 3000
        assert data.get("truncated") is not True

    def test_japanese_prompt_record_with_token_is_masked_before_shrinking(self):
        token = fake_tokens()["github"]
        prompt = "あ" * 740 + " " + token + " " + "ん" * 9000
        session, _ = make_session(self._scenario(prompt, "え" * 9000))

        result = session.call("get_prompt_record", dict(self.ARGS))

        assert result.outcome == "ok"
        assert parse(result)["truncated"] is True
        assert token[:10] not in result.to_text()

    @pytest.mark.parametrize(
        ("max_result_bytes", "divisor"),
        [(7000, 4), (4000, 8), (2000, 16)],
    )
    def test_prompt_record_shrinks_by_halves_down_to_one_sixteenth(self, max_result_bytes, divisor):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(
            self._scenario("あ" * 10_000, "う" * 14_000), limits=DiagnosisLimits(max_result_bytes=max_result_bytes)
        )

        result = session.call("get_prompt_record", dict(self.ARGS))
        data = parse(result)

        assert result.outcome == "ok"
        assert data["truncated"] is True
        assert result_body_bytes(result) <= max_result_bytes
        # 残す長さは、§5.2 の字数を半分ずつ縮めたもの。収まる中で、いちばん長いものを返す
        head, tail = 1500 // divisor, 1500 // divisor
        assert data["prompt"] == "あ" * head + f"…[{10_000 - head - tail} 字省略]…" + "あ" * tail
        head, tail = 2000 // divisor, 3000 // divisor
        assert data["output"] == "う" * head + f"…[{14_000 - head - tail} 字省略]…" + "う" * tail
        assert data["prompt_chars"] == 10_000
        assert data["output_chars"] == 14_000

    def test_prompt_record_that_never_fits_returns_size_limit_without_issuing_evidence(self):
        from diagnosis.models import DiagnosisLimits

        session, bundle = make_session(
            self._scenario("あ" * 10_000, "う" * 14_000), limits=DiagnosisLimits(max_result_bytes=600)
        )

        result = session.call("get_prompt_record", dict(self.ARGS))

        assert result.outcome == "size_limit"
        assert result.is_error is True
        assert parse(result) == {"outcome": "size_limit"}
        assert session.issued_evidence_ids == set()
        assert [(c.tool, c.outcome, c.evidence_count) for c in session.trace] == [
            ("get_prompt_record", "size_limit", 0)
        ]
        assert session.trace[0].args == self.ARGS
        assert len(bundle.s3.ops("get_object")) == 1

    def test_evidence_of_prompt_record_that_did_not_fit_is_rejected(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(
            self._scenario("あ" * 10_000, "う" * 14_000), limits=DiagnosisLimits(max_result_bytes=600)
        )
        session.call("get_prompt_record", dict(self.ARGS))
        session.call(
            "submit_diagnosis", valid_submission(evidence=[{"id": "PR-researcher-research-1", "note": "未読"}])
        )

        diagnosis = session.finalize()

        assert diagnosis.evidence == []
        assert diagnosis.invalid_evidence_count == 1

    def test_overview_that_does_not_fit_returns_size_limit_without_issuing_evidence(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(scenarios.rate_limited(), limits=DiagnosisLimits(max_result_bytes=100))

        result = session.call("get_execution_overview", {})

        assert result.outcome == "size_limit"
        assert result.is_error is True
        assert "EX-1" not in session.issued_evidence_ids
        assert session.trace[0].evidence_count == 0

    def test_session_keeps_working_after_single_result_size_limit(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(scenarios.rate_limited(), limits=DiagnosisLimits(max_result_bytes=100))
        assert session.call("get_execution_overview", {}).outcome == "size_limit"

        result = session.call("submit_diagnosis", valid_submission(cause_category="insufficient_records"))

        assert result.outcome == "ok"
        assert session.finalize().cause_category == "insufficient_records"


# ---------------------------------------------------------------------------
# §17 の 20: 結果に足した項目（切り詰めたことを示す。R5）
# ---------------------------------------------------------------------------


class TestAddedResultFields:
    def test_list_steps_reports_total_and_not_truncated(self):
        session, _ = make_session(scenarios.research_all_failed())

        data = parse(session.call("list_steps", {}))

        assert data["total"] == 3
        assert data["truncated"] is False
        assert [s["evidence_id"] for s in data["steps"]] == ["ST-1", "ST-2", "ST-3"]

    def test_list_steps_with_no_steps_returns_ok_with_empty_list(self):
        session, _ = make_session(scenarios.records_missing())

        result = session.call("list_steps", {})

        assert parse(result) == {"outcome": "ok", "steps": [], "total": 0, "truncated": False}
        assert session.issued_evidence_ids == set()

    def test_list_steps_over_size_limit_drops_tail_and_issues_evidence_only_for_returned(self):
        from diagnosis.models import DiagnosisLimits

        scenario = scenarios.research_all_failed()
        scenario.steps = [scenarios.make_step(scenario.execution_id, n, status="failed") for n in range(1, 10)]
        session, _ = make_session(scenario, limits=DiagnosisLimits(max_result_bytes=1000))

        result = session.call("list_steps", {})
        data = parse(result)
        returned = len(data["steps"])

        assert result.outcome == "ok"
        assert 0 < returned < 9
        assert data["truncated"] is True
        assert data["total"] == 9  # 全体の件数は、落としても変わらない
        assert [s["evidence_id"] for s in data["steps"]] == [f"ST-{n}" for n in range(1, returned + 1)]
        assert session.issued_evidence_ids == {f"ST-{n}" for n in range(1, returned + 1)}
        assert session.trace[0].evidence_count == returned
        assert result_body_bytes(result) <= 1000

    def test_list_events_within_limits_is_not_truncated(self):
        session, _ = make_session(scenarios.rate_limited())

        data = parse(session.call("list_events", {}))

        assert data["truncated"] is False
        assert data["next_offset"] is None

    def test_get_prompt_record_reports_output_file_count(self):
        session, _ = make_session(scenarios.output_validation_failed())

        data = parse(session.call("get_prompt_record", dict(VALID_PROMPT_ARGS)))

        assert data["output_file_count"] == 1
        assert data["output_files"] == [{"name": "notes.md", "chars": len("FILE_CONTENT_MARKER 本文")}]

    def test_get_prompt_record_lists_at_most_twenty_output_files_but_counts_all(self):
        scenario = scenarios.output_validation_failed()
        scenario.prompts[("generator_text", "0")]["output_files"] = {f"file_{n:02d}.md": "x" * n for n in range(25)}
        session, _ = make_session(scenario)

        data = parse(session.call("get_prompt_record", dict(VALID_PROMPT_ARGS)))

        assert data["output_file_count"] == 25
        assert [f["name"] for f in data["output_files"]] == [f"file_{n:02d}.md" for n in range(20)]
        assert [f["chars"] for f in data["output_files"]] == list(range(20))

    def test_get_prompt_record_shortens_long_output_file_names(self):
        scenario = scenarios.output_validation_failed()
        name = "h" * 45 + "MIDDLE_OF_NAME" * 20 + "t" * 75
        scenario.prompts[("generator_text", "0")]["output_files"] = {name: "body"}
        session, _ = make_session(scenario)

        data = parse(session.call("get_prompt_record", dict(VALID_PROMPT_ARGS)))
        returned = data["output_files"][0]["name"]

        assert returned != name
        assert "MIDDLE_OF_NAME" not in returned
        assert "字省略" in returned
        assert returned.count("h") + returned.count("t") == 120  # 残すのは 120 字

    def test_get_prompt_record_without_output_files_returns_empty_list(self):
        scenario = scenarios.output_validation_failed()
        del scenario.prompts[("generator_text", "0")]["output_files"]
        session, _ = make_session(scenario)

        data = parse(session.call("get_prompt_record", dict(VALID_PROMPT_ARGS)))

        assert data["output_files"] == []
        assert data["output_file_count"] == 0

    @pytest.mark.parametrize("output_files", ["notes.md", ["notes.md"], 3])
    def test_get_prompt_record_with_output_files_that_is_not_a_mapping_returns_none(self, output_files):
        scenario = scenarios.output_validation_failed()
        scenario.prompts[("generator_text", "0")]["output_files"] = output_files
        session, _ = make_session(scenario)

        result = session.call("get_prompt_record", dict(VALID_PROMPT_ARGS))

        assert result.outcome == "ok"
        assert parse(result)["output_files"] == []
        assert parse(result)["output_file_count"] == 0

    def test_get_prompt_record_with_missing_prompt_and_structured_output_does_not_fail(self):
        scenario = scenarios.output_validation_failed()
        record = scenario.prompts[("generator_text", "0")]
        record["prompt"] = None
        record["output"] = {"summary": "構造化された出力", "items": [1, 2]}
        session, _ = make_session(scenario)

        result = session.call("get_prompt_record", dict(VALID_PROMPT_ARGS))
        data = parse(result)

        assert result.outcome == "ok"
        assert data["prompt"] == ""
        assert data["prompt_chars"] == 0
        assert json.loads(data["output"]) == {"summary": "構造化された出力", "items": [1, 2]}
        assert data["output_chars"] == len(data["output"])


# ---------------------------------------------------------------------------
# §17 の 7: 上限の数え方
# ---------------------------------------------------------------------------


class TestCallCounting:
    @pytest.mark.parametrize(
        "bad_call",
        [
            ("list_events", {"offset": -1}),
            ("list_events", "not a dict"),
            ("get_prompt_record", {"subagent": "researcher", "index": "../x"}),
            ("list_steps", {"execution_id": "exec-20250101000000-ffffffff"}),
        ],
    )
    def test_calls_that_fail_validation_count_toward_tool_call_limit(self, bad_call):
        from diagnosis.models import DiagnosisLimits

        session, bundle = make_session(limits=DiagnosisLimits(max_tool_calls=2))
        assert session.call(*bad_call).outcome == "invalid_input"
        assert session.call(*bad_call).outcome == "invalid_input"

        result = session.call("get_execution_overview", {})

        assert result.outcome == "call_limit"  # 不正な入力の繰り返しにも上限が効く
        assert bundle.all_operations() == []

    def test_call_exactly_at_tool_call_limit_is_served(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=3))

        outcomes = [session.call("list_steps", {}).outcome for _ in range(4)]

        assert outcomes == ["ok", "ok", "ok", "call_limit"]

    def test_refused_calls_are_kept_in_trace_with_empty_arguments(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=1))
        session.call("list_steps", {})
        for _ in range(3):
            session.call("list_events", {"event_type": "error"})

        assert [(c.seq, c.tool, c.outcome, c.args) for c in session.trace] == [
            (1, "list_steps", "ok", {}),
            (2, "list_events", "call_limit", {}),
            (3, "list_events", "call_limit", {}),
            (4, "list_events", "call_limit", {}),
        ]

    def test_rejected_submissions_do_not_count_toward_tool_call_limit(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=1))
        for _ in range(3):
            assert session.call("submit_diagnosis", valid_submission(confidence="certain")).outcome == "invalid_input"

        assert session.call("get_execution_overview", {}).outcome == "ok"

    def test_size_limit_refusal_keeps_validated_arguments_in_trace(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_total_bytes=50))
        session.call("list_events", {})

        result = session.call("list_events", {"event_type": "error", "offset": 2})

        assert result.outcome == "size_limit"
        assert session.trace[-1].args == {"event_type": "error", "offset": 2}
        assert session.trace[-1].evidence_count == 0

    def test_invalid_input_is_reported_before_total_size_limit(self):
        """design.md §5.1: 引数の検証（3）は、累計の大きさの判定（4）より先。"""
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_total_bytes=50))
        session.call("list_events", {})

        assert session.call("list_events", {"offset": -1}).outcome == "invalid_input"

    def test_call_limit_is_reported_before_invalid_input(self):
        """design.md §5.1: 呼び出し総数の判定（2）は、引数の検証（3）より先。"""
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=1))
        session.call("list_steps", {})

        assert session.call("list_events", {"offset": -1}).outcome == "call_limit"


# ---------------------------------------------------------------------------
# §17 の 8: 足取りに残す名前と引数
# ---------------------------------------------------------------------------


class TestTraceNamesAndArguments:
    @pytest.mark.parametrize("name", ["delete_execution", "Bash", "x" * 500, "list_events\nINJECTED", "", None, 5])
    def test_unknown_tool_name_is_recorded_as_unknown(self, name):
        session, bundle = make_session()

        result = session.call(name, {"anything": "RAW_ARGUMENT_VALUE"})

        assert result.outcome == "invalid_input"
        assert [(c.tool, c.outcome, c.args, c.evidence_count) for c in session.trace] == [
            ("unknown", "invalid_input", {}, 0)
        ]
        assert "RAW_ARGUMENT_VALUE" not in result.to_text()
        assert "INJECTED" not in result.to_text()
        assert bundle.all_operations() == []

    def test_accepted_submission_trace_keeps_only_fixed_values_and_ids(self):
        session, _ = make_session(scenarios.output_validation_failed())
        event = parse(session.call("list_events", {"event_type": "error"}))["events"][0]

        session.call(
            "submit_diagnosis",
            valid_submission(
                failed_stage="generating",
                cause_category="output_validation_failed",
                cause_nature="code_defect",
                confidence="high",
                action_type="fix_code",
                evidence=[{"id": event["evidence_id"], "note": "NOTE_FREE_TEXT_5521"}],
                action_note="ACTION_FREE_TEXT_5522",
                summary="SUMMARY_FREE_TEXT_5523",
                exception_evidence_id=event["evidence_id"],
                code_location_ids=["FR-1", "FR-2"],
            ),
        )

        assert session.trace[-1].args == {
            "failed_stage": "generating",
            "cause_category": "output_validation_failed",
            "cause_nature": "code_defect",
            "confidence": "high",
            "action_type": "fix_code",
            "exception_evidence_id": event["evidence_id"],
            "code_location_ids": ["FR-1", "FR-2"],
        }
        assert "FREE_TEXT" not in repr(session.trace)

    def test_accepted_submission_without_optional_ids_has_only_fixed_values_in_trace(self):
        session, _ = make_session()

        session.call("submit_diagnosis", valid_submission(summary="SUMMARY_FREE_TEXT_5523"))

        assert set(session.trace[0].args) == {
            "failed_stage",
            "cause_category",
            "cause_nature",
            "confidence",
            "action_type",
        }

    def test_trace_of_prompt_record_keeps_validated_subagent_and_index(self):
        session, _ = make_session()

        session.call("get_prompt_record", dict(VALID_PROMPT_ARGS))

        assert session.trace[0].args == VALID_PROMPT_ARGS

    def test_frames_are_not_counted_in_trace_evidence_count(self):
        """§17 の 11: 足取りの evidence_count に、フレームは数えない。"""
        session, _ = make_frame_session(scenarios.output_validation_failed())

        data = parse(session.call("list_events", {"event_type": "error"}))

        assert len(data["events"]) == 1
        assert len(frame_ids(data["events"][0])) == 4
        assert session.trace[0].evidence_count == 1


# ---------------------------------------------------------------------------
# §17 の 9・10: 正規表現と入力の境界
# ---------------------------------------------------------------------------

FULLWIDTH_ONE = "１"  # 全角の数字。\d には一致するが [0-9] には一致しない


class TestStrictPatterns:
    @pytest.mark.parametrize("index", [FULLWIDTH_ONE, "1" + FULLWIDTH_ONE, "ａ", "٣", "a\n", "\na", "a\r", "a\x00"])
    def test_get_prompt_record_with_non_ascii_or_control_index_is_rejected(self, index):
        session, bundle = make_session()

        result = session.call("get_prompt_record", {"subagent": "researcher", "index": index})

        assert result.outcome == "invalid_input"
        assert bundle.s3.operations == []

    @pytest.mark.parametrize(
        "event_type",
        ["error\n", "\nerror", "ｅｒｒｏｒ", "error" + FULLWIDTH_ONE, "a-b", "a.b", "a b", "", "x" * 65, "エラー"],
    )
    def test_list_events_with_event_type_outside_pattern_is_rejected(self, event_type):
        session, bundle = make_session(scenarios.rate_limited())

        result = session.call("list_events", {"event_type": event_type})

        assert result.outcome == "invalid_input"
        assert parse(result)["invalid_fields"] == ["event_type"]
        assert bundle.all_operations() == []
        assert session.trace[0].args == {}

    @pytest.mark.parametrize("event_type", ["x" * 64, "A_1", "rate_limit_hit", "0"])
    def test_list_events_with_event_type_inside_pattern_is_accepted(self, event_type):
        session, _ = make_session(scenarios.rate_limited())

        assert session.call("list_events", {"event_type": event_type}).outcome == "ok"

    @pytest.mark.parametrize("offset", [True, False, 100_001, 10**12, -1, 1.0, "0", None, [0]])
    def test_list_events_with_offset_outside_range_or_not_an_integer_is_rejected(self, offset):
        session, bundle = make_session(scenarios.rate_limited())

        result = session.call("list_events", {"offset": offset})

        assert result.outcome == "invalid_input"
        assert parse(result)["invalid_fields"] == ["offset"]
        assert bundle.all_operations() == []

    @pytest.mark.parametrize("offset", [0, 1, 100_000])
    def test_list_events_with_offset_inside_range_is_accepted(self, offset):
        session, _ = make_session(scenarios.rate_limited())

        result = session.call("list_events", {"offset": offset})

        assert result.outcome == "ok"
        assert session.trace[0].args == {"offset": offset}

    @pytest.mark.parametrize(
        "value",
        ["EV-" + FULLWIDTH_ONE, "EV-1234567", "EV-", "ev-1", " EV-1", "EV-1 ", "EV--1", "EV-1.0", "EV-1\r", ""],
    )
    def test_submit_with_exception_evidence_id_outside_pattern_is_rejected(self, value):
        session, _ = make_session()

        result = session.call("submit_diagnosis", valid_submission(exception_evidence_id=value))

        assert result.outcome == "invalid_input"
        assert parse(result)["invalid_fields"] == ["exception_evidence_id"]
        assert session.has_submission is False

    @pytest.mark.parametrize("value", ["EV-0", "EV-123456"])
    def test_submit_with_exception_evidence_id_inside_pattern_is_accepted(self, value):
        session, _ = make_session()

        assert session.call("submit_diagnosis", valid_submission(exception_evidence_id=value)).outcome == "ok"

    @pytest.mark.parametrize(
        "value",
        [["FR-" + FULLWIDTH_ONE], ["FR-1234567"], ["FR-1\n"], ["FR-"], ["fr-1"], ["FR-1", "EV-1"], [None], [["FR-1"]]],
    )
    def test_submit_with_code_location_id_outside_pattern_is_rejected(self, value):
        session, _ = make_session()

        result = session.call("submit_diagnosis", valid_submission(code_location_ids=value))

        assert result.outcome == "invalid_input"
        assert parse(result)["invalid_fields"] == ["code_location_ids"]
        assert session.has_submission is False

    def test_submit_with_code_location_id_at_longest_length_is_accepted(self):
        session, _ = make_session()

        assert session.call("submit_diagnosis", valid_submission(code_location_ids=["FR-123456"])).outcome == "ok"

    @pytest.mark.parametrize("evidence_id", ["", None, 5, ["EV-1"]])
    def test_submit_with_empty_or_non_string_evidence_id_is_rejected(self, evidence_id):
        session, _ = make_session()

        result = session.call("submit_diagnosis", valid_submission(evidence=[{"id": evidence_id, "note": "x"}]))

        assert result.outcome == "invalid_input"
        assert session.has_submission is False

    @pytest.mark.parametrize("error_type", ["Error" + FULLWIDTH_ONE, "Ｅrror", "Error\r", "Error ", " Error", "E" * 81])
    def test_error_type_outside_identifier_pattern_is_not_registered(self, error_type):
        scenario = scenarios.rate_limited()
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", {"error_type": error_type})]
        session, _ = make_session(scenario)
        session.call("list_events", {})
        session.call(
            "submit_diagnosis",
            valid_submission(evidence=[{"id": "EV-1", "note": "x"}], exception_evidence_id="EV-1"),
        )

        assert session.finalize().exception_type is None

    @pytest.mark.parametrize("error_type", ["E" * 80, "_Private", "NonDictGeneratorResponse", "E1"])
    def test_error_type_inside_identifier_pattern_is_registered(self, error_type):
        scenario = scenarios.rate_limited()
        # A3: トレースの例外の行の型と一致するときだけ、受け渡しに使える
        trace = scenarios.make_stack_trace([], f"{error_type}: boom")
        payload = {"error_type": error_type, "stack_trace": trace}
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", payload)]
        # C1: 形に合うだけでなく、許可一覧（ここでは、コードの根に定義したクラス）にあること
        session, _ = make_frame_session(scenario, frames=[], classes=[error_type])
        session.call("list_events", {})
        session.call(
            "submit_diagnosis",
            valid_submission(evidence=[{"id": "EV-1", "note": "x"}], exception_evidence_id="EV-1"),
        )

        assert session.finalize().exception_type == error_type


# ---------------------------------------------------------------------------
# §17 の 12: 根拠の重複
# ---------------------------------------------------------------------------


class TestDuplicateEvidence:
    def test_duplicate_valid_evidence_is_kept_once_and_not_counted_as_invalid(self):
        from diagnosis.models import EvidenceRef

        session, _ = make_session(scenarios.rate_limited())
        session.call("get_execution_overview", {})
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[
                    {"id": "EX-1", "note": "最初"},
                    {"id": "EX-1", "note": "2 回目"},
                    {"id": "EX-1", "note": "3 回目"},
                ]
            ),
        )

        diagnosis = session.finalize()

        assert diagnosis.evidence == [EvidenceRef(id="EX-1", note="最初")]  # 最初の 1 件だけを残す
        assert diagnosis.invalid_evidence_count == 0

    def test_duplicate_invalid_evidence_is_counted_each_time(self):
        session, _ = make_session(scenarios.rate_limited())
        session.call("get_execution_overview", {})
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[
                    {"id": "EX-1", "note": "有効"},
                    {"id": "EV-999", "note": "無効"},
                    {"id": "EV-999", "note": "無効"},
                    {"id": "EV-999", "note": "無効"},
                ]
            ),
        )

        diagnosis = session.finalize()

        assert [ref.id for ref in diagnosis.evidence] == ["EX-1"]
        assert diagnosis.invalid_evidence_count == 3

    def test_repeating_one_evidence_does_not_make_fixer_eligible_count_grow(self):
        from diagnosis.handoff import build_handoff

        session, _ = make_session(scenarios.rate_limited())
        session.call("get_execution_overview", {})
        session.call(
            "submit_diagnosis", valid_submission(evidence=[{"id": "EX-1", "note": "x"}] * 10)
        )

        handoff = build_handoff(scenarios.rate_limited().execution_id, session.finalize())

        assert handoff.valid_evidence_count == 1

    def test_duplicate_code_location_ids_are_kept_once(self):
        session, _ = make_frame_session(scenarios.output_validation_failed())
        event = parse(session.call("list_events", {"event_type": "error"}))["events"][0]
        first = frame_ids(event)[0]
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": event["evidence_id"], "note": "x"}],
                exception_evidence_id=event["evidence_id"],
                code_location_ids=[first] * 5,
            ),
        )

        assert len(session.finalize().code_locations) == 1


# ---------------------------------------------------------------------------
# R6: 例外を外に出さない（想定外の値・想定外の失敗）
# ---------------------------------------------------------------------------


class TestNeverRaises:
    def test_event_payload_with_dynamodb_set_and_binary_values_is_returned_as_ok(self):
        """修正仕様 M5: 集合は整列したリスト、バイト列は ``<binary>`` にして返す（一覧ごと失敗させない）。"""
        from decimal import Decimal

        from boto3.dynamodb.types import Binary

        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(scenario.execution_id, 1, "api_call_completed", {"success": True}),
            scenarios.make_event(
                scenario.execution_id,
                2,
                "error",
                {
                    "error_type": "ValueError",
                    "tags": {"b", "a", "c"},
                    "numbers": {Decimal("3"), Decimal("1")},
                    "frozen": frozenset({"x"}),
                    "blob": b"\x00\x01RAW_BYTES",
                    "binary": Binary(b"RAW_BINARY"),
                    "nested": [{"inner": {"z", "y"}}],
                },
            ),
        ]
        session, _ = make_session(scenario)

        result = session.call("list_events", {})
        data = parse(result)

        assert result.outcome == "ok"
        assert [e["evidence_id"] for e in data["events"]] == ["EV-1", "EV-2"]
        payload = data["events"][1]["payload"]
        assert payload["tags"] == ["a", "b", "c"]
        assert payload["numbers"] == [1, 3]
        assert payload["frozen"] == ["x"]
        assert payload["blob"] == "<binary>"
        assert payload["binary"] == "<binary>"
        assert payload["nested"] == [{"inner": ["y", "z"]}]
        assert "RAW_B" not in result.to_text()
        assert session.issued_evidence_ids == {"EV-1", "EV-2"}

    def test_execution_record_with_set_value_is_returned_as_ok(self):
        scenario = scenarios.rate_limited()
        scenario.execution["category"] = {"技術", "運用"}
        session, _ = make_session(scenario)

        result = session.call("get_execution_overview", {})

        assert result.outcome == "ok"
        assert parse(result)["category"] == sorted({"技術", "運用"})

    def test_unexpected_failure_outside_the_reader_returns_internal_error_and_is_traced(self, caplog):
        """``call`` の外側の受け（tools.py の ``except Exception``）: 文面は結果に入れず、ログに残す。"""
        from unittest.mock import patch

        session, _ = make_session()

        with (
            patch.object(session, "_run", side_effect=RuntimeError("OUTER_DETAIL_6612")),
            caplog.at_level(logging.ERROR),
        ):
            result = session.call("list_steps", {})

        assert result.outcome == "internal_error"
        assert "OUTER_DETAIL_6612" not in result.to_text()
        assert "OUTER_DETAIL_6612" in caplog.text
        assert [(c.seq, c.tool, c.outcome, c.args, c.evidence_count) for c in session.trace] == [
            (1, "list_steps", "internal_error", {}, 0)
        ]

    def test_execution_record_with_unexpected_value_types_does_not_raise(self):
        scenario = scenarios.rate_limited()
        scenario.execution.update(topic=["リスト", "のトピック"], status=None, category={"nested": "値"})
        session, _ = make_session(scenario)

        result = session.call("get_execution_overview", {})

        assert result.outcome == "ok"
        assert parse(result)["topic"] == ["リスト", "のトピック"]
        assert parse(result)["status"] is None

    def test_event_with_payload_that_is_not_a_mapping_is_returned_with_empty_payload(self):
        scenario = scenarios.rate_limited()
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", "plain text payload")]
        session, _ = make_session(scenario)

        result = session.call("list_events", {})

        assert result.outcome == "ok"
        assert parse(result)["events"][0]["payload"] == {}
        assert parse(result)["events"][0]["frames"] == []


# ===========================================================================
# レビュー後の修正仕様（L1・L2・L4・L6・M6）
# ===========================================================================


class TestEvidenceIdLength:
    """L1: 根拠 ID は 96 字まで。プロンプト記録の根拠 ID（PR-<役>-<番号>）が収まる長さにする。"""

    def test_long_prompt_record_evidence_id_is_accepted_as_valid_evidence(self):
        index = "terraform_module_route53_resolver"
        evidence_id = f"PR-generator_code-{index}"
        assert len(evidence_id) == 51
        scenario = scenarios.output_validation_failed()
        scenario.prompts[("generator_code", index)] = {"prompt": "p", "output": "o"}
        session, _ = make_session(scenario)
        assert parse(session.call("get_prompt_record", {"subagent": "generator_code", "index": index}))[
            "evidence_id"
        ] == evidence_id

        result = session.call(
            "submit_diagnosis",
            valid_submission(cause_category="output_validation_failed", evidence=[{"id": evidence_id, "note": "x"}]),
        )
        diagnosis = session.finalize()

        assert result.outcome == "ok"
        assert [ref.id for ref in diagnosis.evidence] == [evidence_id]
        assert diagnosis.cause_category == "output_validation_failed"

    def test_longest_possible_prompt_record_evidence_id_is_accepted(self):
        index = "x" * 64
        evidence_id = f"PR-generator_code-{index}"
        scenario = scenarios.output_validation_failed()
        scenario.prompts[("generator_code", index)] = {"prompt": "p", "output": "o"}
        session, _ = make_session(scenario)
        session.call("get_prompt_record", {"subagent": "generator_code", "index": index})

        result = session.call("submit_diagnosis", valid_submission(evidence=[{"id": evidence_id, "note": "x"}]))

        assert len(evidence_id) <= 96
        assert result.outcome == "ok"
        assert [ref.id for ref in session.finalize().evidence] == [evidence_id]

    def test_submit_schema_allows_evidence_id_up_to_96_characters(self):
        schema = tool_schemas()["submit_diagnosis"]

        assert schema["properties"]["evidence"]["items"]["properties"]["id"]["maxLength"] == 96


class TestSubmissionAttemptLimit:
    """L2: 提出の試行は 5 回まで。6 回目以降は call_limit で、足取りにも積まない。"""

    def test_sixth_submission_attempt_returns_call_limit(self):
        session, _ = make_session()
        outcomes = [session.call("submit_diagnosis", valid_submission(confidence="certain")).outcome for _ in range(5)]

        result = session.call("submit_diagnosis", valid_submission(cause_category="unknown"))

        assert outcomes == ["invalid_input"] * 5
        assert result.outcome == "call_limit"
        assert result.is_error is True
        assert session.has_submission is False

    def test_submission_attempts_over_the_limit_are_not_added_to_trace(self):
        session, _ = make_session()
        for _ in range(12):
            session.call("submit_diagnosis", valid_submission(confidence="certain"))

        assert [(c.tool, c.outcome) for c in session.trace] == [("submit_diagnosis", "invalid_input")] * 5

    def test_accepted_submissions_also_count_as_attempts(self):
        session, _ = make_session()
        outcomes = [
            session.call("submit_diagnosis", valid_submission(cause_category="unknown", summary=f"提出 {n}")).outcome
            for n in range(1, 8)
        ]

        assert outcomes == ["ok"] * 5 + ["call_limit"] * 2
        assert len(session.trace) == 5

    def test_resubmission_within_the_limit_replaces_the_accepted_one(self):
        """受理された後の再提出は、上限の中なら今までどおり上書きする（最後に受理したものが正本）。"""
        session, _ = make_session()
        for n in range(1, 6):
            session.call("submit_diagnosis", valid_submission(cause_category="unknown", summary=f"提出 {n}"))

        assert session.finalize().summary == "提出 5"

    def test_submission_over_the_limit_does_not_replace_the_accepted_one(self):
        session, _ = make_session()
        for n in range(1, 6):
            session.call("submit_diagnosis", valid_submission(cause_category="unknown", summary=f"提出 {n}"))

        result = session.call(
            "submit_diagnosis", valid_submission(cause_category="insufficient_records", summary="提出 6")
        )
        diagnosis = session.finalize()

        assert result.outcome == "call_limit"
        assert session.has_submission is True
        assert diagnosis.summary == "提出 5"
        assert diagnosis.cause_category == "unknown"

    def test_fifth_attempt_is_still_accepted_after_four_rejections(self):
        session, _ = make_session()
        for _ in range(4):
            session.call("submit_diagnosis", valid_submission(confidence="certain"))

        result = session.call("submit_diagnosis", valid_submission(cause_category="unknown"))

        assert result.outcome == "ok"
        assert session.has_submission is True

    def test_submission_limit_is_independent_of_read_tool_call_limit(self):
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=2))
        for _ in range(6):
            session.call("submit_diagnosis", valid_submission(confidence="certain"))

        assert session.call("list_steps", {}).outcome == "ok"
        assert session.call("list_steps", {}).outcome == "ok"
        assert session.call("list_steps", {}).outcome == "call_limit"

    def test_read_tools_refused_by_call_limit_are_still_traced(self):
        """提出と違い、読み取りツールの上限超えは、今までどおり足取りに残る。"""
        from diagnosis.models import DiagnosisLimits

        session, _ = make_session(limits=DiagnosisLimits(max_tool_calls=1))
        session.call("list_steps", {})
        session.call("list_steps", {})

        assert [(c.tool, c.outcome) for c in session.trace] == [("list_steps", "ok"), ("list_steps", "call_limit")]


class TestOversizedPromptRecord:
    """L4: 1MB を超えるプロンプト記録は、unavailable ではなく size_limit。"""

    def _session(self, body: bytes):
        scenario = scenarios.output_validation_failed()
        key = f"prompts/{scenario.execution_id}/researcher_huge.json"
        return make_session(scenario, raw_objects={(fakes.PROMPTS_BUCKET, key): body})

    def test_prompt_record_over_one_megabyte_returns_size_limit(self):
        body = json.dumps({"prompt": "p", "output": "x" * 1_100_000}).encode("utf-8")
        session, _ = self._session(body)

        result = session.call("get_prompt_record", {"subagent": "researcher", "index": "huge"})

        assert result.outcome == "size_limit"
        assert result.is_error is True
        assert parse(result) == {"outcome": "size_limit"}
        assert session.issued_evidence_ids == set()
        assert [(c.outcome, c.evidence_count) for c in session.trace] == [("size_limit", 0)]

    def test_prompt_record_that_is_not_json_is_still_unavailable(self):
        session, _ = self._session(b"not json {")

        assert session.call("get_prompt_record", {"subagent": "researcher", "index": "huge"}).outcome == "unavailable"

    def test_prompt_record_exactly_one_megabyte_is_read(self):
        skeleton = json.dumps({"prompt": "", "output": "o"})
        body = json.dumps({"prompt": "p" * (1_000_000 - len(skeleton)), "output": "o"}).encode("utf-8")
        assert len(body) == 1_000_000
        session, _ = self._session(body)

        assert session.call("get_prompt_record", {"subagent": "researcher", "index": "huge"}).outcome == "ok"


class TestEventsTotalSizeCap:
    """L6: イベントの読み込みは、500 件に加えて、合計 2MB で読み止める。読み止めたことは truncated で分かる。"""

    def _scenario(self, count: int, chars: int):
        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(scenario.execution_id, n, "api_call_completed", {"n": n, "body": "b" * chars})
            for n in range(1, count + 1)
        ]
        return scenario

    def _session(self, count: int, chars: int, **kwargs):
        """1 ページを 5 件にする（1 回の結果の上限で末尾が落ちて truncated が付くのと、区別するため）。"""
        from diagnosis.models import DiagnosisLimits

        return make_session(self._scenario(count, chars), limits=DiagnosisLimits(events_page_size=5), **kwargs)

    def test_events_over_two_megabytes_in_total_are_cut_and_marked_truncated(self):
        session, _ = self._session(100, 40_000, page_size=10)  # 合計およそ 4MB

        data = parse(session.call("list_events", {}))

        assert data["outcome"] == "ok"
        assert 0 < data["total"] < 100
        assert data["truncated"] is True
        assert [e["evidence_id"] for e in data["events"]] == [f"EV-{n}" for n in range(1, 6)]  # 先頭から読む

    def test_truncation_by_total_size_is_shown_on_every_page(self):
        session, _ = self._session(100, 40_000, page_size=10)
        first = parse(session.call("list_events", {}))

        second = parse(session.call("list_events", {"offset": first["next_offset"]}))
        filtered = parse(session.call("list_events", {"event_type": "api_call_completed", "offset": 5}))

        assert second["truncated"] is True
        assert filtered["truncated"] is True

    def test_reading_stops_instead_of_fetching_every_page(self):
        session, bundle = self._session(100, 40_000, page_size=10)

        total = parse(session.call("list_events", {}))["total"]

        queries = len(bundle.dynamodb.ops("query", fakes.EVENTS_TABLE))
        assert queries < 10  # 100 件 ÷ 10 件のページを、最後までは読まない
        assert queries <= total // 10 + 1

    def test_events_under_two_megabytes_in_total_are_all_read_and_not_marked(self):
        session, _ = self._session(40, 40_000)  # 合計およそ 1.6MB

        data = parse(session.call("list_events", {}))

        assert data["total"] == 40
        assert len(data["events"]) == 5
        assert data["truncated"] is False

    def test_cut_events_cannot_be_used_as_evidence(self):
        session, _ = self._session(100, 40_000, page_size=10)
        collected = read_all_events(session)
        session.call("submit_diagnosis", valid_submission(evidence=[{"id": "EV-100", "note": "読んでいない"}]))

        diagnosis = session.finalize()

        assert len(collected) < 100
        assert "EV-100" not in session.issued_evidence_ids
        assert diagnosis.evidence == []
        assert diagnosis.invalid_evidence_count == 1


class TestFixerEligibilityLinkage:
    """M6: 「修正役に渡せる」には、例外・場所・根拠が、同じイベントでつながっていることが要る。"""

    def _scenario(self):
        """EV-1: 例外の型と、フレームの無いトレース。EV-2: 例外の型とトレース。EV-3: トレースだけ。"""
        scenario = scenarios.rate_limited()
        eid = scenario.execution_id
        scenario.events = [
            scenarios.make_event(
                eid,
                1,
                "subagent_failed",
                # A3: 例外の型は、トレースの例外の行と一致するときだけ、受け渡しに使える
                {"error_type": "FirstError", "stack_trace": scenarios.make_stack_trace([], "FirstError: x")},
            ),
            scenarios.make_event(
                eid,
                2,
                "error",
                {
                    "error_type": "SecondError",
                    "stack_trace": scenarios.make_stack_trace(self.FRAMES[:1], "SecondError: x"),
                },
            ),
            scenarios.make_event(
                eid, 3, "error", {"stack_trace": scenarios.make_stack_trace(self.FRAMES[1:], "ThirdError: x")}
            ),
        ]
        return scenario

    FRAMES = [("/app/orchestrator.py", 10, "first_function"), ("/app/orchestrator.py", 30, "second_function")]

    def _handoff(self, **overrides):
        from diagnosis.handoff import build_handoff

        scenario = self._scenario()
        session, _ = make_frame_session(scenario, frames=self.FRAMES, classes=("FirstError", "SecondError"))
        events = parse(session.call("list_events", {}))["events"]
        assert [frame_ids(e) for e in events] == [[], ["FR-1"], ["FR-2"]]
        submission = valid_submission(
            failed_stage="generating",
            cause_category="output_validation_failed",
            cause_nature="code_defect",
            confidence="high",
            action_type="fix_code",
            evidence=[{"id": "EV-2", "note": "x"}],
            exception_evidence_id="EV-2",
            code_location_ids=["FR-1"],
        )
        submission.update(overrides)
        assert session.call("submit_diagnosis", submission).outcome == "ok"
        return build_handoff(scenario.execution_id, session.finalize())

    def test_linked_exception_location_and_evidence_is_eligible(self):
        handoff = self._handoff()

        assert handoff.fixer_eligible is True
        assert handoff.exception_type == "SecondError"
        assert len(handoff.code_locations) == 1

    @pytest.mark.parametrize(
        "action_type", ["retry_later", "retry_now", "review_config", "no_action", "human_investigation"]
    )
    def test_action_other_than_fix_code_is_not_eligible(self, action_type):
        assert self._handoff(action_type=action_type).fixer_eligible is False

    def test_missing_exception_evidence_id_is_not_eligible(self):
        submission_without_exception = {"evidence": [{"id": "EV-2", "note": "x"}]}
        scenario = self._scenario()
        session, _ = make_frame_session(scenario, frames=self.FRAMES, classes=("FirstError", "SecondError"))
        session.call("list_events", {})
        submission = valid_submission(
            cause_category="output_validation_failed",
            cause_nature="code_defect",
            confidence="high",
            action_type="fix_code",
            code_location_ids=["FR-1"],
            **submission_without_exception,
        )
        session.call("submit_diagnosis", submission)
        from diagnosis.handoff import build_handoff

        assert build_handoff(scenario.execution_id, session.finalize()).fixer_eligible is False

    def test_exception_from_event_without_error_type_is_not_eligible(self):
        handoff = self._handoff(
            evidence=[{"id": "EV-3", "note": "x"}], exception_evidence_id="EV-3", code_location_ids=["FR-2"]
        )

        assert handoff.exception_type is None
        assert handoff.fixer_eligible is False

    def test_exception_and_location_from_different_events_is_not_eligible(self):
        handoff = self._handoff(
            evidence=[{"id": "EV-1", "note": "x"}, {"id": "EV-2", "note": "x"}],
            exception_evidence_id="EV-1",
            code_location_ids=["FR-1"],
        )

        assert handoff.exception_type == "FirstError"
        assert handoff.fixer_eligible is False

    def test_location_only_from_another_event_is_not_eligible(self):
        handoff = self._handoff(code_location_ids=["FR-2"])

        assert handoff.fixer_eligible is False

    def test_location_of_another_event_is_removed_and_the_rest_is_eligible(self):
        """A2: 例外のイベント（EV-2）に由来しない場所（FR-2 は EV-3 由来）は、モデルが選んでも取り除く。"""
        handoff = self._handoff(code_location_ids=["FR-2", "FR-1"])

        assert [(c.function, c.line) for c in handoff.code_locations] == [("first_function", 10)]
        assert handoff.fixer_eligible is True

    def test_evidence_that_does_not_include_the_exception_event_is_not_eligible(self):
        handoff = self._handoff(evidence=[{"id": "EV-1", "note": "x"}, {"id": "EV-3", "note": "x"}])

        assert handoff.valid_evidence_count == 2
        assert handoff.fixer_eligible is False

    @pytest.mark.parametrize("cause_category", ["unknown", "insufficient_records"])
    def test_unknown_or_insufficient_records_is_never_eligible(self, cause_category):
        handoff = self._handoff(cause_category=cause_category)

        assert handoff.cause_category == cause_category
        assert handoff.fixer_eligible is False

    @pytest.mark.parametrize("overrides", [{"confidence": "medium"}, {"cause_nature": "input_or_prompt"}])
    def test_earlier_conditions_still_apply(self, overrides):
        assert self._handoff(**overrides).fixer_eligible is False

    def test_handoff_does_not_expose_linkage_ids(self):
        data = self._handoff().to_dict()

        assert "EV-2" not in json.dumps(data)
        assert all(set(location) == {"path", "function", "line"} for location in data["code_locations"])


class TestMoreMasking:
    """L3: 足したマスクの対象が、ツールの結果（値と、辞書のキー）で効いている。"""

    def test_list_events_masks_added_secret_shapes_in_values_and_keys(self):
        from .factories import extra_secrets

        secrets = extra_secrets()
        token = fake_tokens()["github"]
        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(
                scenario.execution_id,
                n,
                "error",
                {"error_message": f"failed: {secret}", f"header {token}": "v", secret[:60]: "w"},
            )
            for n, (secret, _) in enumerate(secrets.values(), start=1)
        ]
        session, _ = make_session(scenario)

        result = session.call("list_events", {})
        text = json.dumps(parse(result), ensure_ascii=False)

        assert result.outcome == "ok"
        assert token not in text
        for _, must_not_remain in secrets.values():
            for part in must_not_remain:
                assert part not in text

    def test_accepted_free_text_masks_added_secret_shapes(self):
        from .factories import extra_secrets

        secrets = extra_secrets()
        session, _ = make_session()
        session.call(
            "submit_diagnosis",
            valid_submission(
                cause_category="unknown",
                summary=f"JWT {secrets['jwt'][0]}",
                action_note=f"Webhook {secrets['slack_webhook'][0]}",
            ),
        )

        diagnosis = session.finalize()

        assert secrets["jwt"][0] not in diagnosis.summary
        assert "[MASKED]" in diagnosis.summary
        for part in secrets["slack_webhook"][1]:
            assert part not in diagnosis.action_note


# ===========================================================================
# クロスレビュー後の修正仕様（C3・C4）
# ===========================================================================

HUGE_LIST = list(range(100_000))  # JSON にすると 60 万バイトを超える


def deep_value(depth: int = 150) -> dict:
    value: dict = {"leaf": "v" * 500}
    for _ in range(depth):
        value = {"nested": value, "pad": "p" * 200}
    return value


def hostile_values() -> dict:
    """外側の項目に入ってきうる、想定外の形の値。"""
    return {
        "huge_list": HUGE_LIST,
        "deep_nesting": deep_value(),
        "wide_dict": {f"key_{n}": "v" * 100 for n in range(5_000)},
        "list_of_strings": ["s" * 700] * 500,
        "long_string": "x" * 300_000,
    }


class TestStepsLimitsInTool:
    """C3: 段階の読み込みは 200 件・合計 2MB まで。読み止めたことは truncated で分かる。"""

    def _session(self, count: int, **step_overrides):
        from diagnosis.models import DiagnosisLimits

        scenario = scenarios.research_all_failed()
        scenario.steps = []
        for order in range(1, count + 1):
            step = scenarios.make_step(scenario.execution_id, order, status="failed", result={"body": "r" * 1_000})
            step["step_id"] = f"research-{order:04d}"
            step.update(step_overrides)
            scenario.steps.append(step)
        # 1 回の結果の上限で末尾が落ちて truncated が付くのと区別するため、1 回の上限を大きくしておく
        return make_session(scenario, limits=DiagnosisLimits(max_result_bytes=1_000_000, max_total_bytes=5_000_000))

    def test_more_than_200_steps_are_cut_and_marked_truncated(self):
        session, _ = self._session(250)

        data = parse(session.call("list_steps", {}))

        assert data["outcome"] == "ok"
        assert data["truncated"] is True
        assert data["total"] == 200
        assert [s["evidence_id"] for s in data["steps"]] == [f"ST-{n}" for n in range(1, 201)]
        assert session.issued_evidence_ids == {f"ST-{n}" for n in range(1, 201)}

    def test_200_steps_or_fewer_are_not_marked_truncated(self):
        session, _ = self._session(200)

        data = parse(session.call("list_steps", {}))

        assert data["total"] == 200
        assert data["truncated"] is False

    def test_steps_over_two_megabytes_in_total_are_cut_and_marked_truncated(self):
        session, _ = self._session(30, step_name="n" * 100_000)  # 合計 3MB

        data = parse(session.call("list_steps", {}))

        assert data["outcome"] == "ok"
        assert 0 < data["total"] < 30
        assert data["truncated"] is True

    def test_result_presence_is_still_reported_without_reading_the_body(self):
        """段階の result は読まない（C3）が、有無（has_result）は今までどおり示す。"""
        from diagnosis.models import DiagnosisLimits

        scenario = scenarios.output_validation_failed()
        scenario.steps.append(scenarios.make_step(scenario.execution_id, 3, status="failed"))
        session, bundle = make_session(scenario, limits=DiagnosisLimits())

        result = session.call("list_steps", {})

        assert [s["has_result"] for s in parse(result)["steps"]] == [True, True, False]
        assert "STEP_RESULT_MARKER" not in result.to_text()
        for op in bundle.dynamodb.ops("query", fakes.STEPS_TABLE):
            assert "result" not in (fakes.projected_attributes(op.kwargs) or ["result"])


class TestEventOuterFieldTypes:
    """C4: イベントの外側の項目（payload 以外）は、文字列か数だけを通す。それ以外は ``<omitted>``。"""

    def _event(self, **outer):
        scenario = scenarios.rate_limited()
        event = scenarios.make_event(scenario.execution_id, 1, "error", {"success": True})
        event.update(outer)
        scenario.events = [event]
        return scenario

    def test_huge_list_in_timestamp_is_replaced_and_result_stays_within_the_limit(self):
        """窓口の再現: timestamp に 10 万要素の配列（ok で 689,131 バイトが返っていた）。"""
        session, _ = make_session(self._event(timestamp=HUGE_LIST))

        result = session.call("list_events", {})
        event = parse(result)["events"][0]

        assert result.outcome == "ok"
        assert event["timestamp"] == "<omitted>"
        assert result_body_bytes(result) <= 20_000
        assert event["payload"] == {"success": True}  # payload は落とさずに済む
        assert "payload_omitted" not in event
        assert event["evidence_id"] == "EV-1"

    def test_huge_lists_in_two_fields_stay_within_the_limit(self):
        """窓口の再現: 2 項目に入れると 1,378,017 バイト（累計の上限は 200,000）。"""
        session, _ = make_session(self._event(timestamp=HUGE_LIST, status_at_emit=HUGE_LIST))

        result = session.call("list_events", {})
        event = parse(result)["events"][0]

        assert result.outcome == "ok"
        assert (event["timestamp"], event["status_at_emit"]) == ("<omitted>", "<omitted>")
        assert result_body_bytes(result) <= 20_000

    @pytest.mark.parametrize("field", ["timestamp", "status_at_emit", "event_type"])
    @pytest.mark.parametrize(
        "value",
        [[1, 2, 3], ["a"], [], {"nested": "x"}, {}, {"a", "b"}, [[["deep"]]], b"raw bytes"],
        ids=["list", "list_of_str", "empty_list", "dict", "empty_dict", "set", "nested_list", "bytes"],
    )
    def test_value_that_is_not_a_string_or_number_is_replaced(self, field, value):
        session, _ = make_session(self._event(**{field: value}))

        result = session.call("list_events", {})

        assert result.outcome == "ok"
        assert parse(result)["events"][0][field] in ("<omitted>", "<binary>")
        if not isinstance(value, bytes):
            assert parse(result)["events"][0][field] == "<omitted>"

    @pytest.mark.parametrize(
        ("field", "value"),
        [("timestamp", "2026-10-01T09:00:01.000Z"), ("status_at_emit", "failed"), ("timestamp", 1759300000),
         ("status_at_emit", 1.5), ("event_type", "error")],
    )
    def test_string_and_number_values_pass_as_they_are(self, field, value):
        session, _ = make_session(self._event(**{field: value}))

        assert parse(session.call("list_events", {}))["events"][0][field] == value

    def test_long_string_in_outer_field_is_still_truncated(self):
        session, _ = make_session(self._event(timestamp="t" * 5_000))

        timestamp = parse(session.call("list_events", {}))["events"][0]["timestamp"]

        assert "字省略" in timestamp
        assert len(timestamp) < 1_000

    def test_secret_in_outer_field_is_still_masked(self):
        token = fake_tokens()["github"]
        session, _ = make_session(self._event(status_at_emit=f"failed {token}"))

        assert token not in session.call("list_events", {}).to_text()

    def test_event_with_replaced_event_type_is_not_matched_by_a_filter(self):
        scenario = self._event(event_type=["error"])
        scenario.events.append(scenarios.make_event(scenario.execution_id, 2, "error", {"success": False}))
        session, _ = make_session(scenario)

        filtered = parse(session.call("list_events", {"event_type": "error"}))
        everything = parse(session.call("list_events", {}))

        assert [e["evidence_id"] for e in filtered["events"]] == ["EV-2"]
        assert [e["event_type"] for e in everything["events"]] == ["<omitted>", "error"]


class TestMinimalEventForm:
    """C4: payload を落としても 1 回の上限に収まらなければ、evidence_id と event_type だけの最小の形にする。"""

    FRAMES = [("/app/orchestrator.py", 100 + n, f"function_with_a_rather_long_name_{n:02d}") for n in range(15)]

    def _session(self, *, events: int = 1, max_result_bytes: int = 1_500):
        from diagnosis.models import DiagnosisLimits

        scenario = scenarios.rate_limited()
        payload = {
            **oversized_payload(),
            "error_type": "ValueError",
            "stack_trace": scenarios.make_stack_trace(self.FRAMES, "ValueError: x"),
        }
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", payload)]
        scenario.events += [
            scenarios.make_event(scenario.execution_id, n, "api_call_completed", {"success": True})
            for n in range(2, events + 1)
        ]
        limits = DiagnosisLimits(max_result_bytes=max_result_bytes)
        return make_frame_session(scenario, frames=self.FRAMES, limits=limits)

    def test_event_that_does_not_fit_without_payload_is_reduced_to_id_and_type(self):
        session, _ = self._session()

        result = session.call("list_events", {})
        data = parse(result)

        assert result.outcome == "ok"
        assert data["events"] == [{"evidence_id": "EV-1", "event_type": "error"}]
        assert data["truncated"] is True
        assert result_body_bytes(result) <= 1_500

    def test_minimal_event_still_counts_as_returned_evidence(self):
        session, _ = self._session()

        session.call("list_events", {})
        session.call("submit_diagnosis", valid_submission(evidence=[{"id": "EV-1", "note": "x"}]))

        assert session.issued_evidence_ids == {"EV-1"}
        assert session.trace[0].evidence_count == 1
        assert [ref.id for ref in session.finalize().evidence] == ["EV-1"]

    def test_minimal_event_advances_next_offset(self):
        session, _ = self._session(events=3)

        first = parse(session.call("list_events", {}))
        second = parse(session.call("list_events", {"offset": first["next_offset"]}))

        assert first["events"] == [{"evidence_id": "EV-1", "event_type": "error"}]
        assert (first["has_more"], first["next_offset"]) == (True, 1)
        assert [e["evidence_id"] for e in second["events"]] == ["EV-2", "EV-3"]

    def test_event_that_fits_after_dropping_payload_keeps_its_other_fields(self):
        """1 回の上限が既定（20,000 バイト）なら、payload を落とすだけで収まる。最小の形にはしない。"""
        session, _ = self._session(max_result_bytes=20_000)

        event = parse(session.call("list_events", {}))["events"][0]

        assert event["payload_omitted"] is True
        assert len(event["frames"]) == 15
        assert event["timestamp"]


class TestResultSizeInvariant:
    """C4: どんな記録が来ても、1 回の結果は 1 回の上限以内。例外は出ない。"""

    LIMIT = 20_000

    @pytest.mark.parametrize("kind", sorted(hostile_values()))
    @pytest.mark.parametrize("field", ["timestamp", "status_at_emit", "event_type", "payload"])
    def test_list_events_stays_within_the_result_limit(self, kind, field):
        value = hostile_values()[kind]
        scenario = scenarios.rate_limited()
        events = []
        for n in range(1, 3):  # 2 件（合計が、イベントの読み込みの上限 2MB に届かない大きさにしている）
            event = scenarios.make_event(scenario.execution_id, n, "error", {"success": True})
            event[field] = {"wrapped": value} if field == "payload" else value
            events.append(event)
        scenario.events = events
        session, _ = make_session(scenario)

        offset, seen = 0, []
        for _ in range(5):
            result = session.call("list_events", {"offset": offset})
            data = parse(result)
            assert result.outcome == "ok"
            assert result_body_bytes(result) <= self.LIMIT
            seen.extend(e["evidence_id"] for e in data["events"])
            if not data["has_more"]:
                break
            offset = data["next_offset"]

        assert seen == ["EV-1", "EV-2"]  # 大きさを抑えても、全件に届く

    @pytest.mark.parametrize("kind", sorted(hostile_values()))
    @pytest.mark.parametrize("field", ["phase", "step_name", "step_order", "status", "started_at", "completed_at"])
    def test_list_steps_stays_within_the_result_limit(self, kind, field):
        scenario = scenarios.research_all_failed()
        for step in scenario.steps:
            step[field] = hostile_values()[kind]
        session, _ = make_session(scenario)

        result = session.call("list_steps", {})

        assert result.outcome in ("ok", "size_limit")
        assert result_body_bytes(result) <= self.LIMIT

    @pytest.mark.parametrize("kind", sorted(hostile_values()))
    @pytest.mark.parametrize("field", ["status", "created_at", "topic", "category", "total_tokens_used"])
    def test_overview_stays_within_the_result_limit(self, kind, field):
        scenario = scenarios.rate_limited()
        scenario.execution[field] = hostile_values()[kind]
        session, _ = make_session(scenario)

        result = session.call("get_execution_overview", {})

        assert result.outcome in ("ok", "size_limit")
        assert result_body_bytes(result) <= self.LIMIT

    @pytest.mark.parametrize("kind", sorted(hostile_values()))
    @pytest.mark.parametrize("field", ["prompt", "output", "recorded_at", "output_files"])
    def test_prompt_record_stays_within_the_result_limit(self, kind, field):
        value = hostile_values()[kind]
        scenario = scenarios.output_validation_failed()
        record = scenario.prompts[("generator_text", "0")]
        record[field] = {f"name_{n}": value for n in range(30)} if field == "output_files" else value
        if kind in ("long_string", "huge_list") and field == "output_files":
            record[field] = {f"name_{n}": "c" for n in range(5_000)}
        session, _ = make_session(scenario)

        result = session.call("get_prompt_record", dict(VALID_PROMPT_ARGS))

        assert result.outcome in ("ok", "size_limit")
        assert result_body_bytes(result) <= self.LIMIT

    def test_cumulative_size_stays_bounded_with_hostile_events(self):
        """累計の上限（200,000）に達したら、それ以上は読ませない。

        判定は呼び出しの前に行うので、最後の 1 回ぶん（1 回の上限）までは超えうる。
        """
        scenario = scenarios.rate_limited()
        scenario.events = []
        for n in range(1, 41):
            event = scenarios.make_event(scenario.execution_id, n, "error", {"body": "b" * 700, "n": n})
            event["timestamp"] = HUGE_LIST
            event["status_at_emit"] = {"wide": ["w" * 500] * 500}
            scenario.events.append(event)
        session, _ = make_session(scenario)

        total = 0
        outcomes = []
        for _ in range(60):
            for args in ({}, {"offset": 10}, {"offset": 20}, {"offset": 30}):
                result = session.call("list_events", args)
                size = result_body_bytes(result)
                assert size <= self.LIMIT
                total += size
                outcomes.append(result.outcome)

        assert total <= 200_000 + self.LIMIT
        assert set(outcomes) <= {"ok", "size_limit", "call_limit"}


# ===========================================================================
# 再レビュー後の修正仕様（A2・A3・A4）
# ===========================================================================

WORKER = "/app/worker.py"
# EV-1 のトレース（外側から内側へ）。main の 348 行目は、再帰で 2 回現れる（FR-1 と FR-2 は、同じ場所）
EV1_FRAMES = [(WORKER, 348, "main"), (WORKER, 348, "main"), (WORKER, 400, "run"), (WORKER, 520, "load")]
EV2_FRAMES = [(WORKER, 700, "other"), (WORKER, 760, "another")]


def selection_session(*, ev1_type="KeyError", ev1_line="KeyError: 'x'"):
    """EV-1: 例外（KeyError）と 4 フレーム（FR-1〜FR-4）。EV-2: 別の例外（ValueError）と 2 フレーム（FR-5・FR-6）。"""
    scenario = scenarios.rate_limited()
    eid = scenario.execution_id
    scenario.events = [
        scenarios.make_event(
            eid, 1, "error", {"error_type": ev1_type, "stack_trace": scenarios.make_stack_trace(EV1_FRAMES, ev1_line)}
        ),
        scenarios.make_event(
            eid,
            2,
            "error",
            {"error_type": "ValueError", "stack_trace": scenarios.make_stack_trace(EV2_FRAMES, "ValueError: y")},
        ),
    ]
    session, _ = make_frame_session(scenario, frames=[*EV1_FRAMES, *EV2_FRAMES])
    events = parse(session.call("list_events", {}))["events"]
    assert [frame_ids(e) for e in events] == [["FR-1", "FR-2", "FR-3", "FR-4"], ["FR-5", "FR-6"]]
    return session, scenario


def submit_selection(session, code_location_ids, *, exception_evidence_id="EV-1", evidence=("EV-1", "EV-2")):
    submission = valid_submission(
        failed_stage="generating",
        cause_category="output_validation_failed",
        cause_nature="code_defect",
        confidence="high",
        action_type="fix_code",
        evidence=[{"id": evidence_id, "note": "x"} for evidence_id in evidence],
        code_location_ids=code_location_ids,
    )
    if exception_evidence_id is not None:
        submission["exception_evidence_id"] = exception_evidence_id
    result = session.call("submit_diagnosis", submission)
    assert result.outcome == "ok"
    return parse(result), session.finalize()


def places(diagnosis) -> list[tuple[str, int]]:
    return [(location.function, location.line) for location in diagnosis.code_locations]


class TestCodeLocationSelection:
    """A2: 受け渡しの場所は、例外の型を取ったイベントに由来するものだけ。順序と重複は、コードが決める。"""

    def test_reviewer_reproduction_other_event_first_and_duplicate_location(self):
        """窓口の再現: 例外は EV-1 なのに、EV-2 由来の場所を先頭に置き、同じ場所を 2 回含める。"""
        from diagnosis.handoff import build_handoff

        session, scenario = selection_session()

        reply, diagnosis = submit_selection(session, ["FR-5", "FR-2", "FR-1", "FR-3"])
        handoff = build_handoff(scenario.execution_id, diagnosis)

        assert places(diagnosis) == [("main", 348), ("run", 400)]
        assert diagnosis.code_location_evidence_ids == ["EV-1", "EV-1"]
        assert diagnosis.exception_evidence_id == "EV-1"
        assert [(c.path, c.function, c.line) for c in handoff.code_locations] == [
            (WORKER, "main", 348),
            (WORKER, "run", 400),
        ]
        assert handoff.fixer_eligible is True
        assert reply["dropped_code_location_ids"] == ["FR-5"]  # 取り除いた ID は、モデルに知らせる

    def test_locations_are_ordered_as_in_the_stack_trace_not_as_the_model_listed_them(self):
        session, _ = selection_session()

        _, diagnosis = submit_selection(session, ["FR-4", "FR-3", "FR-1"])

        assert places(diagnosis) == [("main", 348), ("run", 400), ("load", 520)]
        assert diagnosis.code_location_evidence_ids == ["EV-1", "EV-1", "EV-1"]

    def test_same_location_selected_through_different_frame_ids_appears_once(self):
        session, _ = selection_session()

        _, diagnosis = submit_selection(session, ["FR-2", "FR-1"])

        assert places(diagnosis) == [("main", 348)]
        assert diagnosis.code_location_evidence_ids == ["EV-1"]

    def test_locations_of_other_events_only_leave_nothing_and_are_not_eligible(self):
        from diagnosis.handoff import build_handoff

        session, scenario = selection_session()

        reply, diagnosis = submit_selection(session, ["FR-5", "FR-6"])
        handoff = build_handoff(scenario.execution_id, diagnosis)

        assert diagnosis.code_locations == []
        assert diagnosis.code_location_evidence_ids == []
        assert diagnosis.exception_type == "KeyError"
        assert handoff.fixer_eligible is False
        assert reply["dropped_code_location_ids"] == ["FR-5", "FR-6"]

    def test_exception_of_the_other_event_keeps_only_its_own_locations(self):
        session, _ = selection_session()

        reply, diagnosis = submit_selection(session, ["FR-1", "FR-6", "FR-5"], exception_evidence_id="EV-2")

        assert diagnosis.exception_type == "ValueError"
        assert places(diagnosis) == [("other", 700), ("another", 760)]
        assert diagnosis.code_location_evidence_ids == ["EV-2", "EV-2"]
        assert reply["dropped_code_location_ids"] == ["FR-1"]

    def test_without_exception_evidence_id_there_are_no_locations(self):
        session, _ = selection_session()

        _, diagnosis = submit_selection(session, ["FR-1", "FR-3"], exception_evidence_id=None)

        assert diagnosis.exception_type is None
        assert diagnosis.exception_evidence_id is None
        assert diagnosis.code_locations == []
        assert diagnosis.code_location_evidence_ids == []

    def test_exception_event_that_was_not_returned_leaves_no_locations(self):
        session, _ = selection_session()

        _, diagnosis = submit_selection(session, ["FR-1", "FR-3"], exception_evidence_id="EV-9")

        assert diagnosis.exception_evidence_id is None
        assert diagnosis.code_locations == []

    def test_unissued_frame_ids_are_still_reported_and_removed(self):
        session, _ = selection_session()

        reply, diagnosis = submit_selection(session, ["FR-3", "FR-900"])

        assert reply["dropped_code_location_ids"] == ["FR-900"]
        assert places(diagnosis) == [("run", 400)]

    def test_all_selected_locations_of_the_exception_event_are_reported_with_nothing_dropped(self):
        session, _ = selection_session()

        reply, diagnosis = submit_selection(session, ["FR-1", "FR-3", "FR-4"])

        assert reply["dropped_code_location_ids"] == []
        assert len(diagnosis.code_locations) == 3

    def test_handoff_never_lists_a_location_twice(self):
        from diagnosis.handoff import build_handoff

        session, scenario = selection_session()
        _, diagnosis = submit_selection(session, ["FR-1", "FR-2", "FR-1", "FR-2", "FR-3"])

        locations = build_handoff(scenario.execution_id, diagnosis).to_dict()["code_locations"]

        assert locations == [
            {"path": WORKER, "function": "main", "line": 348},
            {"path": WORKER, "function": "run", "line": 400},
        ]


class TestExceptionTypeMustMatchTheTrace:
    """A3: 例外の型は、トレースの最初のブロックを終える例外の行の型と一致するときだけ、受け渡しに使える。"""

    CHAINED = (
        "Traceback (most recent call last):\n"
        f'  File "{WORKER}", line 520, in load\n'
        "    return data[key]\n"
        "KeyError: 'x'\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        "\n"
        "Traceback (most recent call last):\n"
        f'  File "{WORKER}", line 400, in run\n'
        "    raise RuntimeError('load failed') from e\n"
        "RuntimeError: load failed\n"
    )

    def _session(self, error_type, stack_trace, **payload):
        scenario = scenarios.rate_limited()
        body = {"error_type": error_type, "stack_trace": stack_trace, **payload}
        if stack_trace is None:
            del body["stack_trace"]
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", body)]
        session, _ = make_frame_session(scenario, frames=EV1_FRAMES, classes=("WorkflowError",))
        event = parse(session.call("list_events", {}))["events"][0]
        return session, scenario, event

    def _handoff(self, error_type, stack_trace):
        from diagnosis.handoff import build_handoff

        session, scenario, event = self._session(error_type, stack_trace)
        _, diagnosis = submit_selection(session, frame_ids(event)[:5], evidence=("EV-1",))
        return event, diagnosis, build_handoff(scenario.execution_id, diagnosis)

    def test_reviewer_reproduction_outer_type_is_not_paired_with_inner_location(self):
        """窓口の再現: ``raise RuntimeError(...) from e``。最初のブロックは KeyError、記録の型は RuntimeError。"""
        event, diagnosis, handoff = self._handoff("RuntimeError", self.CHAINED)

        assert handoff.exception_type is None
        assert tuple(handoff.code_locations) == ()
        assert handoff.fixer_eligible is False
        assert diagnosis.exception_evidence_id is None
        # モデルに返す結果は、今までどおり見せる（診断の材料としては使える）
        assert event["payload"]["error_type"] == "RuntimeError"
        assert [(f["function"], f["line"]) for f in event["frames"]] == [("load", 520)]

    def test_type_that_matches_the_first_block_of_a_chain_is_not_usable(self):
        """5 巡目のレビューの修正仕様 ②で書き換えた（元は「最初のブロックの型と一致すれば使える」）。

        連鎖したトレースは、型が最初のブロックと一致しても、受け渡しに使わない。モデルに返す結果は変えない。
        """
        event, diagnosis, handoff = self._handoff("KeyError", self.CHAINED)

        assert handoff.exception_type is None
        assert tuple(handoff.code_locations) == ()
        assert handoff.fixer_eligible is False
        assert diagnosis.exception_evidence_id is None
        assert event["payload"]["error_type"] == "KeyError"
        assert [(f["function"], f["line"]) for f in event["frames"]] == [("load", 520)]

    def test_type_that_matches_the_only_block_is_usable(self):
        """連鎖の無いトレースなら、型が一致すれば、今までどおり使える。"""
        only_block = self.CHAINED.split("\n\n")[0] + "\n"

        _, diagnosis, handoff = self._handoff("KeyError", only_block)

        assert handoff.exception_type == "KeyError"
        assert [(c.function, c.line) for c in handoff.code_locations] == [("load", 520)]
        assert handoff.fixer_eligible is True
        assert diagnosis.exception_evidence_id == "EV-1"

    @pytest.mark.parametrize(
        ("error_type", "exception_line"),
        [
            ("KeyError", "KeyError: 'x'"),
            ("KeyError", "KeyError"),
            ("ClientError", "botocore.exceptions.ClientError: An error occurred (AccessDenied)"),
            ("WorkflowError", "error_types.WorkflowError: failed"),
            ("NonDictGeneratorResponse", "orchestrator.NonDictGeneratorResponse: missing_keys"),
        ],
    )
    def test_matching_exception_line_makes_the_type_usable(self, error_type, exception_line):
        _, diagnosis, handoff = self._handoff(error_type, scenarios.make_stack_trace(EV1_FRAMES[2:], exception_line))

        assert handoff.exception_type == error_type
        assert handoff.fixer_eligible is True
        assert len(handoff.code_locations) == 2

    @pytest.mark.parametrize(
        ("error_type", "exception_line"),
        [
            ("RuntimeError", "KeyError: 'x'"),
            ("KeyError", "keyerror: 'x'"),
            ("KeyError", "KeyErrors: 'x'"),
            ("KeyError", "MyKeyError: 'x'"),
            ("KeyError", "pkg.KeyError.Inner: 'x'"),
            ("ClientError", "botocore.exceptions.BotoCoreError: x"),
            ("KeyError", "some text mentioning KeyError: 'x'"),
        ],
    )
    def test_exception_line_of_another_type_makes_the_type_unusable(self, error_type, exception_line):
        stack_trace = scenarios.make_stack_trace(EV1_FRAMES[2:], exception_line)
        event, diagnosis, handoff = self._handoff(error_type, stack_trace)

        assert handoff.exception_type is None
        assert diagnosis.exception_evidence_id is None
        assert tuple(handoff.code_locations) == ()
        assert handoff.fixer_eligible is False
        assert event["payload"]["error_type"] == error_type
        assert len(event["frames"]) == 2

    def test_trace_cut_before_the_exception_line_makes_the_type_unusable(self):
        """5,000 字で切られた記録: フレームは読めても、例外の行まで届いていない。"""
        whole = scenarios.make_stack_trace(EV1_FRAMES[2:], "KeyError: 'x'")
        cut = whole[: whole.index("KeyError")]

        event, diagnosis, handoff = self._handoff("KeyError", cut)

        assert len(event["frames"]) == 2
        assert handoff.exception_type is None
        assert tuple(handoff.code_locations) == ()
        assert handoff.fixer_eligible is False

    @pytest.mark.parametrize("stack_trace", [None, "", "KeyError: 'x'", 5, ["Traceback (most recent call last):"]])
    def test_event_without_a_readable_trace_makes_the_type_unusable(self, stack_trace):
        session, scenario, event = self._session("KeyError", stack_trace)
        _, diagnosis = submit_selection(session, [], evidence=("EV-1",))

        assert event["payload"]["error_type"] == "KeyError"
        assert diagnosis.exception_type is None
        assert diagnosis.exception_evidence_id is None

    def test_matching_but_unlisted_type_is_still_unusable(self):
        stack_trace = scenarios.make_stack_trace(EV1_FRAMES[2:], "MadeUpError: x")
        _, diagnosis, handoff = self._handoff("MadeUpError", stack_trace)

        assert handoff.exception_type is None
        assert handoff.fixer_eligible is False

    def test_type_only_in_a_later_block_is_not_usable(self):
        _, _, handoff = self._handoff("RuntimeError", self.CHAINED.replace("KeyError: 'x'", "ValueError: 'x'"))

        assert handoff.exception_type is None


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestDeadline:
    """A4: 診断全体の期限を過ぎてから処理を始める呼び出しは、記録を読まずに deadline_exceeded を返す。

    呼び方（このテストが決めたもの）: ``ToolSession(reader, limits, clock=..., deadline=<clock の時刻>)``。
    ``deadline`` を渡さなければ、今までどおり（期限なし）。
    """

    def _session(self, *, deadline=100.0, scenario=None, **reader_kwargs):
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        clock = _Clock()
        bundle = fakes.build_reader(scenario or scenarios.output_validation_failed(), **reader_kwargs)
        kwargs = {} if deadline is None else {"deadline": deadline}
        return ToolSession(bundle.reader, DiagnosisLimits(), clock=clock, **kwargs), bundle, clock

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("get_execution_overview", {}),
            ("list_steps", {}),
            ("list_events", {}),
            ("get_prompt_record", dict(VALID_PROMPT_ARGS)),
        ],
    )
    def test_read_tool_called_after_the_deadline_does_not_read_records(self, tool, args):
        session, bundle, clock = self._session()
        clock.now = 100.5

        result = session.call(tool, args)

        assert result.outcome == "deadline_exceeded"
        assert result.is_error is True
        assert parse(result) == {"outcome": "deadline_exceeded"}
        assert bundle.all_operations() == []
        assert session.issued_evidence_ids == set()
        assert [(c.seq, c.tool, c.outcome, c.evidence_count) for c in session.trace] == [
            (1, tool, "deadline_exceeded", 0)
        ]

    def test_calls_before_the_deadline_are_served(self):
        session, _, clock = self._session()
        clock.now = 99.0

        assert session.call("get_execution_overview", {}).outcome == "ok"
        assert session.call("list_events", {}).outcome == "ok"

    def test_every_call_after_the_deadline_is_refused_and_traced(self):
        session, bundle, clock = self._session()
        assert session.call("get_execution_overview", {}).outcome == "ok"
        operations = len(bundle.all_operations())
        clock.now = 250.0

        outcomes = [session.call(tool, {}).outcome for tool in ("list_steps", "list_events", "list_steps")]

        assert outcomes == ["deadline_exceeded"] * 3
        assert len(bundle.all_operations()) == operations
        assert [c.outcome for c in session.trace] == ["ok"] + ["deadline_exceeded"] * 3

    def test_events_already_loaded_are_not_returned_after_the_deadline(self):
        session, _, clock = self._session()
        assert session.call("list_events", {}).outcome == "ok"
        issued = set(session.issued_evidence_ids)
        clock.now = 101.0

        result = session.call("list_events", {"offset": 3})

        assert result.outcome == "deadline_exceeded"
        assert "events" not in parse(result)
        assert session.issued_evidence_ids == issued

    def test_submission_after_the_deadline_is_not_accepted(self):
        """読み方: 期限を過ぎてから始まる呼び出しは、提出も含めて受け付けない。"""
        session, _, clock = self._session()
        clock.now = 101.0

        result = session.call("submit_diagnosis", valid_submission(cause_category="unknown"))

        assert result.outcome == "deadline_exceeded"
        assert session.has_submission is False

    def test_submission_before_the_deadline_stays_accepted_after_it(self):
        from diagnosis.models import Diagnosis

        session, _, clock = self._session()
        assert session.call("submit_diagnosis", valid_submission(cause_category="unknown")).outcome == "ok"
        clock.now = 500.0

        assert session.has_submission is True
        assert session.finalize() != Diagnosis.unknown()

    def test_invalid_input_after_the_deadline_is_reported_as_deadline_exceeded(self):
        session, bundle, clock = self._session()
        clock.now = 101.0

        result = session.call("get_prompt_record", {"subagent": "researcher", "index": "../x"})

        assert result.outcome == "deadline_exceeded"
        assert session.trace[0].args == {}
        assert bundle.all_operations() == []

    def test_without_deadline_calls_are_never_refused(self):
        session, _, clock = self._session(deadline=None)
        clock.now = 10**9

        assert session.call("get_execution_overview", {}).outcome == "ok"
        assert session.call("list_steps", {}).outcome == "ok"

    def test_call_that_was_waiting_for_the_lock_is_refused_when_it_gets_it_after_the_deadline(self):
        """窓口の指摘: ロックを待っていたスレッドが、時間切れの後に読み取りを始める。"""
        import threading

        session, bundle, clock = self._session()
        entered, release = threading.Event(), threading.Event()
        original = bundle.reader.get_execution

        def slow_get_execution():
            entered.set()
            assert release.wait(10)
            return original()

        bundle.reader.get_execution = slow_get_execution
        results: dict = {}
        first = threading.Thread(target=lambda: results.update(first=session.call("get_execution_overview", {})))
        second = threading.Thread(target=lambda: results.update(second=session.call("list_steps", {})))

        first.start()
        assert entered.wait(10)  # 1 つ目が、ロックを持ったまま記録を読んでいる
        second.start()  # 2 つ目は、ロックを待つ
        second.join(0.3)
        assert second.is_alive()
        clock.now = 150.0  # 待っている間に、期限を過ぎる
        release.set()
        first.join(10)
        second.join(10)

        assert results["first"].outcome == "ok"  # 期限の前に始めた処理は、最後まで行う
        assert results["second"].outcome == "deadline_exceeded"
        assert bundle.dynamodb.ops("query", fakes.STEPS_TABLE) == []
        assert [(c.tool, c.outcome) for c in session.trace] == [
            ("get_execution_overview", "ok"),
            ("list_steps", "deadline_exceeded"),
        ]

    def test_deadline_exceeded_is_a_known_outcome(self):
        from diagnosis.models import TOOL_OUTCOMES

        assert "deadline_exceeded" in TOOL_OUTCOMES
