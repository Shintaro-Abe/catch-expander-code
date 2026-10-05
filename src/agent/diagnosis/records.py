"""実行記録の読み取り。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §4

実行 ID はコンストラクタで束ね、以後のどのメソッドも引数に取らない。ツールの側から
別の実行を指定する経路を作らないため。使う操作は get_item・query・get_object の 3 つだけ。
"""

from __future__ import annotations

import contextlib
import json
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError
from diagnosis.models import PROMPT_INDEX_PATTERN, PROMPT_SUBAGENTS

MAX_EVENTS = 500
MAX_EVENTS_TOTAL_BYTES = 2_000_000
MAX_STEPS = 200
MAX_STEPS_TOTAL_BYTES = 2_000_000
# 結果の有無を調べる問い合わせの回数の上限（絞り込みは読んだ後にかかるので、大きな result が並ぶと
# 1 ページに数件しか返らない。ページをいくらでも読み続けないようにする）
MAX_STEP_RESULT_QUERIES = 10
MAX_PROMPT_RECORD_BYTES = 1_000_000
BINARY_PLACEHOLDER = "<binary>"

_S3_PERMISSION_CODES = ("AccessDenied", "403")
# 段階の一覧で読む属性。result（Web から取った内容を含み、大きい）は読まない。
# status などは DynamoDB の予約語なので、どの属性も #別名 で指す
_STEP_ATTRIBUTES = ("step_id", "phase", "step_name", "step_order", "status", "started_at", "completed_at")
_HTTP_FORBIDDEN = 403


class RecordNotFound(Exception):
    """該当する記録がない。"""


class RecordUnavailable(Exception):
    """権限がない、または保存先が設定されていない。"""


class RecordTooLarge(Exception):
    """記録が、読み取る大きさの上限を超えている。"""


def _is_binary(value: Any) -> bool:
    # boto3 の Binary は、バイト列を value に持つ（クラスの import に頼らず、形で見分ける）
    return isinstance(value, bytes | bytearray | memoryview) or isinstance(
        getattr(value, "value", None), bytes | bytearray
    )


def _plain(value: Any) -> Any:
    """DynamoDB から読んだ値を、JSON にできる値に平らにする。

    Decimal は int または float に、集合は整列したリストに、バイト列は固定の文字列にする。
    それ以外の JSON にできない値は str() にする。
    """
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, set | frozenset):
        items = [_plain(item) for item in value]
        try:
            return sorted(items)
        except TypeError:
            return sorted(items, key=str)
    if _is_binary(value):
        return BINARY_PLACEHOLDER
    return str(value)


def _step_order(step: dict) -> float:
    order = step.get("step_order")
    return order if isinstance(order, int | float) else float("inf")


def _size(item: Any) -> int:
    return len(json.dumps(item, ensure_ascii=False, default=str).encode("utf-8"))


