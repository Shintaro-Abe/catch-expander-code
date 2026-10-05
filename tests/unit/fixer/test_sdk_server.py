"""SDK への登録（design.md §9.1）。**実物の MCP サーバー**（``create_sdk_mcp_server`` の戻り値）を通して呼ぶ。

対応: AC-29（形の不正な入力が、実物の MCP サーバーを通しても、ハンドラーに届いて ``invalid_input`` になり、
足取りと回数に残る。結果に、不正な値そのものは含まれない）、R7b、AC-5 の部品（許可するツール名）。

実物の SDK のインプロセスの MCP サーバーは、ハンドラーの前に ``jsonschema`` で入力を検証する。厳しいスキーマを
渡すと、形の不正な呼び出しはハンドラーに届かず（``NOT_TOOL_OUTPUT``）、足取りにも回数にも残らない。
"""

import asyncio
import json
import threading

from .fakes import FINISH_FIXED, FakeTestRunner, cases_of, check_each
from .repo_factory import FILES, MAIN, NEW_TEST, NEW_TEST_SOURCE, SRC
from .sdk_fake import NOT_TOOL_OUTPUT, TOOLS, full_name, mcp_call_tool, mcp_list_tools, parse_handler_output

VALUE_MARK = "SDK_SERVER_VALUE_MARK_6630"
EXPECTED_PROPERTIES = {
    "list_files": {"path"},
    "read_file": {"path", "start_line", "max_lines"},
    "search_code": {"pattern", "path"},
    "write_file": {"path", "content"},
    "replace_in_file": {"path", "old", "new"},
    "run_tests": {"paths"},
    "finish": {"outcome", "patch_kind", "explanation"},
}


def build(workspace, *, limits=None, runner=None):
    from src.fixer.models import FixLimits
    from src.fixer.sdk_server import build_handlers, build_server
    from src.fixer.tools import FixToolSession

    session = FixToolSession(workspace, limits or FixLimits(), runner or FakeTestRunner())
    handlers = build_handlers(session)
    return session, handlers, build_server(handlers)


def handler_for(handlers: dict, tool: str):
    """ハンドラーの辞書のキーは、短い名前でも ``mcp__fixer__`` つきでも受け付ける。"""
    return handlers[tool] if tool in handlers else handlers[full_name(tool)]


def call(server, tool, args):
    return asyncio.run(mcp_call_tool(server, tool, args))


class TestNames:
    def test_server_name_is_fixer(self):
        from src.fixer.sdk_server import SERVER_NAME

        assert SERVER_NAME == "fixer"

    def test_allowed_tool_names_are_exactly_the_seven_custom_tools(self):
        """AC-5: 許可は、自分で定義したツールだけ。"""
        from src.fixer.sdk_server import allowed_tool_names

        names = allowed_tool_names()

        assert sorted(names) == sorted(f"mcp__fixer__{tool}" for tool in TOOLS)
        assert len(names) == 7

    def test_build_handlers_returns_one_coroutine_function_per_tool(self, workspace):
        _session, handlers, _server = build(workspace)

        assert len(handlers) == 7
        for tool in TOOLS:
            assert asyncio.iscoroutinefunction(handler_for(handlers, tool))


class TestRegisteredSchemas:
    def test_server_lists_exactly_the_seven_tools(self, workspace):
        _session, _handlers, server = build(workspace)

        tools = asyncio.run(mcp_list_tools(server))

        assert sorted(tool.name for tool in tools) == sorted(TOOLS)
        assert all(tool.description for tool in tools)

    def test_input_schemas_are_loose_and_carry_only_descriptions(self, workspace):
        """design.md §9.1: 最上位は ``type`` と ``properties`` だけ、項目は ``description`` だけ。"""
        _session, _handlers, server = build(workspace)

        tools = {tool.name: tool for tool in asyncio.run(mcp_list_tools(server))}

        for name, expected in EXPECTED_PROPERTIES.items():
            schema = tools[name].inputSchema
            assert set(schema) == {"type", "properties"}, name
            assert schema["type"] == "object"
            assert set(schema["properties"]) == expected, name
            for field, definition in schema["properties"].items():
                assert set(definition) == {"description"}, (name, field)
                assert isinstance(definition["description"], str)
                assert definition["description"]


