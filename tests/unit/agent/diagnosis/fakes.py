"""偽の DynamoDB・S3・イベント emitter。

偽物は、受けた操作の名前と引数を記録する（AC-5・AC-11 の確認に使う）。
読み取り（``get_item``・``query``・``get_object``）以外の操作は、記録したうえで失敗させる。

``diagnosis`` パッケージの import は関数の中で行う（このファイル自体は、実装が無くても import できる）。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from botocore.exceptions import ClientError

TABLE_PREFIX = "catch-expander-test"
EXECUTIONS_TABLE = f"{TABLE_PREFIX}-workflow-executions"
STEPS_TABLE = f"{TABLE_PREFIX}-workflow-steps"
EVENTS_TABLE = "catch-expander-test-events"
PROMPTS_BUCKET = "catch-expander-test-prompts"

READ_OPERATIONS = frozenset({"get_item", "query", "get_object"})

# conftest の自動適用フィクスチャが、テストごとに中身を確かめて空にする。
REGISTRY: list[Any] = []


@dataclass
class Operation:
    name: str
    target: str  # テーブル名またはバケット名
    kwargs: dict


class WriteAttemptedError(AssertionError):
    """読み取り以外の操作が呼ばれた。"""


def _to_dynamodb(value: Any) -> Any:
    """boto3 の resource と同じく、数値を Decimal にして返す。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamodb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamodb(v) for v in value]
    return value


def _is_condition(obj: Any) -> bool:
    """boto3 の条件オブジェクトか（``Key("a").eq("b")`` や、それを ``&`` でつないだもの）。

    クラスの同一性（``isinstance(obj, ConditionBase)``）では見分けない。既存の
    ``tests/unit/trigger/conftest.py`` が収集時に ``boto3.dynamodb.conditions`` を ``sys.modules`` から
    外すため、このファイルが import したクラスと、実装が後から import するクラスが別物になりうる
    （design.md §17 の 21）。条件オブジェクトが持つ形（``_values`` と ``expression_operator``）で見分ける。
    """
    return isinstance(getattr(obj, "_values", None), tuple) and isinstance(
        getattr(obj, "expression_operator", None), str
    )


def _is_attribute(obj: Any) -> bool:
    """boto3 の ``Key(...)``・``Attr(...)`` か（こちらも形で見分ける）。"""
    return not _is_condition(obj) and isinstance(getattr(obj, "name", None), str)


def _condition_partition_values(cond: Any, attr: str) -> set:
    found: set = set()
    if _is_condition(cond):
        values = cond._values  # noqa: SLF001
        if (
            cond.expression_operator == "="
            and len(values) == 2  # noqa: PLR2004
            and _is_attribute(values[0])
            and values[0].name == attr
        ):
            found.add(values[1])
        for value in values:
            found |= _condition_partition_values(value, attr)
    return found


def partition_values(kwargs: dict, attr: str = "execution_id") -> set:
    """query の引数から、パーティションキーに指定された値を取り出す。

    条件オブジェクト（``Key("execution_id").eq(...)``）と、文字列の式のどちらにも対応する。
    """
    cond = kwargs.get("KeyConditionExpression")
    if _is_condition(cond):
        return _condition_partition_values(cond, attr)
    if isinstance(cond, str):
        names = kwargs.get("ExpressionAttributeNames") or {}
        values = kwargs.get("ExpressionAttributeValues") or {}
        found = set()
        for name, placeholder in re.findall(r"(#?\w+)\s*=\s*(:\w+)", cond):
            if names.get(name, name) == attr:
                found.add(values[placeholder])
        return found
    return set()


# DynamoDB の予約語の一部（式の中に、そのままの属性名としては書けない。``#別名`` で逃がす）。
# 全部ではない。このシステムの記録に出てくる名前と、よく当たる名前だけを挙げている。
RESERVED_WORDS = frozenset(
    {
        "status",
        "result",
        "ttl",
        "name",
        "timestamp",
        "data",
        "type",
        "value",
        "values",
        "key",
        "order",
        "index",
        "user",
        "count",
        "size",
        "comment",
        "source",
        "state",
        "role",
        "level",
        "path",
        "position",
        "time",
        "date",
        "zone",
    }
)
_NAME_TOKEN = re.compile(r"#?[A-Za-z_][A-Za-z0-9_]*")
_FILTER = re.compile(r"\s*(attribute_exists|attribute_not_exists)\s*\(\s*(#?[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*")


