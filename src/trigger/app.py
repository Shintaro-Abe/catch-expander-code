import hashlib
import json
import os
import uuid
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr, Key
from slack_sdk import WebClient
from slack_verify import verify_slack_signature

# events DDB への構造化イベント書き込み (T1-3、design.md §7.3)。
# Lambda zip packaging (`CodeUri: src/trigger/`) では `src/observability/` が同梱されない。
# SAM Layer 化等の根本解決は別タスク (T1-12 / T1-2b 周辺) で対応するため、
# 現状は ImportError を graceful skip して既存挙動を維持する。
try:
    from src.observability import EventEmitter as _EventEmitter
except ImportError:  # pragma: no cover - Lambda zip 内でのみ発生
    _EventEmitter = None

logger = Logger(service="catch-expander-trigger")

secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")
ecs_client = boto3.client("ecs")

_cached_secrets: dict[str, str] = {}


# User Profile Modal フィールド定義（F6、5W1H 直交フレーム）
# (key, label, placeholder, multiline)
PROFILE_FIELDS: list[tuple[str, str, str, bool]] = [
    ("role", "役割・職業",
     "クラウドエンジニア（AWS 中心、SRE 担当） / マーケター（B2B SaaS）", False),
    ("interests", "関心分野",
     "AI（LLM のビジネス活用）、料理（時短レシピ）、投資（インデックス中心）", True),
    ("expertise", "専門・得意領域",
     "インフラ設計（5 年）、Python・SQL は実務レベル、統計は基礎のみ", True),
    ("learning_goals", "学習の目的",
     "副業として Web 制作で案件獲得したい。HTML/CSS は理解しているが営業ノウハウが弱い", True),
    ("background", "背景・状況",
     "30 代後半、子育て中、SaaS 企業のインフラチーム所属、転職活動準備中", False),
    ("output_preferences", "受け取り方の好み",
     "箇条書き重視、実例 3 つ必須、専門用語は注釈付き、英語ソース引用 OK", True),
]


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


