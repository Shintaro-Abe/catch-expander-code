import logging
import os

import boto3

from _common import PERIOD_MAP, error_response, json_response, query_event_type, ts_range

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")
    period = (event.get("queryStringParameters") or {}).get("period", "7d")
    if period not in PERIOD_MAP:
        return error_response(400, "INVALID_PARAM", f"period must be one of {list(PERIOD_MAP)}", request_id)

    from_ts, to_ts = ts_range(period)
    table = _dynamodb.Table(os.environ["EVENTS_TABLE"])

    try:
        items = query_event_type(table, "feedback_received", from_ts, to_ts)
    except Exception as e:
        logger.error("DDB query failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    total = len(items)
    preferences_updated = sum(
        1 for i in items if (i.get("payload") or {}).get("learned_preferences_updated")
    )
    new_prefs_list = []
    for i in items:
        v = (i.get("payload") or {}).get("new_preferences_count")
        if v is not None:
            try:
                new_prefs_list.append(int(float(v)))
            except (TypeError, ValueError):
                pass
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
