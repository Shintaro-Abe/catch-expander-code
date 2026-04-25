import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")

# AS OF 2026-04-25: Anthropic OAuth エンドポイント / Claude Code CLI の公開クライアント ID
# 仕様変更で動かなくなった場合はここを更新する単一の差し替え点
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"

# 残り 1 時間以下なら refresh をトリガー（12h 実行間隔と整合）
REFRESH_BUFFER_MS = 60 * 60 * 1000

# refresh API レスポンスに expires_in が無い場合のフォールバック（Anthropic OAuth の典型値 8h）
DEFAULT_EXPIRES_IN_SEC = 8 * 60 * 60


def _get_secret(arn: str) -> str:
    response = secrets_client.get_secret_value(SecretId=arn)
    return response["SecretString"]


def _put_secret(arn: str, value: str) -> None:
    secrets_client.put_secret_value(SecretId=arn, SecretString=value)


def _parse_credentials(raw: str) -> dict[str, Any]:
    """Secrets Manager から取得した JSON 文字列を dict にパース。失敗時は空 dict。"""
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            logger.error("Credentials JSON is not an object")
            return {}
        return parsed
    except json.JSONDecodeError as e:
        logger.error("Failed to parse credentials JSON: %s", e)
        return {}


def _needs_refresh(expires_at_ms: int, buffer_ms: int, now_ms: int) -> bool:
    """残り時間が buffer 以下、または既に失効していれば True。"""
    return now_ms + buffer_ms >= expires_at_ms


def _call_refresh_endpoint(refresh_token: str) -> dict[str, Any]:
    """Anthropic OAuth エンドポイントへ refresh_token を投げて新トークンを取得。

    成功時は access_token / refresh_token / expires_in / scope を含む dict を返す。
    HTTP 4xx/5xx は HTTPError、ネットワーク到達不能は URLError を伝播する。

    User-Agent ヘッダは必須: 既定の Python urllib UA は Cloudflare Bot Management に
    Bot 判定されて 429 を返される（2026-04-25 確認）。Claude CLI 相当の UA を送る。
    """
    payload = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPES,
        }
    ).encode("utf-8")
    req = Request(
        TOKEN_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "claude-cli/2.1.118 (external, cli)",
        },
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_updated_credentials(
    old: dict[str, Any], result: dict[str, Any], now_ms: int
) -> dict[str, Any]:
    """既存 credentials 構造を保ったまま claudeAiOauth を新値で更新した dict を返す。

    レスポンスに refresh_token があれば更新、無ければ既存値を維持。
    expires_in（秒）から expiresAt（ミリ秒）を計算。
    """
    old_oauth = old.get("claudeAiOauth", {})
    new_oauth = {
        **old_oauth,
        "accessToken": result["access_token"],
        "refreshToken": result.get("refresh_token", old_oauth.get("refreshToken")),
        "expiresAt": now_ms + result.get("expires_in", DEFAULT_EXPIRES_IN_SEC) * 1000,
    }
    if "scope" in result:
        new_oauth["scopes"] = result["scope"].split(" ")
    return {**old, "claudeAiOauth": new_oauth}


def _post_slack_failure(
    slack_token: str,
    channel_id: str,
    reason: str,
    expires_at_ms: int | None,
) -> None:
    """自動延命に失敗したときのみ Slack へアラートを送る。

    Slack 側のエラーは WARN ログに留め、Lambda 終了に影響させない（最終手段の通知のため
    通知できなくても CloudWatch のメトリクスで検知できる）。
    """
    if expires_at_ms is not None:
        expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=UTC)
        expiry_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    else:
        expiry_str = "不明"

    message = (
        "🚨 Claude OAuth トークンの自動延命に失敗しました。再認証が必要です。\n"
        "\n"
        f"理由: {reason}\n"
        f"最終 expiresAt: {expiry_str}\n"
        "\n"
        "対処:\n"
        "1. ローカル PC で `claude` を起動し `/login` で再認証\n"
        "2. ~/.claude/.credentials.json の中身を Secrets Manager に投入:\n"
        "   `aws secretsmanager put-secret-value --secret-id catch-expander/claude-oauth"
        " --secret-string file://~/.claude/.credentials.json`\n"
        "\n"
        "詳細手順: .steering/20260425-auth-redesign-aipapers/initial-setup.md"
    )

    client = WebClient(token=slack_token)
    try:
        client.chat_postMessage(channel=channel_id, text=message)
        logger.info("Slack failure notification sent", extra={"reason": reason})
    except SlackApiError:
        logger.warning("Failed to send Slack failure notification", extra={"reason": reason})


