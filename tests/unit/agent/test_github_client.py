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
