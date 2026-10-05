"""``RecordReader``（design.md §4）。

対応: AC-5（範囲の縛り）、AC-7（記録がない・読めない）、AC-11（書き込みなし）。
AC-11 は ``conftest.py`` の自動適用フィクスチャでも、すべてのテストについて確かめている。
"""

import inspect
import json
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from . import fakes, scenarios

OTHER_EXECUTION_ID = "exec-20250101000000-ffffffff"


def _with_other_execution(scenario):
    """同じテーブルに、別の実行の記録を混ぜる（読んではいけないもの）。"""
    scenario.steps.append(scenarios.make_step(OTHER_EXECUTION_ID, 9, status="completed"))
    scenario.events.append(scenarios.make_event(OTHER_EXECUTION_ID, 1, "error", {"error_type": "OtherError"}))
    return scenario


# ---------------------------------------------------------------------------
# 実行レコード
# ---------------------------------------------------------------------------


class TestGetExecution:
    def test_get_execution_with_existing_record_returns_it_from_executions_table(self):
        scenario = scenarios.rate_limited()
        bundle = fakes.build_reader(scenario)

        record = bundle.reader.get_execution()

        assert record["execution_id"] == scenario.execution_id
        assert record["status"] == "failed"
        ops = bundle.dynamodb.operations
        assert [(op.name, op.target) for op in ops] == [("get_item", fakes.EXECUTIONS_TABLE)]
        assert ops[0].kwargs["Key"] == {"execution_id": scenario.execution_id}

    def test_get_execution_with_decimal_values_converts_them_to_plain_numbers(self):
        bundle = fakes.build_reader(scenarios.rate_limited())

        record = bundle.reader.get_execution()

        assert record["total_tokens_used"] == 12345
        assert type(record["total_tokens_used"]) is int
        assert record["total_cost_usd"] == pytest.approx(0.4321)
        assert type(record["total_cost_usd"]) is float
        assert not any(isinstance(v, Decimal) for v in record.values())
        json.dumps(record, ensure_ascii=False)  # Decimal が残っていれば失敗する

    def test_get_execution_with_missing_record_returns_none(self):
        scenario = scenarios.rate_limited()
        scenario.execution = None
        bundle = fakes.build_reader(scenario)

        assert bundle.reader.get_execution() is None


# ---------------------------------------------------------------------------
# 段階
# ---------------------------------------------------------------------------


class TestListSteps:
    def test_list_steps_with_three_steps_returns_them_in_step_order(self):
        scenario = scenarios.research_all_failed()
        # step_id の辞書順と step_order が食い違うようにする
        scenario.steps[0]["step_id"], scenario.steps[2]["step_id"] = "z-first", "a-third"
        bundle = fakes.build_reader(_with_other_execution(scenario))

        steps = bundle.reader.list_steps()

        assert [s["step_order"] for s in steps] == [1, 2, 3]
        assert [s["step_id"] for s in steps] == ["z-first", "research-2", "a-third"]
        assert all(type(s["step_order"]) is int for s in steps)

    def test_list_steps_queries_steps_table_with_bound_execution_id_only(self):
        scenario = _with_other_execution(scenarios.research_all_failed())
        bundle = fakes.build_reader(scenario)

        steps = bundle.reader.list_steps()

        # 別の実行の段階（research-9）は入らない。実行 ID の属性そのものは、返さなくてよい（C3: 必要な属性だけを取る）
        assert {s["step_id"] for s in steps} == {"research-1", "research-2", "research-3"}
        ops = bundle.dynamodb.operations
        assert ops
        assert {(op.name, op.target) for op in ops} == {("query", fakes.STEPS_TABLE)}
        for op in ops:
            assert fakes.partition_values(op.kwargs) == {scenario.execution_id}

    def test_list_steps_with_no_steps_returns_empty_list(self):
        bundle = fakes.build_reader(scenarios.records_missing())

        assert bundle.reader.list_steps() == []


# ---------------------------------------------------------------------------
# イベント
# ---------------------------------------------------------------------------


