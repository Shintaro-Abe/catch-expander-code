import json
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _is_token_stale
# ---------------------------------------------------------------------------


class TestIsTokenStale:
    _THRESHOLD_MS = 24 * 60 * 60 * 1000  # 24h

    def _make_oauth_json(self, expires_at_ms: int) -> str:
        return json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}})

    def test_returns_false_when_not_yet_expired(self):
        from handler import _is_token_stale

        future_ms = int(time.time() * 1000) + 10 * 60 * 60 * 1000  # 10h後
        is_stale, expires_at = _is_token_stale(self._make_oauth_json(future_ms), self._THRESHOLD_MS)

        assert is_stale is False
        assert expires_at == future_ms

    def test_returns_false_when_expired_but_within_threshold(self):
        from handler import _is_token_stale

        # 12h前に期限切れ（閾値 24h 未満）
        past_12h_ms = int(time.time() * 1000) - 12 * 60 * 60 * 1000
        is_stale, expires_at = _is_token_stale(self._make_oauth_json(past_12h_ms), self._THRESHOLD_MS)

        assert is_stale is False
        assert expires_at == past_12h_ms

    def test_returns_true_when_expired_beyond_threshold(self):
        from handler import _is_token_stale

        # 25h前に期限切れ（閾値 24h 超過）
        past_25h_ms = int(time.time() * 1000) - 25 * 60 * 60 * 1000
        is_stale, expires_at = _is_token_stale(self._make_oauth_json(past_25h_ms), self._THRESHOLD_MS)

        assert is_stale is True
        assert expires_at == past_25h_ms

    def test_returns_false_none_when_expires_at_missing(self):
        from handler import _is_token_stale

        oauth_json = json.dumps({"claudeAiOauth": {}})
        is_stale, expires_at = _is_token_stale(oauth_json, self._THRESHOLD_MS)

        assert is_stale is False
        assert expires_at is None

    def test_returns_false_none_when_claude_ai_oauth_missing(self):
        from handler import _is_token_stale

        oauth_json = json.dumps({"someOtherKey": "value"})
        is_stale, expires_at = _is_token_stale(oauth_json, self._THRESHOLD_MS)

        assert is_stale is False
        assert expires_at is None

    def test_returns_false_none_on_invalid_json(self):
        from handler import _is_token_stale

        is_stale, expires_at = _is_token_stale("not-valid-json", self._THRESHOLD_MS)

        assert is_stale is False
        assert expires_at is None


# ---------------------------------------------------------------------------
# _post_slack_notification
# ---------------------------------------------------------------------------


class TestPostSlackNotification:
    def test_posts_message_with_expiry_info(self, monkeypatch):
        from handler import _post_slack_notification

        mock_client = MagicMock()
        with patch("handler.WebClient", return_value=mock_client):
            # 10h前に期限切れ
            expires_at_ms = int(time.time() * 1000) - 10 * 60 * 60 * 1000
            _post_slack_notification("slack-token", "C12345", expires_at_ms)

        mock_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C12345"
        assert "⚠️" in call_kwargs["text"]
        assert "10 時間前に期限切れ" in call_kwargs["text"]
        assert "claude" in call_kwargs["text"]

    def test_posts_message_with_unknown_expiry_when_none(self):
        from handler import _post_slack_notification

        mock_client = MagicMock()
        with patch("handler.WebClient", return_value=mock_client):
            _post_slack_notification("slack-token", "C12345", None)

        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert "不明" in call_kwargs["text"]

    def test_raises_on_slack_api_error(self):
        from handler import _post_slack_notification
        from slack_sdk.errors import SlackApiError

        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = SlackApiError(
            message="channel_not_found",
            response={"error": "channel_not_found"},
        )
        with patch("handler.WebClient", return_value=mock_client):
            with pytest.raises(SlackApiError):
                _post_slack_notification("slack-token", "C_INVALID", None)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------


