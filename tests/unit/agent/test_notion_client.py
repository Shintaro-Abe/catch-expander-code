from unittest.mock import patch

import pytest


class TestNotionClient:
    """NotionClient のテスト"""

    def _make_client(self):
        from storage.notion_client import NotionClient

        return NotionClient("ntn_test_token", "db-id-123")

    @patch("storage.notion_client.requests.request")
    def test_create_page_success(self, mock_request):
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "id": "page-id-abc",
            "url": "https://www.notion.so/test-page-abc",
        }

        client = self._make_client()
        url, page_id = client.create_page("AIパイプライン", "技術", [], None, "U_USER")

        assert url == "https://www.notion.so/test-page-abc"
        assert page_id == "page-id-abc"
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert call_args[0][0] == "POST"
        payload = call_args[1]["json"]
        assert payload["parent"]["database_id"] == "db-id-123"

    @patch("storage.notion_client.requests.request")
    def test_create_page_with_github_url(self, mock_request):
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"id": "page-id", "url": "https://notion.so/page"}

        client = self._make_client()
        client.create_page("Test", "技術", [], "https://github.com/test/repo", "U1")

        payload = mock_request.call_args[1]["json"]
        assert "GitHub URL" in payload["properties"]

    @patch("storage.notion_client.requests.request")
    def test_append_blocks(self, mock_request):
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {}

        client = self._make_client()
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "test"}}]}}]
        client.append_blocks("page-id", blocks)

        call_args = mock_request.call_args
        assert call_args[0][0] == "PATCH"
        assert "blocks/page-id/children" in call_args[0][1]

    @patch("storage.notion_client.requests.request")
    def test_update_page_status(self, mock_request):
        """update_page_status がステータスをPATCHすることを確認"""
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {}

        client = self._make_client()
        client.update_page_status("page-id-abc", "完了")

        call_args = mock_request.call_args
        assert call_args[0][0] == "PATCH"
        assert "pages/page-id-abc" in call_args[0][1]
        payload = call_args[1]["json"]
        assert payload["properties"]["ステータス"]["select"]["name"] == "完了"

    @patch("storage.notion_client.requests.request")
    def test_create_page_chunks_blocks_over_100(self, mock_request):
        """100ブロック超の場合、最初の100件でページ作成し残りをappend_blocksで追記する"""
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"id": "page-id", "url": "https://notion.so/page"}

        client = self._make_client()
        blocks = [{"type": "paragraph"} for _ in range(150)]
        client.create_page("Test", "技術", blocks, None, "U1")

        # POST（ページ作成）+ PATCH（残り50ブロック追記）= 計2回
        assert mock_request.call_count == 2
        first_payload = mock_request.call_args_list[0][1]["json"]
        assert len(first_payload["children"]) == 100
        second_payload = mock_request.call_args_list[1][1]["json"]
        assert len(second_payload["children"]) == 50

    @patch("storage.notion_client.requests.request")
    def test_no_retry_on_4xx_error(self, mock_request):
        """4xxエラーはリトライせず即時raiseする"""
        import requests

        error_response = mock_request.return_value
        error_response.status_code = 400
        error_response.text = "Bad Request"
        error_response.raise_for_status.side_effect = requests.HTTPError(response=error_response)

        client = self._make_client()
        with pytest.raises(requests.HTTPError):
            client.create_page("Test", "技術", [], None, "U1")

        assert mock_request.call_count == 1  # リトライなし

    @patch("storage.notion_client.time.sleep")
    @patch("storage.notion_client.requests.request")
    def test_retry_on_http_error(self, mock_request, mock_sleep):
        import requests

        error_response = mock_request.return_value
        error_response.status_code = 503
        error_response.raise_for_status.side_effect = requests.HTTPError(response=error_response)

        client = self._make_client()
        with pytest.raises(requests.HTTPError):
            client.create_page("Test", "技術", [], None, "U1")

        assert mock_request.call_count == 3
        assert mock_sleep.call_count == 3
