"""Tier 1/2 拡張メトリクス系 Lambda の単体テスト。

対象:
- get_cost_summary      → GET /api/v1/metrics/cost
- get_api_health        → GET /api/v1/metrics/api-health
- get_token_monitor_health → GET /api/v1/metrics/token-monitor
- get_feedback_aggregation → GET /api/v1/metrics/feedback
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_event(
    event_type: str,
    execution_id: str = "exec-001",
    payload: dict | None = None,
    ts: str = "2026-05-01T10:00:00.000Z",
) -> dict:
    return {
        "execution_id": execution_id,
        "event_type": event_type,
        "timestamp": ts,
        "payload": payload or {},
    }


def _mock_table(items: list) -> MagicMock:
    table = MagicMock()
    table.query.return_value = {"Items": items}
    return table


def _mock_table_multi(*item_lists: list) -> MagicMock:
    """Successive query calls return successive item lists."""
    table = MagicMock()
    table.query.side_effect = [{"Items": lst} for lst in item_lists]
    return table


# ---------------------------------------------------------------------------
# get_cost_summary
# ---------------------------------------------------------------------------

class TestGetCostSummary:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")

    def _run(self, items: list, period: str = "7d") -> dict:
        resource = MagicMock()
        resource.Table.return_value = _mock_table(items)
        with patch("src.dashboard_api.get_cost_summary.app._dynamodb", resource):
            from src.dashboard_api.get_cost_summary.app import lambda_handler
            return lambda_handler({"queryStringParameters": {"period": period}}, None)

    def test_returns_200(self):
        result = self._run([])
        assert result["statusCode"] == 200

    def test_no_executions_returns_nulls(self):
        result = self._run([])
        data = json.loads(result["body"])["data"]
        assert data["total_executions"] == 0
        assert data["total_tokens_used"] is None
        assert data["total_cost_usd"] is None
        assert data["avg_tokens_per_execution"] is None

    def test_aggregates_tokens_when_present(self):
        items = [
            _make_event("execution_completed", payload={"total_tokens_used": 1000}),
            _make_event("execution_completed", "exec-002", payload={"total_tokens_used": 2000}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["total_tokens_used"] == 3000
        assert data["avg_tokens_per_execution"] == 1500

    def test_avg_tokens_excludes_executions_without_token_data(self):
        items = [
            _make_event("execution_completed", payload={"total_tokens_used": 1000}),
            _make_event("execution_completed", "exec-002", payload={}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["total_tokens_used"] == 1000
        assert data["avg_tokens_per_execution"] == 1000

    def test_zero_tokens_included_in_avg(self):
        items = [
            _make_event("execution_completed", payload={"total_tokens_used": 0}),
            _make_event("execution_completed", "exec-002", payload={"total_tokens_used": 1000}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["total_tokens_used"] == 1000
        assert data["avg_tokens_per_execution"] == 500

    def test_null_tokens_remain_null(self):
        items = [
            _make_event("execution_completed", payload={"total_tokens_used": None}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["total_tokens_used"] is None
        assert data["avg_tokens_per_execution"] is None

    def test_aggregates_cost_when_present(self):
        items = [
            _make_event("execution_completed", payload={"total_cost_usd": 0.01}),
            _make_event("execution_completed", "exec-002", payload={"total_cost_usd": 0.02}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert abs(data["total_cost_usd"] - 0.03) < 1e-9

    def test_invalid_period_returns_400(self):
        result = self._run([], period="2w")
        assert result["statusCode"] == 400

    def test_ddb_error_returns_500(self):
        resource = MagicMock()
        resource.Table.return_value.query.side_effect = Exception("DDB fail")
        with patch("src.dashboard_api.get_cost_summary.app._dynamodb", resource):
            from src.dashboard_api.get_cost_summary.app import lambda_handler
            result = lambda_handler({"queryStringParameters": {}}, None)
        assert result["statusCode"] == 500


# ---------------------------------------------------------------------------
# get_api_health
# ---------------------------------------------------------------------------

class TestGetApiHealth:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")

    def _run(self, api_calls: list, rate_limits: list | None = None, period: str = "7d") -> dict:
        resource = MagicMock()
        resource.Table.return_value = _mock_table_multi(api_calls, rate_limits or [])
        with patch("src.dashboard_api.get_api_health.app._dynamodb", resource):
            from src.dashboard_api.get_api_health.app import lambda_handler
            return lambda_handler({"queryStringParameters": {"period": period}}, None)

    def test_returns_200(self):
        result = self._run([])
        assert result["statusCode"] == 200

    def test_aggregates_by_service(self):
        calls = [
            _make_event("api_call_completed", payload={"subtype": "notion", "success": True, "duration_ms": 200}),
            _make_event("api_call_completed", "exec-002",
                        payload={"subtype": "notion", "success": False, "duration_ms": 100}),
            _make_event("api_call_completed", "exec-003",
                        payload={"subtype": "github", "success": True, "duration_ms": 300}),
        ]
        result = self._run(calls)
        data = json.loads(result["body"])["data"]
        notion = data["by_service"]["notion"]
        assert notion["total_calls"] == 2
        assert notion["success_rate"] == 0.5
        assert notion["avg_duration_ms"] == 150

        github = data["by_service"]["github"]
        assert github["total_calls"] == 1
        assert github["success_rate"] == 1.0

    def test_rate_limits_normalised_to_service(self):
        calls = [
            _make_event("api_call_completed", payload={"subtype": "anthropic", "success": True, "duration_ms": 500}),
        ]
        limits = [
            _make_event("rate_limit_hit", payload={"subtype": "anthropic_429", "endpoint_path": "/v1/messages"}),
            _make_event("rate_limit_hit", "exec-002",
                        payload={"subtype": "anthropic_429", "endpoint_path": "/v1/messages"}),
        ]
        result = self._run(calls, limits)
        data = json.loads(result["body"])["data"]
        assert "anthropic_429" not in data["by_service"]
        assert data["by_service"]["anthropic"]["total_calls"] == 1
        assert data["by_service"]["anthropic"]["rate_limit_count"] == 2

    def test_cloudflare_block_normalised_to_notion(self):
        calls = []
        limits = [
            _make_event("rate_limit_hit", payload={"subtype": "cloudflare_block"}),
        ]
        result = self._run(calls, limits)
        data = json.loads(result["body"])["data"]
        assert "cloudflare_block" not in data["by_service"]
        assert data["by_service"]["notion"]["rate_limit_count"] == 1

    def test_no_calls_returns_empty_by_service(self):
        result = self._run([])
        data = json.loads(result["body"])["data"]
        assert data["by_service"] == {}

    def test_invalid_period_returns_400(self):
        result = self._run([], period="99d")
        assert result["statusCode"] == 400

    def test_ddb_error_returns_500(self):
        resource = MagicMock()
        resource.Table.return_value.query.side_effect = Exception("fail")
        with patch("src.dashboard_api.get_api_health.app._dynamodb", resource):
            from src.dashboard_api.get_api_health.app import lambda_handler
            result = lambda_handler({"queryStringParameters": {}}, None)
        assert result["statusCode"] == 500


# ---------------------------------------------------------------------------
# get_token_monitor_health
# ---------------------------------------------------------------------------

class TestGetTokenMonitorHealth:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")

    def _run(self, successes: list, failures: list | None = None, period: str = "7d") -> dict:
        resource = MagicMock()
        resource.Table.return_value = _mock_table_multi(successes, failures or [])
        with patch("src.dashboard_api.get_token_monitor_health.app._dynamodb", resource):
            from src.dashboard_api.get_token_monitor_health.app import lambda_handler
            return lambda_handler({"queryStringParameters": {"period": period}}, None)

    def test_returns_200(self):
        result = self._run([])
        assert result["statusCode"] == 200

    def test_counts_successes_and_failures(self):
        successes = [
            _make_event("oauth_refresh_completed", "system-token-refresh-1", ts="2026-05-01T06:00:00.000Z"),
        ]
        failures = [
            _make_event("oauth_refresh_failed", "system-token-refresh-0",
                        payload={"error_message": "http_401"}, ts="2026-04-30T06:00:00.000Z"),
        ]
        result = self._run(successes, failures)
        data = json.loads(result["body"])["data"]
        assert data["success_count"] == 1
        assert data["failure_count"] == 1
        assert data["total_refresh_attempts"] == 2
        assert data["success_rate"] == 0.5

    def test_last_refresh_at_from_latest_success(self):
        successes = [
            _make_event("oauth_refresh_completed", "system-token-refresh-1", ts="2026-05-01T06:00:00.000Z"),
            _make_event("oauth_refresh_completed", "system-token-refresh-2", ts="2026-05-01T18:00:00.000Z"),
        ]
        result = self._run(successes)
        data = json.loads(result["body"])["data"]
        assert data["last_refresh_at"] == "2026-05-01T18:00:00.000Z"

    def test_last_failure_reason_extracted(self):
        failures = [
            _make_event("oauth_refresh_failed", "system-token-refresh-0",
                        payload={"error_message": "no_refresh_token"}),
        ]
        result = self._run([], failures)
        data = json.loads(result["body"])["data"]
        assert data["last_failure_reason"] == "no_refresh_token"

    def test_no_events_returns_nulls(self):
        result = self._run([])
        data = json.loads(result["body"])["data"]
        assert data["total_refresh_attempts"] == 0
        assert data["success_rate"] is None
        assert data["last_refresh_at"] is None
        assert data["last_failure_at"] is None
        assert data["last_failure_reason"] is None

    def test_invalid_period_returns_400(self):
        result = self._run([], period="bad")
        assert result["statusCode"] == 400

    def test_ddb_error_returns_500(self):
        resource = MagicMock()
        resource.Table.return_value.query.side_effect = Exception("fail")
        with patch("src.dashboard_api.get_token_monitor_health.app._dynamodb", resource):
            from src.dashboard_api.get_token_monitor_health.app import lambda_handler
            result = lambda_handler({"queryStringParameters": {}}, None)
        assert result["statusCode"] == 500


# ---------------------------------------------------------------------------
# get_feedback_aggregation
# ---------------------------------------------------------------------------

class TestGetFeedbackAggregation:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")

    def _run(self, items: list, period: str = "7d") -> dict:
        resource = MagicMock()
        resource.Table.return_value = _mock_table(items)
        with patch("src.dashboard_api.get_feedback_aggregation.app._dynamodb", resource):
            from src.dashboard_api.get_feedback_aggregation.app import lambda_handler
            return lambda_handler({"queryStringParameters": {"period": period}}, None)

    def test_returns_200(self):
        result = self._run([])
        assert result["statusCode"] == 200

    def test_no_feedback_returns_zeros_and_nulls(self):
        result = self._run([])
        data = json.loads(result["body"])["data"]
        assert data["total_feedback_count"] == 0
        assert data["preferences_updated_count"] == 0
        assert data["avg_new_preferences"] is None
        assert data["latest_total_preferences"] is None

    def test_aggregates_feedback_counts(self):
        items = [
            _make_event("feedback_received", payload={
                "learned_preferences_updated": True,
                "new_preferences_count": 2,
                "total_preferences_count": 10,
            }),
            _make_event("feedback_received", "exec-002", payload={
                "learned_preferences_updated": False,
                "new_preferences_count": 0,
                "total_preferences_count": 10,
            }),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["total_feedback_count"] == 2
        assert data["preferences_updated_count"] == 1
        assert data["avg_new_preferences"] == 1.0

    def test_latest_total_preferences_from_last_item(self):
        items = [
            _make_event("feedback_received", payload={"total_preferences_count": 5},
                        ts="2026-05-01T08:00:00.000Z"),
            _make_event("feedback_received", "exec-002", payload={"total_preferences_count": 7},
                        ts="2026-05-01T09:00:00.000Z"),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["latest_total_preferences"] == 7

    def test_invalid_period_returns_400(self):
        result = self._run([], period="bad")
        assert result["statusCode"] == 400

    def test_ddb_error_returns_500(self):
        resource = MagicMock()
        resource.Table.return_value.query.side_effect = Exception("fail")
        with patch("src.dashboard_api.get_feedback_aggregation.app._dynamodb", resource):
            from src.dashboard_api.get_feedback_aggregation.app import lambda_handler
            result = lambda_handler({"queryStringParameters": {}}, None)
        assert result["statusCode"] == 500
