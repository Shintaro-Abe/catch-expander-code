import logging
import os
import sys
from pathlib import Path

import boto3
from feedback.feedback_processor import FeedbackProcessor
from notify.slack_client import SlackClient
from orchestrator import Orchestrator
from state.dynamodb_client import DynamoDbClient

logger = logging.getLogger("catch-expander-agent")


def _setup_logging() -> None:
    """ルートロガーを設定する"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _get_secret(arn: str) -> str:
    """Secrets Managerからシークレットを取得する"""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=arn)
    return response["SecretString"]


def _setup_claude_credentials(secret_value: str) -> None:
    """Claude Code OAuthクレデンシャルをホームディレクトリに配置する。

    Claude Code CLIはタスク起動時に ~/.claude/.credentials.json を参照する。
    ECS Fargateのファイルシステムはエフェメラルなため、起動のたびにSecrets Managerから復元する。
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / ".credentials.json").write_text(secret_value)


def _run_feedback(slack_client: SlackClient, db_client: DynamoDbClient) -> None:
    """フィードバック処理を実行する（TASK_TYPE=feedback）"""
    user_id = os.environ["USER_ID"]
    feedback_text = os.environ["FEEDBACK_TEXT"]
    execution_id = os.environ["EXECUTION_ID"]
    slack_channel = os.environ["SLACK_CHANNEL"]
    slack_thread_ts = os.environ["SLACK_THREAD_TS"]

    logger.info("Feedback task started", extra={"user_id": user_id, "execution_id": execution_id})
    FeedbackProcessor(slack_client, db_client).process(
        user_id, feedback_text, execution_id, slack_channel, slack_thread_ts
    )


def _run_orchestrator(
    slack_client: SlackClient,
    db_client: DynamoDbClient,
    notion_token: str,
    github_token: str,
) -> None:
    """ワークフローを実行する（デフォルトパス）"""
    execution_id = os.environ["EXECUTION_ID"]
    user_id = os.environ["USER_ID"]
    topic = os.environ["TOPIC"]
    slack_channel = os.environ["SLACK_CHANNEL"]
    slack_thread_ts = os.environ["SLACK_THREAD_TS"]
    notion_database_id = os.environ["NOTION_DATABASE_ID"]
    github_repo = os.environ["GITHUB_REPO"]

    logger.info("Orchestrator task started", extra={"execution_id": execution_id, "topic": topic})
    orchestrator = Orchestrator(slack_client, db_client, notion_token, notion_database_id, github_token, github_repo)
    try:
        orchestrator.run(execution_id, user_id, topic, slack_channel, slack_thread_ts)
        db_client.update_execution_status(execution_id, "completed")
    except Exception:
        logger.exception("Orchestrator failed", extra={"execution_id": execution_id})
        db_client.update_execution_status(execution_id, "failed")
        raise


def _notify_task_failure(slack_token: str) -> None:
    """タスク失敗時にSlackスレッドへ通知する。

    通知自体の失敗はログに留め、元の例外を隠さない。
    SLACK_CHANNEL / SLACK_THREAD_TS が未設定の場合は何もしない。
    """
    channel = os.environ.get("SLACK_CHANNEL", "")
    thread_ts = os.environ.get("SLACK_THREAD_TS", "")
    if not channel or not thread_ts:
        logger.warning("SLACK_CHANNEL or SLACK_THREAD_TS not set, skipping failure notification")
        return

    message = (
        "タスクの処理中にエラーが発生しました。\n"
        "Claude OAuthトークンが期限切れの場合は、開発環境で `claude` コマンドを実行して再ログインしてください。"
    )
    try:
        SlackClient(slack_token).post_error(channel, thread_ts, message)
    except Exception:
        logger.warning("Failed to send failure notification to Slack")


def main() -> None:
    """ECSタスクのエントリポイント。TASK_TYPE環境変数に基づき処理を分岐する。"""
    _setup_logging()

    slack_token = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
    try:
        notion_token = _get_secret(os.environ["NOTION_TOKEN_SECRET_ARN"])
        github_token = _get_secret(os.environ["GITHUB_TOKEN_SECRET_ARN"])
        claude_oauth = _get_secret(os.environ["CLAUDE_OAUTH_SECRET_ARN"])
        _setup_claude_credentials(claude_oauth)

        table_prefix = os.environ["DYNAMODB_TABLE_PREFIX"]
        slack_client = SlackClient(slack_token)
        db_client = DynamoDbClient(table_prefix)

        task_type = os.environ.get("TASK_TYPE", "")

        if task_type == "feedback":
            _run_feedback(slack_client, db_client)
        else:
            _run_orchestrator(slack_client, db_client, notion_token, github_token)
    except Exception:
        logger.exception("Task failed")
        _notify_task_failure(slack_token)
        raise


if __name__ == "__main__":
    main()
