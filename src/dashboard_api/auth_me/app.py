import logging

from _common import json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event: dict, context: object) -> dict:
    """Lambda Authorizer が検証済みの JWT コンテキストからセッション情報を返す。"""
    auth_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    return json_response(200, {
        "user_sub": auth_ctx.get("user_sub"),
        "user_name": auth_ctx.get("user_name"),
        "expires_at": auth_ctx.get("exp"),
    })
