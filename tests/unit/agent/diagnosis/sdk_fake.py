"""偽の SDK。``diagnosis.runner._invoke_sdk`` と差し替える（design.md §13）。

T0-1 で確かめた、``claude_agent_sdk==0.2.110`` の本物の型の最小の組み立て方:

- ``ToolUseBlock(id=..., name=..., input=...)``（3 つとも必須）
- ``AssistantMessage(content=[...], model=...)``（2 つが必須）
- ``ResultMessage(subtype=..., duration_ms=..., duration_api_ms=..., is_error=..., num_turns=...,
  session_id=...)``（6 つが必須。``total_cost_usd``・``usage``・``result`` は任意）

偽物は、台本どおりにツールを呼び、その呼び出しを表す ``AssistantMessage`` を流し、
最後に ``ResultMessage`` を流す。

**ツールの呼び出しは、実物の MCP サーバーを通す**（修正仕様 M1）。``options.mcp_servers["diag"]["instance"]``
に、通信なしで ``tools/call`` を投げる。実物の SDK と同じく、MCP サーバーの入力検証（jsonschema）を通るので、
SDK に渡す入力スキーマが厳しすぎて呼び出しがハンドラーに届かない、という食い違いをテストが隠さない。
``options`` にサーバーが無いときだけ、ハンドラーを直接呼ぶ。

``init_tools`` を渡すと、最初に初期化のシステムメッセージ（``SystemMessage(subtype="init", data={"tools": [...]})``）
を流す（修正仕様 M3）。台本は、その後で実行する（受信ループが初期化の時点で中断すれば、台本は走らない）。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock

SERVER = "diag"
DEFAULT_USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_creation_input_tokens": 10,
    "cache_read_input_tokens": 5,
}
EXPECTED_USAGE = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 165}


def full_name(tool: str) -> str:
    return f"mcp__{SERVER}__{tool}"


def tool_use_message(tool: str, args: dict | None = None, *, index: int = 1) -> AssistantMessage:
    block = ToolUseBlock(id=f"toolu_{index:04d}", name=full_name(tool), input=args or {})
    return AssistantMessage(content=[TextBlock(text="調べます"), block], model="sonnet")


def result_message(
    subtype: str = "success",
    *,
    is_error: bool = False,
    num_turns: int = 4,
    total_cost_usd: float | None = 0.0123,
    usage: dict | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1200,
        duration_api_ms=1000,
        is_error=is_error,
        num_turns=num_turns,
        session_id="sess-test",
        total_cost_usd=total_cost_usd,
        usage=dict(DEFAULT_USAGE) if usage is None else usage,
        result="done",
    )


@dataclass
class Reply:
    """ハンドラーが返したものを、モデルから見える形で持つ。"""

    raw: dict
    data: dict
    is_error: bool

    @property
    def outcome(self) -> str:
        return self.data["outcome"]


NOT_TOOL_OUTPUT = "<not produced by the tool handler>"


def _reply(raw: dict, text: str, is_error: bool) -> Reply:
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict) or "outcome" not in data:
        # ハンドラーに届かなかった（MCP サーバーの入力検証で止まった、など）。テストが気づけるように印を付ける
        data = {"outcome": NOT_TOOL_OUTPUT, "text": text}
    return Reply(raw=raw, data=data, is_error=is_error)


def parse_handler_output(out: dict) -> Reply:
    return _reply(out, out["content"][0]["text"], bool(out.get("is_error")))


def init_message(tools: Any, **extra: Any) -> SystemMessage:
    """初期化のシステムメッセージ。``tools`` が None なら、ツールの一覧を含めない。

    形は、CLI が流す ``{"type": "system", "subtype": "init", "tools": [名前, ...], ...}`` に合わせている
    （SDK は、これを ``SystemMessage(subtype="init", data=<そのままの辞書>)`` にする。``message_parser.py``）。
    """
    data: dict[str, Any] = {"type": "system", "subtype": "init", "session_id": "sess-test", **extra}
    if tools is not None:
        data["tools"] = list(tools) if isinstance(tools, list | tuple) else tools
    return SystemMessage(subtype="init", data=data)


async def mcp_list_tools(server: Any) -> list[Any]:
    """実物の MCP サーバー（``build_server(...)`` の戻り値）に、通信なしで ``tools/list`` を投げる。"""
    from mcp.types import ListToolsRequest

    result = await server["instance"].request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    return list(result.root.tools)


async def mcp_call_tool(server: Any, tool: str, args: Any) -> Reply:
    """実物の MCP サーバーに、通信なしで ``tools/call`` を投げる（SDK の ``query.py`` と同じ呼び方）。"""
    from mcp.types import CallToolRequest, CallToolRequestParams

    # 実物のクライアントと同じく、先に一覧を取る（入力検証は、この一覧のスキーマで行われる）
    await mcp_list_tools(server)
    request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=tool, arguments=args))
    result = (await server["instance"].request_handlers[CallToolRequest](request)).root
    text = result.content[0].text if result.content else ""
    return _reply({"content": [{"type": "text", "text": text}], "is_error": result.isError}, text, bool(result.isError))


def find_handler(handlers: dict, tool: str) -> Callable[[dict], Awaitable[dict]]:
    """ハンドラーの辞書のキーは、短い名前でも ``mcp__diag__`` つきでも受け付ける。"""
    if tool in handlers:
        return handlers[tool]
    return handlers[full_name(tool)]


Call = Callable[..., Awaitable[Reply]]
Script = Callable[[Call], Awaitable[None]]
_DEFAULT: Any = object()


@dataclass
class FakeSdk:
    script: Script | None = None
    result: Any = _DEFAULT  # ResultMessage / None（ResultMessage なしで終わる）
    raises: Exception | None = None
    delay: float = 0.0  # ResultMessage を返す前に待つ秒数（時間切れの確認用）
    init_tools: Any = None  # 初期化のシステムメッセージに入れるツール名の一覧（None なら流さない）
    script_started: bool = False
    invocations: list[tuple[str, Any]] = field(default_factory=list)
    cwd_snapshots: list[tuple[bool, list[str]]] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)

    def __call__(self, prompt: str, options: Any, handlers: dict) -> AsyncIterator[Any]:
        return self._stream(prompt, options, handlers)

    @property
    def invocation_count(self) -> int:
        return len(self.invocations)

    @property
    def options(self) -> Any:
        return self.invocations[-1][1]

    @property
    def prompt(self) -> str:
        return self.invocations[-1][0]

    async def _stream(self, prompt: str, options: Any, handlers: dict) -> AsyncIterator[Any]:
        self.invocations.append((prompt, options))
        cwd = getattr(options, "cwd", None)
        if cwd:
            exists = os.path.isdir(cwd)
            self.cwd_snapshots.append((exists, sorted(os.listdir(cwd)) if exists else []))
        messages: list[AssistantMessage] = []
        server = (getattr(options, "mcp_servers", None) or {}).get(SERVER)

        async def call(tool: str, args: dict | None = None) -> Reply:
            arguments = args if args is not None else {}
            if server is not None:
                reply = await mcp_call_tool(server, tool, arguments)
            else:
                reply = parse_handler_output(await find_handler(handlers, tool)(arguments))
            self.replies.append(reply)
            messages.append(tool_use_message(tool, args, index=len(messages) + 1))
            return reply

        if self.init_tools is not None:
            yield init_message(self.init_tools)
        self.script_started = True
        if self.script is not None:
            await self.script(call)
        for message in messages:
            yield message
        if self.raises is not None:
            raise self.raises
        if self.delay:
            await asyncio.sleep(self.delay)
        result = result_message() if self.result is _DEFAULT else self.result
        if result is not None:
            yield result
