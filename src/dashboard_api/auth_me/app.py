import json
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event: dict, context: object) -> dict:
    """Lambda Authorizer が検証済みの JWT コンテキストからセッション情報を返す。"""
    auth_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    return {
        "statusCode": 200,
        "body": json.dumps({
            "user_sub": auth_ctx.get("user_sub"),
            "user_name": auth_ctx.get("user_name"),
            "expires_at": auth_ctx.get("exp"),
        }),
    }
