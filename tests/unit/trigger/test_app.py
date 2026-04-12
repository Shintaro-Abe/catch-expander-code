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
