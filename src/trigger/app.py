import json
import os
import uuid
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger
from slack_sdk import WebClient
from slack_verify import verify_slack_signature

logger = Logger(service="catch-expander-trigger")

secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")
ecs_client = boto3.client("ecs")

_cached_secrets: dict[str, str] = {}


def _get_secret(arn: str) -> str:
    """Secrets Managerからシークレットを取得（キャッシュ付��）"""
    if arn not in _cached_secrets:
        response = secrets_client.get_secret_value(SecretId=arn)
        _cached_secrets[arn] = response["SecretString"]
    return _cached_secrets[arn]


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
