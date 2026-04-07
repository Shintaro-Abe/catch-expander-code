import logging
import os
import sys
from pathlib import Path

import boto3
from notify.slack_client import SlackClient
from orchestrator import Orchestrator
from pythonjsonlogger import json_log_formatter
from state.dynamodb_client import DynamoDbClient

logger = logging.getLogger("catch-expander-agent")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(json_log_formatter.JSONFormatter())
logger.addHandler(handler)

secrets_client = boto3.client("secretsmanager")


def _get_secret(arn: str) -> str:
    """Secrets Managerからシークレット値を取得する"""
    response = secrets_client.get_secret_value(SecretId=arn)
    return response["SecretString"]


def _restore_claude_oauth(secret_arn: str) -> None:
    """Claude Code OAuth認証情報を ~/.claude/ に復元する"""
    credential_json = _get_secret(secret_arn)
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    credential_path = claude_dir / "credentials.json"
    credential_path.write_text(credential_json)
    logger.info("Claude OAuth credentials restored")


def main() -> None:
    """ECSタスクのエントリーポイント"""
    # 環境変数から実行パラメータ取得
    execution_id = os.environ["EXECUTION_ID"]
    user_id = os.environ["USER_ID"]
    topic = os.environ["TOPIC"]
    slack_channel = os.environ["SLACK_CHANNEL"]
    slack_thread_ts = os.environ["SLACK_THREAD_TS"]
    table_prefix = os.environ["DYNAMODB_TABLE_PREFIX"]

    logger.info("Agent started", extra={"execution_id": execution_id, "topic": topic})

    # Secrets Managerから認証情報取得
    _restore_claude_oauth(os.environ["CLAUDE_OAUTH_SECRET_ARN"])
    slack_bot_token = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
    notion_token = _get_secret(os.environ["NOTION_TOKEN_SECRET_ARN"])
    github_token = _get_secret(os.environ["GITHUB_TOKEN_SECRET_ARN"])

    # クライアント初期化
    slack_client = SlackClient(slack_bot_token)
    db_client = DynamoDbClient(table_prefix)

    # ステータス更新: analyzing
    db_client.update_execution_status(execution_id, "analyzing")

    try:
        orchestrator = Orchestrator(
            slack_client=slack_client,
            db_client=db_client,
            notion_token=notion_token,
            notion_database_id=os.environ["NOTION_DATABASE_ID"],
            github_token=github_token,
            github_repo=os.environ["GITHUB_REPO"],
        )

        orchestrator.run(
            execution_id=execution_id,
            user_id=user_id,
            topic=topic,
            slack_channel=slack_channel,
            slack_thread_ts=slack_thread_ts,
        )

        db_client.update_execution_status(execution_id, "completed")
        logger.info("Workflow completed", extra={"execution_id": execution_id})

    except Exception:
        logger.exception("Workflow failed", extra={"execution_id": execution_id})
        db_client.update_execution_status(execution_id, "failed")
        try:
            slack_client.post_error(
                channel=slack_channel,
                thread_ts=slack_thread_ts,
                error_message="ワークフローの実行中にエラーが発生しました。詳細はログを確認してください。",
            )
        except Exception:
            logger.exception("Failed to send error notification")
        sys.exit(1)


if __name__ == "__main__":
    main()
