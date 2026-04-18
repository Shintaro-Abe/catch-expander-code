import copy
from unittest.mock import patch

import pytest


class TestSplitLongRichText:
    """_split_long_rich_text のテスト"""

    def _split(self, blocks):
        from storage.notion_client import _split_long_rich_text

        return _split_long_rich_text(blocks)

    def _make_code_block(self, content: str, language: str = "terraform") -> dict:
        return {
            "type": "code",
            "code": {
                "rich_text": [{"type": "text", "text": {"content": content}}],
                "language": language,
            },
        }

    def test_split_short_rich_text_unchanged(self):
        blocks = [self._make_code_block("short content")]
        result = self._split(blocks)
        assert result == blocks

    def test_split_exactly_2000_chars_unchanged(self):
        blocks = [self._make_code_block("x" * 2000)]
        result = self._split(blocks)
        assert len(result[0]["code"]["rich_text"]) == 1
        assert result[0]["code"]["rich_text"][0]["text"]["content"] == "x" * 2000

    def test_split_over_2000_code_block(self):
        blocks = [self._make_code_block("x" * 3921)]
        result = self._split(blocks)
        rich_text = result[0]["code"]["rich_text"]
        assert len(rich_text) == 2
        assert rich_text[0]["text"]["content"] == "x" * 2000
        assert rich_text[1]["text"]["content"] == "x" * 1921

    def test_split_preserves_language_and_annotations(self):
        blocks = [self._make_code_block("y" * 2500, language="python")]
        result = self._split(blocks)
        assert result[0]["type"] == "code"
        assert result[0]["code"]["language"] == "python"

    def test_split_preserves_other_rich_text_fields(self):
        blocks = [
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": "z" * 2500, "link": {"url": "https://example.com"}},
                            "annotations": {"bold": True, "color": "red"},
                        }
                    ]
                },
            }
        ]
        result = self._split(blocks)
        rich_text = result[0]["paragraph"]["rich_text"]
        assert len(rich_text) == 2
        for chunk in rich_text:
            assert chunk["type"] == "text"
            assert chunk["annotations"] == {"bold": True, "color": "red"}
            assert chunk["text"]["link"] == {"url": "https://example.com"}

    def test_split_handles_multiple_blocks(self):
        blocks = [
            self._make_code_block("short"),
            self._make_code_block("a" * 4500),
            self._make_code_block("also short"),
        ]
        result = self._split(blocks)
        assert len(result) == 3
        assert len(result[0]["code"]["rich_text"]) == 1
        assert len(result[1]["code"]["rich_text"]) == 3  # 2000 + 2000 + 500
        assert len(result[2]["code"]["rich_text"]) == 1

    def test_split_handles_block_without_rich_text(self):
        blocks = [{"type": "divider", "divider": {}}]
        result = self._split(blocks)
        assert result == blocks

    def test_split_handles_unknown_type_safely(self):
        blocks = [
            {"type": "mystery", "other_key": "value"},
            {"no_type_field": True},
            {"type": "code", "code": "not-a-dict"},
            {"type": "code", "code": {"rich_text": "not-a-list"}},
        ]
        result = self._split(blocks)
        assert result == blocks

    def test_split_does_not_mutate_input(self):
        blocks = [self._make_code_block("x" * 3921)]
        snapshot = copy.deepcopy(blocks)
        self._split(blocks)
        assert blocks == snapshot


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