class TestLambdaHandler:
    _COMMON_ENV = {
        "CLAUDE_OAUTH_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-1:123:secret:claude",
        "SLACK_BOT_TOKEN_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-1:123:secret:slack",
        "SLACK_NOTIFICATION_CHANNEL_ID": "C_MONITOR",
        "STALE_THRESHOLD_HOURS": "24",
    }

    def _set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in self._COMMON_ENV.items():
            monkeypatch.setenv(k, v)

    def test_does_not_notify_when_token_is_valid(self, monkeypatch):
        self._set_env(monkeypatch)

        future_ms = int(time.time() * 1000) + 10 * 60 * 60 * 1000
        oauth_json = json.dumps({"claudeAiOauth": {"expiresAt": future_ms}})

        with (
            patch("handler._get_secret", return_value=oauth_json),
            patch("handler._post_slack_notification") as mock_notify,
        ):
            from handler import lambda_handler

            lambda_handler({}, None)

        mock_notify.assert_not_called()

    def test_notifies_when_token_is_stale(self, monkeypatch):
        self._set_env(monkeypatch)

        past_25h_ms = int(time.time() * 1000) - 25 * 60 * 60 * 1000
        oauth_json = json.dumps({"claudeAiOauth": {"expiresAt": past_25h_ms}})

        with (
            patch("handler._get_secret", side_effect=[oauth_json, "slack-bot-token"]),
            patch("handler._post_slack_notification") as mock_notify,
        ):
            from handler import lambda_handler

            lambda_handler({}, None)

        mock_notify.assert_called_once_with("slack-bot-token", "C_MONITOR", past_25h_ms)

    def test_fetches_only_claude_secret_when_token_is_valid(self, monkeypatch):
        self._set_env(monkeypatch)

        future_ms = int(time.time() * 1000) + 10 * 60 * 60 * 1000
        oauth_json = json.dumps({"claudeAiOauth": {"expiresAt": future_ms}})

        with patch("handler._get_secret", return_value=oauth_json) as mock_get:
            from handler import lambda_handler

            lambda_handler({}, None)

        # 有効なトークンでは Slack トークン取得が不要
        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0] == "arn:aws:secretsmanager:ap-northeast-1:123:secret:claude"

    def test_fetches_slack_secret_only_when_stale(self, monkeypatch):
        self._set_env(monkeypatch)

        past_25h_ms = int(time.time() * 1000) - 25 * 60 * 60 * 1000
        oauth_json = json.dumps({"claudeAiOauth": {"expiresAt": past_25h_ms}})

        with (
            patch("handler._get_secret", side_effect=[oauth_json, "slack-bot-token"]) as mock_get,
            patch("handler._post_slack_notification"),
        ):
            from handler import lambda_handler

            lambda_handler({}, None)

        arns = [c.args[0] for c in mock_get.call_args_list]
        assert "arn:aws:secretsmanager:ap-northeast-1:123:secret:claude" in arns
        assert "arn:aws:secretsmanager:ap-northeast-1:123:secret:slack" in arns

    def test_does_not_notify_when_expires_at_missing(self, monkeypatch):
        self._set_env(monkeypatch)

        oauth_json = json.dumps({"claudeAiOauth": {}})

        with (
            patch("handler._get_secret", return_value=oauth_json),
            patch("handler._post_slack_notification") as mock_notify,
        ):
            from handler import lambda_handler

            lambda_handler({}, None)

        mock_notify.assert_not_called()

    def test_uses_default_threshold_when_env_not_set(self, monkeypatch):
        for k, v in self._COMMON_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("STALE_THRESHOLD_HOURS", raising=False)

        # デフォルト閾値 24h: 25h 前に期限切れなら失効
        past_25h_ms = int(time.time() * 1000) - 25 * 60 * 60 * 1000
        oauth_json = json.dumps({"claudeAiOauth": {"expiresAt": past_25h_ms}})

        with (
            patch("handler._get_secret", side_effect=[oauth_json, "slack-token"]),
            patch("handler._post_slack_notification") as mock_notify,
        ):
            from handler import lambda_handler

            lambda_handler({}, None)

        mock_notify.assert_called_once()
