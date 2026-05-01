import logging
import os
from typing import Any

import boto3
import jwt

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")

# JWT 署名キーはモジュールレベルでキャッシュ (同一 execution environment で再利用)
_jwt_signing_key: str | None = None


def _get_jwt_signing_key() -> str:
    global _jwt_signing_key
    if _jwt_signing_key is None:
        arn = os.environ["JWT_KEY_SECRET_ARN"]
        response = secrets_client.get_secret_value(SecretId=arn)
        _jwt_signing_key = response["SecretString"]
    return _jwt_signing_key


def _parse_session_token(cookies: list[str]) -> str | None:
    """HTTP API 2.0 payload の cookies リスト (["name=value", ...]) から session cookie を抽出。

    API GW が Cookie ヘッダを "; " で分割してリストに格納するため、
    各エントリを "=" で分割して名前を比較する。
    """
    for entry in cookies:
        for part in entry.split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip() == "session":
                return value.strip()
    return None


def lambda_handler(event: dict, context: object) -> dict[str, Any]:
    """HTTP API Lambda Authorizer (simple response format)。

    - cookie の `session` 値を JWT として検証
    - 成功: {"isAuthorized": True, "context": {"user_sub": ..., "user_name": ...}}
    - 失敗: {"isAuthorized": False}
    """
    cookies: list[str] = event.get("cookies") or []
    token = _parse_session_token(cookies)
    if not token:
        return {"isAuthorized": False}

    try:
        signing_key = _get_jwt_signing_key()
        claims = jwt.decode(token, signing_key, algorithms=["HS256"])
        return {
            "isAuthorized": True,
            "context": {
                "user_sub": claims["sub"],
                "user_name": claims["name"],
            },
        }
    except jwt.PyJWTError as e:
        logger.info("JWT validation failed: %s", type(e).__name__)
        return {"isAuthorized": False}