class TestListEvents:
    def test_list_events_returns_events_in_chronological_order(self):
        scenario = scenarios.output_validation_failed()
        scenario.events.reverse()  # 入れた順には依存しない
        bundle = fakes.build_reader(_with_other_execution(scenario))

        events = bundle.reader.list_events()

        assert [e["sk"] for e in events] == sorted(e["sk"] for e in events)
        assert len(events) == 9
        assert {e["execution_id"] for e in events} == {scenario.execution_id}

    def test_list_events_queries_events_table_with_bound_execution_id_only(self):
        scenario = scenarios.output_validation_failed()
        bundle = fakes.build_reader(scenario)

        bundle.reader.list_events()

        ops = bundle.dynamodb.operations
        assert ops
        assert {(op.name, op.target) for op in ops} == {("query", fakes.EVENTS_TABLE)}
        for op in ops:
            assert fakes.partition_values(op.kwargs) == {scenario.execution_id}

    def test_list_events_with_paginated_results_reads_every_page(self):
        scenario = scenarios.output_validation_failed()
        bundle = fakes.build_reader(scenario, page_size=2)

        events = bundle.reader.list_events()

        assert len(events) == 9
        assert [e["sk"] for e in events] == sorted(e["sk"] for e in scenario.events)
        assert len(bundle.dynamodb.ops("query", fakes.EVENTS_TABLE)) >= 5

    def test_list_events_with_more_than_500_events_returns_first_500(self):
        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(scenario.execution_id, n, "api_call_completed", {"success": True})
            for n in range(1, 521)
        ]
        bundle = fakes.build_reader(scenario, page_size=200)

        events = bundle.reader.list_events()

        assert len(events) == 500
        assert events[0]["sequence_number"] == 1
        assert events[-1]["sequence_number"] == 500

    def test_list_events_with_nested_decimal_payload_converts_numbers(self):
        bundle = fakes.build_reader(scenarios.output_validation_failed())

        events = bundle.reader.list_events()

        json.dumps(events, ensure_ascii=False)
        failed = next(e for e in events if e["event_type"] == "subagent_failed")
        assert failed["payload"]["duration_ms"] == 90000
        assert type(failed["payload"]["duration_ms"]) is int

    def test_list_events_with_no_events_returns_empty_list(self):
        bundle = fakes.build_reader(scenarios.records_missing())

        assert bundle.reader.list_events() == []

    def test_list_events_with_events_table_not_configured_raises_unavailable_without_reading(self):
        from diagnosis.records import RecordUnavailable

        bundle = fakes.build_reader(scenarios.rate_limited(), events_table="")

        with pytest.raises(RecordUnavailable):
            bundle.reader.list_events()
        assert bundle.dynamodb.operations == []


# ---------------------------------------------------------------------------
# プロンプト記録
# ---------------------------------------------------------------------------


class TestGetPromptRecord:
    def test_get_prompt_record_reads_fixed_bucket_and_key_prefix(self):
        scenario = scenarios.output_validation_failed()
        bundle = fakes.build_reader(scenario)

        record = bundle.reader.get_prompt_record("generator_text", "0")

        assert record["subagent"] == "generator_text"
        assert "VALIDATION_PROMPT_MARKER" in record["prompt"]
        assert "VALIDATION_OUTPUT_MARKER" in record["output"]
        ops = bundle.s3.operations
        assert [op.name for op in ops] == ["get_object"]
        assert ops[0].kwargs["Bucket"] == fakes.PROMPTS_BUCKET
        assert ops[0].kwargs["Key"] == f"prompts/{scenario.execution_id}/generator_text_0.json"

    def test_get_prompt_record_with_missing_key_raises_not_found(self):
        from diagnosis.records import RecordNotFound

        bundle = fakes.build_reader(scenarios.output_validation_failed())

        with pytest.raises(RecordNotFound):
            bundle.reader.get_prompt_record("reviewer_eval", "1")

    @pytest.mark.parametrize(("code", "status"), [("AccessDenied", 403), ("403", 403)])
    def test_get_prompt_record_with_permission_error_raises_unavailable(self, code, status):
        from diagnosis.records import RecordUnavailable

        bundle = fakes.build_reader(
            scenarios.output_validation_failed(), s3_error=fakes.client_error(code, status)
        )

        with pytest.raises(RecordUnavailable):
            bundle.reader.get_prompt_record("generator_text", "0")

    def test_get_prompt_record_with_bucket_not_configured_raises_unavailable_without_reading(self):
        from diagnosis.records import RecordUnavailable

        bundle = fakes.build_reader(scenarios.records_missing())

        with pytest.raises(RecordUnavailable):
            bundle.reader.get_prompt_record("researcher", "research-1")
        assert bundle.s3.operations == []

    def test_get_prompt_record_with_other_client_error_propagates_it(self):
        bundle = fakes.build_reader(
            scenarios.output_validation_failed(), s3_error=fakes.client_error("InternalError", 500)
        )

        with pytest.raises(ClientError):
            bundle.reader.get_prompt_record("generator_text", "0")

    def test_get_prompt_record_with_body_that_is_not_json_raises_unavailable(self):
        from diagnosis.records import RecordUnavailable

        scenario = scenarios.output_validation_failed()
        key = f"prompts/{scenario.execution_id}/researcher_broken.json"
        bundle = fakes.build_reader(scenario, raw_objects={(fakes.PROMPTS_BUCKET, key): b"not json {"})

        with pytest.raises(RecordUnavailable):
            bundle.reader.get_prompt_record("researcher", "broken")

    def test_get_prompt_record_with_body_over_one_megabyte_raises_too_large(self):
        from diagnosis.records import RecordTooLarge, RecordUnavailable

        scenario = scenarios.output_validation_failed()
        key = f"prompts/{scenario.execution_id}/researcher_huge.json"
        body = json.dumps({"subagent": "researcher", "index": "huge", "prompt": "p", "output": "x" * 1_100_000})
        bundle = fakes.build_reader(scenario, raw_objects={(fakes.PROMPTS_BUCKET, key): body.encode("utf-8")})

        # 修正仕様 L4: 1MB を超える記録は「読めない」ではなく「大きすぎる」（ツールは size_limit を返す）
        with pytest.raises(RecordTooLarge) as raised:
            bundle.reader.get_prompt_record("researcher", "huge")
        assert not isinstance(raised.value, RecordUnavailable)


