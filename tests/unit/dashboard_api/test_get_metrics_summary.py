"""メトリクス系 Lambda (get_metrics_summary / get_review_quality / get_errors) の単体テスト。"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

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
        "gsi_pk": "GLOBAL",
        "payload": payload or {},
    }


def _mock_table(items: list) -> MagicMock:
    table = MagicMock()
    table.query.return_value = {"Items": items}
    return table


# ---------------------------------------------------------------------------
# get_metrics_summary
# ---------------------------------------------------------------------------

class TestGetMetricsSummary:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")

    def _run(self, items: list, period: str = "7d") -> dict:
        resource = MagicMock()
        resource.Table.return_value = _mock_table(items)
        with patch("src.dashboard_api.get_metrics_summary.app._dynamodb", resource):
            from src.dashboard_api.get_metrics_summary.app import lambda_handler
            return lambda_handler({"queryStringParameters": {"period": period}}, None)

    def test_returns_200_with_aggregated_data(self):
        items = [
            _make_event("execution_completed", payload={"status": "success", "total_duration_ms": 100000}),
            _make_event("execution_completed", "exec-002", payload={"status": "failed", "total_duration_ms": 50000}),
            _make_event("review_completed", payload={"passed": True}),
        ]
        result = self._run(items)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])["data"]
        assert body["total_executions"] == 2
        assert body["status_counts"]["success"] == 1
        assert body["status_counts"]["failed"] == 1
        assert body["avg_duration_ms"] == 75000
        assert body["review_pass_rate"] == 1.0

    def test_review_pass_rate_partial(self):
        items = [
            _make_event("review_completed", payload={"passed": True}),
            _make_event("review_completed", "exec-002", payload={"passed": False}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["review_pass_rate"] == 0.5

    def test_no_events_returns_zeros(self):
        result = self._run([])
        data = json.loads(result["body"])["data"]
        assert data["total_executions"] == 0
        assert data["avg_duration_ms"] is None
        assert data["review_pass_rate"] is None

    def test_invalid_period_returns_400(self):
        result = self._run([], period="2w")
        assert result["statusCode"] == 400

    def test_system_events_excluded(self):
        items = [
            _make_event("execution_completed", "system-token-refresh-123", payload={"status": "success", "total_duration_ms": 1000}),  # noqa: E501
            _make_event("execution_completed", "exec-001", payload={"status": "success", "total_duration_ms": 2000}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["total_executions"] == 1

    def test_ddb_error_returns_500(self):
        resource = MagicMock()
        resource.Table.return_value.query.side_effect = Exception("DDB fail")
        with patch("src.dashboard_api.get_metrics_summary.app._dynamodb", resource):
            from src.dashboard_api.get_metrics_summary.app import lambda_handler
            result = lambda_handler({"queryStringParameters": {}}, None)
        assert result["statusCode"] == 500


# ---------------------------------------------------------------------------
# get_review_quality
# ---------------------------------------------------------------------------

class TestGetReviewQuality:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")

    def _run(self, items: list) -> dict:
        resource = MagicMock()
        resource.Table.return_value = _mock_table(items)
        with patch("src.dashboard_api.get_review_quality.app._dynamodb", resource):
            from src.dashboard_api.get_review_quality.app import lambda_handler
            return lambda_handler({"queryStringParameters": {}}, None)

    def test_returns_pass_rate(self):
        items = [
            _make_event("review_completed", payload={"passed": True, "code_related_unfixed_count": 0}),
            _make_event("review_completed", "exec-002", payload={"passed": False, "code_related_unfixed_count": 2}),
        ]
        result = self._run(items)
        assert result["statusCode"] == 200
        data = json.loads(result["body"])["data"]
        assert data["pass_count"] == 1
        assert data["pass_rate"] == 0.5

    def test_unfixed_code_issues_list(self):
        items = [
            _make_event("review_completed", payload={"passed": False, "code_related_unfixed_count": 3, "iteration": 2}),
            _make_event("review_completed", "exec-002", payload={"passed": True, "code_related_unfixed_count": 0}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert len(data["unfixed_code_issues"]) == 1
        assert data["unfixed_code_issues"][0]["code_related_unfixed_count"] == 3

    def test_no_reviews_returns_null_pass_rate(self):
        result = self._run([])
        data = json.loads(result["body"])["data"]
        assert data["pass_rate"] is None
        assert data["total_reviews"] == 0

    # Tier 2.2 P1: issue_categories 集計テスト
    def test_aggregates_issue_categories_across_events(self):
        items = [
            _make_event(
                "review_completed",
                payload={
                    "passed": True,
                    "code_related_unfixed_count": 0,
                    "issue_categories": {"terraform_schema": 2, "iam_action": 1},
                },
            ),
            _make_event(
                "review_completed",
                "exec-002",
                payload={
                    "passed": False,
                    "code_related_unfixed_count": 1,
                    "issue_categories": {"terraform_schema": 3, "syntax": 1},
                },
            ),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["issue_categories"] == {
            "terraform_schema": 5,
            "iam_action": 1,
            "syntax": 1,
        }

    def test_returns_none_when_no_issue_categories_in_any_event(self):
        items = [
            _make_event("review_completed", payload={"passed": True, "code_related_unfixed_count": 0}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["issue_categories"] is None

    def test_skips_malformed_issue_categories(self):
        # 過去データ (issue_categories 欠損) と新データ (正常 dict) と
        # malformed (非 dict / 値が int でない) の混在を許容して集計する。
        items = [
            _make_event("review_completed", payload={"passed": True, "code_related_unfixed_count": 0}),
            _make_event(
                "review_completed",
                "exec-002",
                payload={
                    "passed": True,
                    "code_related_unfixed_count": 0,
                    "issue_categories": {"terraform_schema": 2, "syntax": "not-an-int"},
                },
            ),
            _make_event(
                "review_completed",
                "exec-003",
                payload={
                    "passed": True,
                    "code_related_unfixed_count": 0,
                    "issue_categories": "should-be-dict",
                },
            ),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        # 正常な値のみ集計、不正値はスキップ
        assert data["issue_categories"] == {"terraform_schema": 2}

    # Codex 1 回目 P1 対応: 実 DDB から返る Decimal を許容し、bool / 負数 / 小数を除外
    def test_handles_decimal_and_rejects_bool_and_non_integer(self):
        items = [
            _make_event(
                "review_completed",
                payload={
                    "passed": True,
                    "code_related_unfixed_count": 0,
                    "issue_categories": {
                        "syntax": Decimal("2"),                  # accept (整数 Decimal)
                        "iam_action": True,                      # reject (bool)
                        "terraform_schema": Decimal("1.5"),      # reject (非整数)
                        "api_version": Decimal("-1"),            # reject (負数)
                        "other": Decimal("3"),                   # accept
                    },
                },
            ),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["issue_categories"] == {"syntax": 2, "other": 3}

    def test_decimal_values_aggregate_across_events(self):
        items = [
            _make_event(
                "review_completed",
                payload={"passed": True, "code_related_unfixed_count": 0,
                         "issue_categories": {"syntax": Decimal("1")}},
            ),
            _make_event(
                "review_completed",
                "exec-002",
                payload={"passed": True, "code_related_unfixed_count": 0,
                         "issue_categories": {"syntax": Decimal("2"), "iam_action": Decimal("4")}},
            ),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["issue_categories"] == {"syntax": 3, "iam_action": 4}

    # Codex 2 回目 P2-B: Decimal の特殊値 (NaN / Infinity / 整数 Decimal の各種表記)
    def test_rejects_decimal_special_values(self):
        items = [
            _make_event(
                "review_completed",
                payload={
                    "passed": True,
                    "code_related_unfixed_count": 0,
                    "issue_categories": {
                        "syntax": Decimal("NaN"),          # reject (非有限)
                        "iam_action": Decimal("Infinity"),  # reject (非有限)
                        "terraform_schema": Decimal("-Infinity"),  # reject (非有限)
                        "api_version": Decimal("2.0"),     # accept (小数点表示の整数)
                        "other": Decimal("0E+10"),         # accept (大指数表記の 0)
                    },
                },
            ),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        # 0 件カテゴリも保持される (欠損ではなく明示的にゼロ件と区別)
        assert data["issue_categories"] == {"api_version": 2, "other": 0}

    # Codex 2 回目 P2-C: 全件欠損 vs 全件 {} の区別
    def test_returns_empty_dict_when_all_events_have_empty_categories(self):
        # 新 pipeline 動作中だが期間内のカテゴリ件数が全 0 のケース。
        # past data のみで field 欠損のケース (None) と区別する。
        items = [
            _make_event(
                "review_completed",
                payload={"passed": True, "code_related_unfixed_count": 0, "issue_categories": {}},
            ),
            _make_event(
                "review_completed",
                "exec-002",
                payload={"passed": True, "code_related_unfixed_count": 0, "issue_categories": {}},
            ),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        # 全 {} は {} を返す (None ではない)
        assert data["issue_categories"] == {}

    def test_returns_none_only_when_all_events_lack_field(self):
        # 全 event で issue_categories field 自体が無い (新 pipeline 投入前の past data) → None
        items = [
            _make_event("review_completed", payload={"passed": True, "code_related_unfixed_count": 0}),
            _make_event("review_completed", "exec-002",
                        payload={"passed": False, "code_related_unfixed_count": 1}),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["issue_categories"] is None

    # Codex 3 回目 P3-D: signaling NaN は is_finite() で False を返すため除外される
    def test_rejects_decimal_signaling_nan(self):
        items = [
            _make_event(
                "review_completed",
                payload={
                    "passed": True,
                    "code_related_unfixed_count": 0,
                    "issue_categories": {"syntax": Decimal("sNaN")},
                },
            ),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        assert data["issue_categories"] == {}

    # Codex 3 回目 P3-D: fallback dict 経路 (orchestrator が non-dict reviewer 応答を
    # 受けて issue_categories={} を emit する) で全 events が来たケース
    def test_returns_empty_dict_when_only_malformed_fallback_events(self):
        items = [
            _make_event("review_completed", payload={
                "passed": False,
                "code_related_unfixed_count": 0,
                "issue_categories": {},
            }),
            _make_event("review_completed", "exec-002", payload={
                "passed": False,
                "code_related_unfixed_count": 0,
                "issue_categories": {},
            }),
        ]
        result = self._run(items)
        data = json.loads(result["body"])["data"]
        # field は存在するが集計値は全 0 → 空 dict (None ではない)
        assert data["issue_categories"] == {}


# ---------------------------------------------------------------------------
# get_errors
# ---------------------------------------------------------------------------

class TestGetErrors:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("EVENTS_TABLE", "test-events")

    def _run(self, items: list) -> dict:
        resource = MagicMock()
        resource.Table.return_value = _mock_table(items)
        with patch("src.dashboard_api.get_errors.app._dynamodb", resource):
            from src.dashboard_api.get_errors.app import lambda_handler
            return lambda_handler({"queryStringParameters": {}}, None)

    def test_returns_errors_with_type_counts(self):
        items = [
            _make_event("error", payload={"error_type": "NotionAPIError", "error_message": "timeout"}),
            _make_event("error", "exec-002", payload={"error_type": "NotionAPIError", "error_message": "rate limit"}),
            _make_event("error", "exec-003", payload={"error_type": "TimeoutError", "error_message": "timed out"}),
        ]
        result = self._run(items)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["meta"]["total"] == 3
        assert body["meta"]["by_type"]["NotionAPIError"] == 2
        assert body["meta"]["by_type"]["TimeoutError"] == 1

    def test_no_errors_returns_empty(self):
        result = self._run([])
        body = json.loads(result["body"])
        assert body["data"] == []
        assert body["meta"]["total"] == 0

    def test_ddb_error_returns_500(self):
        resource = MagicMock()
        resource.Table.return_value.query.side_effect = Exception("fail")
        with patch("src.dashboard_api.get_errors.app._dynamodb", resource):
            from src.dashboard_api.get_errors.app import lambda_handler
            result = lambda_handler({"queryStringParameters": {}}, None)
        assert result["statusCode"] == 500