def _get_deliverable_urls(execution_id: str, table_prefix: str) -> dict | None:
    """deliverables テーブルから対象 execution_id の URL 群を取得する。

    Returns:
        - 該当レコードが存在する場合: {"notion_url": str | None, "github_url": str | None}
        - 該当レコードが存在しない場合: None

    `github_url` フィールドは storage="notion+github" のときのみ存在する。
    旧フォーマットレコード (フィールド未存在) でも KeyError にならないよう .get で取得する。
    """
    table = dynamodb.Table(f"{table_prefix}-deliverables")
    response = table.query(
        KeyConditionExpression=Key("execution_id").eq(execution_id),
        Limit=1,
    )
    items = response.get("Items", [])
    if not items:
        return None
    item = items[0]
    return {
        "notion_url": item.get("external_url"),
        "github_url": item.get("github_url"),
    }


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
            urls = _get_deliverable_urls(e["execution_id"], table_prefix)
        except Exception:
            logger.exception(
                "Failed to get deliverable URLs",
                extra={"execution_id": e["execution_id"]},
            )
            urls = None
        items.append(
            {
                "topic": e.get("topic", ""),
                "category": e.get("category", ""),
                "date": e.get("created_at", "")[:10],
                "notion_url": urls["notion_url"] if urls else None,
                "github_url": urls["github_url"] if urls else None,
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
            if item.get("notion_url"):
                lines.append(f"   📝 {item['notion_url']}")
            if item.get("github_url"):
                lines.append(f"   💻 {item['github_url']}")
            if not item.get("notion_url") and not item.get("github_url"):
                lines.append("   （URL なし）")
        text = "\n".join(lines)

    slack_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


# ---------------------------------------------------------------------------
# User Profile Modal ヘルパー群 (F6)
# ---------------------------------------------------------------------------


def _get_user_profile(user_id: str) -> dict | None:
    """UserProfilesTable から既存プロファイル取得（trigger ローカル実装）。"""
    table = dynamodb.Table(f"{os.environ['DYNAMODB_TABLE_PREFIX']}-user-profiles")
    response = table.get_item(Key={"user_id": user_id})
    return response.get("Item")


def _update_user_profile_fields(user_id: str, fields: dict[str, str]) -> None:
    """値ありは SET、空欄は REMOVE する UpdateItem を 1 発で反映。

    learned_preferences など他キーは UpdateExpression に含めないため自動的に保持される。
    """
    table = dynamodb.Table(f"{os.environ['DYNAMODB_TABLE_PREFIX']}-user-profiles")
    set_keys = {k: v for k, v in fields.items() if v}
    remove_keys = [k for k, v in fields.items() if not v]

    now = datetime.now(UTC).isoformat()
    set_expr_parts: list[str] = []
    remove_expr_parts: list[str] = []
    expr_attr_names: dict[str, str] = {}
    expr_attr_values: dict[str, str] = {}

    for i, (k, v) in enumerate(set_keys.items()):
        name_alias = f"#k{i}"
        value_alias = f":v{i}"
        set_expr_parts.append(f"{name_alias} = {value_alias}")
        expr_attr_names[name_alias] = k
        expr_attr_values[value_alias] = v

    for i, k in enumerate(remove_keys):
        name_alias = f"#r{i}"
        remove_expr_parts.append(name_alias)
        expr_attr_names[name_alias] = k

    expr_attr_names["#u"] = "updated_at"
    expr_attr_values[":u"] = now
    set_expr_parts.append("#u = :u")
    expr_attr_names["#c"] = "created_at"
    expr_attr_values[":c"] = now
    set_expr_parts.append("#c = if_not_exists(#c, :c)")

    update_expr_parts: list[str] = []
    if set_expr_parts:
        update_expr_parts.append("SET " + ", ".join(set_expr_parts))
    if remove_expr_parts:
        update_expr_parts.append("REMOVE " + ", ".join(remove_expr_parts))
    update_expression = " ".join(update_expr_parts)

    table.update_item(
        Key={"user_id": user_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames=expr_attr_names,
        ExpressionAttributeValues=expr_attr_values,
    )


def _post_profile_open_button(slack_token: str, channel: str, user_id: str) -> None:
    """プロファイル登録 Modal を開くボタン付きメッセージを投稿。"""
    client = WebClient(token=slack_token)
    client.chat_postMessage(
        channel=channel,
        text="プロファイル登録",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"<@{user_id}> プロファイルを登録・編集できます。"},
            },
            {
                "type": "actions",
                "block_id": "profile_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "open_profile_modal",
                        "text": {"type": "plain_text", "text": "プロファイルを開く"},
                        "style": "primary",
                    }
                ],
            },
        ],
    )


def _build_profile_modal(existing: dict, callback_metadata: dict) -> dict:
    """Modal の Block Kit view を生成。既存値があれば initial_value に注入。"""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ("💡 *具体例を交えて 100〜300 字で書くと、出力の精度が上がります*。"
                         "各フィールドの placeholder を参考にしてください。"),
            },
        },
    ]
    for key, label, placeholder, multiline in PROFILE_FIELDS:
        element: dict = {
            "type": "plain_text_input",
            "action_id": f"input_{key}",
            "multiline": multiline,
            "max_length": 500,
            "placeholder": {"type": "plain_text", "text": placeholder[:150]},
        }
        if existing.get(key):
            element["initial_value"] = existing[key]
        blocks.append({
            "type": "input",
            "block_id": f"block_{key}",
            "label": {"type": "plain_text", "text": label},
            "element": element,
            "optional": True,
        })
    return {
        "type": "modal",
        "callback_id": "profile_submit",
        "title": {"type": "plain_text", "text": "プロファイル登録"},
        "submit": {"type": "plain_text", "text": "保存"},
        "close": {"type": "plain_text", "text": "キャンセル"},
        "private_metadata": json.dumps(callback_metadata),
        "blocks": blocks,
    }


def _post_profile_saved(slack_token: str, channel: str, user_id: str, fields: dict) -> None:
    """保存完了メッセージを投稿。set/removed を区別してサマリ表示。"""
    client = WebClient(token=slack_token)
    label_map = {k: lbl for k, lbl, _, _ in PROFILE_FIELDS}
    set_fields = [(k, v) for k, v in fields.items() if v]
    removed_fields = [k for k, v in fields.items() if not v]

    text_parts = [f"<@{user_id}> ✅ プロファイルを保存しました"]
    if set_fields:
        text_parts.append("\n*登録された内容:*")
        for k, v in set_fields:
            preview = v if len(v) <= 80 else v[:80] + "..."
            text_parts.append(f"• {label_map[k]}: {preview}")
    if removed_fields:
        text_parts.append("\n*削除されたフィールド:*")
        for k in removed_fields:
            text_parts.append(f"• {label_map[k]}")

    client.chat_postMessage(channel=channel, text="\n".join(text_parts))


