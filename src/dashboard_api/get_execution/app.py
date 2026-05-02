import logging
import os

import boto3
from boto3.dynamodb.conditions import Key

from _common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")
    execution_id = (event.get("pathParameters") or {}).get("execution_id")
    if not execution_id:
        return error_response(400, "MISSING_PARAM", "execution_id is required", request_id)

    exec_table = _dynamodb.Table(os.environ["EXECUTIONS_TABLE"])
    try:
        result = exec_table.get_item(Key={"execution_id": execution_id})
    except Exception as e:
        logger.error("DDB get_item failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    if "Item" not in result:
        return error_response(404, "EXECUTION_NOT_FOUND", f"Execution {execution_id} not found", request_id)

    execution = result["Item"]

    # deliverables テーブルから成果物一覧を取得
    deliverables: list = []
    deliverables_table = _dynamodb.Table(os.environ["DELIVERABLES_TABLE"])
    try:
        del_result = deliverables_table.query(
            KeyConditionExpression=Key("execution_id").eq(execution_id),
        )
        deliverables = del_result.get("Items", [])
    except Exception as e:
        logger.warning("Failed to fetch deliverables for %s: %s", execution_id, e)

    return json_response(200, {
        "data": {
            "execution": execution,
            "deliverables": deliverables,
        },
    })
