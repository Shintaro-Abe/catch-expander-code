"""F6 User Profile 閲覧用 Lambda。

JWT 由来の user_sub から Slack user_id を導出し、UserProfilesTable から
本人プロファイル (6 軸 + learned_preferences) を read-only で取得する。

設計: .steering/20260518-frontend-profile-view/design.md
"""

import logging
import os
import re

import boto3
from _common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

# 5W1H 6 軸キー (src/trigger/app.py:PROFILE_FIELDS と整合)
_PROFILE_KEYS = (
    "role",
    "interests",
    "expertise",
    "learning_goals",
    "background",
    "output_preferences",
)

# Slack user_id は U / W プレフィックス + uppercase alphanumeric。
# 形状破壊時に「placeholder 表示」で隠れず即 401 で原因特定を早めるためのガード。
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


def _extract_slack_user_id(user_sub: str | None) -> str | None:
    """Slack OIDC sub から Slack user_id を取り出す。

    本番 Slack OIDC は pure user_id 形式 (例: "U0XXXXXXXX") を返すことを
    実機 cookie デコードで確認済 (.steering/20260518-frontend-profile-view/tasklist.md T0-1)。
    将来 "<user_id>-<team_id>" 形式に変わっても壊れないよう split("-")[0] で防御。
    Slack user_id は uppercase alphanumeric のみで hyphen を含まない仕様。
    抽出後に Slack user_id らしさを regex 検証し、形状破壊を 401 で早期 fail させる。
    """
    if not user_sub:
        return None
    candidate = user_sub.split("-", 1)[0]
    if not _SLACK_USER_ID_RE.fullmatch(candidate):
        return None
    return candidate


def _serialize_learned_preferences(raw: object) -> list[str]:
    """UserProfilesTable の learned_preferences を frontend 用 string[] に正規化する。

    実保存形式は `{"text": str, "created_at": str}` の dict 配列 (feedback_processor.py:199)。
    将来の互換性のため、文字列要素 / 旧形式 / 想定外型も吸収する:
    - dict: "text" フィールドを抽出 (空文字や whitespace-only はスキップ)
    - str: そのまま採用
    - その他: スキップ
    """
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for pref in raw:
        if isinstance(pref, str):
            text: object = pref
        elif isinstance(pref, dict):
            text = pref.get("text")
        else:
            continue
        if isinstance(text, str) and text.strip():
            result.append(text)
    return result


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")

    auth_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    user_sub = auth_ctx.get("user_sub")
    user_id = _extract_slack_user_id(user_sub)
    if not user_id:
        return error_response(401, "UNAUTHORIZED", "Missing or invalid user_sub", request_id)

    table = _dynamodb.Table(os.environ["USER_PROFILES_TABLE"])
    try:
        result = table.get_item(Key={"user_id": user_id})
    except Exception as e:  # noqa: BLE001
        logger.error("DDB get_item failed for request %s: %s", request_id, type(e).__name__)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    item = result.get("Item") or {}
    body = {
        "user_id": user_id,
        **{k: item.get(k) for k in _PROFILE_KEYS},
        "learned_preferences": _serialize_learned_preferences(item.get("learned_preferences")),
        "updated_at": item.get("updated_at"),
    }
    return json_response(200, {"data": body})