def _handle_block_actions(payload: dict, slack_token: str) -> dict:
    """block_actions ペイロードを処理。open_profile_modal action のみハンドル。"""
    action = payload["actions"][0]
    if action.get("action_id") != "open_profile_modal":
        return {"statusCode": 200, "body": ""}

    trigger_id = payload["trigger_id"]
    user_id = payload["user"]["id"]
    channel_id = payload.get("channel", {}).get("id", "")

    existing = _get_user_profile(user_id) or {}

    client = WebClient(token=slack_token)
    client.views_open(
        trigger_id=trigger_id,
        view=_build_profile_modal(existing, callback_metadata={"channel_id": channel_id}),
    )
    return {"statusCode": 200, "body": ""}


def _handle_view_submission(payload: dict, slack_token: str) -> dict:
    """view_submission ペイロードを処理。profile_submit callback のみハンドル。"""
    view = payload["view"]
    if view.get("callback_id") != "profile_submit":
        return {"statusCode": 200, "body": ""}

    user_id = payload["user"]["id"]
    metadata = json.loads(view.get("private_metadata", "{}"))
    channel_id = metadata.get("channel_id", "")

    values = view["state"]["values"]
    new_fields: dict[str, str] = {}
    for key, _label, _placeholder, _multiline in PROFILE_FIELDS:
        v = values.get(f"block_{key}", {}).get(f"input_{key}", {}).get("value") or ""
        new_fields[key] = v.strip()

    long_keys = [k for k, v in new_fields.items() if len(v) > 500]
    if long_keys:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "response_action": "errors",
                "errors": {f"block_{k}": "500 文字以内で入力してください" for k in long_keys},
            }),
        }

    _update_user_profile_fields(user_id, new_fields)

    if channel_id:
        _post_profile_saved(slack_token, channel_id, user_id, new_fields)

    return {"statusCode": 200, "body": ""}


def _handle_interactive(payload: dict, slack_token: str) -> dict:
    """Slack Interactive Components のディスパッチ。"""
    payload_type = payload.get("type")
    if payload_type == "block_actions":
        return _handle_block_actions(payload, slack_token)
    if payload_type == "view_submission":
        return _handle_view_submission(payload, slack_token)
    logger.info("Ignoring unknown interactive payload", extra={"type": payload_type})
    return {"statusCode": 200, "body": ""}


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

    # Slack Interactive Components は application/x-www-form-urlencoded で
    # payload=<URL encoded JSON> 形式で届く。Event API (application/json) とは
    # ボディ形式が違うため、Content-Type で早期分岐する。
    content_type = headers_lower.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        from urllib.parse import parse_qs
        parsed = parse_qs(body_str)
        payload_str = parsed.get("payload", [None])[0]
        if not payload_str:
            return {"statusCode": 400, "body": "Missing payload"}
        payload = json.loads(payload_str)
        slack_token = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
        return _handle_interactive(payload, slack_token)

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

    # [F6] プロファイル登録コマンド検出（トップレベル投稿、"profile" 完全一致）
    if not is_thread_reply and topic.strip().lower() == "profile":
        slack_token_p = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
        _post_profile_open_button(slack_token_p, channel, user_id)
        return {"statusCode": 200, "body": ""}

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

    # T1-3: topic_received イベント (design.md §7.3)。
    # ECS RunTask 成功直後で emit。EventEmitter は best-effort 設計なので
    # 書き込み失敗してもここで例外は伝播せず、Slack ACK / DDB / RunTask には影響しない。
    # PII: Slack user ID は SHA-256 ハッシュ化して保存、topic / channel は raw 保持 (§7.4)。
    if _EventEmitter is not None:
        emitter = _EventEmitter(execution_id)
        emitter.emit(
            "topic_received",
            {
                "topic": topic,
                "user_id_hash": hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16],
                "channel_id": channel,
                "workflow_run_id": execution_id,
            },
        )

    return {"statusCode": 200, "body": ""}
