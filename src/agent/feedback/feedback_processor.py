import logging
from datetime import UTC, datetime

from notify.slack_client import SlackClient
from orchestrator import _parse_claude_response, call_claude
from state.dynamodb_client import DynamoDbClient

logger = logging.getLogger("catch-expander-agent")

MAX_PREFERENCES_PER_FEEDBACK = 3
MAX_TOTAL_PREFERENCES = 10


class FeedbackProcessor:
    """フィードバックを解析しユーザープロファイルを更新する"""

    def __init__(self, slack_client: SlackClient, db_client: DynamoDbClient) -> None:
        self.slack = slack_client
        self.db = db_client

    def process(
        self,
        user_id: str,
        feedback_text: str,
        execution_id: str,
        slack_channel: str,
        slack_thread_ts: str,
    ) -> None:
        """フィードバックを解析しプロファイルを更新する"""
        try:
            # 1. 実行レコード取得（コンテキスト用）
            try:
                execution = self.db.get_execution(execution_id)
                topic = execution.get("topic", "不明")
                category = execution.get("category", "不明")
            except (KeyError, Exception):
                logger.warning("Execution not found, proceeding without context", extra={"execution_id": execution_id})
                topic = "不明"
                category = "不明"

            # 2. ユーザープロファイル取得
            profile = self.db.get_user_profile(user_id) or {}
            existing_prefs: list[dict] = profile.get("learned_preferences", [])

            # 3. Claude による好み抽出
            prompt = self._build_extraction_prompt(topic, category, existing_prefs, feedback_text)
            raw = call_claude(prompt, allowed_tools=None)
            parsed = _parse_claude_response(raw)

            if parsed.get("parse_error"):
                new_preferences: list[dict] = []
            else:
                new_preferences = parsed.get("preferences", [])[:MAX_PREFERENCES_PER_FEEDBACK]

            # 4. マージ
            merged = self._merge_preferences(existing_prefs, new_preferences)

            # 5. プロファイル更新（learned_preferences と updated_at のみ）
            profile["user_id"] = user_id
            profile["learned_preferences"] = merged
            profile["updated_at"] = datetime.now(tz=UTC).isoformat()
            self.db.put_user_profile(profile)

            logger.info(
                "Preferences updated",
                extra={
                    "user_id": user_id,
                    "new_count": len(new_preferences),
                    "total_count": len(merged),
                },
            )

            # 6. Slack 応答
            if new_preferences:
                self.slack.post_feedback_result(
                    slack_channel,
                    slack_thread_ts,
                    new_preferences,
                    len(merged),
                )
            else:
                self.slack.post_feedback_unextracted(slack_channel, slack_thread_ts)

        except Exception:
            logger.exception("Feedback processing failed", extra={"user_id": user_id, "execution_id": execution_id})
            self.slack.post_error(
                slack_channel,
                slack_thread_ts,
                "フィードバックの反映中にエラーが発生しました。ご不便をおかけします。",
            )
            raise

    def _build_extraction_prompt(
        self,
        topic: str,
        category: str,
        existing_prefs: list[dict],
        feedback_text: str,
    ) -> str:
        """Claude への好み抽出プロンプトを構築する"""
        if existing_prefs:
            existing_text = "\n".join(f"{i}: {p['text']}" for i, p in enumerate(existing_prefs))
        else:
            existing_text = "（まだ登録なし）"

        return (
            "# フィードバック解析タスク\n\n"
            "成果物に対するユーザーのフィードバックから、次回の生成に反映すべき具体的な「好み」を抽出してください。\n\n"
            "## 元のトピック情報\n"
            f"- トピック: {topic}\n"
            f"- カテゴリ: {category}\n\n"
            "## 現在登録されている好み（更新の参考に）\n"
            f"{existing_text}\n\n"
            "## フィードバックテキスト\n"
            f"{feedback_text}\n\n"
            "## 出力ルール\n"
            "1. 好みは「成果物の生成プロンプトにそのまま使える命令形の文章」で表現する\n"
            '   良い例: "Terraformコードはmodule分割してディレクトリ構造で管理する"\n'
            '   悪い例: "コードをもっとモジュール化してほしいと言っていた"\n'
            "2. 既存の好みと意味が重複・矛盾する場合は replaces_index でそのインデックスを指定する\n"
            "   （インデックスは 0 始まり、既存リストの順序に対応）\n"
            "3. 具体的な好みが読み取れない場合は preferences を空配列にする\n"
            "4. 抽出できる好みは最大3件\n\n"
            "**重要**: 前置き文・説明文は不要です。以下のJSON形式のみを```jsonブロックで出力してください。\n\n"
            "```json\n"
            "{\n"
            '  "preferences": [\n'
            "    {\n"
            '      "text": "命令形の好み文章",\n'
            '      "replaces_index": null\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n"
        )

    def _merge_preferences(
        self,
        existing: list[dict],
        new_preferences: list[dict],
        max_items: int = MAX_TOTAL_PREFERENCES,
    ) -> list[dict]:
        """好みリストをマージする

        マージルール:
        - replaces_index が None でなく 0 <= replaces_index < len(existing) の場合:
          既存リストの該当インデックスを新しい preference で置き換え（created_at も更新）
        - replaces_index が None または範囲外（負数・上限超え）の場合:
          末尾に追加
        - マージ後 max_items を超えた場合、先頭から超過分を削除（古い順）
        """
        now = datetime.now(tz=UTC).isoformat()
        result = list(existing)

        for pref in new_preferences:
            text = pref.get("text", "").strip()
            if not text:
                continue
            replaces_index = pref.get("replaces_index")
            new_item: dict = {"text": text, "created_at": now}

            if replaces_index is not None and 0 <= replaces_index < len(result):
                result[replaces_index] = new_item
            else:
                result.append(new_item)

        # 上限を超えた場合は古い（先頭の）ものを削除
        if len(result) > max_items:
            result = result[len(result) - max_items :]

        return result
