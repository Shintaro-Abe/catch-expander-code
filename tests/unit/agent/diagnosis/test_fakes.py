"""偽の DynamoDB（``fakes.py``）自体のテスト。

偽物が、キーの条件の書き方（条件オブジェクト / 文字列の式）と、``boto3.dynamodb.conditions`` の
クラスの同一性に左右されないことを確かめる（design.md §17 の 21）。

背景: 既存の ``tests/unit/trigger/conftest.py`` は、収集時に ``boto3.dynamodb.conditions`` を
``sys.modules`` から外す。その後に実装が ``from boto3.dynamodb.conditions import Key`` をすると、
先に import されていたクラスとは別のクラスになる。偽物が ``isinstance`` で見分けていると、
``Key()`` で書いた正しい実装が「キーの条件なし」と誤って判定される。
"""

import importlib
import sys
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from . import fakes

EXECUTION_ID = "exec-20261004120000-abcdef12"
OTHER_EXECUTION_ID = "exec-20250101000000-ffffffff"


def _reimported_conditions():
    """``boto3.dynamodb.conditions`` を ``sys.modules`` から外して import し直す（別のクラスになる）。"""
    with patch.dict(sys.modules):
        sys.modules.pop("boto3.dynamodb.conditions", None)
        return importlib.import_module("boto3.dynamodb.conditions")


def _table(items: list[dict], sort_key: str | None = "sk") -> tuple[fakes.FakeDynamoDB, fakes.FakeTable]:
    dynamodb = fakes.FakeDynamoDB({"t": (items, sort_key)})
    return dynamodb, dynamodb.Table("t")


def _items() -> list[dict]:
    return [
        {"execution_id": EXECUTION_ID, "sk": "b", "n": 2},
        {"execution_id": OTHER_EXECUTION_ID, "sk": "a", "n": 9},
        {"execution_id": EXECUTION_ID, "sk": "a", "n": 1},
    ]


class TestPartitionValues:
    def test_partition_values_with_string_expression_returns_bound_value(self):
        kwargs = {
            "KeyConditionExpression": "execution_id = :execution_id",
            "ExpressionAttributeValues": {":execution_id": EXECUTION_ID},
        }

        assert fakes.partition_values(kwargs) == {EXECUTION_ID}

    def test_partition_values_with_string_expression_using_name_placeholder_returns_bound_value(self):
        kwargs = {
            "KeyConditionExpression": "#pk = :v AND begins_with(sk, :prefix)",
            "ExpressionAttributeNames": {"#pk": "execution_id"},
            "ExpressionAttributeValues": {":v": EXECUTION_ID, ":prefix": "2026"},
        }

        assert fakes.partition_values(kwargs) == {EXECUTION_ID}

    def test_partition_values_with_string_expression_on_other_attribute_returns_nothing(self):
        kwargs = {"KeyConditionExpression": "sk = :v", "ExpressionAttributeValues": {":v": EXECUTION_ID}}

        assert fakes.partition_values(kwargs) == set()

    def test_partition_values_with_condition_object_returns_bound_value(self):
        from boto3.dynamodb.conditions import Key

        kwargs = {"KeyConditionExpression": Key("execution_id").eq(EXECUTION_ID)}

        assert fakes.partition_values(kwargs) == {EXECUTION_ID}

    def test_partition_values_with_compound_condition_object_returns_only_partition_value(self):
        from boto3.dynamodb.conditions import Key

        condition = Key("execution_id").eq(EXECUTION_ID) & Key("sk").begins_with("2026")

        assert fakes.partition_values({"KeyConditionExpression": condition}) == {EXECUTION_ID}

    def test_partition_values_with_condition_on_other_attribute_returns_nothing(self):
        from boto3.dynamodb.conditions import Key

        assert fakes.partition_values({"KeyConditionExpression": Key("sk").eq(EXECUTION_ID)}) == set()

    def test_partition_values_with_non_equality_condition_returns_nothing(self):
        from boto3.dynamodb.conditions import Key

        condition = Key("execution_id").begins_with("exec-")

        assert fakes.partition_values({"KeyConditionExpression": condition}) == set()

    def test_partition_values_with_condition_object_from_reimported_module_returns_bound_value(self):
        """クラスの同一性に頼らない: import し直した別のクラスの条件オブジェクトでも読める。"""
        from boto3.dynamodb.conditions import ConditionBase as OriginalConditionBase

        conditions = _reimported_conditions()
        condition = conditions.Key("execution_id").eq(EXECUTION_ID) & conditions.Key("sk").gt("0")

        assert not isinstance(condition, OriginalConditionBase)  # 前提: 別のクラスになっている
        assert fakes.partition_values({"KeyConditionExpression": condition}) == {EXECUTION_ID}

    @pytest.mark.parametrize("condition", [None, 5, ["execution_id"], {"execution_id": EXECUTION_ID}])
    def test_partition_values_without_usable_condition_returns_nothing(self, condition):
        assert fakes.partition_values({"KeyConditionExpression": condition}) == set()

    def test_partition_values_without_condition_returns_nothing(self):
        assert fakes.partition_values({}) == set()


