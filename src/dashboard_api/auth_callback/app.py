import json
import logging
import os
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3
import jwt

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_secrets_client = boto3.client("secretsmanager")

_slack_config: dict | None = None
_jwt_signing_key: str | None = None

_JWT_TTL_SEC = 24 * 60 * 60
_SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"  # noqa: S105
_SLACK_USERINFO_URL = "https://slack.com/api/openid.connect.userInfo"


def _get_slack_config() -> dict:
    global _slack_config
    if _slack_config is None:
        arn = os.environ["SLACK_OAUTH_SECRET_ARN"]
        raw = _secrets_client.get_secret_value(SecretId=arn)["SecretString"]
        _slack_config = json.loads(raw)
    return _slack_config


def _get_jwt_signing_key() -> str:
    global _jwt_signing_key
    if _jwt_signing_key is None:
        arn = os.environ["JWT_KEY_SECRET_ARN"]
        _jwt_signing_key = _secrets_client.get_secret_value(SecretId=arn)["SecretString"]
    return _jwt_signing_key


def _slack_post(url: str, data: dict, token: str | None = None) -> dict:
    payload = urlencode(data).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=payload, headers=headers, method="POST")  # noqa: S310
    with urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def _error(status: int, message: str) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message})}


def lambda_handler(event: dict, context: object) -> dict:
    params = event.get("queryStringParameters") or {}
    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        return _error(400, "missing_params")

    # state 検証 (CSRF 対策)
    table = _dynamodb.Table(os.environ["OAUTH_STATE_TABLE"])
    result = table.get_item(Key={"state": state})
    if "Item" not in result:
        return _error(400, "invalid_state")
    # 単一回限り削除 (T1-9b でフィンガープリント検証を追加)
    table.delete_item(Key={"state": state})

    config = _get_slack_config()
    host = (event.get("headers") or {}).get("host") or event["requestContext"]["domainName"]
    redirect_uri = f"https://{host}/api/v1/auth/callback"

    try:
        token_resp = _slack_post(
            _SLACK_TOKEN_URL,
            {
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    except URLError as e:
        logger.error("Slack token exchange network error: %s", e)
        return _error(502, "slack_unavailable")

    if not token_resp.get("ok"):
        logger.error("Slack token exchange failed: %s", token_resp.get("error"))
        return _error(502, "slack_token_exchange_failed")

    access_token = token_resp.get("authed_user", {}).get("access_token")
    if not access_token:
        logger.error("No authed_user.access_token in Slack response")
        return _error(502, "no_access_token")

    try:
        user_resp = _slack_post(_SLACK_USERINFO_URL, {}, token=access_token)
    except URLError as e:
        logger.error("Slack userInfo network error: %s", e)
        return _error(502, "slack_unavailable")

    if not user_resp.get("ok"):
        logger.error("Slack userInfo failed: %s", user_resp.get("error"))
        return _error(502, "slack_userinfo_failed")

    team_id = user_resp.get("https://slack.com/team_id")
    if team_id != config.get("workspace_id"):
        logger.warning("Workspace mismatch: got=%s", team_id)
        return _error(401, "workspace_mismatch")

    sub = user_resp.get("sub", "")
    name = user_resp.get("name", "")
    now = int(time.time())
    exp = now + _JWT_TTL_SEC
    session_token = jwt.encode({"sub": sub, "name": name, "exp": exp}, _get_jwt_signing_key(), algorithm="HS256")

    cookie = f"session={session_token}; HttpOnly; Secure; SameSite=Lax; Max-Age={_JWT_TTL_SEC}; Path=/"
    return {
        "statusCode": 302,
        "headers": {
            "Location": f"https://{host}/",
            "Set-Cookie": cookie,
        },
        "body": "",
    }
