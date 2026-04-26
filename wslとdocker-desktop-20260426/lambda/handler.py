# PoC品質: このコードは概念実証用のスケルトンです。本番環境での使用前に十分なレビューを行ってください。
"""
Lambda ハンドラー関数
API Gateway プロキシ統合形式（event/context）に対応

ローカルテスト（SAM CLI + Docker Desktop WSL2）:
  sam local invoke HelloFunction -e events/hello_event.json \
      -t cdk.out/WslDockerLambdaStack.template.json
  sam local invoke ItemsFunction -e events/items_event.json \
      -t cdk.out/WslDockerLambdaStack.template.json
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _response(status_code: int, body: Any, headers: dict | None = None) -> dict:
    """API Gateway プロキシ統合レスポンスを組み立てる"""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            **(headers or {}),
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def _parse_body(event: dict) -> dict:
    """リクエストボディを JSON パースして返す。失敗時は ValueError を送出する"""
    raw = event.get("body") or "{}"
    if isinstance(raw, str):
        return json.loads(raw)
    return raw  # SAM CLI はすでに dict を渡す場合がある


# ---------------------------------------------------------------------------
# GET /hello
# ---------------------------------------------------------------------------

def hello(event: dict, context: Any) -> dict:
    """
    シンプルな疎通確認エンドポイント。
    クエリ文字列パラメータ `name` が指定された場合はパーソナライズしたメッセージを返す。
    """
    logger.info("hello called: requestId=%s", context.aws_request_id if context else "local")
    logger.debug("event: %s", json.dumps(event))

    query_params: dict = event.get("queryStringParameters") or {}
    name = query_params.get("name", "World")

    payload = {
        "message": f"Hello, {name}!",
        "stage": os.getenv("STAGE", "dev"),
        "runtime": "python3.12",
        "environment": "WSL2 + Docker Desktop",
    }
    return _response(200, payload)


# ---------------------------------------------------------------------------
# POST /items
# ---------------------------------------------------------------------------

def create_item(event: dict, context: Any) -> dict:
    """
    新規アイテムを作成するエンドポイント（PoC のためストレージは省略）。
    リクエストボディに `name` フィールドが必須。
    """
    logger.info("create_item called: requestId=%s", context.aws_request_id if context else "local")
    logger.debug("event: %s", json.dumps(event))

    try:
        body = _parse_body(event)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON body: %s", exc)
        return _response(400, {"error": "Invalid JSON in request body"})

    name = body.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return _response(400, {"error": "'name' field is required and must be a non-empty string"})

    # PoC: 実際のストレージ処理（DynamoDB 等）はここに実装する
    item = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "description": body.get("description", ""),
        "stage": os.getenv("STAGE", "dev"),
    }

    logger.info("Item created: id=%s name=%s", item["id"], item["name"])
    return _response(201, {"item": item}, headers={"Location": f"/items/{item['id']}"})
