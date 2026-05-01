import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def _env_vars(monkeypatch):
    """Lambda環境変数を設定し、シークレットキャッシュをクリアする"""
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "test-prefix")
    monkeypatch.setenv("ECS_CLUSTER_ARN", "arn:aws:ecs:ap-northeast-1:123456789012:cluster/test")
    monkeypatch.setenv("ECS_TASK_DEFINITION_ARN", "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/test:1")
    monkeypatch.setenv("ECS_SUBNET_1", "subnet-111")
    monkeypatch.setenv("ECS_SUBNET_2", "subnet-222")
    monkeypatch.setenv("ECS_SECURITY_GROUP", "sg-333")
    monkeypatch.setenv("SLACK_BOT_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-1:123:secret:bot-token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-1:123:secret:signing")
    # テスト間のキャッシュ汚染を防ぐ
    from app import _cached_secrets

    _cached_secrets.clear()


SIGNING_SECRET = "test_signing_secret"


def _make_lambda_context():
    """テスト用のLambda contextを生成する"""
    ctx = MagicMock()
    ctx.function_name = "catch-expander-trigger"
    ctx.memory_limit_in_mb = 256
    ctx.invoked_function_arn = "arn:aws:lambda:ap-northeast-1:123:function:catch-expander-trigger"
    ctx.aws_request_id = "test-request-id"
    return ctx


def _make_signature(body: str, timestamp: str) -> str:
    """テスト用のSlack署名を生成する"""
    sig_basestring = f"v0:{timestamp}:{body}"
    return "v0=" + hmac.new(SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()


def _make_event(body: dict, timestamp: str | None = None) -> dict:
    """テスト用のAPI Gatewayイベントを生成する"""
    ts = timestamp or str(int(time.time()))
    body_str = json.dumps(body)
    sig = _make_signature(body_str, ts)
    return {
        "body": body_str,
        "headers": {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    }


@pytest.mark.usefixtures("_env_vars")
class TestSlackSignatureVerification:
    """Slack署名検証のテスト"""

    @patch("app.secrets_client")
    def test_invalid_signature_returns_403(self, mock_secrets):
        from app import lambda_handler

        mock_secrets.get_secret_value.return_value = {"SecretString": SIGNING_SECRET}

        event = {
            "body": '{"type": "event_callback"}',
            "headers": {
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=invalid",
            },
        }
        result = lambda_handler(event, _make_lambda_context())
        assert result["statusCode"] == 403

    @patch("app.secrets_client")
    def test_valid_signature_passes(self, mock_secrets):
        from app import lambda_handler

        mock_secrets.get_secret_value.return_value = {"SecretString": SIGNING_SECRET}

        body = {"type": "url_verification", "challenge": "test_challenge"}
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())
        assert result["statusCode"] == 200
        assert result["body"] == "test_challenge"


@pytest.mark.usefixtures("_env_vars")
class TestUrlVerification:
    """URL Verificationチャレンジのテスト"""

    @patch("app.secrets_client")
    def test_url_verification_returns_challenge(self, mock_secrets):
        from app import lambda_handler

        mock_secrets.get_secret_value.return_value = {"SecretString": SIGNING_SECRET}

        body = {"type": "url_verification", "challenge": "abc123"}
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())
        assert result["statusCode"] == 200
        assert result["body"] == "abc123"


@pytest.mark.usefixtures("_env_vars")
class TestEventTypeFiltering:
    """イベントタイプ判定のテスト"""

    @patch("app.secrets_client")
    def test_unknown_event_type_ignored(self, mock_secrets):
        from app import lambda_handler

        mock_secrets.get_secret_value.return_value = {"SecretString": SIGNING_SECRET}

        body = {"type": "event_callback", "event": {"type": "reaction_added"}}
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())
        assert result["statusCode"] == 200
        assert result["body"] == ""

    @patch("app.secrets_client")
    def test_bot_message_ignored(self, mock_secrets):
        from app import lambda_handler

        mock_secrets.get_secret_value.return_value = {"SecretString": SIGNING_SECRET}

        body = {
            "type": "event_callback",
            "event": {"type": "app_mention", "bot_id": "B123", "text": "test", "user": "U1", "channel": "C1"},
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())
        assert result["statusCode"] == 200
        assert result["body"] == ""

    @patch("app.secrets_client")
    def test_message_with_subtype_ignored(self, mock_secrets):
        """subtypeありのmessageイベントは無視する"""
        from app import lambda_handler

        mock_secrets.get_secret_value.return_value = {"SecretString": SIGNING_SECRET}

        body = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel_type": "im",
                "subtype": "bot_message",
                "text": "bot reply",
            },
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())
        assert result["statusCode"] == 200
        assert result["body"] == ""

    @patch("app.secrets_client")
    def test_empty_topic_ignored(self, mock_secrets):
        """メンション除去後にトピックが空になる場合は無視する"""
        from app import lambda_handler

        mock_secrets.get_secret_value.return_value = {"SecretString": SIGNING_SECRET}

        body = {
            "type": "event_callback",
            "event": {"type": "app_mention", "text": "<@U_BOT>", "user": "U1", "channel": "C1"},
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())
        assert result["statusCode"] == 200
        assert result["body"] == ""