class TestFakeTableQuery:
    def test_query_with_string_expression_returns_only_bound_execution_in_sort_key_order(self):
        _, table = _table(_items())

        response = table.query(
            KeyConditionExpression="execution_id = :execution_id",
            ExpressionAttributeValues={":execution_id": EXECUTION_ID},
        )

        assert [item["sk"] for item in response["Items"]] == ["a", "b"]

    def test_query_with_condition_object_from_reimported_module_returns_same_rows(self):
        conditions = _reimported_conditions()
        _, table = _table(_items())

        response = table.query(KeyConditionExpression=conditions.Key("execution_id").eq(EXECUTION_ID))

        assert [item["sk"] for item in response["Items"]] == ["a", "b"]

    def test_query_descending_returns_rows_in_reverse(self):
        _, table = _table(_items())

        response = table.query(
            KeyConditionExpression="execution_id = :v",
            ExpressionAttributeValues={":v": EXECUTION_ID},
            ScanIndexForward=False,
        )

        assert [item["sk"] for item in response["Items"]] == ["b", "a"]

    def test_query_without_partition_key_condition_fails_loudly(self):
        _, table = _table(_items())

        with pytest.raises(AssertionError, match="without an execution_id key condition"):
            table.query(KeyConditionExpression="sk = :v", ExpressionAttributeValues={":v": "a"})

    def test_query_records_operation_with_arguments(self):
        dynamodb, table = _table(_items())

        table.query(KeyConditionExpression="execution_id = :v", ExpressionAttributeValues={":v": EXECUTION_ID})

        assert [(op.name, op.target) for op in dynamodb.operations] == [("query", "t")]
        assert fakes.partition_values(dynamodb.operations[0].kwargs) == {EXECUTION_ID}

    def test_write_operation_is_recorded_and_fails(self):
        dynamodb, table = _table(_items())

        with pytest.raises(fakes.WriteAttemptedError):
            table.put_item(Item={"execution_id": EXECUTION_ID})

        assert [op.name for op in dynamodb.operations] == ["put_item"]
        dynamodb.operations.clear()  # conftest の読み取り専用の確認（AC-11）に、この意図した書き込みを数えさせない


class TestFakeProjection:
    """偽の DynamoDB の ``ProjectionExpression`` と ``ExpressionAttributeNames``（修正仕様 C3）。

    実物と同じく、指定された属性だけを返す。予約語をそのまま書いた式、定義の無い ``#別名``、
    使われていない ``#別名`` は、実物と同じく ValidationException にする。
    """

    KEY = {"KeyConditionExpression": "execution_id = :v", "ExpressionAttributeValues": {":v": EXECUTION_ID}}

    def _rows(self) -> list[dict]:
        return [
            {"execution_id": EXECUTION_ID, "sk": "a", "n": 1, "status": "failed", "result": {"big": "x" * 50}},
            {"execution_id": EXECUTION_ID, "sk": "b", "n": 2, "status": "completed"},
        ]

    def test_query_without_projection_returns_every_attribute(self):
        _, table = _table(self._rows())

        items = table.query(**self.KEY)["Items"]

        assert set(items[0]) == {"execution_id", "sk", "n", "status", "result"}

    def test_query_with_projection_returns_only_the_listed_attributes(self):
        _, table = _table(self._rows())

        items = table.query(**self.KEY, ProjectionExpression="sk, n")["Items"]

        assert [set(item) for item in items] == [
            {"sk", "n"},
            {"sk", "n"},
        ]  # キー（execution_id）も、指定に無ければ返らない
        assert [item["sk"] for item in items] == ["a", "b"]

    def test_query_with_projection_resolves_attribute_name_placeholders(self):
        _, table = _table(self._rows())

        items = table.query(**self.KEY, ProjectionExpression="sk, #s", ExpressionAttributeNames={"#s": "status"})[
            "Items"
        ]

        assert items == [{"sk": "a", "status": "failed"}, {"sk": "b", "status": "completed"}]

    def test_query_with_projection_omits_attributes_the_item_does_not_have(self):
        _, table = _table(self._rows())

        items = table.query(**self.KEY, ProjectionExpression="sk, #r", ExpressionAttributeNames={"#r": "result"})[
            "Items"
        ]

        assert [set(item) for item in items] == [{"sk", "result"}, {"sk"}]

    def test_query_with_projection_still_pages_by_sort_key(self):
        dynamodb = fakes.FakeDynamoDB({"t": (self._rows(), "sk")}, page_size=1)
        table = dynamodb.Table("t")

        first = table.query(**self.KEY, ProjectionExpression="n")
        second = table.query(**self.KEY, ProjectionExpression="n", ExclusiveStartKey=first["LastEvaluatedKey"])

        assert [item["n"] for item in (*first["Items"], *second["Items"])] == [1, 2]
        assert first["LastEvaluatedKey"] == {"execution_id": EXECUTION_ID, "sk": "a"}
        assert "LastEvaluatedKey" not in second

    @pytest.mark.parametrize("expression", ["sk, status", "result", "sk, TTL", "sk, Timestamp"])
    def test_reserved_word_written_as_is_is_rejected_like_the_real_service(self, expression):
        _, table = _table(self._rows())

        with pytest.raises(ClientError) as raised:
            table.query(**self.KEY, ProjectionExpression=expression)
        assert raised.value.response["Error"]["Code"] == "ValidationException"

    def test_placeholder_that_is_not_defined_is_rejected(self):
        _, table = _table(self._rows())

        with pytest.raises(ClientError):
            table.query(**self.KEY, ProjectionExpression="sk, #s")

    def test_placeholder_that_is_not_used_is_rejected(self):
        _, table = _table(self._rows())

        with pytest.raises(ClientError):
            table.query(**self.KEY, ProjectionExpression="sk", ExpressionAttributeNames={"#s": "status"})

    @pytest.mark.parametrize("expression", ["", "  ", "sk,, n", "sk n", "sk, sk", ["sk"]])
    def test_malformed_projection_is_rejected(self, expression):
        _, table = _table(self._rows())

        with pytest.raises(ClientError):
            table.query(**self.KEY, ProjectionExpression=expression)

    def test_placeholder_used_in_key_condition_counts_as_used(self):
        _, table = _table(self._rows())

        items = table.query(
            KeyConditionExpression="#pk = :v",
            ExpressionAttributeNames={"#pk": "execution_id", "#s": "status"},
            ExpressionAttributeValues={":v": EXECUTION_ID},
            ProjectionExpression="#s",
        )["Items"]

        assert items == [{"status": "failed"}, {"status": "completed"}]

    def test_attribute_exists_filter_selects_rows_that_have_the_attribute(self):
        _, table = _table(self._rows())

        items = table.query(
            **self.KEY,
            ProjectionExpression="sk",
            FilterExpression="attribute_exists(#r)",
            ExpressionAttributeNames={"#r": "result"},
        )["Items"]

        assert items == [{"sk": "a"}]

    def test_other_filter_expressions_are_not_supported_and_fail_loudly(self):
        _, table = _table(self._rows())

        with pytest.raises(AssertionError, match="does not support this FilterExpression"):
            table.query(**self.KEY, FilterExpression="n > :v")


