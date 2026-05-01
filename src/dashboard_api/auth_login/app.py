import hashlib
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_secrets_client = boto3.client("secretsmanager")

_slack_config: dict | None = None

_OAUTH_STATE_TTL_SEC = 10 * 60


def _get_slack_config() -> dict:
    global _slack_config
    if _slack_config is None:
        arn = os.environ["SLACK_OAUTH_SECRET_ARN"]
        raw = _secrets_client.get_secret_value(SecretId=arn)["SecretString"]
        _slack_config = json.loads(raw)
    return _slack_config


def _fingerprint(event: dict) -> str:
    """IP + User-Agent の SHA-256 ハッシュ (S3 フィンガープリントバインド)。"""
    ip = (event.get("requestContext") or {}).get("http", {}).get("sourceIp", "")
    ua = (event.get("headers") or {}).get("user-agent", "")
    return hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()


def lambda_handler(event: dict, context: object) -> dict:
    config = _get_slack_config()
    state = secrets.token_urlsafe(32)

    table = _dynamodb.Table(os.environ["OAUTH_STATE_TABLE"])
    table.put_item(Item={
        "state": state,
        "fingerprint": _fingerprint(event),
        "ttl": int(time.time()) + _OAUTH_STATE_TTL_SEC,
    })

    # Host ヘッダを使うことで CloudFront 経由 / 直接 API GW の両方に対応
    host = (event.get("headers") or {}).get("host") or event["requestContext"]["domainName"]
    redirect_uri = f"https://{host}/api/v1/auth/callback"

    params = urlencode({
        "client_id": config["client_id"],
        "scope": "openid profile email",
        "state": state,
        "redirect_uri": redirect_uri,
    })
    return {
        "statusCode": 302,
        "headers": {"Location": f"https://slack.com/oauth/v2/authorize?{params}"},
        "body": "",
    }
