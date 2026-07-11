import logging
from datetime import UTC, datetime

from feedback.scope import (
    SCOPE_DELIVERABLE_LABELS_JA,
    WORKFLOW_TYPE_TO_SCOPE,
    format_scope_label,
    is_general,
    validate_scope,
)
from notify.slack_client import SlackClient
from orchestrator import _parse_claude_response, call_claude
from state.dynamodb_client import DynamoDbClient

# T1-2b (Tier 2.4): F8 フィードバック受信 → feedback_received イベントを emit。
# Lambda zip / ECS image で `src/observability/` 未配置時は graceful skip。
try:
    from src.observability import EventEmitter as _EventEmitter
except ImportError:  # pragma: no cover
    _EventEmitter = None

logger = logging.getLogger("catch-expander-agent")

MAX_PREFERENCES_PER_FEEDBACK = 3
# 20260706-preference-scope: スコープ導入でプロンプト注入量はフィルタで制御される
# ようになったため、保存上限を 10 → 20 に引き上げ（FIFO 削除は維持）
MAX_TOTAL_PREFERENCES = 20
_FEEDBACK_REPLY_SUMMARY_MAX_CHARS = 200


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
                source_deliverable_types = execution.get("deliverable_types", []) or []
            except (KeyError, Exception):
                logger.warning("Execution not found, proceeding without context", extra={"execution_id": execution_id})
                topic = "不明"
                category = "不明"
                source_deliverable_types = []

            # 2. ユーザープロファイル取得
            # Codex Pass 1 P2 対応: DynamoDB 側に string 要素 / malformed dict が混じっても
            # プロンプト構築・merge が落ちないよう、text を持つ dict だけに正規化する
            # （LLM に見せる一覧と merge の replaces_index 対象を同一リストに保つ）
            profile = self.db.get_user_profile(user_id) or {}
            raw_existing = profile.get("learned_preferences", []) or []
            existing_prefs: list[dict] = [
                p
                for p in raw_existing
                if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip()
            ]

            # T1-2b (Codex 2 回目 P2 対応): emitter を Claude / Slack 呼び出しの **前** に作成し、
            # それぞれの API call が api_call_completed として観測されるようにする。
            emitter = _EventEmitter(execution_id) if _EventEmitter is not None else None
            if emitter is not None:
                self.slack._emitter = emitter

            # 3. Claude による好み抽出
            prompt = self._build_extraction_prompt(
                topic, category, existing_prefs, feedback_text, source_deliverable_types
            )
            raw = call_claude(prompt, allowed_tools=None, emitter=emitter)
            parsed = _parse_claude_response(raw)

            if parsed.get("parse_error"):
                new_preferences: list[dict] = []
            else:
                raw_prefs = parsed.get("preferences", [])
                candidates = raw_prefs if isinstance(raw_prefs, list) else []
                new_preferences = [p for p in candidates if isinstance(p, dict)][:MAX_PREFERENCES_PER_FEEDBACK]

            # 3b. 適用スコープの検証（enum 外は破棄、検証失敗次元は元実行の値に縮退。
            # 意図的な空リストは汎用として維持 — design §3.4 非対称フォールバック）
            for pref in new_preferences:
                pref["scope"] = validate_scope(pref.get("scope"), category, source_deliverable_types)

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

            # 6. T1-2b: feedback_received イベント emit (Tier 2.4)。
            # F8 はメンション必須経路のみ実装済み (project_slack_feedback_requires_mention)。
            # emoji_reaction subtype は将来拡張のため、現状は mention_reply 固定。
            # PII 配慮: feedback_text は raw 保存せず先頭 200 文字 + 「…」のサマリのみ。
            #
            # Codex 3 回目 P2 対応: Slack 通知前に emit する。Slack post が retry exhaust
            # で失敗した場合、後続の self.slack.post_feedback_result が例外を投げて
            # outer except に飛んでも、フィードバックを受信・処理した事実は events に
            # 残る (DB 更新は成功している)。
            if emitter is not None:
                reply_summary = feedback_text[:_FEEDBACK_REPLY_SUMMARY_MAX_CHARS]
                if len(feedback_text) > _FEEDBACK_REPLY_SUMMARY_MAX_CHARS:
                    reply_summary += "…"
                emitter.emit(
                    "feedback_received",
                    {
                        "subtype": "mention_reply",
                        "execution_id": execution_id,
                        "reply_text_summary": reply_summary,
                        "learned_preferences_updated": bool(new_preferences),
                        "new_preferences_count": len(new_preferences),
                        "total_preferences_count": len(merged),
                        # 20260706-preference-scope: 今回抽出分の汎用 / スコープ付き内訳
                        "new_general_count": sum(1 for p in new_preferences if is_general(p)),
                        "new_scoped_count": sum(1 for p in new_preferences if not is_general(p)),
                    },
                )

            # 7. Slack 応答 (emitter は step 2 直後で作成済み、self.slack._emitter も伝搬済み)
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
        source_deliverable_types: list | None = None,
    ) -> str:
        """Claude への好み抽出プロンプトを構築する"""
        if existing_prefs:
            existing_text = "\n".join(
                f"{i}: [{format_scope_label(p)}] {p['text']}" for i, p in enumerate(existing_prefs)
            )
        else:
            existing_text = "（まだ登録なし）"

        # 元実行の deliverable_types (workflow 語彙) を成果物区分の日本語ラベルに逆マップして提示
        source_kinds = sorted(
            {
                WORKFLOW_TYPE_TO_SCOPE[t]
                for t in (source_deliverable_types or [])
                if t in WORKFLOW_TYPE_TO_SCOPE
            }
        )
        source_kinds_text = (
            "、".join(SCOPE_DELIVERABLE_LABELS_JA[k] for k in source_kinds) if source_kinds else "不明"
        )

        return (
            "# フィードバック解析タスク\n\n"
            "成果物に対するユーザーのフィードバックから、次回の生成に反映すべき具体的な「好み」を抽出してください。\n\n"
            "## 元のトピック情報\n"
            f"- トピック: {topic}\n"
            f"- カテゴリ: {category}\n"
            f"- 生成した成果物: {source_kinds_text}\n\n"
            "## 現在登録されている好み（更新の参考に。[ ] 内は適用スコープ）\n"
            f"{existing_text}\n\n"
            "## フィードバックテキスト\n"
            f"{feedback_text}\n\n"
            "## 出力ルール\n"
            "1. 好みは「成果物の生成プロンプトにそのまま使える命令形の文章」で表現する\n"
            '   良い例: "コードはmodule分割してディレクトリ構造で管理する"\n'
            '   悪い例: "コードをもっとモジュール化してほしいと言っていた"\n'
            "2. 各好みに適用スコープ scope を付ける。\n"
            "   この好みが**将来どの範囲のトピック・成果物に適用されるべきか**を選ぶ\n"
            "   - categories: 適用するトピックカテゴリのリスト。次の5値のみ使用可:\n"
            "     技術（クラウド・プログラミング・インフラ・AI/ML等）/ 時事（国際情勢・政治・社会問題等）/\n"
            "     ビジネス（市場動向・企業戦略・経済等）/ 学術（研究・論文・理論等）/\n"
            "     カルチャー（技術文化・トレンド等）\n"
            "   - deliverables: 適用する成果物区分のリスト。次の6値のみ使用可:\n"
            "     code（IaC・プログラム等のコード全般）/ research_report（調査レポート）/\n"
            "     architecture_design（アーキテクチャ設計書）/ comparison_table（比較表）/\n"
            "     cost_estimate（料金見積もり）/ procedure_guide（手順書）\n"
            "   - トピックや成果物の種類によらず常に当てはまる嗜好（文体・構成・簡潔さ等）は両方を空配列にする\n"
            "   - 適用範囲に迷う場合は、元のトピック情報の範囲に狭くスコープする\n"
            "3. 既存の好みと意味が重複・矛盾する場合は replaces_index でそのインデックスを指定する\n"
            "   （インデックスは 0 始まり、既存リストの順序に対応）\n"
            "   既存の好みの適用スコープだけを直すフィードバック（例:「それは全トピックに適用して」）も、\n"
            "   同じ text で scope を変えた好み + replaces_index で表現する\n"
            "4. 具体的な好みが読み取れない場合は preferences を空配列にする\n"
            "5. 抽出できる好みは最大3件\n\n"
            "**重要**: 前置き文・説明文は不要です。以下のJSON形式のみを```jsonブロックで出力してください。\n\n"
            "```json\n"
            "{\n"
            '  "preferences": [\n'
            "    {\n"
            '      "text": "命令形の好み文章",\n'
            '      "scope": {"categories": [], "deliverables": []},\n'
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
            raw_text = pref.get("text", "")
            text = raw_text.strip() if isinstance(raw_text, str) else ""
            if not text:
                continue
            replaces_index = pref.get("replaces_index")
            # 置換時は text / scope / created_at をすべて新しい値で上書きする
            new_item: dict = {
                "text": text,
                "created_at": now,
                "scope": pref.get("scope") or {"categories": [], "deliverables": []},
            }

            # Codex Pass 1 P2 対応: LLM 出力由来のため int 以外 (str "0" / bool) は追加扱い
            is_valid_index = isinstance(replaces_index, int) and not isinstance(replaces_index, bool)
            if is_valid_index and 0 <= replaces_index < len(result):
                result[replaces_index] = new_item
            else:
                result.append(new_item)

        # 上限を超えた場合は古い（先頭の）ものを削除
        if len(result) > max_items:
            result = result[len(result) - max_items :]

        return result
