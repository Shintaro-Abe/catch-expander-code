import logging
import os
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from src.dashboard_api._common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _days_ago_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")
    params = event.get("queryStringParameters") or {}

    from_ts = params.get("from") or _days_ago_iso(7)
    to_ts = params.get("to") or _now_iso()
    limit = min(_parse_int(params.get("limit"), _DEFAULT_LIMIT), _MAX_LIMIT)
    status_filter = params.get("status")
    topic_filter = params.get("topic")

    events_table = _dynamodb.Table(os.environ["EVENTS_TABLE"])

    # gsi_global_timestamp で期間内の全イベントを取得し execution_id を収集
    query_kwargs: dict = {
        "IndexName": "gsi_global_timestamp",
        "KeyConditionExpression": Key("gsi_pk").eq("GLOBAL") & Key("timestamp").between(from_ts, to_ts),
        "ProjectionExpression": "execution_id, #ts",
        "ExpressionAttributeNames": {"#ts": "timestamp"},
    }
    seen: dict[str, str] = {}  # execution_id → earliest timestamp
    try:
        while True:
            result = events_table.query(**query_kwargs)
            for item in result.get("Items", []):
                eid = item["execution_id"]
                # system-* prefix は除外 (ユーザー実行のみ返す)
                if not eid.startswith("exec-"):
                    continue
                ts = item.get("timestamp", "")
                if eid not in seen or ts < seen[eid]:
                    seen[eid] = ts
            if "LastEvaluatedKey" not in result:
                break
            query_kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
    except Exception as e:
        logger.error("DDB query failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    # execution_id を earliest timestamp 降順にソート
    sorted_ids = sorted(seen.keys(), key=lambda e: seen[e], reverse=True)

    # WorkflowExecutionsTable からメタデータを取得
    exec_table = _dynamodb.Table(os.environ["EXECUTIONS_TABLE"])
    executions = []
    for eid in sorted_ids:
        try:
            result = exec_table.get_item(Key={"execution_id": eid})
        except Exception as e:
            logger.warning("Failed to get execution %s: %s", eid, e)
            continue
        if "Item" in result:
            executions.append(result["Item"])

    # アプリケーション側フィルタ
    if status_filter:
        allowed = {s.strip() for s in status_filter.split(",")}
        executions = [e for e in executions if e.get("status") in allowed]
    if topic_filter:
        kw = topic_filter.lower()
        executions = [e for e in executions if kw in e.get("topic", "").lower()]

    total = len(executions)
    page = executions[:limit]

    return json_response(200, {
        "data": page,
        "meta": {"total": total, "next_cursor": None},
    })