def lambda_handler(event: dict, context: object) -> dict[str, Any]:
    """Claude OAuth トークンを自動延命する。

    - 残り > 1h なら何もせず終了
    - 残り <= 1h or 失効済み なら refresh_token で新トークンを取得し Secrets Manager 上書き
    - refresh が失敗したら Slack へ通知

    Returns:
        {"refreshed": bool, "reason"?: str, "new_expires_at_ms"?: int}
    """
    claude_oauth_arn = os.environ["CLAUDE_OAUTH_SECRET_ARN"]
    slack_token_arn = os.environ["SLACK_BOT_TOKEN_SECRET_ARN"]
    channel_id = os.environ["SLACK_NOTIFICATION_CHANNEL_ID"]

    raw = _get_secret(claude_oauth_arn)
    creds = _parse_credentials(raw)
    oauth = creds.get("claudeAiOauth", {}) if isinstance(creds, dict) else {}
    expires_at_ms = oauth.get("expiresAt")
    refresh_token = oauth.get("refreshToken")

    now_ms = int(time.time() * 1000)

    if not isinstance(expires_at_ms, int):
        logger.error("expiresAt missing or invalid in credentials")
        slack_token = _get_secret(slack_token_arn)
        _post_slack_failure(slack_token, channel_id, "no_expires_at", None)
        return {"refreshed": False, "reason": "no_expires_at"}

    if not _needs_refresh(expires_at_ms, REFRESH_BUFFER_MS, now_ms):
        remaining_min = (expires_at_ms - now_ms) // 60_000
        logger.info("Token still valid", extra={"remaining_min": remaining_min})
        return {"refreshed": False, "reason": "still_valid"}

    if not refresh_token:
        logger.error("No refresh_token in credentials")
        slack_token = _get_secret(slack_token_arn)
        _post_slack_failure(slack_token, channel_id, "no_refresh_token", expires_at_ms)
        return {"refreshed": False, "reason": "no_refresh_token"}

    try:
        result = _call_refresh_endpoint(refresh_token)
    except HTTPError as e:
        reason = f"http_{e.code}"
        # レスポンスボディを 500 文字までログに残す（OAuth エンドポイントの拒否理由を診断するため）
        try:
            body_preview = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body_preview = "<unreadable>"
        logger.error(
            "Refresh HTTP error",
            extra={"http_code": e.code, "body_preview": body_preview},
        )
        slack_token = _get_secret(slack_token_arn)
        _post_slack_failure(slack_token, channel_id, reason, expires_at_ms)
        return {"refreshed": False, "reason": reason}
    except URLError as e:
        logger.error("Refresh URL error", extra={"error_class": type(e).__name__})
        slack_token = _get_secret(slack_token_arn)
        _post_slack_failure(slack_token, channel_id, "url_error", expires_at_ms)
        return {"refreshed": False, "reason": "url_error"}

    new_creds = _build_updated_credentials(creds, result, now_ms)
    _put_secret(claude_oauth_arn, json.dumps(new_creds))

    new_expires_at_ms = new_creds["claudeAiOauth"]["expiresAt"]
    new_expires_in_min = (new_expires_at_ms - now_ms) // 60_000
    logger.info(
        "Token refreshed successfully",
        extra={"refreshed": True, "new_expires_in_min": new_expires_in_min},
    )
    return {"refreshed": True, "new_expires_at_ms": new_expires_at_ms}
