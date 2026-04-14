import json
import logging
import os
import time
from datetime import UTC, datetime

import boto3
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")

STALE_THRESHOLD_HOURS_DEFAULT = 24


def _get_secret(arn: str) -> str:
    """Secrets Manager からシークレットを取得する。"""
    response = secrets_client.get_secret_value(SecretId=arn)
    return response["SecretString"]


def _is_token_stale(claude_oauth_json: str, stale_threshold_ms: int) -> tuple[bool, int | None]:
    """Claude OAuth トークンが失効かつ閾値時間以上経過しているか判定する。

    Secrets Manager に格納された JSON の `claudeAiOauth.expiresAt`（ミリ秒）を参照し、
    `now_ms > expiresAt + stale_threshold_ms` のときに失効と見なす。

    Returns:
        (is_stale, expires_at_ms): 失効フラグと有効期限（ミリ秒）のタプル。
        expiresAt が取得できない場合は (False, None) を返す。
    """
    try:
        creds = json.loads(claude_oauth_json)
        expires_at_ms = creds.get("claudeAiOauth", {}).get("expiresAt")
        if expires_at_ms is None:
            logger.warning("expiresAt not found in Claude OAuth credentials")
            return False, None
        now_ms = int(time.time() * 1000)
        is_stale = now_ms > expires_at_ms + stale_threshold_ms
        return is_stale, int(expires_at_ms)
    except (json.JSONDecodeError, AttributeError) as e:
        logger.error("Failed to parse Claude OAuth credentials: %s", e)
        return False, None


def _post_slack_notification(slack_token: str, channel_id: str, expires_at_ms: int | None) -> None:
    """Slack チャンネルへトークン失効通知を投稿する。

    通知に失敗した場合は SlackApiError を再スローする。
    """
    if expires_at_ms is not None:
        expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=UTC)
        now_ms = int(time.time() * 1000)
        hours_overdue = (now_ms - expires_at_ms) // (60 * 60 * 1000)
        expiry_str = f"{expires_at.strftime('%Y-%m-%d %H:%M UTC')} （{hours_overdue} 時間前に期限切れ）"
    else:
        expiry_str = "不明"

    message = (
        "⚠️ Claude OAuth トークンの確認が必要です。\n"
        "\n"
        f"有効期限: {expiry_str}\n"
        "\n"
        "DevContainer を起動して `claude` コマンドを実行し、再ログインしてください。\n"
        "再ログイン後、トークンは自動的に Secrets Manager へ同期されます。"
    )

    client = WebClient(token=slack_token)
    try:
        client.chat_postMessage(channel=channel_id, text=message)
        logger.info("Slack notification sent to channel %s", channel_id)
    except SlackApiError as e:
        logger.error("Failed to send Slack notification: %s", e)
        raise


def lambda_handler(event: dict, context: object) -> None:
    """Claude OAuth トークンの失効状態を定期チェックし、必要に応じて Slack 通知する。

    EventBridge Scheduler から 12 時間ごとに呼び出される。
    `expiresAt` が現在時刻より STALE_THRESHOLD_HOURS 以上過去のとき失効と判定し、
    Slack チャンネルへ通知を投稿する。
    """
    claude_oauth_arn = os.environ["CLAUDE_OAUTH_SECRET_ARN"]
    slack_token_arn = os.environ["SLACK_BOT_TOKEN_SECRET_ARN"]
    channel_id = os.environ["SLACK_NOTIFICATION_CHANNEL_ID"]
    stale_threshold_hours = int(os.environ.get("STALE_THRESHOLD_HOURS", str(STALE_THRESHOLD_HOURS_DEFAULT)))
    stale_threshold_ms = stale_threshold_hours * 60 * 60 * 1000

    claude_oauth_json = _get_secret(claude_oauth_arn)
    is_stale, expires_at_ms = _is_token_stale(claude_oauth_json, stale_threshold_ms)

    if not is_stale:
        if expires_at_ms is not None:
            expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=UTC)
            logger.info("Claude OAuth token is valid. Expires at: %s", expires_at.isoformat())
        else:
            logger.info("Claude OAuth token staleness check skipped (no expiresAt)")
        return

    logger.warning(
        "Claude OAuth token is stale. expires_at_ms=%d",
        expires_at_ms if expires_at_ms is not None else -1,
    )

    slack_token = _get_secret(slack_token_arn)
    _post_slack_notification(slack_token, channel_id, expires_at_ms)
