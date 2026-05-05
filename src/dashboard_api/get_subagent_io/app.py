import concurrent.futures
import json
import logging
import os

import boto3

from _common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_SUBAGENT_ORDER = {"researcher": 0, "generator": 1, "reviewer_eval": 2, "reviewer_fix": 3}

_s3 = boto3.client("s3")


def _fetch_record(bucket: str, key: str) -> dict | None:
    try:
        resp = _s3.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read())
    except Exception as e:
        logger.error("Failed to fetch S3 object %s: %s", key, e)
        return None


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")
    execution_id = (event.get("pathParameters") or {}).get("execution_id")
    if not execution_id:
        return error_response(400, "MISSING_PARAM", "execution_id is required", request_id)

    bucket = os.environ["PROMPTS_BUCKET"]

    try:
        paginator = _s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=f"prompts/{execution_id}/")
        keys = [obj["Key"] for page in pages for obj in page.get("Contents", [])]
    except Exception as e:
        logger.error("Failed to list S3 objects for %s: %s", execution_id, e)
        return error_response(500, "INTERNAL_ERROR", "Failed to list records", request_id)

    if not keys:
        return json_response(200, {"data": {"execution_id": execution_id, "records": []}})

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(keys), 10)) as executor:
        futures = {executor.submit(_fetch_record, bucket, key): key for key in keys}
        records = [f.result() for f in concurrent.futures.as_completed(futures)]

    records = [r for r in records if r is not None]
    records.sort(key=lambda r: (
        _SUBAGENT_ORDER.get(r.get("subagent", ""), 99),
        r.get("index", ""),
    ))

    return json_response(200, {"data": {"execution_id": execution_id, "records": records}})
