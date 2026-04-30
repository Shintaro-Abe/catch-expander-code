"""src/observability/event_emitter.py の単体テスト。

T1-1 (tasklist) で要求される完了条件をカバー:
- emit 正常系 (item の構造、TTL、sequence_number)
- DDB 書き込み失敗時の logging (例外を呼出元に伝播させない)
- EVENTS_TABLE 未設定時の graceful skip
- sequence_number の単調増加 (同インスタンス内で連続 emit)
"""

from __future__ import annotations

import importlib
import logging
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def reload_module():
    """observability モジュールを毎回 re-import するヘルパー。

    EventEmitter は env var を __init__ で読むため、テストごとに env var を変えて
    新しいインスタンスを生成すれば十分 (モジュール re-import は不要)。本フィクスチャは
    将来モジュールレベルで env を読む拡張が入った場合に備えて置いてある。
    """
    import src.observability.event_emitter as mod

    importlib.reload(mod)
    return mod


class TestEventEmitterWithTable:
    """EVENTS_TABLE が設定されている前提のテスト群。"""

    @patch("boto3.resource")
    def test_emit_writes_item_with_required_fields(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")
        from src.observability.event_emitter import EventEmitter

        mock_table = MagicMock()
        mock_boto3.return_value.Table.return_value = mock_table

        emitter = EventEmitter("exec-test-123")
        emitter.emit(
            "workflow_planned",
            {"topic_category": "技術", "planned_subagents": ["researcher"]},
        )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["execution_id"] == "exec-test-123"
        assert item["event_type"] == "workflow_planned"
        assert item["sequence_number"] == 1
        assert item["status_at_emit"] == "in_progress"
        assert item["payload"]["topic_category"] == "技術"
        assert item["gsi_pk"] == "GLOBAL"
        # sk は timestamp#sequence の形式
        assert "#00001" in item["sk"]

    @patch("boto3.resource")
    def test_ttl_is_90_days_in_future(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")
        from src.observability.event_emitter import EventEmitter

        mock_table = MagicMock()
        mock_boto3.return_value.Table.return_value = mock_table

        before = int(time.time())
        emitter = EventEmitter("exec-test-ttl")
        emitter.emit("topic_received", {"topic": "test"})
        after = int(time.time())

        item = mock_table.put_item.call_args.kwargs["Item"]
        ttl = item["ttl"]
        # 90 日 = 90*86400 = 7,776,000 秒
        assert before + 7_776_000 <= ttl <= after + 7_776_000

    @patch("boto3.resource")
    def test_sequence_number_increments_monotonically(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")
        from src.observability.event_emitter import EventEmitter

        mock_table = MagicMock()
        mock_boto3.return_value.Table.return_value = mock_table

        emitter = EventEmitter("exec-test-seq")
        emitter.emit("workflow_planned", {"a": 1})
        emitter.emit("research_completed", {"b": 2})
        emitter.emit("execution_completed", {"c": 3}, status_at_emit="success")

        items = [c.kwargs["Item"] for c in mock_table.put_item.call_args_list]
        assert [i["sequence_number"] for i in items] == [1, 2, 3]
        assert items[2]["status_at_emit"] == "success"

    @patch("boto3.resource")
    def test_sk_format_is_timestamp_then_sequence(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")
        from src.observability.event_emitter import EventEmitter

        mock_table = MagicMock()
        mock_boto3.return_value.Table.return_value = mock_table

        emitter = EventEmitter("exec-test-sk")
        emitter.emit("topic_received", {"topic": "X"})

        item = mock_table.put_item.call_args.kwargs["Item"]
        # sk は "<ISO8601 with Z>#<5 桁 sequence>" の形式
        sk = item["sk"]
        ts_part, seq_part = sk.split("#")
        assert ts_part.endswith("Z")
        assert ts_part == item["timestamp"]
        assert seq_part == "00001"

    @patch("boto3.resource")
    def test_ddb_write_failure_logs_error_does_not_raise(self, mock_boto3, monkeypatch, caplog):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")
        from src.observability.event_emitter import EventEmitter

        mock_table = MagicMock()
        mock_table.put_item.side_effect = RuntimeError("DDB unavailable")
        mock_boto3.return_value.Table.return_value = mock_table

        emitter = EventEmitter("exec-test-fail")
        with caplog.at_level(logging.ERROR):
            # best-effort: 例外は呼出元に伝播してはいけない
            emitter.emit("notion_stored", {"url": "https://www.notion.so/x"})

        assert any("Failed to emit event notion_stored" in r.message for r in caplog.records)

    @patch("boto3.resource")
    def test_emit_does_not_log_payload_on_failure(self, mock_boto3, monkeypatch, caplog):
        """エラーログに payload が含まれないことを保証 (NFR-3 / design.md §4.6)。"""
        monkeypatch.setenv("EVENTS_TABLE", "test-events")
        from src.observability.event_emitter import EventEmitter

        mock_table = MagicMock()
        mock_table.put_item.side_effect = RuntimeError("boom")
        mock_boto3.return_value.Table.return_value = mock_table

        secret_marker = "SUPER_SECRET_PII_THAT_MUST_NOT_BE_LOGGED"
        emitter = EventEmitter("exec-test-pii")
        with caplog.at_level(logging.ERROR):
            emitter.emit("topic_received", {"topic": secret_marker})

        for record in caplog.records:
            assert secret_marker not in record.message
            assert secret_marker not in record.getMessage()


class TestEventEmitterWithoutTable:
    """EVENTS_TABLE が未設定の場合の graceful skip テスト群。"""

    def test_emit_is_noop_when_events_table_unset(self, monkeypatch, caplog):
        monkeypatch.delenv("EVENTS_TABLE", raising=False)
        from src.observability.event_emitter import EventEmitter

        # boto3.resource を mock しなくても呼ばれないこと (graceful skip 設計)
        emitter = EventEmitter("exec-test-no-env")
        with caplog.at_level(logging.WARNING):
            emitter.emit("workflow_planned", {"x": 1})

        # 警告ログだけ出して、例外なく完了する
        assert any("EVENTS_TABLE env var not set" in r.message for r in caplog.records)

    def test_import_does_not_fail_when_events_table_unset(self, monkeypatch):
        monkeypatch.delenv("EVENTS_TABLE", raising=False)
        # import 自体が graceful (副作用ゼロ)
        from src.observability.event_emitter import EventEmitter

        # インスタンス化も graceful (boto3 にも触らない)
        emitter = EventEmitter("exec-test-import")
        assert emitter.execution_id == "exec-test-import"
        assert emitter._sequence == 0


class TestEventEmitterSyntheticIds:
    """token_monitor が使う合成 ID パターンの確認 (design.md §2.5)。"""

    @patch("boto3.resource")
    def test_synthetic_token_refresh_id_is_accepted(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")
        from src.observability.event_emitter import EventEmitter

        mock_table = MagicMock()
        mock_boto3.return_value.Table.return_value = mock_table

        # token_monitor 流の合成 ID
        synthetic_id = f"system-token-refresh-{int(time.time())}"
        emitter = EventEmitter(synthetic_id)
        emitter.emit("oauth_refresh_completed", {"status": "ok"})

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["execution_id"].startswith("system-token-refresh-")
        assert item["event_type"] == "oauth_refresh_completed"
