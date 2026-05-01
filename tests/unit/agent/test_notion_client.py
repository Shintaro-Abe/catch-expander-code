import copy
from unittest.mock import MagicMock, patch

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


class TestNotionApiCallEmit:
    """T1-2b: NotionClient の api_call_completed / rate_limit_hit emit"""

    def _make_client(self, emitter=None):
        from storage.notion_client import NotionClient

        client = NotionClient("ntn_test_token", "db-id-123")
        if emitter is not None:
            client._emitter = emitter
        return client

    @patch("storage.notion_client.requests.request")
    def test_success_emits_api_call_completed_only(self, mock_request):
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"id": "page-id", "url": "https://notion.so/page"}

        emitter = MagicMock()
        client = self._make_client(emitter=emitter)
        client.create_page("T", "技術", [], None, "U1")

        # api_call_completed 1 回のみ (rate_limit_hit は出ない)
        events = [c.args[0] for c in emitter.emit.call_args_list]
        assert "api_call_completed" in events
        assert "rate_limit_hit" not in events
        api_payload = next(c.args[1] for c in emitter.emit.call_args_list if c.args[0] == "api_call_completed")
        assert api_payload["subtype"] == "notion"
        assert api_payload["success"] is True
        assert api_payload["response_status_code"] == 200

    @patch("storage.notion_client.requests.request")
    def test_429_emits_rate_limit_hit_and_failed_api_call(self, mock_request):
        import requests

        error_response = mock_request.return_value
        error_response.status_code = 429
        error_response.text = "rate limited"
        error_response.headers = {"Retry-After": "30"}
        error_response.raise_for_status.side_effect = requests.HTTPError(response=error_response)

        emitter = MagicMock()
        client = self._make_client(emitter=emitter)
        with pytest.raises(requests.HTTPError):
            client.create_page("T", "技術", [], None, "U1")

        emitted = {c.args[0]: c.args[1] for c in emitter.emit.call_args_list}
        assert emitted["rate_limit_hit"]["subtype"] == "notion_429"
        assert emitted["rate_limit_hit"]["retry_after_seconds"] == 30
        assert emitted["api_call_completed"]["success"] is False
        assert emitted["api_call_completed"]["response_status_code"] == 429

    @patch("storage.notion_client.requests.request")
    def test_cloudflare_block_emits_rate_limit_hit(self, mock_request):
        import requests

        error_response = mock_request.return_value
        error_response.status_code = 403
        error_response.text = "Attention Required! | Cloudflare"
        error_response.headers = {"CF-Ray": "abc123-NRT"}
        error_response.raise_for_status.side_effect = requests.HTTPError(response=error_response)

        emitter = MagicMock()
        client = self._make_client(emitter=emitter)
        from storage.notion_client import NotionCloudflareBlockError

        with pytest.raises(NotionCloudflareBlockError):
            client.create_page("T", "技術", [], None, "U1")

        emitted = {c.args[0]: c.args[1] for c in emitter.emit.call_args_list}
        assert emitted["rate_limit_hit"]["subtype"] == "cloudflare_block"
        assert "abc123-NRT" in emitted["rate_limit_hit"]["detail"]

    @patch("storage.notion_client.requests.request")
    def test_emit_skipped_when_emitter_not_set(self, mock_request):
        """_emitter 未代入 (None) でも create_page は普通に動く"""
        mock_response = mock_request.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"id": "page-id", "url": "https://notion.so/page"}

        client = self._make_client()  # emitter=None
        client.create_page("T", "技術", [], None, "U1")
        # 例外なく動けば OK


