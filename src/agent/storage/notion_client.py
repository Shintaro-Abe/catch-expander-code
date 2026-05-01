import contextlib
import logging
import time

import requests

# T1-2b (Tier 1.2): API 呼び出し成否 + rate limit を events DDB に emit する。
# Lambda zip / ECS image で `src/observability/` が同梱されない環境では
# graceful skip 用の no-op スタブにフォールバックする (T1-2 / T1-3 と同じ規律)。
try:
    from src.observability import emit_api_call_completed, emit_rate_limit_hit
except ImportError:  # pragma: no cover

    def emit_api_call_completed(emitter, **kwargs) -> None:  # type: ignore[no-redef]
        return None

    def emit_rate_limit_hit(emitter, **kwargs) -> None:  # type: ignore[no-redef]
        return None


logger = logging.getLogger("catch-expander-agent")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_RETRIES = 3
_NOTION_RICH_TEXT_MAX_CHARS = 2000

_CLOUDFLARE_BLOCK_SIGNATURES = (
    "Attention Required! | Cloudflare",
    "cdn-cgi/styles/cf.errors.css",
)
_CF_HEADER_KEYS = ("CF-Ray", "cf-mitigated", "cf-cache-status", "Server")
_SENSITIVE_HEADER_KEYS = {"set-cookie", "authorization"}


class NotionCloudflareBlockError(Exception):
    """Notion 前段の Cloudflare がリクエストを 403 でブロックしたことを示す例外。

    IP reputation / User-Agent / JA3 fingerprint / Payload pattern 等の複合要因で
    Cloudflare が automation と判定したケースをまとめて扱う。Notion 本体の
    権限エラー（JSON で返る 403）はこの例外では扱わない。
    """

    def __init__(self, message: str, cf_ray: str | None = None) -> None:
        super().__init__(message)
        self.cf_ray = cf_ray


def _is_cloudflare_block(status_code: int, body: str) -> bool:
    if status_code != 403:
        return False
    return any(sig in body for sig in _CLOUDFLARE_BLOCK_SIGNATURES)


def _extract_cf_headers(headers) -> dict:
    """Cloudflare 識別用ヘッダを dict で返す。機微ヘッダは除外する。"""
    result: dict = {}
    for key in _CF_HEADER_KEYS:
        value = headers.get(key)
        if value is not None:
            result[key.lower().replace("-", "_")] = value
    safe_headers = {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADER_KEYS}
    result["response_headers"] = str(safe_headers)[:1000]
    return result


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
        # T1-2b: orchestrator が `self._emitter = EventEmitter(...)` を後から代入する。
        # 代入されない経路 (テスト / ECS observability 未配置) では None で graceful skip。
        self._emitter = None

    def _request_with_retry(self, method: str, url: str, json_data: dict | None = None) -> dict:
        """リトライ付きでNotion APIリクエストを送信する"""
        # T1-2b: API 呼び出しの成否・所要時間・status を api_call_completed として emit する。
        # 4xx 内で 429 / Cloudflare ブロックを検出した場合は rate_limit_hit も追加で emit。
        start_ns = time.monotonic_ns()
        success = False
        final_status: int | None = None
        endpoint_path = url.replace(NOTION_API_BASE, "") or "/"
        try:
            last_error: requests.HTTPError | None = None
            for attempt in range(MAX_RETRIES):
                try:
                    response = requests.request(method, url, headers=self.headers, json=json_data, timeout=30)
                    response.raise_for_status()
                    success = True
                    final_status = response.status_code
                    return response.json()
                except requests.HTTPError as e:
                    last_error = e
                    final_status = e.response.status_code
                    response_body = ""
                    with contextlib.suppress(Exception):
                        response_body = e.response.text[:1000]
                    # 4xxはクライアントエラーのためリトライしない
                    if e.response.status_code < 500:
                        if _is_cloudflare_block(e.response.status_code, response_body):
                            cf_headers = _extract_cf_headers(e.response.headers)
                            cf_ray = cf_headers.get("cf_ray")
                            logger.warning(
                                "Notion request blocked by Cloudflare | method=%s url=%s status=403",
                                method,
                                url,
                                extra={
                                    **cf_headers,
                                    "user_agent_sent": self.headers.get("User-Agent", "<default>"),
                                    "body_snippet": response_body[:200],
                                },
                            )
                            emit_rate_limit_hit(
                                self._emitter,
                                subtype="cloudflare_block",
                                endpoint_path=endpoint_path,
                                detail=f"CF-Ray={cf_ray}" if cf_ray else None,
                            )
                            raise NotionCloudflareBlockError(
                                f"Notion request blocked by Cloudflare (CF-Ray={cf_ray})",
                                cf_ray=cf_ray,
                            ) from e
                        if e.response.status_code == 429:
                            retry_after_raw = e.response.headers.get("Retry-After")
                            retry_after_seconds: int | None = None
                            if retry_after_raw is not None:
                                with contextlib.suppress(ValueError):
                                    retry_after_seconds = int(retry_after_raw)
                            emit_rate_limit_hit(
                                self._emitter,
                                subtype="notion_429",
                                endpoint_path=endpoint_path,
                                retry_after_seconds=retry_after_seconds,
                            )
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
        finally:
            duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            emit_api_call_completed(
                self._emitter,
                subtype="notion",
                success=success,
                duration_ms=duration_ms,
                response_status_code=final_status,
                endpoint_path=endpoint_path,
            )

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
