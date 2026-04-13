import json
import os
import uuid
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr, Key
from slack_sdk import WebClient
from slack_verify import verify_slack_signature

logger = Logger(service="catch-expander-trigger")

secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")
ecs_client = boto3.client("ecs")

_cached_secrets: dict[str, str] = {}


def _find_completed_execution(user_id: str, thread_ts: str, table_prefix: str) -> dict | None:
    """workflow-executions テーブルを user-id-index GSI でクエリし、
    指定 thread_ts に一致する実行レコードを返す。
    見つからない場合は None を返す。status の判定は呼び出し側で行う。
    """
    table = dynamodb.Table(f"{table_prefix}-workflow-executions")
    response = table.query(
        IndexName="user-id-index",
        KeyConditionExpression=Key("user_id").eq(user_id),
        FilterExpression=Attr("slack_thread_ts").eq(thread_ts),
    )
    items = response.get("Items", [])
    return items[0] if items else None


def _get_secret(arn: str) -> str:
    """Secrets Managerからシークレットを取得（キャッシュ付��）"""
    if arn not in _cached_secrets:
        response = secrets_client.get_secret_value(SecretId=arn)
        _cached_secrets[arn] = response["SecretString"]
    return _cached_secrets[arn]


# ---------------------------------------------------------------------------
# F9 成果物履歴管理
# ---------------------------------------------------------------------------


def _is_history_command(text: str) -> bool:
    """テキストが履歴コマンドかどうかを判定する。
    「履歴」または「history」（大文字小文字不問）で始まる場合に True を返す。
    メンション除去済みの topic を受け取ることを前提とする。
    """
    lower = text.lower()
    return lower.startswith("履歴") or lower.startswith("history")


def _extract_history_keyword(text: str) -> str | None:
    """履歴コマンドのキーワード部分を抽出する。
    「履歴 Terraform」→ "Terraform"
    「history k8s」    → "k8s"
    「履歴」           → None
    """
    rest = text[7:].strip() if text.lower().startswith("history") else text[2:].strip()
    return rest if rest else None


def _query_completed_executions(user_id: str, table_prefix: str) -> list[dict]:
    """user-id-index GSI でユーザーの実行履歴を created_at 降順で取得し、
    status == "completed" のもののみ返す。
    """
    table = dynamodb.Table(f"{table_prefix}-workflow-executions")
    response = table.query(
        IndexName="user-id-index",
        KeyConditionExpression=Key("user_id").eq(user_id),
        ScanIndexForward=False,
        Limit=20,
    )
    items = response.get("Items", [])
    return [item for item in items if item.get("status") == "completed"]


def _get_deliverable_url(execution_id: str, table_prefix: str) -> str | None:
    """deliverables テーブルから対象 execution_id の external_url を取得する。
    レコードが存在しない場合は None を返す。
    """
    table = dynamodb.Table(f"{table_prefix}-deliverables")
    response = table.query(
        KeyConditionExpression=Key("execution_id").eq(execution_id),
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0].get("external_url") if items else None


def _handle_history_command(
    user_id: str,
    channel: str,
    msg_ts: str,
    keyword: str | None,
    table_prefix: str,
    slack_token: str,
) -> None:
    """履歴コマンドを処理する。ECS タスクは起動しない。"""
    executions = _query_completed_executions(user_id, table_prefix)

    if keyword:
        executions = [e for e in executions if keyword.lower() in e.get("topic", "").lower()]

    executions = executions[:5]

    items = []
    for e in executions:
        try:
            url = _get_deliverable_url(e["execution_id"], table_prefix)
        except Exception:
            logger.exception(
                "Failed to get deliverable URL",
                extra={"execution_id": e["execution_id"]},
            )
            url = None
        items.append(
            {
                "topic": e.get("topic", ""),
                "category": e.get("category", ""),
                "date": e.get("created_at", "")[:10],
                "url": url,
            }
        )

    _post_history_result(channel, msg_ts, items, keyword, slack_token)


