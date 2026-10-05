"""ツールの SDK への登録（インプロセス MCP サーバー）。

設計詳細: `.steering/20261004-execution-fixer/design.md` §9.1

ハンドラーを build_handlers と build_server に分けるのは、テストが SDK を起動せずに
ハンドラーを直接呼べるようにするため（第 1 段階の `diagnosis/sdk_server.py` と同じ作り）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from src.fixer.models import TOOL_NAMES, ToolResult
from src.fixer.tools import (
    CLAIMABLE_PATCH_KINDS,
    FINISH_OUTCOMES,
    TOOL_DESCRIPTIONS,
    TOOL_SCHEMAS,
    FixToolSession,
)

logger = logging.getLogger("catch-expander-fixer")

SERVER_NAME = "fixer"

Handler = Callable[[dict], Awaitable[dict]]


def _one_of(values: tuple[str, ...]) -> str:
    return "次のどれか 1 つ（文字列）: " + " / ".join(values)


_PATH_DESCRIPTION = "複製の根からの相対パス（文字列）。絶対パスと .. は使えない。例: src/agent/orchestrator.py"

# 項目の説明。型・取りうる値・長さは、制約のキーワードでなく、この文で伝える
_FIELD_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "list_files": {"path": "一覧するディレクトリ（必須）。" + _PATH_DESCRIPTION},
    "read_file": {
        "path": "読むファイル（必須）。" + _PATH_DESCRIPTION,
        "start_line": "読み始める行（任意）。1 以上の整数。既定は 1",
        "max_lines": "読む行数（任意）。1 以上 2000 以下の整数。既定は 400",
    },
    "search_code": {
        "pattern": "探す文字列（必須）。正規表現ではなく、固定の文字列。200 字まで",
        "path": "探す範囲のディレクトリ（任意）。" + _PATH_DESCRIPTION,
    },
    "write_file": {
        "path": "書くファイル（必須）。src/ または tests/ の下の .py。" + _PATH_DESCRIPTION,
        "content": "ファイルの中身の全体（必須）。文字列",
    },
    "replace_in_file": {
        "path": "書き換えるファイル（必須）。src/ または tests/ の下の .py。" + _PATH_DESCRIPTION,
        "old": "置き換える前の文字列（必須）。ファイルの中に、ちょうど 1 か所だけ現れること",
        "new": "置き換えた後の文字列（必須）",
    },
    "run_tests": {
        "paths": (
            "走らせるテストのパスの配列（必須。1 件以上 5 件まで）。各要素は、tests/ の下のファイルかディレクトリの"
            "相対パス（文字列）。オプションや :: は使えない"
        ),
    },
    "finish": {
        "outcome": "終わり方（必須）。" + _one_of(FINISH_OUTCOMES),
        "patch_kind": "修正の種類（必須）。" + _one_of(CLAIMABLE_PATCH_KINDS),
        "explanation": "何を、なぜ変えたか。または、なぜ直せないか（必須）。文字列、800 字まで",
    },
}

# SDK に渡す入力スキーマ。項目の名前と説明だけを持つ、緩いもの。
# create_sdk_mcp_server が作る MCP サーバーは、ハンドラーの前にこのスキーマで入力を検証する。
# 厳しいスキーマ（tools.TOOL_SCHEMAS）を渡すと、スキーマ違反の呼び出しがハンドラーに届かず、
# 足取りに残らず、回数にも数えられない。検証は FixToolSession に一本化する。
SDK_INPUT_SCHEMAS: dict[str, dict] = {
    name: {
        "type": "object",
        "properties": {
            field: {"description": _FIELD_DESCRIPTIONS[name][field]} for field in TOOL_SCHEMAS[name]["properties"]
        },
    }
    for name in TOOL_NAMES
}


def allowed_tool_names() -> list[str]:
    """SDK から見たツール名（mcp__<サーバー>__<ツール>）を返す。"""
    return [f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES]


def _to_output(result: ToolResult) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": result.to_text()}]}
    if result.is_error:
        out["is_error"] = True
    return out


class _CallOrder:
    """ツールの呼び出しを、届いた順に 1 件ずつ進めるための、イベントループごとのロック。

    スレッドに渡してからロックを取り合うと、並べて呼ばれたツールの順が、届いた順と入れ替わりうる。
    イベントループの側で順に並べれば、届いた順に進み、時間切れで打ち切られたときは、待っていた呼び出しが
    始まらない（`asyncio.Lock` は、待っている順に渡される）。
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    def lock(self) -> asyncio.Lock:
        # asyncio.Lock は、待ちが生じた時点のイベントループに結び付く。ループが変わったら、作り直す
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._lock = loop, asyncio.Lock()
        return self._lock


def _make_handler(session: FixToolSession, name: str, order: _CallOrder) -> Handler:
    async def handler(args: dict) -> dict:
        try:
            async with order.lock():
                # ファイルの読み書きとテスト実行は同期なので、SDK とのやり取りを担うイベントループを止めないよう、
                # 別スレッドで動かす
                result = await asyncio.to_thread(session.call, name, args)
        except Exception:
            # SDK は未捕捉の例外の文面をそのままモデルに渡すので、ここで必ず受けて固定文を返す
            logger.exception("Fixer tool handler failed: tool=%s", name)
            result = ToolResult("internal_error", {})
        return _to_output(result)

    return handler


def build_handlers(session: FixToolSession) -> dict[str, Handler]:
    """ツール名（短い名前）からハンドラーを引く辞書を返す。"""
    order = _CallOrder()
    return {name: _make_handler(session, name, order) for name in TOOL_NAMES}


def build_server(handlers: dict[str, Handler]) -> Any:
    """ハンドラーを SDK のツールとして包み、MCP サーバーの設定を返す。"""
    tools = [tool(name, TOOL_DESCRIPTIONS[name], SDK_INPUT_SCHEMAS[name])(handlers[name]) for name in TOOL_NAMES]
    return create_sdk_mcp_server(name=SERVER_NAME, tools=tools)
