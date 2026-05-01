"""src/dashboard_api/list_executions/app.py の単体テスト。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _event(**kwargs) -> dict:
    return {"queryStringParameters": kwargs or None}


def _exec_item(eid: str, status: str = "completed", topic: str = "テスト") -> dict:
    return {
        "execution_id": eid,
        "status": status,
        "topic": topic,
        "created_at": "2026-05-01T10:00:00Z",
    }


def _event_items(*eids: str, ts: str = "2026-05-01T10:00:00.000Z") -> list:
    return [{"execution_id": eid, "timestamp": ts} for eid in eids]


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("EVENTS_TABLE", "test-events")
    monkeypatch.setenv("EXECUTIONS_TABLE", "test-executions")


@pytest.fixture()
def mock_tables():
    resource = MagicMock()

    events_table = MagicMock()
    exec_table = MagicMock()

    def _table_selector(name):
        return events_table if name == "test-events" else exec_table

    resource.Table.side_effect = _table_selector

    with patch("src.dashboard_api.list_executions.app._dynamodb", resource):
        yield events_table, exec_table


class TestBasicQuery:
    def test_returns_200_with_data_and_meta(self, mock_tables):
        events_tbl, exec_tbl = mock_tables
        events_tbl.query.return_value = {
            "Items": _event_items("exec-001"),
        }
        exec_tbl.get_item.return_value = {"Item": _exec_item("exec-001")}

        from src.dashboard_api.list_executions.app import lambda_handler

        result = lambda_handler(_event(), None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "data" in body
        assert "meta" in body

    def test_returns_executions_sorted_newest_first(self, mock_tables):
        events_tbl, exec_tbl = mock_tables
        events_tbl.query.return_value = {
            "Items": [
                {"execution_id": "exec-old", "timestamp": "2026-04-01T10:00:00.000Z"},
                {"execution_id": "exec-new", "timestamp": "2026-05-01T10:00:00.000Z"},
            ],
        }
        exec_tbl.get_item.side_effect = lambda Key: {
            "Item": _exec_item(Key["execution_id"])
        }

        from src.dashboard_api.list_executions.app import lambda_handler

        result = lambda_handler(_event(), None)
        data = json.loads(result["body"])["data"]

        assert data[0]["execution_id"] == "exec-new"
        assert data[1]["execution_id"] == "exec-old"

    def test_filters_out_system_prefix(self, mock_tables):
        events_tbl, exec_tbl = mock_tables
        events_tbl.query.return_value = {
            "Items": [
                {"execution_id": "system-token-refresh-123", "timestamp": "2026-05-01T10:00:00.000Z"},
                {"execution_id": "exec-001", "timestamp": "2026-05-01T10:00:00.000Z"},
            ],
        }
        exec_tbl.get_item.return_value = {"Item": _exec_item("exec-001")}

        from src.dashboard_api.list_executions.app import lambda_handler

        result = lambda_handler(_event(), None)
        data = json.loads(result["body"])["data"]

        assert len(data) == 1
        assert data[0]["execution_id"] == "exec-001"

    def test_status_filter_applied(self, mock_tables):
        events_tbl, exec_tbl = mock_tables
        events_tbl.query.return_value = {
            "Items": _event_items("exec-ok", "exec-fail"),
        }
        exec_tbl.get_item.side_effect = lambda Key: {
            "Item": _exec_item(
                Key["execution_id"],
                status="completed" if "ok" in Key["execution_id"] else "failed",
            )
        }

        from src.dashboard_api.list_executions.app import lambda_handler

        result = lambda_handler(_event(status="completed"), None)
        data = json.loads(result["body"])["data"]

        assert len(data) == 1
        assert data[0]["status"] == "completed"

    def test_topic_filter_applied(self, mock_tables):
        events_tbl, exec_tbl = mock_tables
        events_tbl.query.return_value = {
            "Items": _event_items("exec-001", "exec-002"),
        }
        exec_tbl.get_item.side_effect = lambda Key: {
            "Item": _exec_item(
                Key["execution_id"],
                topic="Python" if "001" in Key["execution_id"] else "Go言語",
            )
        }

        from src.dashboard_api.list_executions.app import lambda_handler

        result = lambda_handler(_event(topic="python"), None)
        data = json.loads(result["body"])["data"]

        assert len(data) == 1
        assert "Python" in data[0]["topic"]

    def test_limit_truncates_results(self, mock_tables):
        events_tbl, exec_tbl = mock_tables
        events_tbl.query.return_value = {
            "Items": _event_items("exec-1", "exec-2", "exec-3"),
        }
        exec_tbl.get_item.side_effect = lambda Key: {"Item": _exec_item(Key["execution_id"])}

        from src.dashboard_api.list_executions.app import lambda_handler

        result = lambda_handler(_event(limit="2"), None)
        body = json.loads(result["body"])

        assert len(body["data"]) == 2
        assert body["meta"]["total"] == 3  # total はフィルタ後の全件数

    def test_ddb_error_returns_500(self, mock_tables):
        events_tbl, _ = mock_tables
        events_tbl.query.side_effect = Exception("DDB error")

        from src.dashboard_api.list_executions.app import lambda_handler

        result = lambda_handler(_event(), None)

        assert result["statusCode"] == 500


class TestGetExecutionEvents:
    """get_execution_events のテスト。"""

    @pytest.fixture(autouse=True)
    def set_events_env(self, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")

    def test_returns_events_for_execution(self):
        resource = MagicMock()
        table = MagicMock()
        resource.Table.return_value = table
        table.query.return_value = {
            "Items": [
                {"execution_id": "exec-001", "sk": "2026-05-01T10:00:00.000Z#00001", "event_type": "topic_received"},
                {"execution_id": "exec-001", "sk": "2026-05-01T10:01:00.000Z#00002", "event_type": "workflow_planned"},
            ],
        }

        with patch("src.dashboard_api.get_execution_events.app._dynamodb", resource):
            from src.dashboard_api.get_execution_events.app import lambda_handler

            event = {"pathParameters": {"execution_id": "exec-001"}}
            result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["data"]) == 2
        assert body["meta"]["total"] == 2

    def test_missing_execution_id_returns_400(self):
        from src.dashboard_api.get_execution_events.app import lambda_handler

        result = lambda_handler({"pathParameters": {}}, None)

        assert result["statusCode"] == 400

    def test_ddb_error_returns_500(self):
        resource = MagicMock()
        resource.Table.return_value.query.side_effect = Exception("DDB fail")

        with patch("src.dashboard_api.get_execution_events.app._dynamodb", resource):
            from src.dashboard_api.get_execution_events.app import lambda_handler

            result = lambda_handler({"pathParameters": {"execution_id": "exec-001"}}, None)

        assert result["statusCode"] == 500


class TestGetExecution:
    """get_execution のテスト。"""

    @pytest.fixture(autouse=True)
    def set_exec_env(self, monkeypatch):
        monkeypatch.setenv("EXECUTIONS_TABLE", "test-executions")
        monkeypatch.setenv("DELIVERABLES_TABLE", "test-deliverables")

    def test_returns_execution_with_deliverables(self):
        resource = MagicMock()
        exec_tbl = MagicMock()
        del_tbl = MagicMock()

        resource.Table.side_effect = lambda name: exec_tbl if "executions" in name else del_tbl
        exec_tbl.get_item.return_value = {"Item": _exec_item("exec-001")}
        del_tbl.query.return_value = {"Items": [{"execution_id": "exec-001", "deliverable_id": "d-1"}]}

        with patch("src.dashboard_api.get_execution.app._dynamodb", resource):
            from src.dashboard_api.get_execution.app import lambda_handler

            result = lambda_handler({"pathParameters": {"execution_id": "exec-001"}}, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["data"]["execution"]["execution_id"] == "exec-001"
        assert len(body["data"]["deliverables"]) == 1

    def test_not_found_returns_404(self):
        resource = MagicMock()
        exec_tbl = MagicMock()
        resource.Table.return_value = exec_tbl
        exec_tbl.get_item.return_value = {}

        with patch("src.dashboard_api.get_execution.app._dynamodb", resource):
            from src.dashboard_api.get_execution.app import lambda_handler

            result = lambda_handler({"pathParameters": {"execution_id": "exec-999"}}, None)

        assert result["statusCode"] == 404
        assert json.loads(result["body"])["error"]["code"] == "EXECUTION_NOT_FOUND"

    def test_missing_execution_id_returns_400(self):
        from src.dashboard_api.get_execution.app import lambda_handler

        result = lambda_handler({"pathParameters": {}}, None)

        assert result["statusCode"] == 400
