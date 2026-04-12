import logging
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger("catch-expander-agent")

MAX_RETRIES = 3


class SlackClient:
    """Slack通知クライアント"""

    def __init__(self, bot_token: str) -> None:
        self.client = WebClient(token=bot_token)

    def _post_with_retry(self, channel: str, thread_ts: str, text: str) -> None:
        """リトライ付きでメッセージを投稿する"""
        last_error: SlackApiError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
                return
            except SlackApiError as e:
                last_error = e
                wait = 2**attempt
                logger.warning(
                    "Slack API error, retrying",
                    extra={"attempt": attempt + 1, "wait_seconds": wait, "error": str(e.response["error"])},
                )
                time.sleep(wait)
        if last_error:
            raise last_error

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