# ---------------------------------------------------------------------------
# AC-5・AC-11: 範囲の縛りと読み取りだけ
# ---------------------------------------------------------------------------


class TestScope:
    @pytest.mark.parametrize("method", ["get_execution", "list_steps", "list_events", "get_prompt_record"])
    def test_reader_methods_do_not_accept_execution_table_bucket_or_key(self, method):
        from diagnosis.records import RecordReader

        params = set(inspect.signature(getattr(RecordReader, method)).parameters) - {"self"}

        expected = {"subagent", "index"} if method == "get_prompt_record" else set()
        assert params == expected

    def test_reader_after_reading_everything_used_only_read_operations_and_fixed_targets(self):
        scenario = _with_other_execution(scenarios.output_validation_failed())
        bundle = fakes.build_reader(scenario)

        bundle.reader.get_execution()
        bundle.reader.list_steps()
        bundle.reader.list_events()
        bundle.reader.get_prompt_record("generator_text", "0")

        assert {op.name for op in bundle.all_operations()} == {"get_item", "query", "get_object"}
        assert {op.target for op in bundle.dynamodb.operations} == {
            fakes.EXECUTIONS_TABLE,
            fakes.STEPS_TABLE,
            fakes.EVENTS_TABLE,
        }
        assert set(bundle.dynamodb.requested_tables) <= {
            fakes.EXECUTIONS_TABLE,
            fakes.STEPS_TABLE,
            fakes.EVENTS_TABLE,
        }
        for op in bundle.s3.operations:
            assert op.kwargs["Bucket"] == fakes.PROMPTS_BUCKET
            assert op.kwargs["Key"].startswith(f"prompts/{scenario.execution_id}/")


# ---------------------------------------------------------------------------
# 実装の後に決まった振る舞い（design.md §17 の 3・21）と、失敗の扱い
# ---------------------------------------------------------------------------


class TestPermissionErrors:
    @pytest.mark.parametrize(
        ("code", "status"),
        [
            ("AccessDenied", 400),  # エラーコードだけで判定できる
            ("403", 400),
            ("Forbidden", 403),  # コードが別でも、HTTP ステータスが 403 なら読めない
            ("", 403),  # コードが付かない 403
            ("SignatureDoesNotMatch", 403),
        ],
    )
    def test_get_prompt_record_with_code_or_http_403_raises_unavailable(self, code, status):
        """design.md §17 の 3。"""
        from diagnosis.records import RecordUnavailable

        bundle = fakes.build_reader(scenarios.output_validation_failed(), s3_error=fakes.client_error(code, status))

        with pytest.raises(RecordUnavailable):
            bundle.reader.get_prompt_record("generator_text", "0")

    @pytest.mark.parametrize(("code", "status"), [("NoSuchBucket", 404), ("SlowDown", 503), ("Throttling", 400)])
    def test_get_prompt_record_with_other_error_code_propagates_client_error(self, code, status):
        bundle = fakes.build_reader(scenarios.output_validation_failed(), s3_error=fakes.client_error(code, status))

        with pytest.raises(ClientError):
            bundle.reader.get_prompt_record("generator_text", "0")

    def test_get_prompt_record_with_client_error_without_metadata_propagates_it(self):
        error = ClientError({"Error": {"Code": "Throttling"}}, "GetObject")
        bundle = fakes.build_reader(scenarios.output_validation_failed(), s3_error=error)

        with pytest.raises(ClientError):
            bundle.reader.get_prompt_record("generator_text", "0")

    def test_get_prompt_record_with_client_error_without_error_code_propagates_it(self):
        error = ClientError({}, "GetObject")
        bundle = fakes.build_reader(scenarios.output_validation_failed(), s3_error=error)

        with pytest.raises(ClientError):
            bundle.reader.get_prompt_record("generator_text", "0")

    def test_get_prompt_record_with_missing_key_is_not_reported_as_unavailable(self):
        from diagnosis.records import RecordNotFound, RecordUnavailable

        bundle = fakes.build_reader(scenarios.output_validation_failed())

        with pytest.raises(RecordNotFound) as raised:
            bundle.reader.get_prompt_record("researcher", "nothing-here")
        assert not isinstance(raised.value, RecordUnavailable)


