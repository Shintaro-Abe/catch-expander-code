import logging
import os
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from src.dashboard_api._common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

_DEFAULT_DAYS = 7


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
        kwargs: dict = {
            "IndexName": "gsi_event_type_timestamp",
            "KeyConditionExpression": Key("event_type").eq("error") & Key("timestamp").between(from_ts, to_ts),
            "ScanIndexForward": False,  # 新しい順
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

    # error_type 別の集計
    by_type: dict[str, int] = {}
    normalized = []
    for item in items:
        payload = item.get("payload") or {}
        et = payload.get("error_type", "unknown")
        by_type[et] = by_type.get(et, 0) + 1
        normalized.append({
            "execution_id": item.get("execution_id"),
            "timestamp": item.get("timestamp"),
            "error_type": et,
            "error_message": payload.get("error_message", ""),
            "stage": payload.get("stage", ""),
            "is_recoverable": payload.get("is_recoverable"),
        })

    return json_response(200, {
        "data": normalized,
        "meta": {"total": len(normalized), "by_type": by_type},
    })