@pytest.mark.usefixtures("_env_vars")
class TestSlackRetry:
    """X-Slack-Retry-Num ヘッダーによるリトライリクエストのテスト"""

    @patch("app.secrets_client")
    def test_retry_header_ignored(self, mock_secrets):
        """X-Slack-Retry-Num があれば署名検証前に200を返す"""
        from app import lambda_handler

        event = {
            "body": "{}",
            "headers": {"X-Slack-Retry-Num": "1", "X-Slack-Retry-Reason": "http_timeout"},
        }
        result = lambda_handler(event, _make_lambda_context())
        assert result["statusCode"] == 200
        assert result["body"] == ""
        mock_secrets.get_secret_value.assert_not_called()


@pytest.mark.usefixtures("_env_vars")
class TestAckAndEcsRunTask:
    """ACK応答 + ECS RunTask呼び出しのテスト"""

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_app_mention_triggers_workflow(self, mock_secrets, mock_webclient_cls, mock_dynamodb, mock_ecs):
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = lambda SecretId: {  # noqa: N803
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:signing": {"SecretString": SIGNING_SECRET},
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:bot-token": {"SecretString": "xoxb-test"},
        }[SecretId]

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack
        mock_slack.chat_postMessage.return_value = {"ts": "1234567890.000100"}

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> AIパイプライン",
                "user": "U_USER",
                "channel": "C_CHANNEL",
            },
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())

        assert result["statusCode"] == 200

        # ACK投稿の確認
        mock_slack.chat_postMessage.assert_called_once()
        call_kwargs = mock_slack.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C_CHANNEL"
        assert "AIパイプライン" in call_kwargs["text"]

        # DynamoDB書き込みの確認
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["status"] == "received"
        assert item["topic"] == "AIパイプライン"
        assert item["user_id"] == "U_USER"

        # ECS RunTaskの確認
        mock_ecs.run_task.assert_called_once()
        run_task_kwargs = mock_ecs.run_task.call_args[1]
        assert run_task_kwargs["launchType"] == "FARGATE"
        env_vars = {e["name"]: e["value"] for e in run_task_kwargs["overrides"]["containerOverrides"][0]["environment"]}
        assert env_vars["USER_ID"] == "U_USER"
        assert env_vars["TOPIC"] == "AIパイプライン"
        assert env_vars["SLACK_CHANNEL"] == "C_CHANNEL"

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_dm_triggers_workflow(self, mock_secrets, mock_webclient_cls, mock_dynamodb, mock_ecs):
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = lambda SecretId: {  # noqa: N803
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:signing": {"SecretString": SIGNING_SECRET},
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:bot-token": {"SecretString": "xoxb-test"},
        }[SecretId]

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack
        mock_slack.chat_postMessage.return_value = {"ts": "1234567890.000100"}
        mock_dynamodb.Table.return_value = MagicMock()

        body = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel_type": "im",
                "text": "Kubernetes入門",
                "user": "U_USER",
                "channel": "D_DM",
            },
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())

        assert result["statusCode"] == 200
        mock_ecs.run_task.assert_called_once()


@pytest.mark.usefixtures("_env_vars")
class TestFindCompletedExecution:
    """_find_completed_execution 関数のテスト"""

    @patch("app.dynamodb")
    def test_returns_item_when_thread_ts_matches(self, mock_dynamodb):
        from app import _find_completed_execution

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [{"execution_id": "exec-001", "status": "completed", "slack_thread_ts": "111.000"}]
        }

        result = _find_completed_execution("U_USER", "111.000", "test-prefix")

        assert result is not None
        assert result["execution_id"] == "exec-001"
        # status フィルタはクエリに含まれない（呼び出し側で判定する）
        call_kwargs = mock_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "user-id-index"

    @patch("app.dynamodb")
    def test_returns_item_regardless_of_status(self, mock_dynamodb):
        """status != completed であっても Items があれば返す（status 判定は呼び出し側）"""
        from app import _find_completed_execution

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [{"execution_id": "exec-002", "status": "in_progress", "slack_thread_ts": "111.000"}]
        }

        result = _find_completed_execution("U_USER", "111.000", "test-prefix")
        assert result is not None
        assert result["status"] == "in_progress"

    @patch("app.dynamodb")
    def test_returns_none_when_no_items(self, mock_dynamodb):
        from app import _find_completed_execution

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": []}

        result = _find_completed_execution("U_USER", "111.000", "test-prefix")
        assert result is None


