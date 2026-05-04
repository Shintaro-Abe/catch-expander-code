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
        items = query_event_type(table, "execution_completed", from_ts, to_ts)
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