def validation_error(message: str, operation: str = "Query") -> ClientError:
    """実物の DynamoDB が、式の誤りで返すエラー。"""
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": message}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        operation,
    )


def _resolve_name(token: str, names: dict, used: set) -> str:
    if token.startswith("#"):
        if token not in names:
            raise validation_error(f"An expression attribute name used in the document path is not defined: {token}")
        used.add(token)
        return names[token]
    if token.lower() in RESERVED_WORDS:
        raise validation_error(f"Attribute name is a reserved keyword; reserved keyword: {token}")
    return token


def projected_attributes(kwargs: dict, used: set | None = None) -> list[str] | None:
    """``ProjectionExpression`` が指す属性名（最上位）の一覧。指定が無ければ None。

    実物と同じく、予約語をそのまま書いた式と、定義の無い ``#別名`` は ValidationException にする。
    """
    expression = kwargs.get("ProjectionExpression")
    if expression is None:
        return None
    if not isinstance(expression, str) or not expression.strip():
        raise validation_error("Invalid ProjectionExpression: The expression can not be empty")
    names = kwargs.get("ExpressionAttributeNames") or {}
    used = set() if used is None else used
    attributes = []
    for part in expression.split(","):
        token = part.strip()
        if _NAME_TOKEN.fullmatch(token) is None:
            raise validation_error(f"Invalid ProjectionExpression: Syntax error; token: {token!r}")
        attribute = _resolve_name(token, names, used)
        if attribute in attributes:
            raise validation_error(f"Invalid ProjectionExpression: Two document paths overlap: {attribute}")
        attributes.append(attribute)
    return attributes


def _filter_rows(rows: list[dict], kwargs: dict, used: set) -> list[dict]:
    """``FilterExpression`` は、``attribute_exists(名前)`` と ``attribute_not_exists(名前)`` だけに対応する。"""
    expression = kwargs.get("FilterExpression")
    if expression is None:
        return rows
    match = _FILTER.fullmatch(expression) if isinstance(expression, str) else None
    if match is None:
        raise AssertionError(f"the fake DynamoDB does not support this FilterExpression: {expression!r}")
    attribute = _resolve_name(match.group(2), kwargs.get("ExpressionAttributeNames") or {}, used)
    wanted = match.group(1) == "attribute_exists"
    return [row for row in rows if (attribute in row) is wanted]


def _check_names_are_used(kwargs: dict, used: set) -> None:
    names = kwargs.get("ExpressionAttributeNames")
    if names is None:
        return
    if not isinstance(names, dict) or not names:
        raise validation_error("ExpressionAttributeNames must not be empty")
    for alias, attribute in names.items():
        if not (isinstance(alias, str) and alias.startswith("#") and isinstance(attribute, str) and attribute):
            raise validation_error(f"ExpressionAttributeNames contains invalid key or value: {alias!r}")
    condition = kwargs.get("KeyConditionExpression")
    if isinstance(condition, str):
        used |= {alias for alias in names if re.search(re.escape(alias) + r"(?![A-Za-z0-9_])", condition)}
    unused = sorted(set(names) - used)
    if unused:
        raise validation_error(f"Value provided in ExpressionAttributeNames unused in expressions: keys: {unused}")


