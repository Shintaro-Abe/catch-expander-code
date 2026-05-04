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
        # token_monitor uses system- prefix execution_ids — do NOT filter by exec- prefix
        successes = query_event_type(table, "oauth_refresh_completed", from_ts, to_ts)
        failures = query_event_type(table, "oauth_refresh_failed", from_ts, to_ts)
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
