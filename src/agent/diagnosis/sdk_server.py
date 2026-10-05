"""ツールの SDK への登録（インプロセス MCP サーバー）。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §7.1、§18 の M1

ハンドラーを build_handlers と build_server に分けるのは、テストが SDK を起動せずに
ハンドラーを直接呼べるようにするため。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from diagnosis.models import (
    ACTION_TYPES,
    CAUSE_CATEGORIES,
    CAUSE_NATURES,
    CONFIDENCES,
    FAILED_STAGES,
    PROMPT_SUBAGENTS,
    ToolResult,
)
from diagnosis.tools import TOOL_DESCRIPTIONS, TOOL_NAMES, TOOL_SCHEMAS, ToolSession

logger = logging.getLogger("catch-expander-agent")

SERVER_NAME = "diag"

Handler = Callable[[dict], Awaitable[dict]]


def _one_of(values: tuple[str, ...]) -> str:
    return "次のどれか 1 つ（文字列）: " + " / ".join(values)


# 項目の説明。型・取りうる値・長さは、制約のキーワードでなく、この文で伝える
_FIELD_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "get_execution_overview": {},
    "list_steps": {},
    "list_events": {
        "event_type": "絞り込むイベントの種別（任意）。英数字と下線だけの文字列、64 字まで。例: error",
        "offset": "読み始める位置（任意）。0 以上の整数。前の結果の next_offset を渡す",
    },
    "get_prompt_record": {
        "subagent": "役（必須）。" + _one_of(PROMPT_SUBAGENTS),
        "index": "番号（必須）。英数字・ハイフン・下線だけの文字列、64 字まで",
    },
    "submit_diagnosis": {
        "failed_stage": "失敗した段階（必須）。" + _one_of(FAILED_STAGES),
        "cause_category": "原因の分類（必須）。" + _one_of(CAUSE_CATEGORIES),
        "cause_nature": "原因の性質（必須）。" + _one_of(CAUSE_NATURES),
        "confidence": "確からしさ（必須）。" + _one_of(CONFIDENCES),
        "evidence": (
            "根拠の配列（必須。空でもよい。10 件まで）。各要素は id と note の 2 項目だけを持つオブジェクト。"
            "id はツールが返した evidence_id（文字列）、note は短い説明（文字列、200 字まで）"
        ),
        "action_type": "推奨する対処の分類（必須）。" + _one_of(ACTION_TYPES),
        "action_note": "対処の短い説明（必須）。文字列、300 字まで",
        "summary": "要約（必須）。文字列、500 字まで",
        "exception_evidence_id": "例外を記録したイベントの evidence_id（任意）。EV-<数字> の形の文字列",
        "code_location_ids": (
            "関係するコードの場所（任意）。frames の frame_id（FR-<数字> の形の文字列）の配列、5 件まで"
        ),
    },
}

# SDK に渡す入力スキーマ。項目の名前と説明だけを持つ、緩いもの。
# create_sdk_mcp_server が作る MCP サーバーは、ハンドラーの前にこのスキーマで入力を検証する。
# 厳しいスキーマ（tools.TOOL_SCHEMAS）を渡すと、スキーマ違反の呼び出しがハンドラーに届かず、
# 足取りに残らず、呼び出し総数にも数えられない。検証は ToolSession に一本化する。
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


def _make_handler(session: ToolSession, name: str) -> Handler:
    async def handler(args: dict) -> dict:
        try:
            # boto3 の呼び出しは同期なので、SDK とのやり取りを担うイベントループを止めないよう別スレッドで動かす
            result = await asyncio.to_thread(session.call, name, args)
        except Exception:
            # SDK は未捕捉の例外の文面をそのままモデルに渡すので、ここで必ず受けて固定文を返す
            logger.exception("Diagnosis tool handler failed: tool=%s", name)
            result = ToolResult("internal_error", {})
        return _to_output(result)

    return handler


def build_handlers(session: ToolSession) -> dict[str, Handler]:
    """ツール名（短い名前）からハンドラーを引く辞書を返す。"""
    return {name: _make_handler(session, name) for name in TOOL_NAMES}


def build_server(handlers: dict[str, Handler]) -> Any:
    """ハンドラーを SDK のツールとして包み、MCP サーバーの設定を返す。"""
    tools = [tool(name, TOOL_DESCRIPTIONS[name], SDK_INPUT_SCHEMAS[name])(handlers[name]) for name in TOOL_NAMES]
    return create_sdk_mcp_server(name=SERVER_NAME, tools=tools)
