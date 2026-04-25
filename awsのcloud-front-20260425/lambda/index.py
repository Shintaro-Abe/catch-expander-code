# PoC品質 - 本番利用前に十分なレビューとセキュリティ評価を実施してください
#
# API Gateway Lambda プロキシ統合ハンドラー
# CloudFront → API Gateway → Lambda の統合を示すスケルトン実装
#
# ルーティング:
#   GET    /items        → list_items()
#   POST   /items        → create_item()
#   GET    /items/{id}   → get_item()
#   PUT    /items/{id}   → update_item()
#   DELETE /items/{id}   → delete_item()

import json
import logging
import os
from typing import Any

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event: dict, context: Any) -> dict:
    """API Gateway プロキシ統合のメインハンドラー。"""
    logger.info("Event: %s", json.dumps(event, default=str))

    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    path_params = event.get("pathParameters") or {}

    try:
        # ルーティング
        if resource == "/items":
            if http_method == "GET":
                return list_items(event)
            if http_method == "POST":
                return create_item(event)

        if resource == "/items/{id}":
            item_id = path_params.get("id")
            if http_method == "GET":
                return get_item(item_id, event)
            if http_method == "PUT":
                return update_item(item_id, event)
            if http_method == "DELETE":
                return delete_item(item_id, event)

        return _response(404, {"message": f"Route not found: {http_method} {resource}"})

    except ValueError as exc:
        logger.warning("Validation error: %s", exc)
        return _response(400, {"message": str(exc)})
    except Exception as exc:
        logger.exception("Unhandled error")
        return _response(500, {"message": "Internal server error"})


# ------------------------------------------------------------------ #
# ルートハンドラー (スケルトン)
# ------------------------------------------------------------------ #

def list_items(event: dict) -> dict:
    query = event.get("queryStringParameters") or {}
    # TODO: データストア (DynamoDB 等) からアイテム一覧を取得
    items = [
        {"id": "1", "name": "Item 1"},
        {"id": "2", "name": "Item 2"},
    ]
    return _response(200, {"items": items, "query": query})


def create_item(event: dict) -> dict:
    body = _parse_body(event)
    if not body.get("name"):
        raise ValueError("'name' is required")
    # TODO: データストアへの書き込み
    new_item = {"id": "new-id-placeholder", "name": body["name"]}
    return _response(201, new_item)


def get_item(item_id: str, event: dict) -> dict:
    if not item_id:
        raise ValueError("item id is required")
    # TODO: データストアからの取得
    return _response(200, {"id": item_id, "name": f"Item {item_id}"})


def update_item(item_id: str, event: dict) -> dict:
    body = _parse_body(event)
    # TODO: データストアの更新
    return _response(200, {"id": item_id, **body})


def delete_item(item_id: str, event: dict) -> dict:
    # TODO: データストアからの削除
    return _response(204, {})


# ------------------------------------------------------------------ #
# ヘルパー
# ------------------------------------------------------------------ #

def _parse_body(event: dict) -> dict:
    """リクエストボディを JSON パース。Base64 エンコード済みの場合も処理。"""
    import base64
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc


def _response(status_code: int, body: Any) -> dict:
    """API Gateway プロキシレスポンス形式で返す。"""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            # CORS ヘッダーは API Gateway の CORS 設定で付与するため省略
        },
        "body": json.dumps(body, default=str) if body else "",
    }
