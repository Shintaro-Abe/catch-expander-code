# PoC 品質 - 本番利用前にセキュリティレビューおよび設定の見直しを行ってください
#
# このハンドラは ADOT Lambda Layer の自動計装によってトレースが取得される。
# AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument が設定されている場合、
# SDK の初期化（TracerProvider, Propagator 設定）は Layer 側が行うため
# ハンドラ内での OTel 初期化コードは不要。
#
# 手動でカスタムスパンやスパン属性を追加したい場合は
# opentelemetry-api パッケージをインポートして使用する（Layer に同梱済み）。

import json
import logging
import os
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# グローバルトレーサーの取得（ADOT Layer が TracerProvider を初期化済み）
tracer = trace.get_tracer(__name__)

# コールドスタートフラグ
_cold_start = True


def lambda_handler(event: dict, context: Any) -> dict:
    """
    API Gateway (REST API) プロキシ統合ハンドラ。
    ADOT Layer の自動計装により:
      - Lambda 関数全体のルートスパンが自動生成される
      - X-Amzn-Trace-Id ヘッダから API Gateway トレースコンテキストが伝播される
      - W3C traceparent ヘッダにも対応（OTEL_PROPAGATORS=tracecontext,baggage,xray）
    """
    global _cold_start

    http_method = event.get("httpMethod", "UNKNOWN")
    path = event.get("path", "/")

    # カスタムビジネスロジックスパンの追加例
    with tracer.start_as_current_span(
        f"{http_method} {path}",
        kind=SpanKind.SERVER,
        attributes={
            # FaaS セマンティック規約 (SemConv v1.40.0)
            "faas.trigger": "http",
            "faas.invocation_id": context.aws_request_id,
            "faas.coldstart": _cold_start,
            # HTTP セマンティック規約
            "http.method": http_method,
            "http.route": path,
            "http.user_agent": (event.get("headers") or {}).get("User-Agent", ""),
        },
    ) as span:
        _cold_start = False

        try:
            response = _route(event, context, span)
            span.set_status(StatusCode.OK)
            return response
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            logger.exception("Unhandled error")
            return _error_response(500, "Internal server error")


def _route(event: dict, context: Any, parent_span: Any) -> dict:
    http_method = event.get("httpMethod", "")
    path = event.get("path", "")

    if path.startswith("/items"):
        if http_method == "GET":
            return _get_items(event, parent_span)
        if http_method == "POST":
            return _post_item(event, parent_span)

    return _error_response(404, f"Not found: {http_method} {path}")


def _get_items(event: dict, parent_span: Any) -> dict:
    """GET /items - アイテム一覧取得（スケルトン実装）"""
    with tracer.start_as_current_span(
        "db.query.items",
        attributes={
            "db.system": "dynamodb",  # 実際の DB に合わせて変更
            "db.operation": "Scan",
        },
    ):
        # TODO: 実際のデータ取得ロジックをここに実装
        items = [
            {"id": "1", "name": "item-alpha"},
            {"id": "2", "name": "item-beta"},
        ]

    parent_span.set_attribute("items.count", len(items))
    return _ok_response({"items": items, "count": len(items)})


def _post_item(event: dict, parent_span: Any) -> dict:
    """POST /items - アイテム作成（スケルトン実装）"""
    body_raw = event.get("body") or "{}"
    try:
        body = json.loads(body_raw)
    except json.JSONDecodeError:
        return _error_response(400, "Invalid JSON body")

    item_name = body.get("name", "").strip()
    if not item_name:
        return _error_response(400, "'name' is required")

    with tracer.start_as_current_span(
        "db.put.item",
        attributes={
            "db.system": "dynamodb",
            "db.operation": "PutItem",
        },
    ):
        # TODO: 実際の永続化ロジックをここに実装
        new_id = "generated-uuid"

    parent_span.set_attribute("item.id", new_id)
    parent_span.set_attribute("item.name", item_name)

    return _ok_response({"id": new_id, "name": item_name}, status_code=201)


def _ok_response(body: dict, status_code: int = 200) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _error_response(status_code: int, message: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }
