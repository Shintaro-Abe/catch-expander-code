"""統合テスト: ワークフロー全体のE2Eフロー（外部APIはモック）"""

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_clients():
    """全外部クライアントをモック化する"""
    slack = MagicMock()
    db = MagicMock()

    # get_user_profile はプロファイルなし
    db.get_user_profile.return_value = None
    # _table().update_item はそのまま通す
    db._table.return_value = MagicMock()

    return slack, db


def _make_claude_responses():
    """Claude CLIの応答をシミュレートする"""
    # 1. トピック解析
    analysis = {
        "category": "技術",
        "intent": "AIパイプラインの技術導入検討",
        "perspectives": ["概要", "クラウド実装", "コスト"],
        "deliverable_types": ["research_report"],
    }
    # 2. ワークフロー設計
    workflow = {
        "research_steps": [
            {
                "step_id": "r-1",
                "step_name": "概要調査",
                "description": "AIパイプラインの概要",
                "search_hints": ["AI pipeline"],
            },
            {
                "step_id": "r-2",
                "step_name": "クラウド実装",
                "description": "AWS実装手順",
                "search_hints": ["AWS pipeline"],
            },
        ],
        "generate_steps": [{"step_id": "g-1", "step_name": "レポート生成", "deliverable_type": "research_report"}],
        "storage_targets": ["notion"],
    }
    # 3. リサーチャー結果（2回）
    research1 = {
        "step_id": "r-1",
        "summary": "AIパイプラインは...",
        "sources": [
            {"source_id": "s-1", "url": "https://example.com/1", "title": "Example", "priority": 1, "verified": True}
        ],
    }
    research2 = {
        "step_id": "r-2",
        "summary": "AWSでは...",
        "sources": [
            {"source_id": "s-2", "url": "https://example.com/2", "title": "AWS Docs", "priority": 1, "verified": True}
        ],
    }
    # 4. ジェネレーター結果
    deliverables = {
        "content_blocks": [
            {
                "type": "heading_1",
                "heading_1": {
                    "rich_text": [{"type": "text", "text": {"content": "AIパイプライン"}}],
                },
            },
        ],
        "code_files": None,
        "summary": "AIパイプラインについて調査しました。",
    }
    # 5. レビュー結果（合格）
    review = {
        "passed": True,
        "issues": [],
        "quality_metadata": {
            "sources_verified": 2,
            "sources_unverified": 0,
            "newest_source_date": "2026-04-01",
            "oldest_source_date": "2026-03-15",
            "checklist_passed": 4,
            "checklist_total": 4,
            "notes": [],
        },
    }

    responses = [
        json.dumps({"result": json.dumps(analysis)}),
        json.dumps({"result": json.dumps(workflow)}),
        json.dumps({"result": json.dumps(research1)}),
        json.dumps({"result": json.dumps(research2)}),
        json.dumps({"result": json.dumps(deliverables)}),
        json.dumps({"result": json.dumps(review)}),
    ]
    return responses


class TestWorkflowE2E:
    """ワークフロー全体の統合テスト"""

    @patch("orchestrator.call_claude")
    @patch("storage.notion_client.requests.request")
    def test_full_workflow_success(self, mock_notion_request, mock_claude, mock_clients):
        from orchestrator import Orchestrator

        slack, db = mock_clients
        mock_claude.side_effect = _make_claude_responses()

        # Notion APIモック
        mock_notion_response = mock_notion_request.return_value
        mock_notion_response.status_code = 200
        mock_notion_response.raise_for_status.return_value = None
        mock_notion_response.json.return_value = {"id": "page-123", "url": "https://notion.so/page-123"}

        orch = Orchestrator(slack, db, "ntn_token", "db_id", "gh_token", "owner/repo")
        orch.run(
            execution_id="exec-test-001",
            user_id="U_TEST",
            topic="AIパイプライン",
            slack_channel="C_TEST",
            slack_thread_ts="ts123",
        )

        # ステータス遷移の確認
        status_calls = [c[0] for c in db.update_execution_status.call_args_list]
        statuses = [c[1] for c in status_calls]
        assert "planning" in statuses
        assert "researching" in statuses
        assert "generating" in statuses
        assert "reviewing" in statuses
        assert "storing" in statuses

        # Slack完了通知の確認
        slack.post_completion.assert_called_once()
        call_args = slack.post_completion.call_args
        assert "https://notion.so/page-123" in str(call_args)

        # 成果物保存の確認
        db.put_deliverable.assert_called_once()

    @patch("orchestrator.call_claude")
    @patch("storage.notion_client.requests.request")
    def test_partial_research_failure_continues(self, mock_notion_request, mock_claude, mock_clients):
        from orchestrator import Orchestrator

        slack, db = mock_clients

        analysis = {"category": "技術", "intent": "test", "perspectives": [], "deliverable_types": ["research_report"]}
        workflow = {
            "research_steps": [
                {"step_id": "r-1", "step_name": "成功ステップ", "description": "test", "search_hints": []},
                {"step_id": "r-2", "step_name": "失敗ステップ", "description": "test", "search_hints": []},
            ],
            "generate_steps": [],
            "storage_targets": ["notion"],
        }
        research_ok = {"step_id": "r-1", "summary": "成功", "sources": []}
        deliverables = {"content_blocks": [], "code_files": None, "summary": "部分結果"}
        review = {
            "passed": True,
            "issues": [],
            "quality_metadata": {
                "sources_verified": 0,
                "sources_unverified": 0,
                "checklist_passed": 0,
                "checklist_total": 0,
                "notes": [],
            },
        }

        call_count = 0

        def mock_claude_side_effect(prompt, allowed_tools=None, model="sonnet"):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return json.dumps({"result": json.dumps(analysis)})
            if call_count == 2:
                return json.dumps({"result": json.dumps(workflow)})
            # リサーチャー呼び出し（WebSearch + WebFetch）
            if allowed_tools == ["WebSearch", "WebFetch"]:
                if "失敗ステップ" in prompt:
                    raise RuntimeError("Search failed")
                return json.dumps({"result": json.dumps(research_ok)})
            # レビュアー呼び出し（WebFetchのみ）
            if allowed_tools == ["WebFetch"]:
                return json.dumps({"result": json.dumps(review)})
            # ジェネレーター（toolsなし）
            return json.dumps({"result": json.dumps(deliverables)})

        mock_claude.side_effect = mock_claude_side_effect

        mock_notion_response = mock_notion_request.return_value
        mock_notion_response.status_code = 200
        mock_notion_response.raise_for_status.return_value = None
        mock_notion_response.json.return_value = {"id": "page-456", "url": "https://notion.so/page-456"}

        orch = Orchestrator(slack, db, "ntn_token", "db_id", "gh_token", "owner/repo")

        # 部分失敗でもワークフロー自体は完了する
        orch.run(
            execution_id="exec-test-002",
            user_id="U_TEST",
            topic="テスト",
            slack_channel="C_TEST",
            slack_thread_ts="ts456",
        )

        slack.post_completion.assert_called_once()
