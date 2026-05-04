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

    events_table = _dynamodb.Table(os.environ["EVENTS_TABLE"])
    try:
        kwargs: dict = {
            "KeyConditionExpression": Key("execution_id").eq(execution_id),
            "ScanIndexForward": True,  # SK 昇順 = 時系列順
        }
        items: list = []
        while True:
            result = events_table.query(**kwargs)
            items.extend(result.get("Items", []))
            if "LastEvaluatedKey" not in result:
                break
            kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
    except Exception as e:
        logger.error("DDB query failed: %s", e)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    return json_response(200, {
        "data": items,
        "meta": {"total": len(items)},
    })
