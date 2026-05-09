import decimal
import logging
import os
from datetime import UTC, datetime, timedelta

import boto3

from _common import error_response, json_response, query_event_type

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

_DEFAULT_DAYS = 30


def _coerce_count(value: object) -> int | None:
    """events DDB から読み取った件数を非負整数に変換する。

    boto3 DynamoDB resource は Number 属性を `decimal.Decimal` で返すため、
    `isinstance(v, int)` 単独では本番 DDB からの値を弾いてしまう。一方で
    `bool` は `int` のサブクラスのため明示的に除外する必要がある。

    Codex 1 回目 P1-1: data layer 互換性のための Decimal 受容ヘルパー。
    Codex 2 回目 P2-B: Decimal NaN / Infinity 等の特殊値を `is_finite()`
    ガードで除外し、`int(Decimal)` 変換時の OverflowError / ValueError も捕捉する。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, decimal.Decimal):
        try:
            if not value.is_finite() or value < 0 or value != value.to_integral_value():
                return None
            return int(value)
        except (decimal.InvalidOperation, decimal.Overflow, OverflowError, ValueError):
            return None
    return None


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")
    params = event.get("queryStringParameters") or {}
    try:
        days = int(params.get("days", _DEFAULT_DAYS))
    except (ValueError, TypeError):
        return error_response(400, "INVALID_PARAM", "days must be an integer", request_id)

    now = datetime.now(UTC)
    from_ts = (now - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    to_ts = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    table = _dynamodb.Table(os.environ["EVENTS_TABLE"])
    try:
        items = query_event_type(table, "review_completed", from_ts, to_ts)
    except Exception as e:
        logger.error("DDB query failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    total = len(items)
    pass_count = sum(1 for i in items if (i.get("payload") or {}).get("passed"))
    pass_rate = round(pass_count / total, 4) if total > 0 else None

    # code_related_unfixed_count > 0 の一覧 (案 A 起票判断用)
    unfixed_list = [
        {
            "execution_id": i.get("execution_id"),
            "timestamp": i.get("timestamp"),
            "iteration": (i.get("payload") or {}).get("iteration"),
            "code_related_unfixed_count": (i.get("payload") or {}).get("code_related_unfixed_count", 0),
        }
        for i in items
        if int((i.get("payload") or {}).get("code_related_unfixed_count", 0)) > 0
    ]

    # Tier 2.2 P1: issue_category 別件数を全 review_completed イベント横断で集計。
    # past data (issue_categories 欠損) は集計から除外し、全件欠損なら None を返す。
    # 件数値は DDB から Decimal で返るため _coerce_count で int に正規化（bool / 負数 / 非整数は除外）。
    #
    # Codex 2 回目 P2-C: 全 event が `issue_categories: {}` (新 pipeline 動作中だが期間内の
    # categories 件数 0) と、全 event で field 欠損 (新 pipeline 投入前の past data のみ) を
    # 区別する。前者は `{}`、後者は None を返す。
    seen_issue_categories_payload = False
    issue_category_totals: dict[str, int] = {}
    for i in items:
        cats = (i.get("payload") or {}).get("issue_categories")
        if not isinstance(cats, dict):
            continue
        seen_issue_categories_payload = True
        for k, v in cats.items():
            if not isinstance(k, str):
                continue
            count = _coerce_count(v)
            if count is None:
                continue
            issue_category_totals[k] = issue_category_totals.get(k, 0) + count

    return json_response(200, {
        "data": {
            "period_days": days,
            "total_reviews": total,
            "pass_count": pass_count,
            "pass_rate": pass_rate,
            "unfixed_code_issues": unfixed_list,
            "issue_categories": issue_category_totals if seen_issue_categories_payload else None,
        },
    })
