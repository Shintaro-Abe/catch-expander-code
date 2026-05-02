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
                Key("event_type").eq("execution_completed") & Key("timestamp").between(from_ts, to_ts)
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
    tokens_list = []
    cost_list = []
    for i in items:
        p = i.get("payload") or {}
        t = p.get("total_tokens_used")
        if t is not None:
            try:
                v = int(float(t))
                if v >= 0:
                    tokens_list.append(v)
            except (TypeError, ValueError):
                pass
        c = p.get("total_cost_usd")
        if c is not None:
            try:
                v2 = float(c)
                if v2 >= 0:
                    cost_list.append(v2)
            except (TypeError, ValueError):
                pass

    total_tokens = sum(tokens_list) if tokens_list else None
    total_cost = round(sum(cost_list), 6) if cost_list else None
    avg_tokens = int(total_tokens / len(tokens_list)) if tokens_list else None

    return json_response(200, {
        "data": {
            "period": period,
            "total_executions": total,
            "total_tokens_used": total_tokens,
            "total_cost_usd": total_cost,
            "avg_tokens_per_execution": avg_tokens,
        },
    })