class RecordReader:
    """1 つの execution_id の記録だけを読む reader。"""

    def __init__(
        self,
        execution_id: str,
        *,
        dynamodb: Any,
        s3: Any,
        table_prefix: str,
        events_table: str,
        prompts_bucket: str,
    ) -> None:
        self._execution_id = execution_id
        self._dynamodb = dynamodb
        self._s3 = s3
        self._executions_table = f"{table_prefix}-workflow-executions"
        self._steps_table = f"{table_prefix}-workflow-steps"
        self._events_table = events_table
        self._prompts_bucket = prompts_bucket
        # 直近の list_events / list_steps / list_step_ids_with_result が、上限で読み止めたか
        self.events_truncated = False
        self.steps_truncated = False
        self.step_results_truncated = False

    def _query(
        self,
        table_name: str,
        *,
        max_items: int,
        max_bytes: int,
        max_queries: int | None = None,
        expressions: dict[str, Any] | None = None,
    ) -> tuple[list[dict], bool]:
        """束ねた execution_id のアイテムを、ソートキーの昇順で読む。上限で読み止めたかを併せて返す。"""
        table = self._dynamodb.Table(table_name)
        # キーの条件は、条件オブジェクト（boto3.dynamodb.conditions の Key）でなく文字列の式で書く。
        # 全テストを通しで実行すると、tests/unit/trigger/conftest.py が boto3.dynamodb.conditions を
        # sys.modules から外すため、テストの偽物が先に import したクラスと、ここで後から import する
        # クラスが別物になり、偽物の isinstance が合わなくなる（design.md §17 の 21）。
        # 文字列の式なら、そのモジュールに依存しない。値は束ねた実行 ID だけ。
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "execution_id = :execution_id",
            "ExpressionAttributeValues": {":execution_id": self._execution_id},
            "ScanIndexForward": True,
            **(expressions or {}),
        }
        items: list[dict] = []
        total_bytes = 0
        queries = 0
        while True:
            kwargs["Limit"] = max_items - len(items)
            response = table.query(**kwargs)
            queries += 1
            for item in response.get("Items", []):
                plain = _plain(item)
                total_bytes += _size(plain)
                # 1 件目は、上限を超えていても返す（1 件も読めない、を避ける）
                if items and total_bytes > max_bytes:
                    return items, True
                items.append(plain)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items, False
            if len(items) >= max_items or (max_queries is not None and queries >= max_queries):
                return items[:max_items], True
            kwargs["ExclusiveStartKey"] = last_key

    def get_execution(self) -> dict | None:
        """実行レコードを返す。無ければ None。"""
        response = self._dynamodb.Table(self._executions_table).get_item(Key={"execution_id": self._execution_id})
        item = response.get("Item")
        return _plain(item) if item else None

    def list_steps(self) -> list[dict]:
        """段階のレコードを step_order の昇順で返す。MAX_STEPS 件、または合計 MAX_STEPS_TOTAL_BYTES で読み止める。

        読むのは一覧に要る属性だけで、result の本体は読まない（有無は list_step_ids_with_result で調べる）。
        """
        names = {f"#{name}": name for name in _STEP_ATTRIBUTES}
        steps, self.steps_truncated = self._query(
            self._steps_table,
            max_items=MAX_STEPS,
            max_bytes=MAX_STEPS_TOTAL_BYTES,
            expressions={"ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names},
        )
        return sorted(steps, key=_step_order)

    def list_step_ids_with_result(self) -> set[str]:
        """result を持つ段階の step_id を返す。result の本体は取らない（属性の有無で絞り込み、step_id だけを読む）。"""
        items, self.step_results_truncated = self._query(
            self._steps_table,
            max_items=MAX_STEPS,
            max_bytes=MAX_STEPS_TOTAL_BYTES,
            max_queries=MAX_STEP_RESULT_QUERIES,
            expressions={
                "FilterExpression": "attribute_exists(#result)",
                "ProjectionExpression": "#step_id",
                "ExpressionAttributeNames": {"#result": "result", "#step_id": "step_id"},
            },
        )
        return {item["step_id"] for item in items if isinstance(item.get("step_id"), str)}

    def list_events(self) -> list[dict]:
        """イベントを sk の昇順で返す。MAX_EVENTS 件、または合計 MAX_EVENTS_TOTAL_BYTES で読み止める。"""
        if not self._events_table:
            raise RecordUnavailable("events table is not configured")
        events, self.events_truncated = self._query(
            self._events_table, max_items=MAX_EVENTS, max_bytes=MAX_EVENTS_TOTAL_BYTES
        )
        return events

    def get_prompt_record(self, subagent: str, index: str) -> dict:
        """プロンプト記録を返す。"""
        if not self._prompts_bucket:
            raise RecordUnavailable("prompts bucket is not configured")
        # 形の検証は呼び出し側（ToolSession）が済ませているが、キーを組み立てる直前にも確かめる（多層防御）
        if subagent not in PROMPT_SUBAGENTS or not isinstance(index, str) or not PROMPT_INDEX_PATTERN.fullmatch(index):
            raise ValueError("invalid prompt record key")
        key = f"prompts/{self._execution_id}/{subagent}_{index}.json"
        try:
            response = self._s3.get_object(Bucket=self._prompts_bucket, Key=key)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "NoSuchKey":
                raise RecordNotFound("prompt record does not exist") from e
            if code in _S3_PERMISSION_CODES or status == _HTTP_FORBIDDEN:
                raise RecordUnavailable("prompt record is not readable") from e
            raise
        with contextlib.closing(response["Body"]) as body:
            # 上限を 1 バイト超えるところまでしか読まない
            raw = body.read(MAX_PROMPT_RECORD_BYTES + 1)
        # 大きさの判定は、JSON の解析より先
        if len(raw) > MAX_PROMPT_RECORD_BYTES:
            raise RecordTooLarge("prompt record is too large")
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise RecordUnavailable("prompt record is not readable as JSON") from e
        if not isinstance(record, dict):
            raise RecordUnavailable("prompt record is not a JSON object")
        return record