@pytest.mark.usefixtures("_env_vars")
class TestFeedbackDetection:
    """フィードバック検出フローのテスト"""

    def _make_secrets_side_effect(self):
        return lambda SecretId: {  # noqa: N803
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:signing": {"SecretString": SIGNING_SECRET},
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:bot-token": {"SecretString": "xoxb-test"},
        }[SecretId]

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_thread_reply_to_completed_execution_triggers_feedback(
        self, mock_secrets, mock_webclient_cls, mock_dynamodb, mock_ecs
    ):
        """スレッド返信 + 完了済み実行あり → ACK投稿 + TASK_TYPE=feedback でECS起動"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()
        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [{"execution_id": "exec-001", "status": "completed", "slack_thread_ts": "111.000"}]
        }

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> コードが参考になりました",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "222.000",
                "thread_ts": "111.000",
            },
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())

        assert result["statusCode"] == 200

        # ACK がスレッドに投稿されること
        mock_slack.chat_postMessage.assert_called_once()
        call_kwargs = mock_slack.chat_postMessage.call_args[1]
        assert call_kwargs["thread_ts"] == "111.000"
        assert "フィードバック" in call_kwargs["text"]

        # ECS が TASK_TYPE=feedback で起動されること
        mock_ecs.run_task.assert_called_once()
        env_vars = {
            e["name"]: e["value"]
            for e in mock_ecs.run_task.call_args[1]["overrides"]["containerOverrides"][0]["environment"]
        }
        assert env_vars["TASK_TYPE"] == "feedback"
        assert env_vars["USER_ID"] == "U_USER"
        assert env_vars["EXECUTION_ID"] == "exec-001"
        assert env_vars["SLACK_THREAD_TS"] == "111.000"
        assert "TOPIC" not in env_vars  # 新規トピックフローの環境変数は含まれない

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_thread_reply_to_in_progress_execution_is_ignored(
        self, mock_secrets, mock_webclient_cls, mock_dynamodb, mock_ecs
    ):
        """スレッド返信 + status=in_progress の実行 → 無視（HTTP 200、新規トピックフローに入らない）"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [{"execution_id": "exec-002", "status": "in_progress", "slack_thread_ts": "111.000"}]
        }

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> 良かったです",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "222.000",
                "thread_ts": "111.000",
            },
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())

        assert result["statusCode"] == 200
        mock_ecs.run_task.assert_not_called()
        mock_webclient_cls.assert_not_called()  # Slack への投稿なし

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_thread_reply_with_no_matching_execution_falls_through_to_topic_flow(
        self, mock_secrets, mock_webclient_cls, mock_dynamodb, mock_ecs
    ):
        """スレッド返信 + 実行レコードなし → 新規トピックフロー"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()
        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack
        mock_slack.chat_postMessage.return_value = {"ts": "333.000"}

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": []}

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> 別トピック",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "222.000",
                "thread_ts": "111.000",
            },
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())

        assert result["statusCode"] == 200
        # 新規トピックフロー（ECS起動）が実行されること
        mock_ecs.run_task.assert_called_once()
        env_vars = {
            e["name"]: e["value"]
            for e in mock_ecs.run_task.call_args[1]["overrides"]["containerOverrides"][0]["environment"]
        }
        assert env_vars.get("TASK_TYPE", "topic") != "feedback"  # feedback ルートではない

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_non_thread_message_goes_to_topic_flow(self, mock_secrets, mock_webclient_cls, mock_dynamodb, mock_ecs):
        """スレッドなし（トップレベル投稿）→ DynamoDB クエリなしで新規トピックフロー"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()
        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack
        mock_slack.chat_postMessage.return_value = {"ts": "444.000"}
        mock_dynamodb.Table.return_value = MagicMock()

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> Terraform入門",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "444.000",
                # thread_ts なし
            },
        }
        event = _make_event(body)
        result = lambda_handler(event, _make_lambda_context())

        assert result["statusCode"] == 200
        mock_ecs.run_task.assert_called_once()
        env_vars = {
            e["name"]: e["value"]
            for e in mock_ecs.run_task.call_args[1]["overrides"]["containerOverrides"][0]["environment"]
        }
        assert env_vars["TOPIC"] == "Terraform入門"