class TestPromptRecordBody:
    def _reader_with_raw(self, body: bytes, index: str = "raw"):
        scenario = scenarios.output_validation_failed()
        key = f"prompts/{scenario.execution_id}/researcher_{index}.json"
        return fakes.build_reader(scenario, raw_objects={(fakes.PROMPTS_BUCKET, key): body})

    @pytest.mark.parametrize("body", [b"[1, 2, 3]", b'"just a string"', b"42", b"null", b"true"])
    def test_get_prompt_record_with_json_that_is_not_an_object_raises_unavailable(self, body):
        from diagnosis.records import RecordUnavailable

        bundle = self._reader_with_raw(body)

        with pytest.raises(RecordUnavailable):
            bundle.reader.get_prompt_record("researcher", "raw")

    @pytest.mark.parametrize("body", [b"\xff\xfe\x00broken", b"", b'{"prompt": "\xe3\x81"}'])
    def test_get_prompt_record_with_undecodable_or_empty_body_raises_unavailable(self, body):
        from diagnosis.records import RecordUnavailable

        bundle = self._reader_with_raw(body)

        with pytest.raises(RecordUnavailable):
            bundle.reader.get_prompt_record("researcher", "raw")

    def test_get_prompt_record_with_body_exactly_one_megabyte_is_read(self):
        skeleton = json.dumps({"prompt": "", "output": "o"})
        body = json.dumps({"prompt": "p" * (1_000_000 - len(skeleton)), "output": "o"}).encode("utf-8")
        assert len(body) == 1_000_000
        bundle = self._reader_with_raw(body)

        record = bundle.reader.get_prompt_record("researcher", "raw")

        assert record["output"] == "o"
        assert len(record["prompt"]) == 1_000_000 - len(skeleton)

    def test_get_prompt_record_with_body_one_byte_over_one_megabyte_raises_too_large(self):
        from diagnosis.records import RecordNotFound, RecordTooLarge, RecordUnavailable

        skeleton = json.dumps({"prompt": "", "output": "o"})
        body = json.dumps({"prompt": "p" * (1_000_001 - len(skeleton)), "output": "o"}).encode("utf-8")
        assert len(body) == 1_000_001
        bundle = self._reader_with_raw(body)

        with pytest.raises(RecordTooLarge) as raised:
            bundle.reader.get_prompt_record("researcher", "raw")
        assert not isinstance(raised.value, RecordUnavailable | RecordNotFound)

    def test_get_prompt_record_over_one_megabyte_that_is_not_json_raises_too_large(self):
        """大きさの判定が先。中身が JSON かどうかは見ない。"""
        from diagnosis.records import RecordTooLarge

        bundle = self._reader_with_raw(b"x" * 1_200_000)

        with pytest.raises(RecordTooLarge):
            bundle.reader.get_prompt_record("researcher", "raw")

    def test_get_prompt_record_reads_japanese_text(self):
        body = json.dumps({"prompt": "日本語の指示", "output": "日本語の出力"}, ensure_ascii=False).encode("utf-8")
        bundle = self._reader_with_raw(body)

        record = bundle.reader.get_prompt_record("researcher", "raw")

        assert record == {"prompt": "日本語の指示", "output": "日本語の出力"}


class TestQueryShape:
    def test_queries_bind_only_the_execution_id_and_read_ascending(self):
        """design.md §17 の 21: キーの条件の値は、束ねた実行 ID だけ。昇順で読む。"""
        scenario = _with_other_execution(scenarios.output_validation_failed())
        bundle = fakes.build_reader(scenario, page_size=2)

        bundle.reader.list_steps()
        bundle.reader.list_events()

        queries = bundle.dynamodb.ops("query")
        assert len(queries) > 2  # 複数ページを読んでいる
        for op in queries:
            assert fakes.partition_values(op.kwargs) == {scenario.execution_id}
            assert op.kwargs.get("ScanIndexForward", True) is True
            assert "IndexName" not in op.kwargs
            if op.target == fakes.EVENTS_TABLE:
                assert "FilterExpression" not in op.kwargs
            values = op.kwargs.get("ExpressionAttributeValues")
            if values is not None:
                assert set(values.values()) == {scenario.execution_id}

    def test_list_events_stops_reading_once_500_events_are_collected(self):
        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(scenario.execution_id, n, "api_call_completed", {"success": True})
            for n in range(1, 1201)
        ]
        bundle = fakes.build_reader(scenario, page_size=250)

        events = bundle.reader.list_events()

        assert len(events) == 500
        assert [e["sequence_number"] for e in events] == list(range(1, 501))
        assert len(bundle.dynamodb.ops("query", fakes.EVENTS_TABLE)) == 2  # 250 件 × 2 ページで止まる

    def test_list_events_with_exactly_500_events_returns_all(self):
        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(scenario.execution_id, n, "api_call_completed", {"success": True})
            for n in range(1, 501)
        ]
        bundle = fakes.build_reader(scenario, page_size=200)

        assert len(bundle.reader.list_events()) == 500

    def test_list_steps_with_missing_or_malformed_step_order_puts_them_last_without_raising(self):
        scenario = scenarios.research_all_failed()
        del scenario.steps[0]["step_order"]
        scenario.steps[1]["step_order"] = "second"
        bundle = fakes.build_reader(scenario)

        steps = bundle.reader.list_steps()

        assert len(steps) == 3
        assert steps[0]["step_id"] == "research-3"  # step_order が数値のものが先
        assert {s["step_id"] for s in steps[1:]} == {"research-1", "research-2"}

    def test_reader_does_not_read_anything_on_construction(self):
        bundle = fakes.build_reader(scenarios.output_validation_failed())

        assert bundle.all_operations() == []
        assert bundle.dynamodb.requested_tables == []


