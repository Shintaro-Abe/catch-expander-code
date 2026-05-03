import hashlib
import logging
import os
import sys
from pathlib import Path

import boto3
from feedback.feedback_processor import FeedbackProcessor
from notify.slack_client import SlackClient
from orchestrator import Orchestrator
from state.dynamodb_client import DynamoDbClient
from storage.notion_client import NotionCloudflareBlockError

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


def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _write_secret_file(path: Path, content: str) -> None:
    """ファイルを 0600 権限で原子的に作成・書き込む。umask の影響を受けない。"""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


def _setup_claude_credentials(secret_value: str) -> str:
    """Claude Code OAuthクレデンシャルをホームディレクトリに配置し、起動時の hash を返す。

    Claude Code CLIはタスク起動時に ~/.claude/.credentials.json を参照する。
    ECS Fargateのファイルシステムはエフェメラルなため、起動のたびにSecrets Managerから復元する。
    戻り値の hash はタスク終了時に refresh が起きたかを判定するために _writeback_claude_credentials が使う。
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(exist_ok=True)
    _write_secret_file(claude_dir / ".credentials.json", secret_value)
    return _hash_text(secret_value)


def _writeback_claude_credentials(secret_arn: str, initial_hash: str) -> None:
    """タスク終了時、credentials が refresh されていれば Secrets Manager に書き戻す。

    ベストエフォート: 書き戻し失敗はタスク本体の終了コードに影響させない。
    書き戻し前に Secrets Manager の現在値を再取得し、起動時から変更されていれば skip する
    （並行タスクによる上書きを防ぐ optimistic concurrency）。
    """
    creds_path = Path.home() / ".claude" / ".credentials.json"
    try:
        if not creds_path.exists():
            logger.warning("Credentials file not found at task exit; skipping writeback")
            return
        current = creds_path.read_text()
        if _hash_text(current) == initial_hash:
            logger.info("Credentials unchanged at task exit")
            return
        sm = boto3.client("secretsmanager")
        remote_now = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
        if _hash_text(remote_now) != initial_hash:
            logger.warning("Credentials in Secrets Manager changed since task start; skipping writeback")
            return
        sm.put_secret_value(SecretId=secret_arn, SecretString=current)
        logger.info("Credentials writeback succeeded")
    except Exception:
        logger.exception("Credentials writeback failed (non-fatal)")


def _setup_codex_credentials(secret_value: str) -> str:
    """Codex CLI auth.json をホームディレクトリに配置し、起動時の hash を返す。

    Codex CLI は ~/.codex/auth.json を参照して ChatGPT OAuth 認証を行う。
    実行中に access_token が失効した場合、CLI が自動で refresh して auth.json を上書きする。
    戻り値の hash はタスク終了時に refresh が起きたかを _writeback_codex_credentials が判定するために使う。
    """
    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(exist_ok=True)
    _write_secret_file(codex_dir / "auth.json", secret_value)
    return _hash_text(secret_value)


def _writeback_codex_credentials(secret_arn: str, initial_hash: str) -> None:
    """タスク終了時、Codex auth.json が refresh されていれば Secrets Manager に書き戻す。

    ベストエフォート: 書き戻し失敗はタスク本体の終了コードに影響させない。
    書き戻し前に Secrets Manager の現在値を再取得し、起動時から変更されていれば skip する
    （並行タスクによる上書きを防ぐ optimistic concurrency）。
    """
    auth_path = Path.home() / ".codex" / "auth.json"
    try:
        if not auth_path.exists():
            logger.warning("Codex auth.json not found at task exit; skipping writeback")
            return
        current = auth_path.read_text()
        if _hash_text(current) == initial_hash:
            logger.info("Codex credentials unchanged at task exit")
            return
        sm = boto3.client("secretsmanager")
        remote_now = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
        if _hash_text(remote_now) != initial_hash:
            logger.warning("Codex credentials in Secrets Manager changed since task start; skipping writeback")
            return
        sm.put_secret_value(SecretId=secret_arn, SecretString=current)
        logger.info("Codex credentials writeback succeeded")
    except Exception:
        logger.exception("Codex credentials writeback failed (non-fatal)")


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


def _notify_task_failure(slack_token: str, exc: BaseException | None = None) -> None:
    """タスク失敗時にSlackスレッドへ通知する。

    通知自体の失敗はログに留め、元の例外を隠さない。
    SLACK_CHANNEL / SLACK_THREAD_TS が未設定の場合は何もしない。
    NotionCloudflareBlockError の場合はリトライ案内、それ以外は汎用（OAuth 切れ）文言を送る。
    """
    channel = os.environ.get("SLACK_CHANNEL", "")
    thread_ts = os.environ.get("SLACK_THREAD_TS", "")
    if not channel or not thread_ts:
        logger.warning("SLACK_CHANNEL or SLACK_THREAD_TS not set, skipping failure notification")
        return

    if isinstance(exc, NotionCloudflareBlockError):
        execution_id = os.environ.get("EXECUTION_ID", "<unknown>")
        message = (
            "Notion 前段（Cloudflare）でリクエストが拒否されたため、保存に失敗しました。\n"
            "数分〜数十分ほど時間を空けて再投入をお試しください。\n"
            "繰り返し失敗する場合はログを確認しますのでお知らせください。\n"
            f"execution_id: `{execution_id}`"
        )
    else:
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
    claude_secret_arn = os.environ["CLAUDE_OAUTH_SECRET_ARN"]
    claude_initial_hash: str | None = None
    codex_secret_arn: str | None = None
    codex_initial_hash: str | None = None
    try:
        notion_token = _get_secret(os.environ["NOTION_TOKEN_SECRET_ARN"])
        github_token = _get_secret(os.environ["GITHUB_TOKEN_SECRET_ARN"])
        claude_oauth = _get_secret(claude_secret_arn)
        claude_initial_hash = _setup_claude_credentials(claude_oauth)

        table_prefix = os.environ["DYNAMODB_TABLE_PREFIX"]
        slack_client = SlackClient(slack_token)
        db_client = DynamoDbClient(table_prefix)

        task_type = os.environ.get("TASK_TYPE", "")

        if task_type == "feedback":
            _run_feedback(slack_client, db_client)
        else:
            codex_secret_arn = os.environ["CODEX_AUTH_SECRET_ARN"]
            codex_auth = _get_secret(codex_secret_arn)
            codex_initial_hash = _setup_codex_credentials(codex_auth)
            _run_orchestrator(slack_client, db_client, notion_token, github_token)
    except Exception as exc:
        logger.exception("Task failed")
        _notify_task_failure(slack_token, exc)
        raise
    finally:
        if claude_initial_hash is not None:
            _writeback_claude_credentials(claude_secret_arn, claude_initial_hash)
        if codex_initial_hash is not None:
            _writeback_codex_credentials(codex_secret_arn, codex_initial_hash)


if __name__ == "__main__":
    main()
