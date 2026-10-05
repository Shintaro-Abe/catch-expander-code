"""実行診断役の実行（オプション、受信ループ、時間切れ、公開する関数）。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §7・§10、§18 の M3・L5

SDK の呼び出しは 1 回だけで、再試行も上位モデルへの切り替えもしない。上限への到達と
SDK の失敗は、例外にせず DiagnosisRun の status / stop_detail で返す。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, ToolUseBlock
from claude_agent_sdk import query as _sdk_query
from diagnosis.handoff import build_handoff
from diagnosis.models import Diagnosis, DiagnosisLimits, DiagnosisRun
from diagnosis.prompt import SYSTEM_PROMPT, USER_PROMPT
from diagnosis.records import RecordReader
from diagnosis.sdk_server import SERVER_NAME, Handler, allowed_tool_names, build_handlers, build_server
from diagnosis.tools import ToolSession

# orchestrator.py と同じ扱い: Docker build context に src/observability/ が無い環境では
# ImportError を graceful skip し、イベントを残さない。
try:
    from src.observability import EventEmitter as _EventEmitter
except ImportError:  # pragma: no cover - src/observability を同梱しない環境でのみ発生
    _EventEmitter = None

logger = logging.getLogger("catch-expander-agent")

DEFAULT_MODEL = "sonnet"
EVENT_TYPE = "diagnosis_completed"
# 数字は [0-9] と書く（\d は全角の数字にも一致するため）。検証は fullmatch で行う。
_EXECUTION_ID_PATTERN = re.compile(r"exec-[0-9]{14}-[0-9a-f]{8}")
_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,80}")
_MAX_LOGGED_TOOL_NAMES = 10
# tools=[] が効かなかった場合に備えて、組み込みツールを名前でも外す。どちらが効いても、もう一方は害にならない。
DISALLOWED_BUILTIN_TOOLS = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "Skill",
    "TodoWrite",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
)
_TRIGGERS = ("manual", "auto")
_EMPTY_USAGE = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
# 時間切れの後も、ツールのスレッドが終わるまでイベントループは閉じない。記録の読み取りが長く待たないようにする。
# total_max_attempts は、初回を含む試行の総数（max_attempts は「初回を除く再試行の回数」なので使わない）
_AWS_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2})


class _SdkStreamError(Exception):
    """SDK のストリームが出した例外。時間切れ（TimeoutError）と区別するために包む。"""


class _ToolIsolationError(Exception):
    """自分で定義した 5 個以外のツールが、使える状態にある、または使われた。"""


def _is_valid_execution_id(execution_id: object) -> bool:
    return isinstance(execution_id, str) and _EXECUTION_ID_PATTERN.fullmatch(execution_id) is not None


def _build_default_reader(execution_id: str) -> RecordReader:
    """環境変数と boto3 から RecordReader を組み立てる（reader を渡さなかったときだけ）。"""
    return RecordReader(
        execution_id,
        dynamodb=boto3.resource("dynamodb", config=_AWS_CONFIG),
        s3=boto3.client("s3", config=_AWS_CONFIG),
        table_prefix=os.environ["DYNAMODB_TABLE_PREFIX"],
        events_table=os.environ.get("EVENTS_TABLE", ""),
        prompts_bucket=os.environ.get("PROMPTS_BUCKET", ""),
    )


def build_options(*, model: str, limits: DiagnosisLimits, server: Any, cwd: str) -> ClaudeAgentOptions:
    """診断役に渡すオプションを組み立てる。

    組み込みツールをすべて外し、許可は自分で定義した 5 個だけにする。permission_mode は
    指定しない（許可していないツールは確認待ちになり、対話のない実行では通らない）。
    """
    kwargs: dict[str, Any] = {}
    if limits.max_budget_usd is not None:
        kwargs["max_budget_usd"] = limits.max_budget_usd
    return ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,  # 素の文字列。claude_code のプリセットは使わない
        tools=[],
        allowed_tools=allowed_tool_names(),
        disallowed_tools=list(DISALLOWED_BUILTIN_TOOLS),
        mcp_servers={SERVER_NAME: server},
        strict_mcp_config=True,  # 設定ファイル由来の MCP サーバーを読ませない
        max_turns=limits.max_turns,
        setting_sources=[],  # 設定ファイル・CLAUDE.md を読ませない
        cwd=cwd,
        **kwargs,
    )


async def _invoke_sdk(prompt: str, options: ClaudeAgentOptions, handlers: dict[str, Handler]) -> AsyncIterator[Any]:
    """Agent SDK の query() を呼ぶ。ユニットテストはこの関数を patch する。

    handlers はここでは使わない。テストの偽物が、台本どおりにハンドラーを呼ぶために受け取る。
    """
    async with contextlib.aclosing(_sdk_query(prompt=prompt, options=options)) as stream:
        async for message in stream:
            yield message


async def _receive(
    prompt: str,
    options: ClaudeAgentOptions,
    handlers: dict[str, Handler],
    observed: list[str],
) -> ResultMessage | None:
    """最初の ResultMessage を返す（orchestrator の _query_claude_sync と同じ。最後まで回すと結果を失う）。

    自分で定義した 5 個以外のツールが見えたら、その場で中断する（_ToolIsolationError）。見るのは、
    起動時のツール一覧（初期化のシステムメッセージ）と、途中で流れてくるツールの呼び出し。
    """
    async with contextlib.aclosing(_invoke_sdk(prompt, options, handlers)) as stream:
        async for message in stream:
            if isinstance(message, SystemMessage):
                data = message.data if isinstance(message.data, dict) else {}
                # 一覧が無い・list でないときは、確かめようがないので続行する
                if message.subtype == "init" and isinstance(data.get("tools"), list):
                    _abort_on_unexpected_tools(data["tools"])
            elif isinstance(message, AssistantMessage):
                names = [block.name for block in message.content if isinstance(block, ToolUseBlock)]
                observed.extend(names)
                _abort_on_unexpected_tools(names)
            elif isinstance(message, ResultMessage):
                return message
    return None


def _abort_on_unexpected_tools(names: list) -> None:
    """自分で定義した 5 個以外の名前があれば、警告を 1 行出して中断する。"""
    allowed = allowed_tool_names()
    unexpected = [name for name in names if name not in allowed]
    if not unexpected:
        return
    # ログに出すのは、英数字と下線だけの名前（_MAX_LOGGED_TOOL_NAMES 件まで）。それ以外は <other> とする
    shown = [
        name if isinstance(name, str) and _TOOL_NAME_PATTERN.fullmatch(name) else "<other>" for name in unexpected
    ]
    logger.warning(
        "Diagnosis aborted: found tools outside the custom tools: %s",
        ", ".join(list(dict.fromkeys(shown))[:_MAX_LOGGED_TOOL_NAMES]),
    )
    raise _ToolIsolationError


async def _receive_with_timeout(
    options: ClaudeAgentOptions,
    handlers: dict[str, Handler],
    observed: list[str],
    timeout_seconds: float,
) -> ResultMessage | None:
    async def _guarded() -> ResultMessage | None:
        try:
            return await _receive(USER_PROMPT, options, handlers, observed)
        except _ToolIsolationError:
            raise
        except Exception as e:
            raise _SdkStreamError from e

    return await asyncio.wait_for(_guarded(), timeout=timeout_seconds)


def _decide_stop(
    session: ToolSession | None,
    result_message: ResultMessage | None,
    *,
    timed_out: bool,
    sdk_failed: bool,
    setup_failed: bool,
    isolation_failed: bool,
) -> tuple[str, str]:
    """終了理由を決める。is_error と subtype の組み合わせに頼らず、提出の有無を先に見る。"""
    # 隔離が破れた実行の結果は信頼しない（提出が受理されていても、中断が勝つ）
    if isolation_failed:
        return "failed", "tool_isolation_failed"
    if setup_failed:
        return "failed", "setup_error"
    if session is not None and session.has_submission:
        return "completed", "submitted"
    if timed_out:
        return "incomplete", "timeout"
    subtype = getattr(result_message, "subtype", None)
    if subtype == "error_max_turns":
        return "incomplete", "max_turns"
    if subtype == "error_max_budget_usd":
        return "incomplete", "max_budget"
    if sdk_failed or result_message is None or result_message.is_error:
        return "failed", "sdk_error"
    return "incomplete", "no_submission"


def _usage_of(result_message: ResultMessage | None) -> dict[str, int]:
    """ResultMessage からトークン数を取る。合計はキャッシュのぶんを足す（orchestrator の _accumulate_cost と同じ）。"""
    usage = getattr(result_message, "usage", None)
    if not isinstance(usage, dict):
        return dict(_EMPTY_USAGE)
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
    except (TypeError, ValueError):
        return dict(_EMPTY_USAGE)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens + cache_creation + cache_read,
    }


def _cost_of(result_message: ResultMessage | None) -> float | None:
    cost = getattr(result_message, "total_cost_usd", None)
    if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(cost):
        return None
    return float(cost)


def _completion_payload(run: DiagnosisRun, trigger: str) -> dict[str, Any]:
    """diagnosis_completed の payload。自由記述・ツールの引数・記録の中身は入れない。"""
    payload: dict[str, Any] = {
        "trigger": trigger,
        "status": run.status,
        "stop_detail": run.stop_detail,
        "failed_stage": run.diagnosis.failed_stage,
        "cause_category": run.diagnosis.cause_category,
        "cause_nature": run.diagnosis.cause_nature,
        "confidence": run.diagnosis.confidence,
        "action_type": run.diagnosis.action_type,
        "valid_evidence_count": len(run.diagnosis.evidence),
        "invalid_evidence_count": run.diagnosis.invalid_evidence_count,
        "tool_sequence": [call.tool for call in run.trace],
        "tool_call_count": len(run.trace),
        "num_turns": run.num_turns,
        "duration_ms": run.duration_ms,
        "input_tokens": run.usage.get("input_tokens", 0),
        "output_tokens": run.usage.get("output_tokens", 0),
        "total_tokens": run.usage.get("total_tokens", 0),
        "handoff": run.handoff.to_dict(),
    }
    if run.total_cost_usd is not None:
        # DynamoDB は float を受け付けない
        payload["total_cost_usd"] = Decimal(str(round(run.total_cost_usd, 6)))
    return payload


def _emit_completion(run: DiagnosisRun, trigger: str, emitter: object | None) -> None:
    """終了のイベントを 1 件残す（best-effort）。"""
    try:
        if emitter is None:
            if _EventEmitter is None:
                return
            emitter = _EventEmitter(run.execution_id)
        payload = _completion_payload(run, trigger)
        if trigger == "auto":
            emitter.emit(EVENT_TYPE, payload, status_at_emit="failed")
        else:
            emitter.emit(EVENT_TYPE, payload)
    except Exception:
        logger.warning("Failed to emit %s (non-fatal)", EVENT_TYPE, exc_info=True)


def _log_completion(run: DiagnosisRun, trigger: str) -> None:
    # ECS の formatter は extra を出さないので、値はメッセージ本文に入れる
    logger.info(
        "Execution diagnosis finished: execution_id=%s trigger=%s status=%s stop_detail=%s cause_category=%s "
        "confidence=%s tool_call_count=%d num_turns=%s duration_ms=%d total_tokens=%d",
        run.execution_id or "<invalid>",
        trigger,
        run.status,
        run.stop_detail,
        run.diagnosis.cause_category,
        run.diagnosis.confidence,
        len(run.trace),
        run.num_turns,
        run.duration_ms,
        run.usage.get("total_tokens", 0),
    )


def diagnose_execution(
    execution_id: str,
    *,
    reader: RecordReader | None = None,
    limits: DiagnosisLimits | None = None,
    model: str | None = None,
    emitter: object | None = None,
    trigger: str = "manual",
    clock: Callable[[], float] = time.monotonic,
    code_root: str | os.PathLike[str] | None = None,
) -> DiagnosisRun:
    """実行 ID を 1 つ受け取り、その実行の記録を調べて診断結果と足取りを返す。例外を出さない。

    Args:
        execution_id: 診断する実行の ID。形式が不正なら、記録にも SDK にも触れずに failed を返す。
        reader: 記録の読み取り。None なら環境変数と boto3 から組み立てる。
        limits: 上限。None なら既定値。
        model: モデル名。None なら環境変数 DIAGNOSIS_MODEL、それも無ければ sonnet。
        emitter: emit(event_type, payload, status_at_emit=...) を持つもの。None なら EventEmitter を使う。
        trigger: "manual"（手動）または "auto"（失敗時の自動起動）。
        clock: 所要時間の計測に使う時計。
        code_root: スタックトレースのフレームを突き合わせる「コードの根」（/app/ に当たるディレクトリ）。
            None なら diagnosis パッケージの親ディレクトリ。

    Returns:
        DiagnosisRun。status は completed（提出を受理）/ incomplete（上限に達して提出なし）/
        failed（SDK の失敗、準備の失敗、5 個以外のツールの検出、実行 ID の形式が不正）。
    """
    started = clock()
    limits = limits or DiagnosisLimits()
    trigger = trigger if trigger in _TRIGGERS else "manual"

    if not _is_valid_execution_id(execution_id):
        # 検証に通らなかった値は、結果にもログにも残さない
        diagnosis = Diagnosis.unknown()
        run = DiagnosisRun(
            execution_id="",
            status="failed",
            stop_detail="invalid_execution_id",
            diagnosis=diagnosis,
            handoff=build_handoff("", diagnosis),
            trace=[],
            observed_tool_uses=[],
            num_turns=None,
            duration_ms=round((clock() - started) * 1000),
            usage=dict(_EMPTY_USAGE),
            total_cost_usd=None,
        )
        _log_completion(run, trigger)
        return run

    session: ToolSession | None = None
    observed: list[str] = []
    result_message: ResultMessage | None = None
    timed_out = sdk_failed = setup_failed = isolation_failed = False
    cwd: str | None = None
    try:
        # SDK を呼ぶ前の準備。ここでの失敗は setup_error とし、SDK の失敗（sdk_error）と分ける
        if reader is None:
            reader = _build_default_reader(execution_id)
        # 制限時間を、診断全体の期限としてツールにも渡す（期限を過ぎてから始まる呼び出しは、記録を読まない）
        deadline = started + limits.timeout_seconds
        session = ToolSession(reader, limits, clock=clock, code_root=code_root, deadline=deadline)
        handlers = build_handlers(session)
        server = build_server(handlers)
        # 万一ファイルを読むツールが残っていても読めるものを無くすため、空のディレクトリで動かす
        cwd = tempfile.mkdtemp(prefix="diagnosis-")
        options = build_options(
            model=model or os.environ.get("DIAGNOSIS_MODEL") or DEFAULT_MODEL,
            limits=limits,
            server=server,
            cwd=cwd,
        )
    except Exception:
        setup_failed = True
        logger.warning("Execution diagnosis could not be set up (non-fatal)", exc_info=True)
    else:
        try:
            result_message = asyncio.run(_receive_with_timeout(options, handlers, observed, limits.timeout_seconds))
        except _ToolIsolationError:
            isolation_failed = True
        except TimeoutError:
            timed_out = True
            logger.warning("Execution diagnosis timed out after %s seconds", limits.timeout_seconds)
        except Exception:
            sdk_failed = True
            logger.warning("Execution diagnosis failed (non-fatal)", exc_info=True)
    finally:
        if cwd is not None:
            shutil.rmtree(cwd, ignore_errors=True)

    # 5 個以外のツールが見えた実行の診断結果は使わない（それまでの足取りだけを残す）
    diagnosis = session.finalize() if session is not None and not isolation_failed else Diagnosis.unknown()
    status, stop_detail = _decide_stop(
        session,
        result_message,
        timed_out=timed_out,
        sdk_failed=sdk_failed,
        setup_failed=setup_failed,
        isolation_failed=isolation_failed,
    )
    num_turns = getattr(result_message, "num_turns", None)
    run = DiagnosisRun(
        execution_id=execution_id,
        status=status,
        stop_detail=stop_detail,
        diagnosis=diagnosis,
        handoff=build_handoff(execution_id, diagnosis),
        trace=list(session.trace) if session is not None else [],
        observed_tool_uses=observed,
        num_turns=num_turns if isinstance(num_turns, int) else None,
        duration_ms=round((clock() - started) * 1000),
        usage=_usage_of(result_message),
        total_cost_usd=_cost_of(result_message),
    )
    _emit_completion(run, trigger, emitter)
    _log_completion(run, trigger)
    return run
