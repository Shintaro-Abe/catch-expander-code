"""SDK への登録（design.md §7.1）。

対応: R13（ハンドラーから例外を漏らさない）、AC-16（自分で定義した 5 個）、AC-12（``is_error`` で返す）。
ハンドラーは SDK を起動せずに直接呼ぶ。
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from . import fakes, scenarios
from .factories import valid_submission
from .sdk_fake import NOT_TOOL_OUTPUT, find_handler, mcp_call_tool, mcp_list_tools, parse_handler_output

TOOLS = ("get_execution_overview", "list_steps", "list_events", "get_prompt_record", "submit_diagnosis")


def _session(scenario=None):
    from diagnosis.models import DiagnosisLimits
    from diagnosis.tools import ToolSession

    bundle = fakes.build_reader(scenario or scenarios.output_validation_failed())
    return ToolSession(bundle.reader, DiagnosisLimits()), bundle


class TestNames:
    def test_server_name_is_diag(self):
        from diagnosis.sdk_server import SERVER_NAME

        assert SERVER_NAME == "diag"

    def test_allowed_tool_names_are_exactly_the_five_custom_tools(self):
        from diagnosis.sdk_server import allowed_tool_names

        assert sorted(allowed_tool_names()) == sorted(f"mcp__diag__{tool}" for tool in TOOLS)


class TestHandlers:
    def test_build_handlers_returns_one_handler_per_tool(self):
        from diagnosis.sdk_server import build_handlers

        session, _ = _session()
        handlers = build_handlers(session)

        assert len(handlers) == 5
        for tool in TOOLS:
            assert asyncio.iscoroutinefunction(find_handler(handlers, tool))

    def test_handler_with_successful_call_returns_text_content_without_is_error(self):
        from diagnosis.sdk_server import build_handlers

        session, _ = _session()
        handler = find_handler(build_handlers(session), "get_execution_overview")

        out = asyncio.run(handler({}))

        assert set(out) <= {"content", "is_error"}
        assert not out.get("is_error")
        assert len(out["content"]) == 1
        assert out["content"][0]["type"] == "text"
        data = json.loads(out["content"][0]["text"])
        assert data["outcome"] == "ok"
        assert data["evidence_id"] == "EX-1"

    def test_handler_with_failed_call_sets_is_error(self):
        from diagnosis.sdk_server import build_handlers

        session, _ = _session()
        handler = find_handler(build_handlers(session), "get_prompt_record")

        reply = parse_handler_output(asyncio.run(handler({"subagent": "researcher", "index": "../x"})))

        assert reply.is_error is True
        assert reply.raw["is_error"] is True
        assert reply.outcome == "invalid_input"

    def test_handler_with_rejected_submission_sets_is_error_and_allows_resubmission(self):
        """AC-12: 形が不正なら ``is_error`` で返し、出し直しができる。"""
        from diagnosis.sdk_server import build_handlers

        session, _ = _session()
        handler = find_handler(build_handlers(session), "submit_diagnosis")

        rejected = parse_handler_output(asyncio.run(handler(valid_submission(confidence="certain"))))
        accepted = parse_handler_output(asyncio.run(handler(valid_submission(cause_category="unknown"))))

        assert rejected.is_error is True
        assert rejected.outcome == "invalid_input"
        assert accepted.is_error is False
        assert accepted.data["accepted"] is True
        assert session.has_submission is True

    def test_handler_records_call_in_session_trace(self):
        from diagnosis.sdk_server import build_handlers

        session, _ = _session()
        handlers = build_handlers(session)

        asyncio.run(find_handler(handlers, "list_steps")({}))
        asyncio.run(find_handler(handlers, "list_events")({"event_type": "error"}))

        assert [(c.tool, c.outcome) for c in session.trace] == [("list_steps", "ok"), ("list_events", "ok")]

    def test_handler_when_session_call_raises_does_not_leak_exception_or_its_message(self):
        """R13: SDK は未捕捉の例外の文面をそのままモデルに渡すので、ハンドラーで必ず受ける。"""
        from diagnosis.sdk_server import build_handlers

        session = MagicMock()
        session.call.side_effect = RuntimeError("HANDLER_LEAK_5512 internal detail")
        handlers = build_handlers(session)

        for tool in TOOLS:
            out = asyncio.run(find_handler(handlers, tool)({}))
            reply = parse_handler_output(out)

            assert reply.is_error is True
            assert reply.outcome == "internal_error"
            assert "HANDLER_LEAK_5512" not in json.dumps(out, ensure_ascii=False)

    def test_handlers_called_concurrently_record_every_call(self):
        """ハンドラーは ``asyncio.to_thread`` で動く。足取りと上限の数え方が壊れないこと。"""
        from diagnosis.sdk_server import build_handlers

        session, _ = _session()
        handler = find_handler(build_handlers(session), "list_steps")

        async def _burst() -> list:
            return await asyncio.gather(*(handler({}) for _ in range(10)))

        outs = asyncio.run(_burst())

        assert all(parse_handler_output(out).outcome == "ok" for out in outs)
        assert sorted(c.seq for c in session.trace) == list(range(1, 11))


class TestServer:
    def test_build_server_returns_sdk_server_config_named_diag(self):
        from diagnosis.sdk_server import build_handlers, build_server

        session, _ = _session()

        server = build_server(build_handlers(session))

        assert server["type"] == "sdk"
        assert server["name"] == "diag"
        assert server["instance"] is not None

    def test_build_server_does_not_read_any_record(self):
        from diagnosis.sdk_server import build_handlers, build_server

        session, bundle = _session()

        build_server(build_handlers(session))

        assert bundle.all_operations() == []


# ---------------------------------------------------------------------------
# レビュー後の修正仕様 M1: 検証を ToolSession に一本化する
# ---------------------------------------------------------------------------
#
# 背景: ``create_sdk_mcp_server`` が作る MCP サーバーは、ハンドラーの前に jsonschema で入力を検証する。
# SDK に厳しいスキーマを渡すと、スキーマ違反の呼び出しは「Input validation error」で返り、ハンドラーに届かない
# （足取りに残らず、呼び出し総数にも数えられない）。SDK に渡すスキーマは緩くし、検証は ToolSession だけで行う。
#
# 置き場所（このテストが決めたもの）: ``diagnosis.sdk_server.SDK_INPUT_SCHEMAS``（ツール名 → 緩いスキーマ）。
# 厳しいスキーマ ``diagnosis.tools.TOOL_SCHEMAS`` は、今までどおり残す。

CONSTRAINT_KEYWORDS = (
    "required",
    "additionalProperties",
    "enum",
    "pattern",
    "minimum",
    "maximum",
    "maxLength",
    "minLength",
    "maxItems",
    "minItems",
    "items",
    "const",
    "format",
    "anyOf",
    "oneOf",
    "allOf",
    "not",
)

INVALID_CALLS = [
    ("list_events", {"offset": -1}, ["offset"]),
    ("list_events", {"offset": "3"}, ["offset"]),
    ("list_events", {"limit": 500}, ["limit"]),
    ("list_events", {"execution_id": "exec-20250101000000-ffffffff"}, ["execution_id"]),
    ("list_steps", {"table": "other-table"}, ["table"]),
    ("get_execution_overview", {"key": "x"}, ["key"]),
    ("get_prompt_record", {"subagent": "researcher", "index": "../x"}, ["index"]),
    ("get_prompt_record", {"subagent": "orchestrator", "index": "0"}, ["subagent"]),
    ("get_prompt_record", {"subagent": "researcher"}, ["index"]),
    ("get_prompt_record", {}, ["subagent", "index"]),
    ("get_prompt_record", {"subagent": "researcher", "index": 0}, ["index"]),
    ("submit_diagnosis", {**valid_submission(), "fixer_eligible": True}, ["fixer_eligible"]),
    ("submit_diagnosis", valid_submission(confidence="certain"), ["confidence"]),
    ("submit_diagnosis", valid_submission(evidence="EV-1"), ["evidence"]),
    ("submit_diagnosis", {}, None),
]


def _real_server(scenario=None, **limits):
    from diagnosis.models import DiagnosisLimits
    from diagnosis.sdk_server import build_handlers, build_server
    from diagnosis.tools import ToolSession

    bundle = fakes.build_reader(scenario or scenarios.output_validation_failed())
    session = ToolSession(bundle.reader, DiagnosisLimits(**limits))
    return build_server(build_handlers(session)), session, bundle


class TestLooseSdkSchemas:
    def test_sdk_input_schemas_cover_exactly_the_five_tools(self):
        from diagnosis.sdk_server import SDK_INPUT_SCHEMAS

        assert set(SDK_INPUT_SCHEMAS) == set(TOOLS)

    @pytest.mark.parametrize("tool", TOOLS)
    def test_sdk_input_schema_is_an_object_with_descriptions_only(self, tool):
        from diagnosis.sdk_server import SDK_INPUT_SCHEMAS

        schema = SDK_INPUT_SCHEMAS[tool]

        assert set(schema) == {"type", "properties"}
        assert schema["type"] == "object"
        for name, definition in schema["properties"].items():
            assert set(definition) == {"description"}, f"{tool}.{name} carries a constraint"
            assert isinstance(definition["description"], str)
            assert definition["description"].strip()

    @pytest.mark.parametrize("tool", TOOLS)
    def test_sdk_input_schema_has_no_constraint_keyword_at_any_depth(self, tool):
        from diagnosis.sdk_server import SDK_INPUT_SCHEMAS

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    if path and path[-1] == "properties":
                        walk(value, [*path, key])  # ここのキーは項目名
                        continue
                    assert key not in CONSTRAINT_KEYWORDS, f"{tool}: {'/'.join(path)}/{key}"
                    if key == "type":
                        assert path == [], f"{tool}: per-field type at {'/'.join(path)}"
                    walk(value, [*path, key])
            elif isinstance(node, list):
                for item in node:
                    walk(item, path)

        walk(SDK_INPUT_SCHEMAS[tool], [])

    @pytest.mark.parametrize("tool", TOOLS)
    def test_sdk_input_schema_lists_the_same_fields_as_the_strict_schema(self, tool):
        from diagnosis.sdk_server import SDK_INPUT_SCHEMAS
        from diagnosis.tools import TOOL_SCHEMAS

        assert set(SDK_INPUT_SCHEMAS[tool]["properties"]) == set(TOOL_SCHEMAS[tool]["properties"])

    def test_sdk_input_schemas_do_not_offer_scope_fields(self):
        """R1: 緩いスキーマにも、実行 ID・テーブル・バケット・キーの項目は無い。"""
        from diagnosis.sdk_server import SDK_INPUT_SCHEMAS

        forbidden = {"execution_id", "table", "table_name", "bucket", "bucket_name", "key", "prefix", "fixer_eligible"}
        for tool, schema in SDK_INPUT_SCHEMAS.items():
            assert not set(schema["properties"]) & forbidden, tool

    def test_strict_schemas_are_kept_for_tool_session(self):
        from diagnosis.tools import TOOL_SCHEMAS

        for tool in TOOLS:
            assert TOOL_SCHEMAS[tool]["additionalProperties"] is False
        assert TOOL_SCHEMAS["get_prompt_record"]["required"] == ["subagent", "index"]
        assert "pattern" in TOOL_SCHEMAS["get_prompt_record"]["properties"]["index"]


class TestRealMcpServer:
    """実物の MCP サーバー（``build_server`` の ``instance``）に、通信なしで tools/list・tools/call を投げる。"""

    def test_tools_list_returns_the_five_tools_with_loose_schemas(self):
        from diagnosis.sdk_server import SDK_INPUT_SCHEMAS
        from diagnosis.tools import TOOL_DESCRIPTIONS

        server, _, bundle = _real_server()

        tools = asyncio.run(mcp_list_tools(server))

        assert sorted(tool.name for tool in tools) == sorted(TOOLS)
        for tool in tools:
            assert tool.inputSchema == SDK_INPUT_SCHEMAS[tool.name]
            assert tool.description == TOOL_DESCRIPTIONS[tool.name]
        assert bundle.all_operations() == []

    @pytest.mark.parametrize(("tool", "args", "invalid_fields"), INVALID_CALLS)
    def test_invalid_input_reaches_the_handler_and_is_rejected_by_tool_session(self, tool, args, invalid_fields):
        server, session, bundle = _real_server()

        reply = asyncio.run(mcp_call_tool(server, tool, args))

        assert reply.outcome != NOT_TOOL_OUTPUT, f"did not reach the handler: {reply.data.get('text')}"
        assert reply.outcome == "invalid_input"
        assert reply.is_error is True
        if invalid_fields is not None:
            assert set(invalid_fields) <= set(reply.data["invalid_fields"])
        # 足取りに残る
        assert [(c.tool, c.outcome, c.args) for c in session.trace] == [(tool, "invalid_input", {})]
        # 記録は読まれない
        assert bundle.all_operations() == []

    def test_invalid_read_calls_through_the_server_count_toward_the_call_limit(self):
        server, session, bundle = _real_server(max_tool_calls=3)

        async def calls() -> list:
            return [
                await mcp_call_tool(server, "list_events", {"offset": -1}),
                await mcp_call_tool(server, "get_prompt_record", {"subagent": "researcher", "index": "../x"}),
                await mcp_call_tool(server, "list_steps", {"execution_id": "exec-20250101000000-ffffffff"}),
                await mcp_call_tool(server, "get_execution_overview", {}),
            ]

        replies = asyncio.run(calls())

        assert [reply.outcome for reply in replies] == ["invalid_input"] * 3 + ["call_limit"]
        assert [c.outcome for c in session.trace] == ["invalid_input"] * 3 + ["call_limit"]
        assert bundle.all_operations() == []

    def test_submission_with_fixer_eligible_through_the_server_is_rejected_and_can_be_resubmitted(self):
        server, session, _ = _real_server()

        async def calls() -> list:
            return [
                await mcp_call_tool(server, "submit_diagnosis", {**valid_submission(), "fixer_eligible": True}),
                await mcp_call_tool(server, "submit_diagnosis", valid_submission(cause_category="unknown")),
            ]

        rejected, accepted = asyncio.run(calls())

        assert rejected.outcome == "invalid_input"
        assert rejected.data["invalid_fields"] == ["fixer_eligible"]
        assert accepted.outcome == "ok"
        assert accepted.is_error is False
        assert session.has_submission is True
        assert [c.outcome for c in session.trace] == ["invalid_input", "ok"]

    def test_valid_calls_through_the_server_return_tool_results(self):
        server, session, _ = _real_server()

        async def calls() -> list:
            return [
                await mcp_call_tool(server, "get_execution_overview", {}),
                await mcp_call_tool(server, "list_events", {"event_type": "error", "offset": 0}),
                await mcp_call_tool(server, "get_prompt_record", {"subagent": "generator_text", "index": "0"}),
            ]

        replies = asyncio.run(calls())

        assert [reply.outcome for reply in replies] == ["ok", "ok", "ok"]
        assert [reply.is_error for reply in replies] == [False, False, False]
        assert replies[0].data["evidence_id"] == "EX-1"
        assert [c.tool for c in session.trace] == ["get_execution_overview", "list_events", "get_prompt_record"]

    def test_value_of_invalid_input_is_not_echoed_back_through_the_server(self):
        server, _, _ = _real_server()

        reply = asyncio.run(
            mcp_call_tool(server, "get_prompt_record", {"subagent": "researcher", "index": "../SECRET_VALUE_8812"})
        )

        assert "SECRET_VALUE_8812" not in json.dumps(reply.raw, ensure_ascii=False)

    def test_unknown_tool_name_through_the_server_does_not_read_records(self):
        server, session, bundle = _real_server()

        reply = asyncio.run(mcp_call_tool(server, "delete_execution", {}))

        assert reply.is_error is True
        assert bundle.all_operations() == []
        assert all(c.tool in (*TOOLS, "unknown") for c in session.trace)


# ---------------------------------------------------------------------------
# テストの弱点への対応: ツールの処理の間も、イベントループが進む（別スレッドで動かしている）
# ---------------------------------------------------------------------------


class _BlockingSession:
    """``call`` が、イベントループの側からの合図を待つ偽のセッション。

    ハンドラーが ``call`` を同期で（イベントループのスレッドで）呼ぶ実装だと、合図を出すコルーチンが
    動けないので、待ち時間いっぱいまで止まり、``released`` が偽になる。
    """

    WAIT_SECONDS = 5.0

    def __init__(self) -> None:
        import threading

        self.entered = threading.Event()
        self.release = threading.Event()
        self.released: bool | None = None
        self.thread_name: str | None = None

    def call(self, name, args):  # noqa: ARG002
        import threading

        from diagnosis.models import ToolResult

        self.thread_name = threading.current_thread().name
        self.entered.set()
        self.released = self.release.wait(self.WAIT_SECONDS)
        return ToolResult("ok", {"released": self.released})


class TestHandlerDoesNotBlockTheEventLoop:
    def _run(self, tool: str = "list_steps"):
        import threading

        from diagnosis.sdk_server import build_handlers

        session = _BlockingSession()
        handler = find_handler(build_handlers(session), tool)
        observed = {"ticks": 0, "loop_thread": None}

        async def scenario():
            observed["loop_thread"] = threading.current_thread().name
            task = asyncio.ensure_future(handler({}))
            # ツールの処理が始まるのを、ループを回しながら待つ
            for _ in range(2_000):
                if session.entered.is_set():
                    break
                await asyncio.sleep(0.001)
            # 処理の最中に、ループの別のコルーチンが進む
            for _ in range(5):
                await asyncio.sleep(0)
                observed["ticks"] += 1
            running = not task.done()
            session.release.set()  # ループの側から合図を出す
            return running, await asyncio.wait_for(task, timeout=10)

        running, out = asyncio.run(scenario())
        return session, observed, running, parse_handler_output(out)

    def test_event_loop_keeps_running_while_the_tool_is_working(self):
        session, observed, running, reply = self._run()

        assert session.released is True, "the handler blocked the event loop while the tool was working"
        assert running is True  # 合図を出すまで、ツールの処理は終わっていない
        assert observed["ticks"] == 5
        assert reply.outcome == "ok"
        assert reply.data["released"] is True

    def test_tool_runs_outside_the_event_loop_thread(self):
        session, observed, _, _ = self._run()

        assert session.thread_name is not None
        assert session.thread_name != observed["loop_thread"]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_every_handler_runs_its_tool_off_the_event_loop(self, tool):
        session, _, _, reply = self._run(tool)

        assert session.released is True
        assert reply.outcome == "ok"

    def test_two_handlers_can_be_waiting_at_the_same_time(self):
        """別スレッドに逃がしているので、片方が待っている間に、もう片方も処理に入れる。"""
        import threading

        from diagnosis.models import ToolResult
        from diagnosis.sdk_server import build_handlers

        barrier = threading.Barrier(2, timeout=5)

        class Session:
            def call(self, name, args):  # noqa: ARG002
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    return ToolResult("internal_error", {})
                return ToolResult("ok", {})

        handlers = build_handlers(Session())

        async def scenario():
            return await asyncio.gather(
                find_handler(handlers, "list_steps")({}), find_handler(handlers, "list_events")({})
            )

        outs = asyncio.run(scenario())

        assert [parse_handler_output(out).outcome for out in outs] == ["ok", "ok"]