# ---------------------------------------------------------------------------
# レビュー後の修正仕様 M5: DynamoDB の集合型・バイト列を、JSON にできる値に平らにする
# ---------------------------------------------------------------------------


class TestPlainValues:
    def _events_with_payload(self, payload: dict) -> list[dict]:
        scenario = scenarios.rate_limited()
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", payload)]
        return fakes.build_reader(scenario).reader.list_events()

    def test_string_set_becomes_sorted_list(self):
        events = self._events_with_payload({"tags": {"b", "c", "a"}})

        assert events[0]["payload"]["tags"] == ["a", "b", "c"]

    def test_number_set_becomes_sorted_list_of_plain_numbers(self):
        events = self._events_with_payload({"numbers": {Decimal("10"), Decimal("2"), Decimal("1.5")}})

        assert events[0]["payload"]["numbers"] == [1.5, 2, 10]
        assert not any(isinstance(v, Decimal) for v in events[0]["payload"]["numbers"])

    def test_frozenset_becomes_sorted_list(self):
        events = self._events_with_payload({"tags": frozenset({"y", "x"})})

        assert events[0]["payload"]["tags"] == ["x", "y"]

    def test_bytes_and_boto3_binary_become_fixed_placeholder(self):
        from boto3.dynamodb.types import Binary

        events = self._events_with_payload(
            {"blob": b"RAW_BYTES_1", "binary": Binary(b"RAW_BYTES_2"), "array": bytearray(b"RAW_BYTES_3")}
        )

        assert events[0]["payload"]["blob"] == "<binary>"
        assert events[0]["payload"]["binary"] == "<binary>"
        assert "RAW_BYTES_1" not in json.dumps(events, default=str)
        assert "RAW_BYTES_2" not in json.dumps(events, default=str)

    def test_set_of_binary_values_becomes_list_of_placeholders(self):
        events = self._events_with_payload({"blobs": {b"one", b"two"}})

        assert events[0]["payload"]["blobs"] == ["<binary>", "<binary>"]

    def test_nested_sets_are_flattened_at_every_depth(self):
        events = self._events_with_payload({"outer": [{"inner": {"z", "y"}}, {"deep": {"k": {"q", "p"}}}]})

        assert events[0]["payload"]["outer"] == [{"inner": ["y", "z"]}, {"deep": {"k": ["p", "q"]}}]

    def test_other_values_that_json_cannot_hold_become_strings(self):
        import datetime

        moment = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
        events = self._events_with_payload({"when": moment, "pair": (1, 2)})

        assert events[0]["payload"]["when"] == str(moment)
        json.dumps(events, ensure_ascii=False)  # JSON にできない値が残っていれば失敗する

    def test_flattened_records_are_json_serializable(self):
        from boto3.dynamodb.types import Binary

        scenario = scenarios.rate_limited()
        scenario.execution["labels"] = {"a", "b"}
        scenario.execution["raw"] = Binary(b"x")
        scenario.steps = [scenarios.make_step(scenario.execution_id, 1, status="failed", result={"ids": {"s1"}})]
        bundle = fakes.build_reader(scenario)

        json.dumps(bundle.reader.get_execution(), ensure_ascii=False)
        json.dumps(bundle.reader.list_steps(), ensure_ascii=False)
        assert bundle.reader.get_execution()["labels"] == ["a", "b"]
        assert bundle.reader.get_execution()["raw"] == "<binary>"

    def test_ordinary_values_are_unchanged(self):
        payload = {"text": "文字列", "number": 3, "ratio": 0.5, "flag": True, "none": None, "list": [1, "a"]}

        events = self._events_with_payload(dict(payload))

        assert events[0]["payload"] == payload
        assert type(events[0]["payload"]["flag"]) is bool


# ---------------------------------------------------------------------------
# レビュー後の修正仕様 L6: イベントの合計の大きさの上限（2MB）
# ---------------------------------------------------------------------------


class TestEventsTotalSize:
    def _reader(self, count: int, chars: int, **kwargs):
        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(scenario.execution_id, n, "api_call_completed", {"n": n, "body": "b" * chars})
            for n in range(1, count + 1)
        ]
        return fakes.build_reader(scenario, **kwargs)

    def test_list_events_stops_reading_at_about_two_megabytes(self):
        bundle = self._reader(100, 40_000, page_size=10)  # 合計およそ 4MB

        events = bundle.reader.list_events()

        assert 0 < len(events) < 100
        assert [e["sequence_number"] for e in events] == list(range(1, len(events) + 1))  # 先頭から、抜けなく
        assert len(json.dumps(events, ensure_ascii=False).encode("utf-8")) <= 2_100_000
        assert len(bundle.dynamodb.ops("query", fakes.EVENTS_TABLE)) < 10

    def test_list_events_under_two_megabytes_reads_everything(self):
        bundle = self._reader(40, 40_000)  # 合計およそ 1.6MB

        assert len(bundle.reader.list_events()) == 40

    def test_one_event_larger_than_the_cap_does_not_raise(self):
        bundle = self._reader(3, 390_000)  # DynamoDB の 1 項目の上限（400KB）に近い大きさ

        assert len(bundle.reader.list_events()) == 3


