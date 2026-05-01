"""dashboard_api Lambda 共通ユーティリティ。"""
from __future__ import annotations

import decimal
import json
from typing import Any


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