class FakeTable:
    def __init__(self, parent: FakeDynamoDB, name: str, items: list[dict], sort_key: str | None) -> None:
        self.name = name
        self.table_name = name
        self._parent = parent
        self._items = items
        self._sort_key = sort_key

    def _record(self, op: str, kwargs: dict) -> None:
        self._parent.operations.append(Operation(op, self.name, kwargs))
        if self._parent.raise_on_read is not None and op in READ_OPERATIONS:
            raise self._parent.raise_on_read

    def get_item(self, **kwargs: Any) -> dict:
        self._record("get_item", kwargs)
        key = kwargs["Key"]
        for item in self._items:
            if all(item.get(k) == v for k, v in key.items()):
                return {"Item": _to_dynamodb(copy.deepcopy(item))}
        return {}

    def query(self, **kwargs: Any) -> dict:
        self._record("query", kwargs)
        wanted = partition_values(kwargs)
        if not wanted:
            raise AssertionError(f"query on {self.name} without an execution_id key condition: {kwargs!r}")
        used_names: set = set()
        attributes = projected_attributes(kwargs, used_names)
        rows = [i for i in self._items if i.get("execution_id") in wanted]
        _filter_rows([], kwargs, used_names)  # 式の検証と、使われた #別名 の記録だけ（絞り込みは、ページを切った後）
        _check_names_are_used(kwargs, used_names)
        if self._sort_key:
            rows.sort(key=lambda i: i[self._sort_key], reverse=not kwargs.get("ScanIndexForward", True))
        start = kwargs.get("ExclusiveStartKey")
        if start and self._sort_key:
            position = [i[self._sort_key] for i in rows].index(start[self._sort_key])
            rows = rows[position + 1 :]
        size = min(x for x in (kwargs.get("Limit"), self._parent.page_size, len(rows) or 1) if x)
        scanned, rest = rows[:size], rows[size:]
        # 実物と同じ順: Limit（と 1 回の大きさ）でページを切ってから、FilterExpression を掛ける。
        # その結果、「絞り込みで空になったページ + 続きのキー（LastEvaluatedKey）」が起きる
        page = _filter_rows(scanned, kwargs, set())
        # 実物と同じく、ProjectionExpression があれば、指定された属性だけを返す（キーも、指定に無ければ返さない）
        returned = page if attributes is None else [{k: v for k, v in row.items() if k in attributes} for row in page]
        response: dict = {
            "Items": _to_dynamodb(copy.deepcopy(returned)),
            "Count": len(page),
            "ScannedCount": len(scanned),
        }
        if rest and self._sort_key:
            last = scanned[-1]
            response["LastEvaluatedKey"] = {"execution_id": last["execution_id"], self._sort_key: last[self._sort_key]}
        return response

    def __getattr__(self, name: str) -> Any:
        # put_item / update_item / delete_item / batch_writer / scan など
        if name.startswith("_"):
            raise AttributeError(name)

        def _forbidden(*_args: Any, **kwargs: Any) -> None:
            self._parent.operations.append(Operation(name, self.name, kwargs))
            raise WriteAttemptedError(f"{name} must not be called on {self.name}")

        return _forbidden


class FakeDynamoDB:
    """``boto3.resource("dynamodb")`` の代わり。"""

    kind = "dynamodb"

    def __init__(
        self,
        tables: dict[str, tuple[list[dict], str | None]],
        *,
        page_size: int | None = None,
        raise_on_read: Exception | None = None,
    ) -> None:
        self.operations: list[Operation] = []
        self.page_size = page_size
        self.raise_on_read = raise_on_read
        self.requested_tables: list[str] = []
        self._tables = tables
        REGISTRY.append(self)

    def Table(self, name: str) -> FakeTable:  # noqa: N802 (boto3 と同じ名前)
        self.requested_tables.append(name)
        items, sort_key = self._tables.get(name, ([], None))
        return FakeTable(self, name, items, sort_key)

    def ops(self, name: str | None = None, table: str | None = None) -> list[Operation]:
        return [
            op
            for op in self.operations
            if (name is None or op.name == name) and (table is None or op.target == table)
        ]


class _NoSuchKey(ClientError):
    pass


