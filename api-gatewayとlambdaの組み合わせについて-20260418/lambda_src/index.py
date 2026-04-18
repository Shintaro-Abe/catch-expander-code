# PoC Quality — skeletal implementation only
# Replace handler logic with real business code before any production use.

import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event, context):
    """Lambda proxy integration handler.

    API Gateway passes the full request as `event` and expects a response
    dict with statusCode, headers, and body (string).
    See: https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html
    """
    http_method = event.get("httpMethod", "")
    path = event.get("path", "/")
    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}
    body_raw = event.get("body") or "{}"

    logger.info({
        "requestId": context.aws_request_id,
        "method": http_method,
        "path": path,
        "pathParams": path_params,
        "queryParams": query_params,
    })

    try:
        body = json.loads(body_raw) if body_raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body"})

    # --- Route dispatch skeleton ---
    if path == "/items" and http_method == "GET":
        return _response(200, {"items": [], "message": "TODO: fetch items from data store"})

    if path == "/items" and http_method == "POST":
        return _response(201, {"message": "TODO: persist item", "received": body})

    if http_method == "GET" and path_params.get("itemId"):
        item_id = path_params["itemId"]
        return _response(200, {"itemId": item_id, "message": "TODO: fetch single item"})

    return _response(404, {"error": "Not found"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "X-Request-Id": "",  # populated by API Gateway via $context.requestId mapping if needed
        },
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }