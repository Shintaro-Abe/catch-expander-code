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

# 現行観測形式の Slack user_id (U / W プレフィックス + 英数字) を許容する防御 regex。
# Slack 公式は ID 文字集合を固定契約として扱わない方針なので
# (https://docs.slack.dev/changelog/2016/08/11/user-id-format-changes/)、
# このパターンは「現在動いているキー形状の早期検証」目的に限定する。
# 仕様変化時は本ファイルと src/dashboard_api/auth_callback/app.py を両方更新すること。
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


def _extract_slack_user_id(user_sub: str | None) -> str | None:
    """Slack OIDC sub から Slack user_id を取り出す。

    本番 Slack OIDC は pure user_id 形式 (例: "U0XXXXXXXX") を返すことを
    実機 cookie デコードで確認済 (.steering/20260518-frontend-profile-view/tasklist.md T0-1)。
    将来 "<user_id>-<team_id>" 形式に変わっても壊れないよう split("-")[0] で防御。
    現行観測される Slack user_id は U / W プレフィックス + 英数字構成 (hyphen を含まない)。
    Slack 公式は文字集合を固定契約とせず将来変化の可能性を示唆 (詳細は _SLACK_USER_ID_RE 上のコメント)。
    抽出後に Slack user_id らしさを regex 検証し、形状破壊を 401 で早期 fail させる。
    """
    if not user_sub:
        return None
    candidate = user_sub.split("-", 1)[0]
    if not _SLACK_USER_ID_RE.fullmatch(candidate):
        return None
    return candidate


# 好みスコープの成果物区分 6 値 (src/agent/feedback/scope.py:SCOPE_DELIVERABLES と整合)
_SCOPE_DELIVERABLES = frozenset(
    {
        "code",
        "research_report",
        "architecture_design",
        "comparison_table",
        "cost_estimate",
        "procedure_guide",
    }
)
_SCOPE_CATEGORIES = frozenset({"技術", "時事", "ビジネス", "学術", "カルチャー"})


def _serialize_scope(raw: object) -> dict:
    """learned_preferences 要素の scope を frontend 用に正規化する。

    scope 欠損・型不正・enum 外の値は汎用側（空リスト）に倒す (read-only 表示用途のため、
    書き込み側 feedback_processor の validate_scope のような縮退フォールバックは不要)。
    """
    if not isinstance(raw, dict):
        return {"categories": [], "deliverables": []}
    categories = raw.get("categories")
    deliverables = raw.get("deliverables")
    return {
        "categories": [c for c in categories if isinstance(c, str) and c in _SCOPE_CATEGORIES]
        if isinstance(categories, list)
        else [],
        "deliverables": [d for d in deliverables if isinstance(d, str) and d in _SCOPE_DELIVERABLES]
        if isinstance(deliverables, list)
        else [],
    }


def _serialize_learned_preferences(raw: object) -> list[dict]:
    """UserProfilesTable の learned_preferences を frontend 用 `{text, scope}[]` に正規化する。

    実保存形式は `{"text": str, "created_at": str, "scope": {...}}` の dict 配列
    (feedback_processor.py / scope.py)。将来の互換性のため、文字列要素 / 旧形式
    (scope なし) / 想定外型も吸収する:
    - dict: "text" フィールドを抽出 (空文字や whitespace-only はスキップ)、scope を正規化
    - str: text として採用、scope は汎用
    - その他: スキップ
    """
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    for pref in raw:
        if isinstance(pref, str):
            text: object = pref
            scope = _serialize_scope(None)
        elif isinstance(pref, dict):
            text = pref.get("text")
            scope = _serialize_scope(pref.get("scope"))
        else:
            continue
        if isinstance(text, str) and text.strip():
            result.append({"text": text, "scope": scope})
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
