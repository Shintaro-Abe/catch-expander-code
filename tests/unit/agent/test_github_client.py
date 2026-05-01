from unittest.mock import MagicMock, patch

import pytest


class TestGitHubClient:
    """GitHubClient のテスト"""

    def _make_client(self):
        from storage.github_client import GitHubClient

        return GitHubClient("github_pat_test", "owner/catch-expander-code")

    @patch("storage.github_client.requests.get")
    @patch("storage.github_client.requests.request")
    def test_push_files_success(self, mock_request, mock_get):
        # 既存ファイルなし（404）
        mock_get.return_value = MagicMock(status_code=404)

        mock_response = mock_request.return_value
        mock_response.status_code = 201
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"content": {"sha": "abc123"}}

        client = self._make_client()
        url = client.push_files("test-dir", {"main.tf": "# terraform", "variables.tf": "# vars"})

        assert "owner/catch-expander-code" in url
        assert "test-dir" in url
        assert mock_request.call_count == 2

    @patch("storage.github_client.requests.get")
    @patch("storage.github_client.requests.request")
    def test_push_files_with_existing_file(self, mock_request, mock_get):
        # 既存ファイルあり（200, SHA返却）
        existing_response = MagicMock(status_code=200)
        existing_response.json.return_value = {"sha": "existing_sha"}
        mock_get.return_value = existing_response

        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"content": {"sha": "new_sha"}}

        client = self._make_client()
        client.push_files("dir", {"file.txt": "content"})

        payload = mock_request.call_args[1]["json"]
        assert payload["sha"] == "existing_sha"

    @patch("storage.github_client.requests.get")
    @patch("storage.github_client.requests.request")
    def test_create_readme(self, mock_request, mock_get):
        mock_get.return_value = MagicMock(status_code=404)

        mock_response = mock_request.return_value
        mock_response.status_code = 201
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {}

        client = self._make_client()
        client.create_readme("test-dir", "# Title", "https://notion.so/page")

        payload = mock_request.call_args[1]["json"]
        import base64

        content = base64.b64decode(payload["content"]).decode()
        assert "https://notion.so/page" in content

    @patch("storage.github_client.time.sleep")
    @patch("storage.github_client.requests.get")
    @patch("storage.github_client.requests.request")
    def test_retry_on_http_error(self, mock_request, mock_get, mock_sleep):
        import requests

        mock_get.return_value = MagicMock(status_code=404)

        error_response = mock_request.return_value
        error_response.status_code = 500
        error_response.raise_for_status.side_effect = requests.HTTPError(response=error_response)

        client = self._make_client()
        with pytest.raises(requests.HTTPError):
            client.push_files("dir", {"file.txt": "content"})

        assert mock_request.call_count == 3


class TestGitHubApiCallEmit:
    """T1-2b: GitHubClient の api_call_completed / rate_limit_hit emit"""

    def _make_client(self, emitter=None):
        from storage.github_client import GitHubClient

        client = GitHubClient("github_pat_test", "owner/catch-expander-code")
        if emitter is not None:
            client._emitter = emitter
        return client

    @patch("storage.github_client.requests.get")
    @patch("storage.github_client.requests.request")
    def test_success_emits_api_call_completed(self, mock_request, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        mock_response = mock_request.return_value
        mock_response.status_code = 201
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"content": {"sha": "abc"}}

        emitter = MagicMock()
        client = self._make_client(emitter=emitter)
        client.push_files("dir", {"main.tf": "x"})

        events = [c.args[0] for c in emitter.emit.call_args_list]
        assert "api_call_completed" in events
        api_payload = next(c.args[1] for c in emitter.emit.call_args_list if c.args[0] == "api_call_completed")
        assert api_payload["subtype"] == "github"
        assert api_payload["success"] is True

    @patch("storage.github_client.time.sleep")
    @patch("storage.github_client.requests.get")
    @patch("storage.github_client.requests.request")
    def test_429_emits_rate_limit_hit(self, mock_request, mock_get, mock_sleep):
        import requests

        mock_get.return_value = MagicMock(status_code=404)
        error_response = mock_request.return_value
        error_response.status_code = 429
        error_response.headers = {"Retry-After": "60"}
        error_response.raise_for_status.side_effect = requests.HTTPError(response=error_response)

        emitter = MagicMock()
        client = self._make_client(emitter=emitter)
        with pytest.raises(requests.HTTPError):
            client.push_files("dir", {"file.txt": "content"})

        emitted = {c.args[0]: c.args[1] for c in emitter.emit.call_args_list}
        assert emitted["rate_limit_hit"]["subtype"] == "github_429"
        assert emitted["rate_limit_hit"]["retry_after_seconds"] == 60
        assert emitted["api_call_completed"]["success"] is False

    @patch("storage.github_client.requests.get")
    @patch("storage.github_client.requests.request")
    def test_preflight_get_emits_api_call_completed(self, mock_request, mock_get):
        """T1-2b Codex P2: SHA 取得用 preflight GET も api_call_completed で観測される。"""
        # 既存ファイルあり (200) → preflight GET success + PUT success
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"sha": "abc"})
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"content": {"sha": "new"}}

        emitter = MagicMock()
        client = self._make_client(emitter=emitter)
        client.push_files("dir", {"file.txt": "content"})

        # 1 ファイル push で api_call_completed が 2 回 (preflight GET + PUT) emit される
        api_events = [c.args[1] for c in emitter.emit.call_args_list if c.args[0] == "api_call_completed"]
        assert len(api_events) == 2
        # 全て github subtype
        assert all(e["subtype"] == "github" for e in api_events)
        # preflight GET は status 200
        get_event = next(e for e in api_events if e["response_status_code"] == 200 and e["success"] is True)
        assert get_event is not None

    @patch("storage.github_client.requests.get")
    @patch("storage.github_client.requests.request")
    def test_preflight_get_429_emits_rate_limit_hit(self, mock_request, mock_get):
        """preflight GET が 429 を返した場合も rate_limit_hit を emit"""
        mock_get.return_value = MagicMock(
            status_code=429,
            headers={"Retry-After": "45"},
        )
        # PUT 側は成功させる
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {}

        emitter = MagicMock()
        client = self._make_client(emitter=emitter)
        client.push_files("dir", {"file.txt": "content"})

        rate_limit_events = [c.args[1] for c in emitter.emit.call_args_list if c.args[0] == "rate_limit_hit"]
        assert len(rate_limit_events) >= 1
        assert rate_limit_events[0]["subtype"] == "github_429"
        assert rate_limit_events[0]["retry_after_seconds"] == 45
