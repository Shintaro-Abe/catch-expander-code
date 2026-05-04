"""dashboard_api Lambda 共通ユーティリティ。"""
from __future__ import annotations

import decimal
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from boto3.dynamodb.conditions import Key


PERIOD_MAP = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, decimal.Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def json_response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def error_response(status: int, code: str, message: str, request_id: str = "") -> dict:
    return json_response(status, {"error": {"code": code, "message": message, "request_id": request_id}})


def ts_range(period: str) -> tuple[str, str]:
    delta = PERIOD_MAP[period]
    now = datetime.now(UTC)
    from_ts = (now - delta).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    to_ts = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return from_ts, to_ts


def query_event_type(table, event_type: str, from_ts: str, to_ts: str) -> list:
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