# ---------------------------------------------------------------------------
# クロスレビュー後の修正仕様 C3: 段階の読み込みの上限と、必要な属性だけの取得
# ---------------------------------------------------------------------------

STEP_ATTRIBUTES = {"step_id", "phase", "step_name", "step_order", "status", "started_at", "completed_at"}
BIG_RESULT_MARKER = "BIG_STEP_RESULT_BODY_5530"


def _steps_scenario(count: int, *, name_chars: int = 0, with_result: bool = True):
    scenario = scenarios.research_all_failed()
    scenario.steps = []
    for order in range(1, count + 1):
        result = {"summary": BIG_RESULT_MARKER + "r" * 2_000} if with_result else None
        step = scenarios.make_step(scenario.execution_id, order, status="completed", result=result)
        step["step_id"] = f"research-{order:04d}"
        if name_chars:
            step["step_name"] = "n" * name_chars
        scenario.steps.append(step)
    return scenario


class TestStepsLimits:
    def test_list_steps_reads_at_most_200_steps(self):
        bundle = fakes.build_reader(_steps_scenario(250), page_size=40)

        steps = bundle.reader.list_steps()

        assert len(steps) == 200
        assert [s["step_order"] for s in steps] == list(range(1, 201))
        assert len(bundle.dynamodb.ops("query", fakes.STEPS_TABLE)) <= 6  # 最後のページまでは読まない

    def test_list_steps_with_exactly_200_steps_reads_all(self):
        bundle = fakes.build_reader(_steps_scenario(200), page_size=80)

        assert len(bundle.reader.list_steps()) == 200

    def test_list_steps_stops_reading_at_about_two_megabytes(self):
        bundle = fakes.build_reader(_steps_scenario(30, name_chars=100_000, with_result=False), page_size=5)  # 3MB

        steps = bundle.reader.list_steps()

        assert 0 < len(steps) < 30
        assert len(json.dumps(steps, ensure_ascii=False).encode("utf-8")) <= 2_150_000
        assert len(bundle.dynamodb.ops("query", fakes.STEPS_TABLE)) < 6

    def test_list_steps_under_the_limits_reads_everything(self):
        bundle = fakes.build_reader(_steps_scenario(150))

        assert len(bundle.reader.list_steps()) == 150

    def test_step_results_do_not_count_toward_the_size_limit(self):
        """result は読まないので、どれだけ大きくても、段階は読み止められない。"""
        scenario = _steps_scenario(20)
        for step in scenario.steps:
            step["result"] = {"body": "r" * 300_000}  # 合計 6MB
        bundle = fakes.build_reader(scenario)

        assert len(bundle.reader.list_steps()) == 20


class TestStepsProjection:
    def _projection(self, op) -> set[str]:
        attributes = fakes.projected_attributes(op.kwargs)
        assert attributes is not None, "the steps query has no ProjectionExpression"
        return set(attributes)

    def test_steps_query_projects_only_the_attributes_it_returns(self):
        bundle = fakes.build_reader(_steps_scenario(3), page_size=2)

        bundle.reader.list_steps()

        queries = bundle.dynamodb.ops("query", fakes.STEPS_TABLE)
        assert queries
        listing = [op for op in queries if "FilterExpression" not in op.kwargs]
        assert listing
        for op in listing:
            attributes = self._projection(op)
            assert attributes >= STEP_ATTRIBUTES
            assert attributes <= STEP_ATTRIBUTES | {"execution_id"}
        for op in queries:
            assert "result" not in self._projection(op)  # どの問い合わせも、result の本体を取らない
            assert fakes.partition_values(op.kwargs) == {bundle.execution_id}

    def test_steps_query_escapes_reserved_attribute_names(self):
        """``status`` は DynamoDB の予約語。そのまま式に書くと、実物は ValidationException を返す。"""
        bundle = fakes.build_reader(_steps_scenario(3))

        bundle.reader.list_steps()

        for op in bundle.dynamodb.ops("query", fakes.STEPS_TABLE):
            expression = op.kwargs["ProjectionExpression"]
            names = op.kwargs.get("ExpressionAttributeNames") or {}
            tokens = [token.strip() for token in expression.split(",")]
            assert "status" not in [token.lower() for token in tokens]
            assert "result" not in [token.lower() for token in tokens]
            assert "status" in {names.get(token, token) for token in tokens}

    def test_steps_returned_by_the_reader_do_not_carry_result_bodies(self):
        bundle = fakes.build_reader(_steps_scenario(5))

        steps = bundle.reader.list_steps()

        assert BIG_RESULT_MARKER not in json.dumps(steps, ensure_ascii=False, default=str)
        for step in steps:
            assert set(step) >= STEP_ATTRIBUTES

    def test_events_query_is_not_projected(self):
        """イベントは payload を含めて返すので、今までどおり全部の属性を読む。"""
        bundle = fakes.build_reader(scenarios.output_validation_failed())

        events = bundle.reader.list_events()

        assert all("payload" in event for event in events)