class TestFakeFilterIsAppliedAfterPaging:
    """実物の DynamoDB と同じ順: ``Limit``（と 1 回の大きさ）でページを切ってから、``FilterExpression`` を掛ける。

    その結果、「絞り込みで空になったページ + 続きのキー（``LastEvaluatedKey``）」が起きる。
    偽物が先に絞り込んでしまうと、この形を扱えない実装（空のページで読み終える、など）を見逃す。
    """

    KEY = {"KeyConditionExpression": "execution_id = :v", "ExpressionAttributeValues": {":v": EXECUTION_ID}}
    FILTER = {"FilterExpression": "attribute_exists(#r)", "ExpressionAttributeNames": {"#r": "result"}}

    def _rows(self, with_result: set[int], count: int = 6) -> list[dict]:
        rows = []
        for n in range(1, count + 1):
            row = {"execution_id": EXECUTION_ID, "sk": f"{n:02d}"}
            if n in with_result:
                row["result"] = {"n": n}
            rows.append(row)
        return rows

    def test_limit_counts_scanned_rows_not_matching_rows(self):
        _, table = _table(self._rows({5, 6}))

        response = table.query(**self.KEY, **self.FILTER, Limit=2)

        assert response["Items"] == []  # 先頭の 2 件を読んでから絞り込むので、空になる
        assert response["Count"] == 0
        assert response["ScannedCount"] == 2
        assert response["LastEvaluatedKey"] == {"execution_id": EXECUTION_ID, "sk": "02"}  # それでも、続きがある

    def test_following_the_key_after_an_empty_page_reaches_matching_rows(self):
        _, table = _table(self._rows({5}))
        collected, start, pages = [], None, 0

        while True:
            extra = {"ExclusiveStartKey": start} if start else {}
            response = table.query(**self.KEY, **self.FILTER, Limit=2, **extra)
            pages += 1
            collected.extend(item["sk"] for item in response["Items"])
            start = response.get("LastEvaluatedKey")
            if not start:
                break

        assert collected == ["05"]
        assert pages == 3

    def test_page_size_also_cuts_before_filtering(self):
        dynamodb = fakes.FakeDynamoDB({"t": (self._rows({3}), "sk")}, page_size=2)

        first = dynamodb.Table("t").query(**self.KEY, **self.FILTER)

        assert first["Items"] == []
        assert "LastEvaluatedKey" in first

    def test_last_page_has_no_continuation_key_even_when_empty(self):
        _, table = _table(self._rows(set(), count=2))

        response = table.query(**self.KEY, **self.FILTER, Limit=5)

        assert response["Items"] == []
        assert "LastEvaluatedKey" not in response

    def test_query_without_filter_is_unchanged(self):
        _, table = _table(self._rows({1}))

        response = table.query(**self.KEY, Limit=4)

        assert [item["sk"] for item in response["Items"]] == ["01", "02", "03", "04"]
        assert response["Count"] == response["ScannedCount"] == 4
