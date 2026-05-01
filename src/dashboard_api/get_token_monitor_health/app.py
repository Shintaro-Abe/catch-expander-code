import logging
import os
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from src.dashboard_api._common import error_response, json_response

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


def _query_event_type(table, event_type: str, from_ts: str, to_ts: str) -> list:
    kwargs: dict = {
        "IndexName": "gsi_event_type_timestamp",
        "KeyConditionExpression": (
            Key("event_type").eq(event_type) & Key("timestamp").between(from_ts, to_ts)
        ),
    }
    items: list = []
    while True:
        result = table.query(**kwargs)
        items.extend(result.get("Items", []))
        if "LastEvaluatedKey" not in result:
            break
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
    return items


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")
    period = (event.get("queryStringParameters") or {}).get("period", "7d")
    if period not in _PERIOD_MAP:
        return error_response(400, "INVALID_PARAM", f"period must be one of {list(_PERIOD_MAP)}", request_id)

    from_ts, to_ts = _ts_range(period)
    table = _dynamodb.Table(os.environ["EVENTS_TABLE"])

    try:
        # token_monitor uses system- prefix execution_ids — do NOT filter by exec- prefix
        successes = _query_event_type(table, "oauth_refresh_completed", from_ts, to_ts)
        failures = _query_event_type(table, "oauth_refresh_failed", from_ts, to_ts)
    except Exception as e:
        logger.error("DDB query failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    success_count = len(successes)
    failure_count = len(failures)
    total = success_count + failure_count

    # Latest event timestamps (ISO string; items are ordered by timestamp in GSI)
    last_refresh_at = successes[-1]["timestamp"] if successes else None
    last_failure_at = failures[-1]["timestamp"] if failures else None
    last_failure_reason = None
    if failures:
        last_failure_reason = (failures[-1].get("payload") or {}).get("error_message")

    return json_response(200, {
        "data": {
            "period": period,
            "total_refresh_attempts": total,
            "success_count": success_count,
            "failure_count": failure_count,
            "success_rate": round(success_count / total, 4) if total > 0 else None,
            "last_refresh_at": last_refresh_at,
            "last_failure_at": last_failure_at,
            "last_failure_reason": last_failure_reason,
        },
    })
