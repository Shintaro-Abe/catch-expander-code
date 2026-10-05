"""偽の SDK。``src.fixer.runner._invoke_sdk`` と差し替える（design.md §14）。

第 1 段階の ``tests/unit/agent/diagnosis/sdk_fake.py`` と同じ作りで、サーバー名だけが違う（``fixer``）。
サーバー名に依らない部品（``mcp_call_tool`` など）は、第 1 段階のものをそのまま使う。

**ツールの呼び出しは、実物の MCP サーバーを通す**。``options.mcp_servers["fixer"]["instance"]`` に、
通信なしで ``tools/call`` を投げる。実物の SDK と同じく、MCP サーバーの入力検証（jsonschema）を通るので、
SDK に渡す入力スキーマが厳しすぎて呼び出しがハンドラーに届かない、という食い違いをテストが隠さない。
``options`` にサーバーが無いときは、ハンドラーを直接呼ばずに失敗する（食い違いを隠さないため）。

台本は ``async def script(agent: Agent)``。``agent.call(tool, args)`` でツールを呼び、``agent.root`` で
複製の根を得る。合否の検査で落ちる差分は、ツールが書き込みを拒否するので、台本の中で ``agent.root`` の下の
ファイルを直接書き換えて作る（修正役が走らせたテストのコードが書き換えた、という状況に当たる）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from ..agent.diagnosis.sdk_fake import (
    NOT_TOOL_OUTPUT,
    Reply,
    init_message,
    mcp_call_tool,
    mcp_list_tools,
    parse_handler_output,
)

__all__ = [
    "NOT_TOOL_OUTPUT",
    "SERVER",
    "TOOLS",
    "Agent",
    "FakeSdk",
    "Reply",
    "full_name",
    "init_message",
    "mcp_call_tool",
    "mcp_list_tools",
    "parse_handler_output",
    "result_message",
]

SERVER = "fixer"
TOOLS = ("list_files", "read_file", "search_code", "write_file", "replace_in_file", "run_tests", "finish")
DEFAULT_USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_creation_input_tokens": 10,
    "cache_read_input_tokens": 5,
}


def full_name(tool: str) -> str:
    return f"mcp__{SERVER}__{tool}"


def tool_use_message(name: str, args: dict | None = None, *, index: int = 1) -> AssistantMessage:
    """``name`` は SDK から見た名前（``mcp__fixer__read_file``、``Bash`` など）。"""
    block = ToolUseBlock(id=f"toolu_{index:04d}", name=name, input=args or {})
    return AssistantMessage(content=[TextBlock(text="調べます"), block], model="sonnet")


def result_message(
    subtype: str = "success",
    *,
    is_error: bool = False,
    num_turns: int = 6,
    total_cost_usd: float | None = 0.0456,
    usage: dict | None = None,
    result: str = "done",
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
        result=result,
    )


@dataclass
class Agent:
    """台本に渡すもの。"""

    call: Callable[..., Awaitable[Reply]]
    root: Path  # 複製の根（``options.cwd``）


Script = Callable[[Agent], Awaitable[None]]
_DEFAULT: Any = object()


@dataclass
class FakeSdk:
    script: Script | None = None
    result: Any = _DEFAULT  # ResultMessage / None（ResultMessage なしで終わる）
    raises: BaseException | None = None
    delay: float = 0.0  # ResultMessage を返す前に待つ秒数（時間切れの確認用）
    init_tools: Any = None  # 初期化のシステムメッセージに入れるツール名の一覧（None なら流さない）
    foreign_tool_uses: tuple[str, ...] = ()  # 自分で定義したもの以外のツール呼び出し（``Bash`` など）を流す
    final_text: str | None = None  # 最後に流す、モデルの文章
    script_started: bool = False
    invocations: list[tuple[str, Any]] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)
    root_listings: list[list[str]] = field(default_factory=list)  # 呼ばれた時点の、複製の根の直下
    replies: list[Reply] = field(default_factory=list)
    called: list[tuple[str, Any]] = field(default_factory=list)

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

    @property
    def root(self) -> Path:
        return self.roots[-1]

    def outcomes(self) -> list[str]:
        return [reply.outcome for reply in self.replies]

    async def _stream(self, prompt: str, options: Any, _handlers: dict) -> AsyncIterator[Any]:
        self.invocations.append((prompt, options))
        cwd = getattr(options, "cwd", None)
        root = Path(os.fsdecode(cwd)) if cwd else Path("/nonexistent-fixer-workspace")
        self.roots.append(root)
        self.root_listings.append(sorted(os.listdir(root)) if root.is_dir() else [])
        messages: list[AssistantMessage] = []
        server = (getattr(options, "mcp_servers", None) or {}).get(SERVER)

        async def call(tool: str, args: Any = None) -> Reply:
            if server is None:
                raise AssertionError(f"options.mcp_servers has no {SERVER!r} server")
            arguments = {} if args is None else args
            reply = await mcp_call_tool(server, tool, arguments)
            self.replies.append(reply)
            self.called.append((tool, arguments))
            shown = arguments if isinstance(arguments, dict) else {}
            messages.append(tool_use_message(full_name(tool), shown, index=len(messages) + 1))
            return reply

        if self.init_tools is not None:
            yield init_message(self.init_tools)
        self.script_started = True
        if self.script is not None:
            await self.script(Agent(call=call, root=root))
        for message in messages:
            yield message
        for index, name in enumerate(self.foreign_tool_uses):
            yield tool_use_message(name, {"command": "true"}, index=900 + index)
        if self.final_text is not None:
            yield AssistantMessage(content=[TextBlock(text=self.final_text)], model="sonnet")
        if self.raises is not None:
            raise self.raises
        if self.delay:
            await asyncio.sleep(self.delay)
        result = result_message() if self.result is _DEFAULT else self.result
        if result is not None:
            yield result
