from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError


class TestSlackClient:
    """SlackClient のテスト"""

    def _make_client(self):
        from notify.slack_client import SlackClient

        return SlackClient("xoxb-test-token")

    def test_post_progress_success(self):
        client = self._make_client()
        client.client = MagicMock()

        client.post_progress("C1", "ts1", "テスト通知")
        client.client.chat_postMessage.assert_called_once_with(channel="C1", thread_ts="ts1", text="テスト通知")

    def test_post_completion_with_github(self):
        client = self._make_client()
        client.client = MagicMock()

        client.post_completion("C1", "ts1", "サマリー", "https://notion.so/page", "https://github.com/repo")
        call_kwargs = client.client.chat_postMessage.call_args[1]
        assert "https://notion.so/page" in call_kwargs["text"]
        assert "https://github.com/repo" in call_kwargs["text"]

    def test_post_completion_without_github(self):
        client = self._make_client()
        client.client = MagicMock()

        client.post_completion("C1", "ts1", "サマリー", "https://notion.so/page", None)
        call_kwargs = client.client.chat_postMessage.call_args[1]
        assert "https://notion.so/page" in call_kwargs["text"]
        assert "GitHub" not in call_kwargs["text"]

    def test_post_error(self):
        client = self._make_client()
        client.client = MagicMock()

        client.post_error("C1", "ts1", "エラーメッセージ")
        call_kwargs = client.client.chat_postMessage.call_args[1]
        assert "❌" in call_kwargs["text"]
        assert "エラーメッセージ" in call_kwargs["text"]

    @patch("notify.slack_client.time.sleep")
    def test_retry_on_slack_api_error(self, mock_sleep):
        client = self._make_client()
        client.client = MagicMock()

        error_response = MagicMock()
        error_response.__getitem__ = MagicMock(return_value="rate_limited")
        error_response.status_code = 429
        slack_error = SlackApiError("rate limited", error_response)

        client.client.chat_postMessage.side_effect = [slack_error, slack_error, None]
        client.post_progress("C1", "ts1", "test")

        assert client.client.chat_postMessage.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("notify.slack_client.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        client = self._make_client()
        client.client = MagicMock()

        error_response = MagicMock()
        error_response.__getitem__ = MagicMock(return_value="rate_limited")
        error_response.status_code = 429
        slack_error = SlackApiError("rate limited", error_response)

        client.client.chat_postMessage.side_effect = slack_error

        with pytest.raises(SlackApiError):
            client.post_progress("C1", "ts1", "test")
        assert client.client.chat_postMessage.call_count == 3

    def test_post_feedback_result_single_preference(self):
        client = self._make_client()
        client.client = MagicMock()
        prefs = [{"text": "Terraformはmodule分割する", "replaces_index": None}]

        client.post_feedback_result("C1", "ts1", prefs, 1)

        call_kwargs = client.client.chat_postMessage.call_args[1]
        assert "✅" in call_kwargs["text"]
        assert "Terraformはmodule分割する" in call_kwargs["text"]
        assert "1 件" in call_kwargs["text"]

    def test_post_feedback_result_multiple_preferences(self):
        client = self._make_client()
        client.client = MagicMock()
        prefs = [
            {"text": "好み1", "replaces_index": None},
            {"text": "好み2", "replaces_index": None},
        ]

        client.post_feedback_result("C1", "ts1", prefs, 5)

        call_kwargs = client.client.chat_postMessage.call_args[1]
        text = call_kwargs["text"]
        assert "好み1" in text
        assert "好み2" in text
        assert "5 件" in text

    def test_post_feedback_unextracted(self):
        client = self._make_client()
        client.client = MagicMock()

        client.post_feedback_unextracted("C1", "ts1")

        call_kwargs = client.client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C1"
        assert call_kwargs["thread_ts"] == "ts1"
        assert "📝" in call_kwargs["text"]
        assert "抽出できませんでした" in call_kwargs["text"]
