import logging
import os
from datetime import UTC, datetime, timedelta

import boto3

from _common import error_response, json_response, query_event_type

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

_DEFAULT_DAYS = 30


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

    return json_response(200, {
        "data": {
            "period_days": days,
            "total_reviews": total,
            "pass_count": pass_count,
            "pass_rate": pass_rate,
            "unfixed_code_issues": unfixed_list,
        },
    })