def _post_history_result(
    channel: str,
    thread_ts: str,
    items: list[dict],
    keyword: str | None,
    slack_token: str,
) -> None:
    """成果物一覧を Slack スレッドに投稿する。"""
    slack_client = WebClient(token=slack_token)

    if not items:
        if keyword:
            text = f"📭 「{keyword}」に一致する成果物は見つかりません。"
        else:
            text = "📭 まだ成果物がありません。トピックを送信すると調査を開始します。"
    else:
        n = len(items)
        header = f"📚 成果物履歴「{keyword}」（最新 {n} 件）" if keyword else f"📚 成果物履歴（最新 {n} 件）"
        lines = [header, ""]
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. {item['topic']} — {item['category']} — {item['date']}")
            lines.append(f"   {item['url']}" if item["url"] else "   （URL なし）")
        text = "\n".join(lines)

    slack_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


@logger.inject_lambda_context
def lambda_handler(event: dict, context: object) -> dict:
    """Slackイベントを受信し、ECSタスクを起動するLambdaハンドラー"""
    body_str = event.get("body", "")
    headers = event.get("headers", {})

    # ヘッダーキーを小文字に正規化
    headers_lower = {k.lower(): v for k, v in headers.items()}

    # Slackリトライを無視（コールドスタート時のタイムアウトによる再送）
    if headers_lower.get("x-slack-retry-num"):
        logger.info("Ignoring Slack retry", extra={"retry_num": headers_lower["x-slack-retry-num"]})
        return {"statusCode": 200, "body": ""}

    # Slack署名検証
    signing_secret = _get_secret(os.environ["SLACK_SIGNING_SECRET_ARN"])
    timestamp = headers_lower.get("x-slack-request-timestamp", "")
    signature = headers_lower.get("x-slack-signature", "")

    if not verify_slack_signature(signing_secret, timestamp, body_str, signature):
        logger.warning("Slack signature verification failed")
        return {"statusCode": 403, "body": "Invalid signature"}

    body = json.loads(body_str)

    # URL Verification チャレンジ対応
    if body.get("type") == "url_verification":
        return {"statusCode": 200, "body": body["challenge"]}

    # イベントタイプ判定
    event_data = body.get("event", {})
    event_type = event_data.get("type", "")
    event_subtype = event_data.get("subtype")

    if event_type == "app_mention":
        pass  # メンション: 処理を継続
    elif event_type == "message" and event_data.get("channel_type") == "im" and event_subtype is None:
        pass  # DM（ボット自身のメッセージやsubtypeありを除外）
    else:
        return {"statusCode": 200, "body": ""}

    # ボット自身のメッセージを無視
    if event_data.get("bot_id"):
        return {"statusCode": 200, "body": ""}

    user_id = event_data.get("user", "")
    topic = event_data.get("text", "").strip()
    channel = event_data.get("channel", "")

    # メンション記法を除去
    if topic.startswith("<@"):
        topic = topic.split(">", 1)[-1].strip()

    if not topic:
        return {"statusCode": 200, "body": ""}

    logger.info("Topic received", extra={"user_id": user_id, "topic": topic})

    # フィードバック検出（スレッド返信かどうかを判定）
    event_thread_ts = event_data.get("thread_ts")
    event_msg_ts = event_data.get("ts", "")
    is_thread_reply = bool(event_thread_ts and event_thread_ts != event_msg_ts)

    # [F9] 履歴コマンド検出（トップレベル投稿のみ）
    if not is_thread_reply and _is_history_command(topic):
        table_prefix_hist = os.environ["DYNAMODB_TABLE_PREFIX"]
        slack_bot_token_hist = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
        keyword = _extract_history_keyword(topic)
        try:
            _handle_history_command(user_id, channel, event_msg_ts, keyword, table_prefix_hist, slack_bot_token_hist)
        except Exception:
            logger.exception("History command failed", extra={"user_id": user_id, "keyword": keyword})
            WebClient(token=slack_bot_token_hist).chat_postMessage(
                channel=channel,
                thread_ts=event_msg_ts,
                text="❌ 履歴の取得中にエラーが発生しました。しばらく経ってから再試行してください。",
            )
        return {"statusCode": 200, "body": ""}

    # [既存] F8 フィードバック判定
    if is_thread_reply:
        table_prefix_fb = os.environ["DYNAMODB_TABLE_PREFIX"]
        execution = _find_completed_execution(user_id, event_thread_ts, table_prefix_fb)
        if execution is not None:
            if execution.get("status") == "completed":
                # フィードバックルート: ACK投稿 → ECS起動（TASK_TYPE=feedback）
                slack_bot_token_fb = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
                slack_client_fb = WebClient(token=slack_bot_token_fb)
                slack_client_fb.chat_postMessage(
                    channel=channel,
                    thread_ts=event_thread_ts,
                    text="📝 フィードバックを受け取りました。プロファイルに反映中...",
                )
                ecs_client.run_task(
                    cluster=os.environ["ECS_CLUSTER_ARN"],
                    taskDefinition=os.environ["ECS_TASK_DEFINITION_ARN"],
                    launchType="FARGATE",
                    networkConfiguration={
                        "awsvpcConfiguration": {
                            "subnets": [
                                os.environ["ECS_SUBNET_1"],
                                os.environ["ECS_SUBNET_2"],
                            ],
                            "securityGroups": [os.environ["ECS_SECURITY_GROUP"]],
                            "assignPublicIp": "ENABLED",
                        }
                    },
                    overrides={
                        "containerOverrides": [
                            {
                                "name": "agent",
                                "environment": [
                                    {"name": "TASK_TYPE", "value": "feedback"},
                                    {"name": "USER_ID", "value": user_id},
                                    {"name": "FEEDBACK_TEXT", "value": topic},
                                    {"name": "EXECUTION_ID", "value": execution["execution_id"]},
                                    {"name": "SLACK_CHANNEL", "value": channel},
                                    {"name": "SLACK_THREAD_TS", "value": event_thread_ts},
                                ],
                            }
                        ]
                    },
                )
                logger.info("Feedback task started", extra={"execution_id": execution["execution_id"]})
                return {"statusCode": 200, "body": ""}
            else:
                # 実行レコードはあるが status != "completed" → 無視
                logger.info(
                    "Thread reply to non-completed execution, ignoring",
                    extra={"status": execution.get("status")},
                )
                return {"statusCode": 200, "body": ""}
        # execution is None → 新規トピックフローへ fall through

    # SlackへACKメッセージ投稿
    slack_bot_token = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
    slack_client = WebClient(token=slack_bot_token)

    ack_response = slack_client.chat_postMessage(
        channel=channel,
        text=f"📨 トピックを受け取りました。リサーチを開始します。\nトピック: {topic}",
    )
    thread_ts = ack_response["ts"]

    # DynamoDBに実行レコード作成
    execution_id = f"exec-{datetime.now(tz=UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    table_prefix = os.environ["DYNAMODB_TABLE_PREFIX"]
    executions_table = dynamodb.Table(f"{table_prefix}-workflow-executions")

    executions_table.put_item(
        Item={
            "execution_id": execution_id,
            "user_id": user_id,
            "topic": topic,
            "status": "received",
            "slack_channel": channel,
            "slack_thread_ts": thread_ts,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
    )

    logger.info("Execution created", extra={"execution_id": execution_id})

    # ECS RunTask API呼び出し
    ecs_client.run_task(
        cluster=os.environ["ECS_CLUSTER_ARN"],
        taskDefinition=os.environ["ECS_TASK_DEFINITION_ARN"],
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [
                    os.environ["ECS_SUBNET_1"],
                    os.environ["ECS_SUBNET_2"],
                ],
                "securityGroups": [os.environ["ECS_SECURITY_GROUP"]],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": "agent",
                    "environment": [
                        {"name": "EXECUTION_ID", "value": execution_id},
                        {"name": "USER_ID", "value": user_id},
                        {"name": "TOPIC", "value": topic},
                        {"name": "SLACK_CHANNEL", "value": channel},
                        {"name": "SLACK_THREAD_TS", "value": thread_ts},
                    ],
                }
            ]
        },
    )

    logger.info("ECS task started", extra={"execution_id": execution_id})

    return {"statusCode": 200, "body": ""}