# ---------------------------------------------------------------------------
# F9 成果物履歴管理
# ---------------------------------------------------------------------------


class TestIsHistoryCommand:
    """_is_history_command のテスト"""

    def test_rekishi_returns_true(self):
        from app import _is_history_command

        assert _is_history_command("履歴") is True

    def test_rekishi_with_keyword_returns_true(self):
        from app import _is_history_command

        assert _is_history_command("履歴 Terraform") is True

    def test_rekishi_without_space_returns_true(self):
        from app import _is_history_command

        assert _is_history_command("履歴Terraform") is True

    def test_history_lowercase_returns_true(self):
        from app import _is_history_command

        assert _is_history_command("history") is True

    def test_history_titlecase_returns_true(self):
        from app import _is_history_command

        assert _is_history_command("History k8s") is True

    def test_history_with_compound_keyword_returns_true(self):
        from app import _is_history_command

        assert _is_history_command("history k8s overview") is True

    def test_regular_topic_returns_false(self):
        from app import _is_history_command

        assert _is_history_command("Terraform入門") is False

    def test_feedback_keyword_returns_false(self):
        from app import _is_history_command

        assert _is_history_command("フィードバック") is False


class TestExtractHistoryKeyword:
    """_extract_history_keyword のテスト"""

    def test_rekishi_only_returns_none(self):
        from app import _extract_history_keyword

        assert _extract_history_keyword("履歴") is None

    def test_rekishi_with_space_keyword(self):
        from app import _extract_history_keyword

        assert _extract_history_keyword("履歴 Terraform") == "Terraform"

    def test_rekishi_without_space_keyword(self):
        from app import _extract_history_keyword

        assert _extract_history_keyword("履歴Terraform") == "Terraform"

    def test_history_only_returns_none(self):
        from app import _extract_history_keyword

        assert _extract_history_keyword("history") is None

    def test_history_with_compound_keyword(self):
        from app import _extract_history_keyword

        assert _extract_history_keyword("history k8s overview") == "k8s overview"

    def test_history_titlecase_with_keyword(self):
        from app import _extract_history_keyword

        assert _extract_history_keyword("History Terraform") == "Terraform"


class TestQueryCompletedExecutions:
    """_query_completed_executions のテスト"""

    @patch("app.dynamodb")
    def test_returns_only_completed_items(self, mock_dynamodb):
        from app import _query_completed_executions

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [
                {"execution_id": "e1", "status": "completed", "user_id": "U1"},
                {"execution_id": "e2", "status": "completed", "user_id": "U1"},
                {"execution_id": "e3", "status": "completed", "user_id": "U1"},
                {"execution_id": "e4", "status": "failed", "user_id": "U1"},
                {"execution_id": "e5", "status": "in_progress", "user_id": "U1"},
            ]
        }

        result = _query_completed_executions("U1", "test-prefix")

        assert len(result) == 3
        assert all(r["status"] == "completed" for r in result)

    @patch("app.dynamodb")
    def test_returns_empty_list_when_no_items(self, mock_dynamodb):
        from app import _query_completed_executions

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": []}

        result = _query_completed_executions("U1", "test-prefix")

        assert result == []

    @patch("app.dynamodb")
    def test_passes_scan_index_forward_false_and_limit(self, mock_dynamodb):
        from app import _query_completed_executions

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": []}

        _query_completed_executions("U1", "test-prefix")

        call_kwargs = mock_table.query.call_args[1]
        assert call_kwargs["ScanIndexForward"] is False
        assert call_kwargs["Limit"] == 20
        assert call_kwargs["IndexName"] == "user-id-index"


class TestGetDeliverableUrls:
    """_get_deliverable_urls のテスト"""

    @patch("app.dynamodb")
    def test_returns_notion_url_only_when_legacy_record(self, mock_dynamodb):
        """github_url フィールド未存在の旧フォーマットレコードでは notion_url のみセットされる"""
        from app import _get_deliverable_urls

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [{"execution_id": "exec-001", "external_url": "https://notion.so/page-123"}]
        }

        result = _get_deliverable_urls("exec-001", "test-prefix")

        assert result == {"notion_url": "https://notion.so/page-123", "github_url": None}

    @patch("app.dynamodb")
    def test_returns_both_urls_when_github_url_present(self, mock_dynamodb):
        """github_url フィールド有りの新フォーマットレコードでは両方セットされる"""
        from app import _get_deliverable_urls

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [
                {
                    "execution_id": "exec-001",
                    "external_url": "https://notion.so/page-123",
                    "github_url": "https://github.com/owner/repo/tree/main/topic-20260429",
                }
            ]
        }

        result = _get_deliverable_urls("exec-001", "test-prefix")

        assert result == {
            "notion_url": "https://notion.so/page-123",
            "github_url": "https://github.com/owner/repo/tree/main/topic-20260429",
        }

    @patch("app.dynamodb")
    def test_returns_none_when_no_record(self, mock_dynamodb):
        from app import _get_deliverable_urls

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": []}

        result = _get_deliverable_urls("exec-001", "test-prefix")

        assert result is None