def client_error(code: str, status: int = 400, operation: str = "GetObject") -> ClientError:
    cls = _NoSuchKey if code == "NoSuchKey" else ClientError
    return cls(
        {"Error": {"Code": code, "Message": f"fake {code}"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class FakeBody:
    """S3 の応答の Body。読み取りの後に閉じられたか（``closed``）を記録する。"""

    def __init__(self, data: bytes, *, read_error: Exception | None = None) -> None:
        self._data = data
        self._pos = 0
        self._read_error = read_error
        self.closed = False
        self.read_sizes: list[int | None] = []

    def read(self, amt: int | None = None) -> bytes:
        self.read_sizes.append(amt)
        if self.closed:
            raise ValueError("read of closed body")
        if self._read_error is not None:
            raise self._read_error
        if amt is None or amt < 0:
            chunk, self._pos = self._data[self._pos :], len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + amt]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeBody:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class FakeS3:
    """``boto3.client("s3")`` の代わり。"""

    kind = "s3"
    exceptions = SimpleNamespace(NoSuchKey=_NoSuchKey, ClientError=ClientError)

    def __init__(
        self,
        objects: dict[tuple[str, str], bytes] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.operations: list[Operation] = []
        self._objects = objects or {}
        self._error = error
        self.read_error: Exception | None = None  # Body の read() で上げる例外
        self.bodies: list[FakeBody] = []  # 返した Body（閉じられたかを確かめる）
        REGISTRY.append(self)

    def get_object(self, **kwargs: Any) -> dict:
        self.operations.append(Operation("get_object", kwargs.get("Bucket", ""), kwargs))
        if self._error is not None:
            raise self._error
        data = self._objects.get((kwargs["Bucket"], kwargs["Key"]))
        if data is None:
            raise client_error("NoSuchKey", 404)
        body = FakeBody(data, read_error=self.read_error)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(data)}

    def ops(self, name: str | None = None) -> list[Operation]:
        return [op for op in self.operations if name is None or op.name == name]

    def __getattr__(self, name: str) -> Any:
        # put_object / delete_object / list_objects_v2 など
        if name.startswith("_"):
            raise AttributeError(name)

        def _forbidden(*_args: Any, **kwargs: Any) -> None:
            self.operations.append(Operation(name, kwargs.get("Bucket", ""), kwargs))
            raise WriteAttemptedError(f"{name} must not be called on the fake S3 client")

        return _forbidden


@dataclass
class FakeEmitter:
    """``EventEmitter`` の代わり。emit の呼び出しを記録する。"""

    calls: list[tuple[str, dict, str]] = field(default_factory=list)
    raises: Exception | None = None

    def emit(self, event_type: str, payload: dict, status_at_emit: str = "in_progress") -> None:
        self.calls.append((event_type, payload, status_at_emit))
        if self.raises is not None:
            raise self.raises


@dataclass
class ReaderBundle:
    reader: Any
    dynamodb: FakeDynamoDB
    s3: FakeS3
    execution_id: str

    def all_operations(self) -> list[Operation]:
        return [*self.dynamodb.operations, *self.s3.operations]


_UNSET: Any = object()


def build_reader(
    scenario: Any,
    *,
    execution_id: str | None = None,
    events_table: str = EVENTS_TABLE,
    prompts_bucket: Any = _UNSET,
    page_size: int | None = None,
    dynamodb_error: Exception | None = None,
    s3_error: Any = _UNSET,
    raw_objects: dict[tuple[str, str], bytes] | None = None,
) -> ReaderBundle:
    """筋書きの記録を入れた偽クライアントと、それを読む本物の ``RecordReader`` を作る。"""
    from diagnosis.records import RecordReader

    bucket = scenario.prompts_bucket if prompts_bucket is _UNSET else prompts_bucket
    error = scenario.s3_error if s3_error is _UNSET else s3_error
    tables = {
        EXECUTIONS_TABLE: ([scenario.execution] if scenario.execution else [], None),
        STEPS_TABLE: (list(scenario.steps), "step_id"),
        EVENTS_TABLE: (list(scenario.events), "sk"),
    }
    dynamodb = FakeDynamoDB(tables, page_size=page_size, raise_on_read=dynamodb_error)
    objects = {
        (PROMPTS_BUCKET, f"prompts/{scenario.execution_id}/{subagent}_{index}.json"): json.dumps(
            body, ensure_ascii=False
        ).encode("utf-8")
        for (subagent, index), body in scenario.prompts.items()
    }
    objects.update(raw_objects or {})
    s3 = FakeS3(objects, error=error)
    target = execution_id or scenario.execution_id
    reader = RecordReader(
        target,
        dynamodb=dynamodb,
        s3=s3,
        table_prefix=TABLE_PREFIX,
        events_table=events_table,
        prompts_bucket=bucket,
    )
    return ReaderBundle(reader=reader, dynamodb=dynamodb, s3=s3, execution_id=target)
