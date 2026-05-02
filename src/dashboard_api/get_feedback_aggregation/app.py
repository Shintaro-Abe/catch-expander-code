import logging
import os
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from _common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

_PERIOD_MAP = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _ts_range(period: str) -> tuple[str, str]:
    delta = _PERIOD_MAP[period]
    now = datetime.now(UTC)
    from_ts = (now - delta).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    to_ts = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return from_ts, to_ts


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")
    period = (event.get("queryStringParameters") or {}).get("period", "7d")
    if period not in _PERIOD_MAP:
        return error_response(400, "INVALID_PARAM", f"period must be one of {list(_PERIOD_MAP)}", request_id)

    from_ts, to_ts = _ts_range(period)
    table = _dynamodb.Table(os.environ["EVENTS_TABLE"])

    try:
        kwargs: dict = {
            "IndexName": "gsi_event_type_timestamp",
            "KeyConditionExpression": (
                Key("event_type").eq("feedback_received") & Key("timestamp").between(from_ts, to_ts)
            ),
        }
        items: list = []
        while True:
            result = table.query(**kwargs)
            items.extend(result.get("Items", []))
            if "LastEvaluatedKey" not in result:
                break
            kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
    except Exception as e:
        logger.error("DDB query failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    total = len(items)
    preferences_updated = sum(
        1 for i in items if (i.get("payload") or {}).get("learned_preferences_updated")
    )
    new_prefs_list = [
        int(p["new_preferences_count"])
        for i in items
        if isinstance((p := (i.get("payload") or {})).get("new_preferences_count"), int | float)
    ]
    avg_new_prefs = round(sum(new_prefs_list) / len(new_prefs_list), 2) if new_prefs_list else None

    # Most recent item's total_preferences_count as a snapshot
    latest_total_prefs = None
    if items:
        latest_total_prefs = (items[-1].get("payload") or {}).get("total_preferences_count")

    return json_response(200, {
        "data": {
            "period": period,
            "total_feedback_count": total,
            "preferences_updated_count": preferences_updated,
            "avg_new_preferences": avg_new_prefs,
            "latest_total_preferences": latest_total_prefs,
        },
    })
