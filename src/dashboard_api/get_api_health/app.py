import logging
import os
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from _common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

_RATE_LIMIT_SUBTYPE_TO_SERVICE = {
    "anthropic_429": "anthropic",
    "notion_429": "notion",
    "cloudflare_block": "notion",
    "github_429": "github",
    "slack_rate_limit": "slack",
}

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
        api_calls = _query_event_type(table, "api_call_completed", from_ts, to_ts)
        rate_limits = _query_event_type(table, "rate_limit_hit", from_ts, to_ts)
    except Exception as e:
        logger.error("DDB query failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    # subtype → { total_calls, success_count, duration_ms_list }
    by_service: dict[str, dict] = {}

    for item in api_calls:
        p = item.get("payload") or {}
        svc = p.get("subtype", "unknown")
        if svc not in by_service:
            by_service[svc] = {"total_calls": 0, "success_count": 0, "duration_ms_list": [], "rate_limit_count": 0}
        by_service[svc]["total_calls"] += 1
        if p.get("success"):
            by_service[svc]["success_count"] += 1
        dur = p.get("duration_ms")
        if dur is not None:
            try:
                v = int(float(dur))
                if v >= 0:
                    by_service[svc]["duration_ms_list"].append(v)
            except (TypeError, ValueError):
                pass

    for item in rate_limits:
        p = item.get("payload") or {}
        raw_svc = p.get("subtype", "unknown")
        svc = _RATE_LIMIT_SUBTYPE_TO_SERVICE.get(raw_svc, raw_svc)
        if svc not in by_service:
            by_service[svc] = {"total_calls": 0, "success_count": 0, "duration_ms_list": [], "rate_limit_count": 0}
        by_service[svc]["rate_limit_count"] += 1

    result_by_service = {}
    for svc, agg in by_service.items():
        durations = agg["duration_ms_list"]
        total = agg["total_calls"]
        result_by_service[svc] = {
            "total_calls": total,
            "success_rate": round(agg["success_count"] / total, 4) if total > 0 else None,
            "rate_limit_count": agg["rate_limit_count"],
            "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
        }

    return json_response(200, {
        "data": {
            "period": period,
            "by_service": result_by_service,
        },
    })