class TestCallsThroughTheRealServer:
    def test_well_formed_call_reaches_the_session_and_returns_its_result(self, workspace):
        session, _handlers, server = build(workspace)

        reply = call(server, "read_file", {"path": SRC})

        assert reply.outcome == "ok"
        assert reply.is_error is False
        assert "def average(values):" in reply.data["content"]
        assert [(item.tool, item.outcome) for item in session.trace] == [("read_file", "ok")]

    def test_write_through_the_server_changes_the_workspace(self, workspace):
        from pathlib import Path

        session, _handlers, server = build(workspace)

        reply = call(server, "write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})

        assert reply.outcome == "ok"
        assert (Path(workspace.root) / NEW_TEST).read_text(encoding="utf-8") == NEW_TEST_SOURCE
        assert session.trace[0].path == NEW_TEST

    def test_refused_call_sets_is_error(self, workspace):
        from pathlib import Path

        _session, _handlers, server = build(workspace)

        reply = call(server, "write_file", {"path": MAIN, "content": "X = 1\n"})

        assert reply.outcome == "denied"
        assert reply.is_error is True
        assert reply.raw["is_error"] is True
        assert (Path(workspace.root) / MAIN).read_text(encoding="utf-8") == FILES[MAIN]

    def test_malformed_call_reaches_the_handler_and_is_traced_as_invalid_input(self, workspace):
        """AC-29: 形の不正な入力は、MCP サーバーの検証で止まらず、ハンドラーに届いて ``invalid_input`` になる。"""
        cases = cases_of(
            ("tool", "args"),
            [
                # 未定義の項目
                ("list_files", {"path": "src", "recursive": True}),
                ("read_file", {"path": SRC, "unknown_field": VALUE_MARK}),
                ("search_code", {"pattern": "average", "regex": VALUE_MARK}),
                ("write_file", {"path": NEW_TEST, "content": "x", "mode": VALUE_MARK}),
                ("replace_in_file", {"path": SRC, "old": "a", "new": "b", "count": 2}),
                ("run_tests", {"paths": [NEW_TEST], "args": [VALUE_MARK]}),
                ("finish", {**FINISH_FIXED, "tests_passed": VALUE_MARK}),
                # 型の違い
                ("list_files", {"path": 3}),
                ("read_file", {"path": [VALUE_MARK]}),
                ("read_file", {"path": SRC, "start_line": VALUE_MARK}),
                ("read_file", {"path": SRC, "max_lines": {"value": VALUE_MARK}}),
                ("search_code", {"pattern": {"text": VALUE_MARK}}),
                ("write_file", {"path": NEW_TEST, "content": [VALUE_MARK]}),
                ("write_file", {"path": {"p": VALUE_MARK}, "content": "x"}),
                ("replace_in_file", {"path": SRC, "old": 1, "new": None}),
                ("run_tests", {"paths": VALUE_MARK}),
                ("run_tests", {"paths": [{"p": VALUE_MARK}]}),
                ("finish", {**FINISH_FIXED, "outcome": [VALUE_MARK]}),
                ("finish", {**FINISH_FIXED, "explanation": 3}),
                # 必須の項目が無い
                ("list_files", {}),
                ("read_file", {}),
                ("search_code", {}),
                ("write_file", {"path": NEW_TEST}),
                ("replace_in_file", {"path": SRC}),
                ("run_tests", {}),
                ("finish", {}),
            ],
        )

        def check(case):
            tool, args = case
            session, _handlers, server = build(workspace)

            reply = call(server, tool, args)

            assert reply.outcome != NOT_TOOL_OUTPUT, reply.data
            assert reply.outcome == "invalid_input"
            assert reply.is_error is True
            assert VALUE_MARK not in json.dumps(reply.raw, ensure_ascii=False)
            assert [(item.tool, item.outcome, item.path) for item in session.trace] == [(tool, "invalid_input", None)]
            assert session.finished is None

        check_each(cases, check)

    def test_malformed_calls_through_the_server_use_up_the_call_limit(self, workspace):
        """AC-29: 回数にも残る（不正な入力を繰り返しても、上限が効く）。"""
        from src.fixer.models import FixLimits

        session, _handlers, server = build(workspace, limits=FixLimits(max_tool_calls=2))

        first = call(server, "read_file", {"path": 3})
        second = call(server, "read_file", {"bogus": VALUE_MARK})
        third = call(server, "read_file", {"path": SRC})

        assert (first.outcome, second.outcome, third.outcome) == ("invalid_input", "invalid_input", "call_limit")
        assert [item.outcome for item in session.trace] == ["invalid_input", "invalid_input", "call_limit"]

    def test_call_without_arguments_reaches_the_handler(self, workspace):
        session, _handlers, server = build(workspace)

        reply = call(server, "read_file", None)

        assert reply.outcome == "invalid_input"
        assert len(session.trace) == 1


class TestHandlers:
    def test_handler_output_has_text_content_and_no_error_flag_on_success(self, workspace):
        _session, handlers, _server = build(workspace)

        out = asyncio.run(handler_for(handlers, "list_files")({"path": "src"}))

        assert set(out) <= {"content", "is_error"}
        assert not out.get("is_error")
        assert len(out["content"]) == 1
        assert out["content"][0]["type"] == "text"
        assert json.loads(out["content"][0]["text"])["outcome"] == "ok"

    def test_handler_never_leaks_an_exception_and_returns_fixed_internal_error(self, workspace, monkeypatch):
        """design.md §9.1: ハンドラーでも例外を受けて、``internal_error`` の固定文を返す。"""
        session, handlers, server = build(workspace)

        def explode(name, args):
            raise RuntimeError(f"boom {VALUE_MARK}")

        monkeypatch.setattr(session, "call", explode)

        direct = parse_handler_output(asyncio.run(handler_for(handlers, "read_file")({"path": SRC})))
        through_server = call(server, "read_file", {"path": SRC})

        for reply in (direct, through_server):
            assert reply.outcome == "internal_error"
            assert reply.is_error is True
            assert VALUE_MARK not in json.dumps(reply.raw, ensure_ascii=False)

    def test_handler_runs_the_session_call_off_the_event_loop_thread(self, workspace, monkeypatch):
        """design.md §9.1: ``asyncio.to_thread`` で呼ぶ（テスト実行の間、イベントループを止めない）。"""
        session, handlers, _server = build(workspace)
        seen: list[int] = []
        original = session.call

        def recording(name, args):
            seen.append(threading.get_ident())
            return original(name, args)

        monkeypatch.setattr(session, "call", recording)

        async def run() -> int:
            await handler_for(handlers, "read_file")({"path": SRC})
            return threading.get_ident()

        loop_thread = asyncio.run(run())

        assert len(seen) == 1
        assert seen[0] != loop_thread