class TestHandleHistoryCommand:
    """_handle_history_command の統合テスト（外部依存はモック）"""

    @patch("app._post_history_result")
    @patch("app._get_deliverable_urls")
    @patch("app._query_completed_executions")
    def test_no_keyword_passes_all_completed_up_to_5(self, mock_query, mock_get_urls, mock_post):
        from app import _handle_history_command

        mock_query.return_value = [
            {"execution_id": f"e{i}", "topic": f"Topic {i}", "category": "tech", "created_at": "2026-01-01T00:00:00"}
            for i in range(3)
        ]
        mock_get_urls.return_value = {"notion_url": "https://notion.so/page", "github_url": None}

        _handle_history_command("U1", "C1", "111.000", None, "prefix", "token")

        mock_post.assert_called_once()
        items = mock_post.call_args[0][2]
        assert len(items) == 3

    @patch("app._post_history_result")
    @patch("app._get_deliverable_urls")
    @patch("app._query_completed_executions")
    def test_keyword_filters_by_topic(self, mock_query, mock_get_urls, mock_post):
        from app import _handle_history_command

        mock_query.return_value = [
            {"execution_id": "e1", "topic": "Terraform入門", "category": "infra", "created_at": "2026-01-01T00:00:00"},
            {"execution_id": "e2", "topic": "Kubernetes概要", "category": "infra", "created_at": "2026-01-02T00:00:00"},
            {"execution_id": "e3", "topic": "terraform応用", "category": "infra", "created_at": "2026-01-03T00:00:00"},
        ]
        mock_get_urls.return_value = None

        _handle_history_command("U1", "C1", "111.000", "terraform", "prefix", "token")

        items = mock_post.call_args[0][2]
        assert len(items) == 2
        assert all("terraform" in item["topic"].lower() for item in items)

    @patch("app._post_history_result")
    @patch("app._get_deliverable_urls")
    @patch("app._query_completed_executions")
    def test_truncates_to_5_items(self, mock_query, mock_get_urls, mock_post):
        from app import _handle_history_command

        mock_query.return_value = [
            {"execution_id": f"e{i}", "topic": f"Topic {i}", "category": "tech", "created_at": "2026-01-01T00:00:00"}
            for i in range(8)
        ]
        mock_get_urls.return_value = {"notion_url": "https://notion.so/page", "github_url": None}

        _handle_history_command("U1", "C1", "111.000", None, "prefix", "token")

        items = mock_post.call_args[0][2]
        assert len(items) == 5

    @patch("app._post_history_result")
    @patch("app._get_deliverable_urls")
    @patch("app._query_completed_executions")
    def test_url_none_is_passed_to_post(self, mock_query, mock_get_urls, mock_post):
        from app import _handle_history_command

        mock_query.return_value = [
            {"execution_id": "e1", "topic": "Topic", "category": "tech", "created_at": "2026-01-01T00:00:00"}
        ]
        mock_get_urls.return_value = None

        _handle_history_command("U1", "C1", "111.000", None, "prefix", "token")

        items = mock_post.call_args[0][2]
        assert items[0]["notion_url"] is None
        assert items[0]["github_url"] is None

    @patch("app._post_history_result")
    @patch("app._get_deliverable_urls")
    @patch("app._query_completed_executions")
    def test_github_url_is_passed_to_post(self, mock_query, mock_get_urls, mock_post):
        """deliverables に github_url が含まれるとき、items に notion_url / github_url 両方が入る"""
        from app import _handle_history_command

        mock_query.return_value = [
            {"execution_id": "e1", "topic": "Topic", "category": "tech", "created_at": "2026-01-01T00:00:00"}
        ]
        mock_get_urls.return_value = {
            "notion_url": "https://notion.so/page",
            "github_url": "https://github.com/owner/repo/tree/main/topic-20260429",
        }

        _handle_history_command("U1", "C1", "111.000", None, "prefix", "token")

        items = mock_post.call_args[0][2]
        assert items[0]["notion_url"] == "https://notion.so/page"
        assert items[0]["github_url"] == "https://github.com/owner/repo/tree/main/topic-20260429"

    @patch("app._post_history_result")
    @patch("app._get_deliverable_urls")
    @patch("app._query_completed_executions")
    def test_empty_executions_passes_empty_items(self, mock_query, mock_get_urls, mock_post):
        from app import _handle_history_command

        mock_query.return_value = []

        _handle_history_command("U1", "C1", "111.000", None, "prefix", "token")

        items = mock_post.call_args[0][2]
        assert items == []
        mock_get_urls.assert_not_called()

    @patch("app._post_history_result")
    @patch("app._get_deliverable_urls")
    @patch("app._query_completed_executions")
    def test_get_deliverable_urls_exception_sets_urls_none_and_continues(self, mock_query, mock_get_urls, mock_post):
        from app import _handle_history_command

        mock_query.return_value = [
            {"execution_id": "e1", "topic": "Topic A", "category": "tech", "created_at": "2026-01-01T00:00:00"},
            {"execution_id": "e2", "topic": "Topic B", "category": "tech", "created_at": "2026-01-02T00:00:00"},
        ]

        def get_urls_side_effect(execution_id, table_prefix):
            if execution_id == "e1":
                raise RuntimeError("DynamoDB error")
            return {"notion_url": "https://notion.so/page-e2", "github_url": None}

        mock_get_urls.side_effect = get_urls_side_effect

        _handle_history_command("U1", "C1", "111.000", None, "prefix", "token")

        items = mock_post.call_args[0][2]
        assert len(items) == 2
        assert items[0]["notion_url"] is None  # e1: exception → None
        assert items[0]["github_url"] is None
        assert items[1]["notion_url"] == "https://notion.so/page-e2"  # e2: unaffected
        assert items[1]["github_url"] is None


