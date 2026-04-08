import logging
import time

import requests

logger = logging.getLogger("catch-expander-agent")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_RETRIES = 3


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
                try:
                    response_body = e.response.text[:1000]
                except Exception:
                    pass
                # 4xxはクライアントエラーのためリトライしない
                if e.response.status_code < 500:
                    logger.error(
                        "Notion API client error",
                        extra={"status": e.response.status_code, "response_body": response_body},
                    )
                    raise
                wait = 2**attempt
                logger.warning(
                    "Notion API server error, retrying",
                    extra={
                        "attempt": attempt + 1,
                        "wait_seconds": wait,
                        "status": e.response.status_code,
                        "response_body": response_body,
                    },
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
    ) -> str:
        """成果物ページを作成し、ページURLを返す"""
        properties: dict = {
            "タイトル": {"title": [{"text": {"content": title}}]},
            "カテゴリ": {"select": {"name": category}},
            "日付": {"date": {"start": time.strftime("%Y-%m-%d")}},
            "ステータス": {"select": {"name": "作成中"}},
            "Slack User": {"rich_text": [{"text": {"content": slack_user}}]},
        }
        if github_url:
            properties["GitHub URL"] = {"url": github_url}

        payload: dict = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        if content_blocks:
            payload["children"] = content_blocks

        result = self._request_with_retry("POST", f"{NOTION_API_BASE}/pages", payload)
        page_url = result["url"]
        logger.info("Notion page created", extra={"page_id": result["id"], "url": page_url})
        return page_url

    def update_page_status(self, page_id: str, status: str) -> None:
        """ページステータスを更新する"""
        payload = {"properties": {"ステータス": {"select": {"name": status}}}}
        self._request_with_retry("PATCH", f"{NOTION_API_BASE}/pages/{page_id}", payload)

    def append_blocks(self, page_id: str, blocks: list[dict]) -> None:
        """ページにブロックを追記する"""
        payload = {"children": blocks}
        self._request_with_retry("PATCH", f"{NOTION_API_BASE}/blocks/{page_id}/children", payload)
