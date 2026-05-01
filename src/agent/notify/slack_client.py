import contextlib
import logging
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# T1-2b (Tier 1.2): API 呼び出し成否 + rate limit emit。詳細は notion_client.py 参照。
try:
    from src.observability import emit_api_call_completed, emit_rate_limit_hit
except ImportError:  # pragma: no cover

    def emit_api_call_completed(emitter, **kwargs) -> None:  # type: ignore[no-redef]
        return None

    def emit_rate_limit_hit(emitter, **kwargs) -> None:  # type: ignore[no-redef]
        return None


logger = logging.getLogger("catch-expander-agent")

MAX_RETRIES = 3
_SLACK_CHAT_POSTMESSAGE_PATH = "/chat.postMessage"


class SlackClient:
    """Slack通知クライアント"""

    def __init__(self, bot_token: str) -> None:
        self.client = WebClient(token=bot_token)
        # T1-2b: orchestrator が後から self._emitter を代入。未代入時は graceful skip。
        self._emitter = None

    def _post_with_retry(self, channel: str, thread_ts: str, text: str) -> None:
        """リトライ付きでメッセージを投稿する"""
        # T1-2b: 終端で api_call_completed、ratelimited は rate_limit_hit を追加 emit。
        start_ns = time.monotonic_ns()
        success = False
        final_status: int | None = None
        try:
            last_error: SlackApiError | None = None
            for attempt in range(MAX_RETRIES):
                try:
                    self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
                    success = True
                    final_status = 200
                    return
                except SlackApiError as e:
                    last_error = e
                    # SlackApiError の response は SlackResponse (status_code 属性あり)
                    with contextlib.suppress(Exception):
                        final_status = e.response.status_code
                    error_code = ""
                    with contextlib.suppress(Exception):
                        error_code = str(e.response["error"])
                    if error_code == "ratelimited":
                        retry_after_raw = ""
                        with contextlib.suppress(Exception):
                            retry_after_raw = e.response.headers.get("Retry-After", "") or ""
                        retry_after_seconds: int | None = None
                        if retry_after_raw:
                            with contextlib.suppress(ValueError):
                                retry_after_seconds = int(retry_after_raw)
                        emit_rate_limit_hit(
                            self._emitter,
                            subtype="slack_rate_limit",
                            endpoint_path=_SLACK_CHAT_POSTMESSAGE_PATH,
                            retry_after_seconds=retry_after_seconds,
                            detail=f"error={error_code}",
                        )
                    wait = 2**attempt
                    logger.warning(
                        "Slack API error, retrying",
                        extra={"attempt": attempt + 1, "wait_seconds": wait, "error": error_code},
                    )
                    time.sleep(wait)
            if last_error:
                raise last_error
        finally:
            duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            emit_api_call_completed(
                self._emitter,
                subtype="slack",
                success=success,
                duration_ms=duration_ms,
                response_status_code=final_status,
                endpoint_path=_SLACK_CHAT_POSTMESSAGE_PATH,
            )

    def post_progress(self, channel: str, thread_ts: str, message: str) -> None:
        """進捗通知をスレッドに投稿"""
        self._post_with_retry(channel, thread_ts, message)

    def post_completion(
        self, channel: str, thread_ts: str, summary: str, notion_url: str, github_url: str | None
    ) -> None:
        """完了通知をスレッドに投稿"""
        lines = [
            "✅ 成果物が完成しました",
            "",
            "■ サマリー",
            summary,
            "",
            f"🔗 Notion → {notion_url}",
        ]
        if github_url:
            lines.append(f"🔗 GitHub → {github_url}")

        self._post_with_retry(channel, thread_ts, "\n".join(lines))

    def post_error(self, channel: str, thread_ts: str, error_message: str) -> None:
        """エラー通知をスレッドに投稿"""
        self._post_with_retry(channel, thread_ts, f"❌ {error_message}")

    def post_feedback_result(
        self,
        channel: str,
        thread_ts: str,
        preferences: list[dict],
        total_count: int,
    ) -> None:
        """フィードバック記録完了通知をスレッドに投稿"""
        lines = ["✅ フィードバックを記録しました。次回から以下の好みを反映します："]
        for p in preferences:
            lines.append(f"• {p['text']}")
        lines.append(f"（現在 {total_count} 件の好みが登録されています）")
        self._post_with_retry(channel, thread_ts, "\n".join(lines))

    def post_feedback_unextracted(self, channel: str, thread_ts: str) -> None:
        """具体的な好みが抽出できなかった場合の通知をスレッドに投稿"""
        text = (
            "📝 フィードバックを受け取りました。具体的な好みを抽出できませんでしたが、参考にします。\n"
            "もう少し詳しく教えていただけると、より精度の高い反映ができます。"
        )
        self._post_with_retry(channel, thread_ts, text)