class TestPostHistoryResult:
    """_post_history_result のテスト"""

    @patch("app.WebClient")
    def test_empty_no_keyword_posts_no_deliverables_message(self, mock_webclient_cls):
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        _post_history_result("C1", "111.000", [], None, "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "📭" in text
        assert "まだ成果物がありません" in text

    @patch("app.WebClient")
    def test_empty_with_keyword_posts_not_found_message(self, mock_webclient_cls):
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        _post_history_result("C1", "111.000", [], "Terraform", "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "📭" in text
        assert "Terraform" in text
        assert "見つかりません" in text

    @patch("app.WebClient")
    def test_items_no_keyword_uses_default_header(self, mock_webclient_cls):
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        items = [
            {
                "topic": "T1",
                "category": "cat",
                "date": "2026-01-01",
                "notion_url": "https://notion.so/p1",
                "github_url": None,
            }
        ]
        _post_history_result("C1", "111.000", items, None, "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "📚 成果物履歴（最新 1 件）" in text

    @patch("app.WebClient")
    def test_items_with_keyword_uses_keyword_header(self, mock_webclient_cls):
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        items = [
            {
                "topic": "Terraform入門",
                "category": "infra",
                "date": "2026-01-01",
                "notion_url": None,
                "github_url": None,
            }
        ]
        _post_history_result("C1", "111.000", items, "Terraform", "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "📚 成果物履歴「Terraform」（最新 1 件）" in text

    @patch("app.WebClient")
    def test_history_command_displays_github_url(self, mock_webclient_cls):
        """github_url ありの item は notion_url と github_url の 2 行が表示される"""
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        items = [
            {
                "topic": "T1",
                "category": "cat",
                "date": "2026-01-01",
                "notion_url": "https://notion.so/p1",
                "github_url": "https://github.com/owner/repo/tree/main/t1-20260101",
            }
        ]
        _post_history_result("C1", "111.000", items, None, "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "   📝 https://notion.so/p1" in text
        assert "   💻 https://github.com/owner/repo/tree/main/t1-20260101" in text

    @patch("app.WebClient")
    def test_history_command_omits_github_url_when_absent(self, mock_webclient_cls):
        """github_url が None の item は notion_url の 1 行のみ表示され、💻 行は出ない"""
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        items = [
            {
                "topic": "T1",
                "category": "cat",
                "date": "2026-01-01",
                "notion_url": "https://notion.so/p1",
                "github_url": None,
            }
        ]
        _post_history_result("C1", "111.000", items, None, "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "   📝 https://notion.so/p1" in text
        assert "💻" not in text

    @patch("app.WebClient")
    def test_history_command_handles_legacy_record(self, mock_webclient_cls):
        """github_url キー自体が存在しない旧フォーマット item でも例外を出さず notion_url のみ表示"""
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        # 旧フォーマット: github_url キーが含まれていない（item.get("github_url") が None になる）
        items = [{"topic": "T1", "category": "cat", "date": "2026-01-01", "notion_url": "https://notion.so/p1"}]
        _post_history_result("C1", "111.000", items, None, "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "   📝 https://notion.so/p1" in text
        assert "💻" not in text

    @patch("app.WebClient")
    def test_history_command_no_url(self, mock_webclient_cls):
        """notion_url も github_url も None のとき「（URL なし）」表示が維持される"""
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        items = [{"topic": "T1", "category": "cat", "date": "2026-01-01", "notion_url": None, "github_url": None}]
        _post_history_result("C1", "111.000", items, None, "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "   （URL なし）" in text
        assert "📝" not in text
        assert "💻" not in text

    @patch("app.WebClient")
    def test_multiple_items_are_numbered(self, mock_webclient_cls):
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        items = [
            {"topic": "Topic A", "category": "cat", "date": "2026-01-01", "notion_url": None, "github_url": None},
            {"topic": "Topic B", "category": "cat", "date": "2026-01-02", "notion_url": None, "github_url": None},
        ]
        _post_history_result("C1", "111.000", items, None, "token")

        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "1. Topic A" in text
        assert "2. Topic B" in text

    @patch("app.WebClient")
    def test_posts_with_correct_thread_ts(self, mock_webclient_cls):
        from app import _post_history_result

        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        _post_history_result("C1", "999.111", [], None, "token")

        call_kwargs = mock_slack.chat_postMessage.call_args[1]
        assert call_kwargs["thread_ts"] == "999.111"
        assert call_kwargs["channel"] == "C1"


@pytest.mark.usefixtures("_env_vars")
class TestHistoryCommandRouting:
    """lambda_handler の F9 履歴コマンドルーティングのテスト"""

    def _make_secrets_side_effect(self):
        return lambda SecretId: {  # noqa: N803
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:signing": {"SecretString": SIGNING_SECRET},
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:bot-token": {"SecretString": "xoxb-test"},
        }[SecretId]

    @patch("app.ecs_client")
    @patch("app._handle_history_command")
    @patch("app.secrets_client")
    def test_rekishi_toplevel_calls_history_handler_not_ecs(self, mock_secrets, mock_handle_hist, mock_ecs):
        """「履歴」トップレベル投稿 → _handle_history_command 呼び出し、ECS 非起動、HTTP 200"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> 履歴",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "111.000",
            },
        }
        result = lambda_handler(_make_event(body), _make_lambda_context())

        assert result["statusCode"] == 200
        mock_handle_hist.assert_called_once()
        mock_ecs.run_task.assert_not_called()

    @patch("app.ecs_client")
    @patch("app._handle_history_command")
    @patch("app.secrets_client")
    def test_history_with_keyword_passes_keyword(self, mock_secrets, mock_handle_hist, mock_ecs):
        """「history Terraform」トップレベル投稿 → keyword="Terraform" で呼び出し"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> history Terraform",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "111.000",
            },
        }
        lambda_handler(_make_event(body), _make_lambda_context())

        call_kwargs = mock_handle_hist.call_args
        keyword = call_kwargs[0][3]  # positional arg index 3
        assert keyword == "Terraform"

    @patch("app.ecs_client")
    @patch("app._handle_history_command")
    @patch("app.secrets_client")
    def test_history_uppercase_calls_history_handler(self, mock_secrets, mock_handle_hist, mock_ecs):
        """「History」大文字始まりでも _handle_history_command が呼ばれること"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> History",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "111.000",
            },
        }
        lambda_handler(_make_event(body), _make_lambda_context())

        mock_handle_hist.assert_called_once()

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app._handle_history_command")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_rekishi_thread_reply_goes_to_f8_flow(
        self, mock_secrets, mock_webclient_cls, mock_handle_hist, mock_dynamodb, mock_ecs
    ):
        """「履歴」スレッド返信 → _handle_history_command 非呼び出し、F8 フロー"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {
            "Items": [{"execution_id": "exec-001", "status": "completed", "slack_thread_ts": "111.000"}]
        }
        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> 履歴",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "222.000",
                "thread_ts": "111.000",
            },
        }
        result = lambda_handler(_make_event(body), _make_lambda_context())

        assert result["statusCode"] == 200
        mock_handle_hist.assert_not_called()
        # F8 フィードバックフローが動いていること（ECS 起動）
        mock_ecs.run_task.assert_called_once()

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app._handle_history_command")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_regular_topic_goes_to_existing_flow(
        self, mock_secrets, mock_webclient_cls, mock_handle_hist, mock_dynamodb, mock_ecs
    ):
        """通常トピック → _handle_history_command 非呼び出し、既存 ECS 起動フロー"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()
        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack
        mock_slack.chat_postMessage.return_value = {"ts": "555.000"}
        mock_dynamodb.Table.return_value = MagicMock()

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> Terraform入門",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "555.000",
            },
        }
        result = lambda_handler(_make_event(body), _make_lambda_context())

        assert result["statusCode"] == 200
        mock_handle_hist.assert_not_called()
        mock_ecs.run_task.assert_called_once()

    @patch("app.ecs_client")
    @patch("app.WebClient")
    @patch("app._handle_history_command")
    @patch("app.secrets_client")
    def test_history_command_exception_posts_error_and_returns_200(
        self, mock_secrets, mock_handle_hist, mock_webclient_cls, mock_ecs
    ):
        """_handle_history_command が例外を raise → エラーメッセージ投稿、HTTP 200"""
        from app import lambda_handler

        mock_secrets.get_secret_value.side_effect = self._make_secrets_side_effect()
        mock_handle_hist.side_effect = RuntimeError("DynamoDB down")
        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack

        body = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> 履歴",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "ts": "111.000",
            },
        }
        result = lambda_handler(_make_event(body), _make_lambda_context())

        assert result["statusCode"] == 200
        mock_ecs.run_task.assert_not_called()
        mock_slack.chat_postMessage.assert_called_once()
        text = mock_slack.chat_postMessage.call_args[1]["text"]
        assert "❌" in text
        assert "エラー" in text


@pytest.mark.usefixtures("_env_vars")
class TestTopicReceivedEmit:
    """T1-3: 新規トピック受領時に topic_received イベントが emit される"""

    @staticmethod
    def _make_event_body() -> dict:
        return {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U_BOT> Terraform 入門",
                "user": "U_USER123",
                "channel": "C_CHANNEL",
            },
        }

    def _setup_basic_mocks(self, mock_secrets, mock_webclient_cls, mock_dynamodb):
        mock_secrets.get_secret_value.side_effect = lambda SecretId: {  # noqa: N803
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:signing": {"SecretString": SIGNING_SECRET},
            "arn:aws:secretsmanager:ap-northeast-1:123:secret:bot-token": {"SecretString": "xoxb-test"},
        }[SecretId]
        mock_slack = MagicMock()
        mock_webclient_cls.return_value = mock_slack
        mock_slack.chat_postMessage.return_value = {"ts": "1234567890.000100"}
        mock_dynamodb.Table.return_value = MagicMock()

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_topic_received_emitted_with_correct_payload(
        self, mock_secrets, mock_webclient_cls, mock_dynamodb, mock_ecs
    ):
        from app import lambda_handler

        self._setup_basic_mocks(mock_secrets, mock_webclient_cls, mock_dynamodb)
        emitter_instance = MagicMock()
        with patch("app._EventEmitter") as mock_emitter_cls:
            mock_emitter_cls.return_value = emitter_instance
            result = lambda_handler(_make_event(self._make_event_body()), _make_lambda_context())

        assert result["statusCode"] == 200
        mock_ecs.run_task.assert_called_once()

        # EventEmitter は execution_id (= workflow_run_id) で初期化される
        mock_emitter_cls.assert_called_once()
        execution_id_arg = mock_emitter_cls.call_args.args[0]
        assert execution_id_arg.startswith("exec-")

        # emit は topic_received で 1 回
        emitter_instance.emit.assert_called_once()
        event_type, payload = emitter_instance.emit.call_args.args[:2]
        assert event_type == "topic_received"
        assert payload["topic"] == "Terraform 入門"
        assert payload["channel_id"] == "C_CHANNEL"
        # PII: user_id_hash は SHA-256 16 文字 prefix (raw user_id を含まない)
        expected_hash = hashlib.sha256(b"U_USER123").hexdigest()[:16]
        assert payload["user_id_hash"] == expected_hash
        assert "U_USER123" not in str(payload)
        # workflow_run_id は execution_id と一致 (design.md §2.5 整合性)
        assert payload["workflow_run_id"] == execution_id_arg

    @patch("app.ecs_client")
    @patch("app.dynamodb")
    @patch("app.WebClient")
    @patch("app.secrets_client")
    def test_emit_skipped_when_event_emitter_unavailable(
        self, mock_secrets, mock_webclient_cls, mock_dynamodb, mock_ecs
    ):
        """_EventEmitter が None (Lambda zip 内 fallback) でも 200 + 既存挙動を維持する"""
        from app import lambda_handler

        self._setup_basic_mocks(mock_secrets, mock_webclient_cls, mock_dynamodb)
        with patch("app._EventEmitter", None):
            result = lambda_handler(_make_event(self._make_event_body()), _make_lambda_context())

        assert result["statusCode"] == 200
        mock_ecs.run_task.assert_called_once()