class TestCloudflareBlockDetection:
    """_request_with_retry の Cloudflare 403 検知テスト"""

    _CF_BLOCK_HTML = (
        '<!DOCTYPE html><html lang="en-US"><head>'
        "<title>Attention Required! | Cloudflare</title>"
        '<link rel="stylesheet" href="/cdn-cgi/styles/cf.errors.css" />'
        "</head><body>blocked</body></html>"
    )
    _CF_CSS_ONLY_HTML = '<html><head><link href="/cdn-cgi/styles/cf.errors.css" /></head></html>'

    def _make_client(self):
        from storage.notion_client import NotionClient

        return NotionClient("ntn_test_token", "db-id-123")

    def _make_error_response(self, status_code: int, text: str, headers: dict | None = None):
        import requests

        response = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        response.status_code = status_code
        response.text = text
        response.headers = headers or {}
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        return response

    @patch("storage.notion_client.requests.request")
    def test_cloudflare_block_raises_dedicated_exception(self, mock_request):
        from storage.notion_client import NotionCloudflareBlockError

        mock_request.return_value = self._make_error_response(
            403, self._CF_BLOCK_HTML, {"CF-Ray": "abc123-NRT", "Server": "cloudflare"}
        )

        client = self._make_client()
        with pytest.raises(NotionCloudflareBlockError):
            client.create_page("Test", "技術", [], None, "U1")

        assert mock_request.call_count == 1

    @patch("storage.notion_client.requests.request")
    def test_cloudflare_css_signature_also_detected(self, mock_request):
        from storage.notion_client import NotionCloudflareBlockError

        mock_request.return_value = self._make_error_response(
            403, self._CF_CSS_ONLY_HTML, {"CF-Ray": "xyz999"}
        )

        client = self._make_client()
        with pytest.raises(NotionCloudflareBlockError):
            client.create_page("Test", "技術", [], None, "U1")

    @patch("storage.notion_client.requests.request")
    def test_cloudflare_block_exposes_cf_ray(self, mock_request):
        from storage.notion_client import NotionCloudflareBlockError

        mock_request.return_value = self._make_error_response(
            403, self._CF_BLOCK_HTML, {"CF-Ray": "ray-value-42", "Server": "cloudflare"}
        )

        client = self._make_client()
        with pytest.raises(NotionCloudflareBlockError) as exc_info:
            client.create_page("Test", "技術", [], None, "U1")

        assert exc_info.value.cf_ray == "ray-value-42"

    @patch("storage.notion_client.requests.request")
    def test_notion_json_403_is_not_cloudflare(self, mock_request):
        """Notion 本体からの JSON 403（権限不足等）は従来通り HTTPError"""
        import requests

        from storage.notion_client import NotionCloudflareBlockError

        mock_request.return_value = self._make_error_response(
            403,
            '{"object":"error","status":403,"code":"restricted_resource","message":"Missing permissions"}',
            {"Content-Type": "application/json"},
        )

        client = self._make_client()
        with pytest.raises(requests.HTTPError) as exc_info:
            client.create_page("Test", "技術", [], None, "U1")

        assert not isinstance(exc_info.value, NotionCloudflareBlockError)

    @patch("storage.notion_client.requests.request")
    def test_401_is_not_cloudflare(self, mock_request):
        import requests

        from storage.notion_client import NotionCloudflareBlockError

        mock_request.return_value = self._make_error_response(401, "Unauthorized", {})

        client = self._make_client()
        with pytest.raises(requests.HTTPError) as exc_info:
            client.create_page("Test", "技術", [], None, "U1")

        assert not isinstance(exc_info.value, NotionCloudflareBlockError)

    @patch("storage.notion_client.time.sleep")
    @patch("storage.notion_client.requests.request")
    def test_500_retries_as_before(self, mock_request, mock_sleep):
        """500 は既存リトライ動作のまま（Cloudflare 判定の影響を受けない）"""
        import requests

        mock_request.return_value = self._make_error_response(500, "Server Error", {})

        client = self._make_client()
        with pytest.raises(requests.HTTPError):
            client.create_page("Test", "技術", [], None, "U1")

        assert mock_request.call_count == 3
        assert mock_sleep.call_count == 3

    @patch("storage.notion_client.requests.request")
    def test_cloudflare_block_logs_cf_headers(self, mock_request, caplog):
        import logging

        from storage.notion_client import NotionCloudflareBlockError

        mock_request.return_value = self._make_error_response(
            403,
            self._CF_BLOCK_HTML,
            {"CF-Ray": "log-ray-1", "cf-mitigated": "challenge", "Server": "cloudflare"},
        )

        client = self._make_client()
        with caplog.at_level(logging.WARNING, logger="catch-expander-agent"):
            with pytest.raises(NotionCloudflareBlockError):
                client.create_page("Test", "技術", [], None, "U1")

        records = [r for r in caplog.records if "blocked by Cloudflare" in r.getMessage()]
        assert len(records) == 1
        record = records[0]
        assert getattr(record, "cf_ray", None) == "log-ray-1"
        assert getattr(record, "cf_mitigated", None) == "challenge"
        assert getattr(record, "user_agent_sent", None) == "<default>"

    @patch("storage.notion_client.requests.request")
    def test_sensitive_headers_are_not_logged(self, mock_request, caplog):
        import logging

        from storage.notion_client import NotionCloudflareBlockError

        mock_request.return_value = self._make_error_response(
            403,
            self._CF_BLOCK_HTML,
            {
                "CF-Ray": "sec-ray",
                "Set-Cookie": "__cf_bm=SECRET-COOKIE; Path=/",
                "Authorization": "Bearer LEAKED",
                "Server": "cloudflare",
            },
        )

        client = self._make_client()
        with caplog.at_level(logging.WARNING, logger="catch-expander-agent"):
            with pytest.raises(NotionCloudflareBlockError):
                client.create_page("Test", "技術", [], None, "U1")

        records = [r for r in caplog.records if "blocked by Cloudflare" in r.getMessage()]
        assert len(records) == 1
        response_headers_log = getattr(records[0], "response_headers", "")
        assert "SECRET-COOKIE" not in response_headers_log
        assert "LEAKED" not in response_headers_log
        assert "__cf_bm" not in response_headers_log
