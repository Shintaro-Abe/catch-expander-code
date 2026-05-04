"""構造化イベントを events DDB テーブルに書き込む best-effort emitter。

設計詳細: `.steering/20260430-workflow-observability/design.md` §7
仕様確定: T0-7 で execution_id 整合性確認、token_monitor は合成 ID 使用 (§2.5)

使い方:
    from src.observability import EventEmitter

    emitter = EventEmitter(execution_id)
    emitter.emit("workflow_planned", {
        "topic_category": "技術",
        "expected_deliverable_type": "iac_code",
        "planned_subagents": ["researcher", "generator", "reviewer"],
        "planning_summary": "Terraform で Route 53 Resolver を IaC...",
    })

設計上の不変条件:
- 書き込み失敗は logging.error で記録するのみ。例外は呼び出し元へ伝播させない
  (orchestrator / Lambda Trigger の主要パスをブロックしない best-effort 設計)
- EVENTS_TABLE 環境変数が未設定でも import エラーにならない
  (graceful skip。dev 環境やテストで env 未設定でも既存処理を阻害しない)
- sequence_number はインスタンス内で単調増加 (同 ms に複数 emit が重なっても順序保持)
- TTL は emit 時刻から 90 日後 (events テーブル仕様 §2.1)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import boto3

logger = logging.getLogger(__name__)

_TTL_DAYS = 90


class EventEmitter:
    """1 つの execution_id に紐づくイベントを events DDB に書き込む emitter。

    Attributes:
        execution_id: 書き込むイベントの execution_id (PK)。
            通常実行: ``exec-{datetime}-{uuid}`` (Lambda Trigger 発番)
            token_monitor: ``system-token-refresh-{epoch}`` (合成 ID、design.md §2.5)
    """

    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        self._sequence = 0
        # 書き込み先テーブル名は環境変数から解決 (未設定なら空文字 → emit が graceful skip)
        self._table_name = os.environ.get("EVENTS_TABLE", "")
        # boto3 リソースはインスタンスごとに生成。Lambda 環境では呼出 1 回ごとの
        # 軽量な操作のため、グローバルキャッシュは行わない (テスト容易性優先)。
        self._dynamodb = boto3.resource("dynamodb") if self._table_name else None

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        status_at_emit: str = "in_progress",
    ) -> None:
        """1 件のイベントを events テーブルに書き込む (best-effort)。

        Args:
            event_type: イベント種別 (例: "workflow_planned", "review_completed")。
                許容値は AC-7 / Tier 1/2 拡張で定義された種別。
            payload: イベント固有の構造化データ (event_type 別スキーマは design.md §2.3)。
            status_at_emit: 観測時点の execution status。デフォルト "in_progress"。
                "success" / "failed" / "partial" を `execution_completed` 等で渡す。

        Returns:
            None。書き込み成功/失敗は呼出元には伝えない (best-effort)。

        Note:
            EVENTS_TABLE が未設定の環境 (dev / 単体テスト等) では何もしない。
            DDB 書き込み失敗は logger.error で記録するのみ。
        """
        if not self._table_name or self._dynamodb is None:
            logger.warning("EVENTS_TABLE env var not set, skipping event %s", event_type)
            return

        self._sequence += 1
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        item = {
            "execution_id": self.execution_id,
            "sk": f"{timestamp}#{self._sequence:05d}",
            "event_type": event_type,
            "timestamp": timestamp,
            "sequence_number": self._sequence,
            "status_at_emit": status_at_emit,
            "payload": payload,
            "ttl": int(time.time()) + _TTL_DAYS * 86400,
            "gsi_pk": "GLOBAL",
        }
        try:
            self._dynamodb.Table(self._table_name).put_item(Item=item)
        except Exception as e:  # noqa: BLE001
            # best-effort: イベント書き込み失敗で業務処理をブロックしない。
            # payload は logger に出さない (PII / 機密含む可能性。NFR-3 / design.md §4.6)。
            logger.error(
                "Failed to emit event %s for %s: %s",
                event_type,
                self.execution_id,
                e,
            )


# ---------------------------------------------------------------------------
# T1-2b 共通ヘルパー (Tier 1.2): API call 観測
# ---------------------------------------------------------------------------
#
# storage/notify クライアント (NotionClient / GitHubClient / SlackClient) から
# 同じ payload 形で emit するための共通ヘルパー。各クライアントの
# `_request_with_retry` / `_post_with_retry` の終端で呼ぶ想定。
#
# emitter が None の場合は no-op (orchestrator が emitter を伝搬していない
# ユニットテスト経路でも安全に呼べる)。


def emit_api_call_completed(
    emitter: EventEmitter | None,
    *,
    subtype: str,
    success: bool,
    duration_ms: int,
    response_status_code: int | None,
    endpoint_path: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
) -> None:
    """外部 API 呼び出しの終了を `api_call_completed` イベントとして emit する (Tier 1.2)。

    Args:
        emitter: orchestrator が伝搬した EventEmitter インスタンス。
            None の場合は no-op (テスト/ECS 未配置経路で安全)。
        subtype: "notion" / "github" / "slack" / "anthropic" 等の API 種別。
        success: HTTP 2xx で成功とみなしたか。
        duration_ms: API 呼び出し所要時間 (ms)。retry 含む全所要時間。
        response_status_code: 最終 HTTP status code。例外で取得不能なら None。
        endpoint_path: 呼び出した URL の path 部 (host + ?query は除く想定)。
        input_tokens: この呼び出しの入力トークン数 (Anthropic 系のみ)。
        output_tokens: この呼び出しの出力トークン数 (Anthropic 系のみ)。
        total_tokens: input + output + cache の合計トークン数。
    """
    if emitter is None:
        return
    payload: dict = {
        "subtype": subtype,
        "success": success,
        "duration_ms": duration_ms,
        "response_status_code": response_status_code,
        "endpoint_path": endpoint_path,
    }
    if input_tokens is not None:
        payload["input_tokens"] = input_tokens
    if output_tokens is not None:
        payload["output_tokens"] = output_tokens
    if total_tokens is not None:
        payload["total_tokens"] = total_tokens
    emitter.emit("api_call_completed", payload)


def emit_rate_limit_hit(
    emitter: EventEmitter | None,
    *,
    subtype: str,
    endpoint_path: str,
    retry_after_seconds: int | None = None,
    detail: str | None = None,
) -> None:
    """rate limit 検出を `rate_limit_hit` イベントとして emit する (Tier 1.2)。

    Args:
        emitter: orchestrator が伝搬した EventEmitter インスタンス。None なら no-op。
        subtype: "anthropic_429" / "slack_rate_limit" / "github_429" /
            "cloudflare_block" 等の検出元種別。
        endpoint_path: rate limit を返した URL の path 部。
        retry_after_seconds: ``Retry-After`` ヘッダ値 (取得できれば)。
        detail: 補助情報 (例: Cloudflare CF-Ray、Slack `error` 文字列)。
    """
    if emitter is None:
        return
    payload: dict[str, Any] = {
        "subtype": subtype,
        "endpoint_path": endpoint_path,
    }
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = retry_after_seconds
    if detail is not None:
        payload["detail"] = detail
    emitter.emit("rate_limit_hit", payload)
