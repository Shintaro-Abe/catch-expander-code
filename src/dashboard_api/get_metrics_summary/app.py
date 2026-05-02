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
    delta = _PERIOD_MAP.get(period, _PERIOD_MAP["7d"])
    now = datetime.now(UTC)
    from_ts = (now - delta).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    to_ts = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return from_ts, to_ts


def _query_all(table, index: str, pk_name: str, pk_val: str, from_ts: str, to_ts: str) -> list:
    kwargs: dict = {
        "IndexName": index,
        "KeyConditionExpression": Key(pk_name).eq(pk_val) & Key("timestamp").between(from_ts, to_ts),
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
        all_events = _query_all(table, "gsi_global_timestamp", "gsi_pk", "GLOBAL", from_ts, to_ts)
    except Exception as e:
        logger.error("DDB query failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    # execution_completed イベントからステータスと duration を集計
    exec_statuses: dict[str, str] = {}
    durations: list[int] = []
    review_total = 0
    review_passed = 0

    for item in all_events:
        eid = item.get("execution_id", "")
        if not eid.startswith("exec-"):
            continue
        et = item.get("event_type")
        payload = item.get("payload") or {}

        if et == "execution_completed":
            exec_statuses[eid] = payload.get("status", "unknown")
            dur = payload.get("total_duration_ms")
            if dur is not None:
                try:
                    v = int(float(dur))
                    if v > 0:
                        durations.append(v)
                except (TypeError, ValueError):
                    pass

        elif et == "review_completed":
            review_total += 1
            if payload.get("passed"):
                review_passed += 1

    status_counts: dict[str, int] = {}
    for s in exec_statuses.values():
        status_counts[s] = status_counts.get(s, 0) + 1

    avg_duration_ms = int(sum(durations) / len(durations)) if durations else None
    review_pass_rate = round(review_passed / review_total, 4) if review_total > 0 else None

    return json_response(200, {
        "data": {
            "period": period,
            "total_executions": len(exec_statuses),
            "status_counts": status_counts,
            "avg_duration_ms": avg_duration_ms,
            "review_pass_rate": review_pass_rate,
        },
    })