class TestStepsQueryAgainstRealBoto3:
    """実物の boto3 に、通信なしで（``botocore.stub.Stubber``）問い合わせの引数を検証させる。

    Stubber は、引数をサービスの定義と照合する（型の誤り、未定義の引数は ParamValidationError になる）。
    式の中身（予約語など）までは見ないので、そこは偽の DynamoDB の側で確かめている。
    """

    def _reader(self):
        import boto3
        from botocore.stub import Stubber
        from diagnosis.records import RecordReader

        # 通信はしない（Stubber が応答を返す）。認証情報は、署名に進まないので使われない
        offline = {"region_name": "ap-northeast-1", "aws_access_key_id": "dummy", "aws_secret_access_key": "dummy"}
        dynamodb = boto3.resource("dynamodb", **offline)
        sent: list[dict] = []
        dynamodb.meta.client.meta.events.register(
            "provide-client-params.dynamodb.Query", lambda params, **_kwargs: sent.append(dict(params))
        )
        reader = RecordReader(
            scenarios.rate_limited().execution_id,
            dynamodb=dynamodb,
            s3=None,
            table_prefix=fakes.TABLE_PREFIX,
            events_table=fakes.EVENTS_TABLE,
            prompts_bucket="",
        )
        return reader, Stubber(dynamodb.meta.client), sent

    def test_steps_query_is_accepted_by_boto3_and_items_are_converted(self):
        reader, stubber, sent = self._reader()
        item = {
            "step_id": {"S": "research-1"},
            "phase": {"S": "research"},
            "step_name": {"S": "調査"},
            "step_order": {"N": "1"},
            "status": {"S": "failed"},
            "started_at": {"S": "2026-10-01T09:01:01.000000+00:00"},
        }
        with stubber:
            # 問い合わせを 2 回する実装（結果の有無を別に確かめる、など）でも通るよう、同じ応答を 2 つ用意する
            stubber.add_response("query", {"Items": [item], "Count": 1, "ScannedCount": 1})
            stubber.add_response("query", {"Items": [item], "Count": 1, "ScannedCount": 1})
            steps = reader.list_steps()

        assert [(s["step_id"], s["step_order"], s["status"]) for s in steps] == [("research-1", 1, "failed")]
        assert type(steps[0]["step_order"]) is int
        assert sent
        for params in sent:
            assert params["TableName"] == fakes.STEPS_TABLE
            assert isinstance(params["ProjectionExpression"], str)
            assert "status" in set((params.get("ExpressionAttributeNames") or {}).values())
            assert fakes.partition_values(params) == {scenarios.rate_limited().execution_id}

    def test_events_query_is_accepted_by_boto3(self):
        reader, stubber, sent = self._reader()
        item = {
            "execution_id": {"S": scenarios.rate_limited().execution_id},
            "sk": {"S": "2026-10-01T09:00:01.000Z#00001"},
            "event_type": {"S": "error"},
            "payload": {"M": {"error_type": {"S": "KeyError"}, "tags": {"SS": ["b", "a"]}, "n": {"N": "3"}}},
        }
        with stubber:
            stubber.add_response("query", {"Items": [item], "Count": 1, "ScannedCount": 1})
            events = reader.list_events()

        assert events[0]["payload"] == {"error_type": "KeyError", "tags": ["a", "b"], "n": 3}
        assert sent[0]["TableName"] == fakes.EVENTS_TABLE
        assert sent[0]["Limit"] <= 500


# ---------------------------------------------------------------------------
# テストの弱点への対応（再レビューの指摘）: 絞り込みで空になるページ、Body を閉じること
# ---------------------------------------------------------------------------


def _steps_with_results(count: int, with_result: set[int]):
    scenario = scenarios.research_all_failed()
    scenario.steps = []
    for order in range(1, count + 1):
        result = {"summary": "r"} if order in with_result else None
        step = scenarios.make_step(scenario.execution_id, order, status="completed", result=result)
        step["step_id"] = f"research-{order:04d}"
        scenario.steps.append(step)
    return scenario


def _result_queries(bundle) -> list:
    return [op for op in bundle.dynamodb.ops("query", fakes.STEPS_TABLE) if "FilterExpression" in op.kwargs]


