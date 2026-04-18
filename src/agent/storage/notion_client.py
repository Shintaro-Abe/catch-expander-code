import contextlib
import logging
import time

import requests

logger = logging.getLogger("catch-expander-agent")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_RETRIES = 3
_NOTION_RICH_TEXT_MAX_CHARS = 2000


def _split_long_rich_text(blocks: list[dict]) -> list[dict]:
    """rich_text[N].text.content が 2000 文字を超える要素を 2000 文字単位で分割した新しい list を返す。

    入力 list / dict は mutate せず、必要な箇所のみ shallow copy で再構築する。
    block["type"] を動的キーとして使い、対応キーが存在しない / 不正構造はスキップして堅牢化する。
    """
    result: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            result.append(block)
            continue
        block_type = block.get("type")
        type_payload = block.get(block_type) if isinstance(block_type, str) else None
        if not isinstance(type_payload, dict):
            result.append(block)
            continue
        rich_text = type_payload.get("rich_text")
        if not isinstance(rich_text, list):
            result.append(block)
            continue

        new_rich_text: list[dict] = []
        changed = False
        for element in rich_text:
            if not isinstance(element, dict):
                new_rich_text.append(element)
                continue
            text_field = element.get("text")
            content = text_field.get("content") if isinstance(text_field, dict) else None
            if not isinstance(content, str) or len(content) <= _NOTION_RICH_TEXT_MAX_CHARS:
                new_rich_text.append(element)
                continue

            changed = True
            for i in range(0, len(content), _NOTION_RICH_TEXT_MAX_CHARS):
                chunk = content[i : i + _NOTION_RICH_TEXT_MAX_CHARS]
                new_element = dict(element)
                new_text = dict(text_field)
                new_text["content"] = chunk
                new_element["text"] = new_text
                new_rich_text.append(new_element)

        if not changed:
            result.append(block)
            continue

        new_type_payload = dict(type_payload)
        new_type_payload["rich_text"] = new_rich_text
        new_block = dict(block)
        new_block[block_type] = new_type_payload
        result.append(new_block)
    return result


class NotionClient:
    """Notion API操作クライアント"""

    def __init__(self, token: str, database_id: str) -> None:
        self.database_id = database_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request_with_retry(self, method: str, url: str, json_data: dict | None = None) -> dict:
        """リトライ付きでNotion APIリクエストを送信する"""
        last_error: requests.HTTPError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.request(method, url, headers=self.headers, json=json_data, timeout=30)
                response.raise_for_status()
                return response.json()
            except requests.HTTPError as e:
                last_error = e
                response_body = ""
                with contextlib.suppress(Exception):
                    response_body = e.response.text[:1000]
                # 4xxはクライアントエラーのためリトライしない
                if e.response.status_code < 500:
                    logger.error(
                        "Notion API client error | method=%s url=%s status=%d response_body=%r",
                        method,
                        url,
                        e.response.status_code,
                        response_body,
                    )
                    raise
                wait = 2**attempt
                logger.warning(
                    "Notion API server error, retrying | attempt=%d wait_seconds=%d "
                    "method=%s url=%s status=%d response_body=%r",
                    attempt + 1,
                    wait,
                    method,
                    url,
                    e.response.status_code,
                    response_body,
                )
                time.sleep(wait)
        if last_error:
            raise last_error
        msg = "Unexpected: no error and no response"
        raise RuntimeError(msg)

    def create_page(
        self,
        title: str,
        category: str,
        content_blocks: list[dict],
        github_url: str | None,
        slack_user: str,
    ) -> tuple[str, str]:
        """成果物ページを作成し、(ページURL, ページID)を返す"""
        content_blocks = _split_long_rich_text(content_blocks)
        properties: dict = {
            "タイトル": {"title": [{"text": {"content": title}}]},
            "カテゴリ": {"select": {"name": category}},
            "日付": {"date": {"start": time.strftime("%Y-%m-%d")}},
            "ステータス": {"select": {"name": "作成中"}},
            "Slack User": {"rich_text": [{"text": {"content": slack_user}}]},
        }
        if github_url:
            properties["GitHub URL"] = {"url": github_url}

        # ページ作成（最初の100ブロックのみ含める。Notion APIの上限）
        max_children = 100
        payload: dict = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        if content_blocks:
            payload["children"] = content_blocks[:max_children]

        result = self._request_with_retry("POST", f"{NOTION_API_BASE}/pages", payload)
        page_id = result["id"]
        page_url = result["url"]
        logger.info("Notion page created", extra={"page_id": page_id, "url": page_url})

        # 残りのブロックを100件ずつ追記
        remaining = content_blocks[max_children:]
        for i in range(0, len(remaining), max_children):
            chunk = remaining[i : i + max_children]
            self.append_blocks(page_id, chunk)

        return page_url, page_id

    def update_page_status(self, page_id: str, status: str) -> None:
        """ページステータスを更新する"""
        payload = {"properties": {"ステータス": {"select": {"name": status}}}}
        self._request_with_retry("PATCH", f"{NOTION_API_BASE}/pages/{page_id}", payload)

    def append_blocks(self, page_id: str, blocks: list[dict]) -> None:
        """ページにブロックを追記する"""
        blocks = _split_long_rich_text(blocks)
        payload = {"children": blocks}
        self._request_with_retry("PATCH", f"{NOTION_API_BASE}/blocks/{page_id}/children", payload)