class TestStepResultLookupAcrossEmptyPages:
    """実物の DynamoDB は、ページを切ってから絞り込む。

    「空のページ + 続きのキー」の後ろにも、結果を持つ段階がありうる。
    """

    def test_result_after_empty_pages_is_still_found(self):
        """空のページで読み終える実装は、ここで取りこぼす。"""
        bundle = fakes.build_reader(_steps_with_results(12, {11}), page_size=5)

        with_result = bundle.reader.list_step_ids_with_result()

        assert with_result == {"research-0011"}
        assert bundle.reader.step_results_truncated is False
        assert len(_result_queries(bundle)) == 3  # 5 件 + 5 件（どちらも空）+ 2 件

    def test_results_on_both_sides_of_an_empty_page_are_found(self):
        bundle = fakes.build_reader(_steps_with_results(15, {2, 14}), page_size=5)

        assert bundle.reader.list_step_ids_with_result() == {"research-0002", "research-0014"}

    def test_lookup_stops_after_ten_queries_and_reports_truncation(self):
        """問い合わせの回数の上限（10 回）。空のページが続いても、際限なく読まない。"""
        bundle = fakes.build_reader(_steps_with_results(150, {149}), page_size=5)

        with_result = bundle.reader.list_step_ids_with_result()

        assert len(_result_queries(bundle)) == 10
        assert with_result == set()  # 10 回（50 件ぶん）では届かない
        assert bundle.reader.step_results_truncated is True

    def test_lookup_that_finishes_within_ten_queries_is_not_truncated(self):
        bundle = fakes.build_reader(_steps_with_results(50, {50}), page_size=5)

        assert bundle.reader.list_step_ids_with_result() == {"research-0050"}
        assert len(_result_queries(bundle)) == 10
        assert bundle.reader.step_results_truncated is False

    def test_tool_reports_unknown_result_presence_when_lookup_was_cut(self):
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        from .factories import parse

        bundle = fakes.build_reader(_steps_with_results(150, {1, 149}), page_size=5)
        limits = DiagnosisLimits(max_result_bytes=1_000_000, max_total_bytes=5_000_000)
        session = ToolSession(bundle.reader, limits)

        steps = parse(session.call("list_steps", {}))["steps"]

        assert steps[0]["has_result"] is True
        assert steps[148]["has_result"] is None  # 調べ切れていないので「無い」とは言わない
        assert len(_result_queries(bundle)) == 10

    def test_tool_finds_result_behind_empty_pages(self):
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        from .factories import parse

        bundle = fakes.build_reader(_steps_with_results(12, {11}), page_size=5)
        session = ToolSession(bundle.reader, DiagnosisLimits())

        steps = parse(session.call("list_steps", {}))["steps"]

        assert [s["has_result"] for s in steps] == [False] * 10 + [True, False]


class TestPromptRecordBodyIsClosed:
    """S3 の Body は、読み取りの後に必ず閉じる（成功・大きすぎる・読めない・例外のいずれでも）。"""

    def _bundle(self, body: bytes):
        scenario = scenarios.output_validation_failed()
        key = f"prompts/{scenario.execution_id}/researcher_raw.json"
        return fakes.build_reader(scenario, raw_objects={(fakes.PROMPTS_BUCKET, key): body})

    def _assert_closed(self, bundle) -> None:
        assert len(bundle.s3.bodies) == 1
        assert bundle.s3.bodies[0].closed is True

    def test_body_is_closed_after_successful_read(self):
        bundle = self._bundle(b'{"prompt": "p", "output": "o"}')

        assert bundle.reader.get_prompt_record("researcher", "raw") == {"prompt": "p", "output": "o"}
        self._assert_closed(bundle)

    def test_body_is_closed_when_record_is_too_large(self):
        from diagnosis.records import RecordTooLarge

        bundle = self._bundle(b"x" * 1_200_000)

        with pytest.raises(RecordTooLarge):
            bundle.reader.get_prompt_record("researcher", "raw")
        self._assert_closed(bundle)

    def test_body_is_closed_when_record_is_not_json(self):
        from diagnosis.records import RecordUnavailable

        bundle = self._bundle(b"not json {")

        with pytest.raises(RecordUnavailable):
            bundle.reader.get_prompt_record("researcher", "raw")
        self._assert_closed(bundle)

    def test_body_is_closed_when_reading_raises(self):
        bundle = self._bundle(b'{"prompt": "p"}')
        bundle.s3.read_error = ConnectionError("READ_FAILED_7733 connection reset while reading")

        with pytest.raises(ConnectionError):
            bundle.reader.get_prompt_record("researcher", "raw")
        self._assert_closed(bundle)

    def test_body_is_read_with_a_size_bound(self):
        """上限を 1 バイト超えるところまでしか読まない（全体を読み込まない）。"""
        bundle = self._bundle(b"x" * 3_000_000)

        with pytest.raises(Exception):  # noqa: B017, PT011
            bundle.reader.get_prompt_record("researcher", "raw")

        sizes = bundle.s3.bodies[0].read_sizes
        assert sizes
        assert all(size is not None and 0 < size <= 1_000_001 for size in sizes)
        assert sum(sizes) <= 1_000_001

    def test_tool_closes_body_and_returns_internal_error_when_reading_raises(self):
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        bundle = self._bundle(b'{"prompt": "p"}')
        bundle.s3.read_error = ConnectionError("READ_FAILED_7733")
        session = ToolSession(bundle.reader, DiagnosisLimits())

        result = session.call("get_prompt_record", {"subagent": "researcher", "index": "raw"})

        assert result.outcome == "internal_error"
        assert "READ_FAILED_7733" not in result.to_text()
        self._assert_closed(bundle)
