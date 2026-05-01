import json
from unittest.mock import MagicMock, patch


class TestBuildExtractionPrompt:
    """_build_extraction_prompt のテスト"""

    def _make_processor(self):
        from feedback.feedback_processor import FeedbackProcessor

        return FeedbackProcessor(slack_client=MagicMock(), db_client=MagicMock())

    def test_prompt_contains_topic_and_feedback(self):
        processor = self._make_processor()
        prompt = processor._build_extraction_prompt("Terraform入門", "技術", [], "コードが良かった")

        assert "Terraform入門" in prompt
        assert "技術" in prompt
        assert "コードが良かった" in prompt

    def test_prompt_shows_no_prefs_when_empty(self):
        processor = self._make_processor()
        prompt = processor._build_extraction_prompt("topic", "cat", [], "feedback")

        assert "（まだ登録なし）" in prompt

    def test_prompt_shows_indexed_prefs_when_existing(self):
        processor = self._make_processor()
        existing = [
            {"text": "箇条書きにする", "created_at": "2026-01-01T00:00:00Z"},
            {"text": "コードを短くする", "created_at": "2026-01-02T00:00:00Z"},
        ]
        prompt = processor._build_extraction_prompt("topic", "cat", existing, "feedback")

        assert "0: 箇条書きにする" in prompt
        assert "1: コードを短くする" in prompt

    def test_prompt_instructs_json_only_output(self):
        processor = self._make_processor()
        prompt = processor._build_extraction_prompt("t", "c", [], "f")

        assert "replaces_index" in prompt
        assert "```json" in prompt


class TestMergePreferences:
    """_merge_preferences のテスト"""

    def _make_processor(self):
        from feedback.feedback_processor import FeedbackProcessor

        return FeedbackProcessor(slack_client=MagicMock(), db_client=MagicMock())

    def test_null_replaces_index_appends_to_end(self):
        processor = self._make_processor()
        existing = [{"text": "既存の好み", "created_at": "2026-01-01T00:00:00Z"}]
        new_prefs = [{"text": "新しい好み", "replaces_index": None}]

        result = processor._merge_preferences(existing, new_prefs)

        assert len(result) == 2
        assert result[0]["text"] == "既存の好み"
        assert result[1]["text"] == "新しい好み"

    def test_valid_replaces_index_replaces_item(self):
        processor = self._make_processor()
        existing = [
            {"text": "古い好み0", "created_at": "2026-01-01T00:00:00Z"},
            {"text": "古い好み1", "created_at": "2026-01-01T00:00:00Z"},
        ]
        new_prefs = [{"text": "新しい好み1", "replaces_index": 1}]

        result = processor._merge_preferences(existing, new_prefs)

        assert len(result) == 2
        assert result[0]["text"] == "古い好み0"
        assert result[1]["text"] == "新しい好み1"

    def test_negative_replaces_index_appends(self):
        processor = self._make_processor()
        existing = [{"text": "既存", "created_at": "2026-01-01T00:00:00Z"}]
        new_prefs = [{"text": "追加", "replaces_index": -1}]

        result = processor._merge_preferences(existing, new_prefs)

        assert len(result) == 2
        assert result[1]["text"] == "追加"

    def test_out_of_range_replaces_index_appends(self):
        processor = self._make_processor()
        existing = [{"text": "既存", "created_at": "2026-01-01T00:00:00Z"}]
        new_prefs = [{"text": "追加", "replaces_index": 99}]

        result = processor._merge_preferences(existing, new_prefs)

        assert len(result) == 2
        assert result[1]["text"] == "追加"

    def test_exceeding_max_removes_oldest(self):
        processor = self._make_processor()
        existing = [{"text": f"好み{i}", "created_at": "2026-01-01T00:00:00Z"} for i in range(9)]
        new_prefs = [
            {"text": "新9番目", "replaces_index": None},
            {"text": "新10番目（超過）", "replaces_index": None},
        ]

        result = processor._merge_preferences(existing, new_prefs, max_items=10)

        assert len(result) == 10
        assert result[0]["text"] == "好み1"  # 先頭（最古）が削除される
        assert result[-1]["text"] == "新10番目（超過）"

    def test_multiple_new_preferences_processed_in_order(self):
        processor = self._make_processor()
        existing = [{"text": "既存A", "created_at": "2026-01-01T00:00:00Z"}]
        new_prefs = [
            {"text": "追加1", "replaces_index": None},
            {"text": "追加2", "replaces_index": None},
        ]

        result = processor._merge_preferences(existing, new_prefs)

        assert len(result) == 3
        assert result[1]["text"] == "追加1"
        assert result[2]["text"] == "追加2"

    def test_replaced_item_has_new_created_at(self):
        processor = self._make_processor()
        old_ts = "2020-01-01T00:00:00Z"
        existing = [{"text": "古い", "created_at": old_ts}]
        new_prefs = [{"text": "新しい", "replaces_index": 0}]

        result = processor._merge_preferences(existing, new_prefs)

        assert result[0]["created_at"] != old_ts


class TestFeedbackProcessorProcess:
    """FeedbackProcessor.process メソッドの統合テスト"""

    def _make_processor(self, mock_slack=None, mock_db=None):
        from feedback.feedback_processor import FeedbackProcessor

        slack = mock_slack or MagicMock()
        db = mock_db or MagicMock()
        return FeedbackProcessor(slack_client=slack, db_client=db)

    @patch("feedback.feedback_processor.call_claude")
    def test_extracts_preferences_and_updates_profile(self, mock_call_claude):
        """好みが抽出できた場合: DynamoDB更新 + post_feedback_result"""
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "Terraform", "category": "技術"}
        mock_db.get_user_profile.return_value = {"user_id": "U1", "role": "エンジニア"}

        mock_call_claude.return_value = json.dumps(
            {"result": json.dumps({"preferences": [{"text": "Terraformはmodule分割する", "replaces_index": None}]})}
        )

        mock_slack = MagicMock()
        processor = self._make_processor(mock_slack=mock_slack, mock_db=mock_db)
        processor.process("U1", "コードが良かった", "exec-001", "C1", "ts1")

        # DynamoDB に保存された内容を確認
        mock_db.put_user_profile.assert_called_once()
        saved_profile = mock_db.put_user_profile.call_args[0][0]
        assert len(saved_profile["learned_preferences"]) == 1
        assert saved_profile["learned_preferences"][0]["text"] == "Terraformはmodule分割する"
        assert "role" in saved_profile  # 既存フィールドが保持されている

        # Slack 通知
        mock_slack.post_feedback_result.assert_called_once()
        mock_slack.post_feedback_unextracted.assert_not_called()

    @patch("feedback.feedback_processor.call_claude")
    def test_no_preferences_extracted_posts_unextracted_message(self, mock_call_claude):
        """好みが0件の場合: post_feedback_unextracted を呼ぶ"""
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "topic", "category": "cat"}
        mock_db.get_user_profile.return_value = None

        mock_call_claude.return_value = json.dumps({"result": json.dumps({"preferences": []})})

        mock_slack = MagicMock()
        processor = self._make_processor(mock_slack=mock_slack, mock_db=mock_db)
        processor.process("U1", "良かった", "exec-001", "C1", "ts1")

        mock_slack.post_feedback_unextracted.assert_called_once_with("C1", "ts1")
        mock_slack.post_feedback_result.assert_not_called()

    @patch("feedback.feedback_processor.call_claude")
    def test_execution_not_found_continues_with_unknown_context(self, mock_call_claude):
        """実行レコードが見つからない場合: topic="不明" でフィードバック解析を続行"""
        mock_db = MagicMock()
        mock_db.get_execution.side_effect = KeyError("Item")
        mock_db.get_user_profile.return_value = None
        mock_call_claude.return_value = json.dumps({"result": json.dumps({"preferences": []})})

        processor = self._make_processor(mock_db=mock_db)
        # 例外が外に出ないこと
        processor.process("U1", "良かった", "exec-missing", "C1", "ts1")

        prompt_arg = mock_call_claude.call_args[0][0]
        assert "不明" in prompt_arg

    @patch("feedback.feedback_processor.call_claude")
    def test_claude_parse_error_treats_as_empty_preferences(self, mock_call_claude):
        """Claude JSON パース失敗 → preferences = [] として扱う"""
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "t", "category": "c"}
        mock_db.get_user_profile.return_value = None
        mock_call_claude.return_value = "not a json"

        mock_slack = MagicMock()
        processor = self._make_processor(mock_slack=mock_slack, mock_db=mock_db)
        processor.process("U1", "feedback", "exec-001", "C1", "ts1")

        mock_slack.post_feedback_unextracted.assert_called_once()

    @patch("feedback.feedback_processor.call_claude")
    def test_dynamodb_error_posts_error_message_to_slack(self, mock_call_claude):
        """DynamoDB更新エラー → Slackにエラーメッセージ投稿"""
        import pytest

        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "t", "category": "c"}
        mock_db.get_user_profile.return_value = None
        mock_call_claude.return_value = json.dumps(
            {"result": json.dumps({"preferences": [{"text": "好み", "replaces_index": None}]})}
        )
        mock_db.put_user_profile.side_effect = RuntimeError("DynamoDB error")

        mock_slack = MagicMock()
        processor = self._make_processor(mock_slack=mock_slack, mock_db=mock_db)

        with pytest.raises(RuntimeError):
            processor.process("U1", "feedback", "exec-001", "C1", "ts1")

        mock_slack.post_error.assert_called_once()
        error_msg = mock_slack.post_error.call_args[0][2]
        assert "エラー" in error_msg

    @patch("feedback.feedback_processor.call_claude")
    def test_empty_feedback_text_is_passed_to_claude(self, mock_call_claude):
        """空文字・絵文字のみのフィードバックテキスト → Claude に渡してそのまま解析"""
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "t", "category": "c"}
        mock_db.get_user_profile.return_value = None
        mock_call_claude.return_value = json.dumps({"result": json.dumps({"preferences": []})})

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "👍", "exec-001", "C1", "ts1")

        mock_call_claude.assert_called_once()
        prompt_arg = mock_call_claude.call_args[0][0]
        assert "👍" in prompt_arg

    @patch("feedback.feedback_processor.call_claude")
    def test_new_preferences_capped_at_three(self, mock_call_claude):
        """1回のフィードバックから最大3件のみ抽出（それ以上は切り捨て）"""
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "t", "category": "c"}
        mock_db.get_user_profile.return_value = None
        mock_call_claude.return_value = json.dumps(
            {
                "result": json.dumps(
                    {
                        "preferences": [
                            {"text": "好み1", "replaces_index": None},
                            {"text": "好み2", "replaces_index": None},
                            {"text": "好み3", "replaces_index": None},
                            {"text": "好み4（切り捨て）", "replaces_index": None},
                        ]
                    }
                )
            }
        )

        mock_slack = MagicMock()
        processor = self._make_processor(mock_slack=mock_slack, mock_db=mock_db)
        processor.process("U1", "feedback", "exec-001", "C1", "ts1")

        saved_prefs = mock_db.put_user_profile.call_args[0][0]["learned_preferences"]
        assert len(saved_prefs) == 3

        # Slack通知に渡すのも3件以内
        notified_prefs = mock_slack.post_feedback_result.call_args[0][2]
        assert len(notified_prefs) == 3


class TestFeedbackReceivedEmit:
    """T1-2b (Tier 2.4): feedback_received イベントが Slack 応答後に emit される。"""

    def _make_processor(self, mock_slack=None, mock_db=None):
        from feedback.feedback_processor import FeedbackProcessor

        slack = mock_slack or MagicMock()
        db = mock_db or MagicMock()
        return FeedbackProcessor(slack_client=slack, db_client=db)

    @patch("feedback.feedback_processor._EventEmitter")
    @patch("feedback.feedback_processor.call_claude")
    def test_feedback_received_emitted_with_preferences_updated(self, mock_call_claude, mock_emitter_cls):
        """好みが抽出された場合、learned_preferences_updated=True で emit される"""
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "T", "category": "技術"}
        mock_db.get_user_profile.return_value = None
        mock_call_claude.return_value = json.dumps(
            {"result": json.dumps({"preferences": [{"text": "新しい好み", "replaces_index": None}]})}
        )

        emitter_instance = MagicMock()
        mock_emitter_cls.return_value = emitter_instance

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "コードが良かった", "exec-001", "C1", "ts1")

        mock_emitter_cls.assert_called_once_with("exec-001")
        emitter_instance.emit.assert_called_once()
        event_type, payload = emitter_instance.emit.call_args.args[:2]
        assert event_type == "feedback_received"
        assert payload["subtype"] == "mention_reply"
        assert payload["execution_id"] == "exec-001"
        assert payload["learned_preferences_updated"] is True
        assert payload["new_preferences_count"] == 1
        assert payload["total_preferences_count"] == 1
        assert payload["reply_text_summary"] == "コードが良かった"

    @patch("feedback.feedback_processor._EventEmitter")
    @patch("feedback.feedback_processor.call_claude")
    def test_feedback_received_marks_learned_preferences_false_when_no_extraction(
        self, mock_call_claude, mock_emitter_cls
    ):
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "T", "category": "技術"}
        mock_db.get_user_profile.return_value = None
        # parse_error → preferences 空配列 → learned_preferences_updated False
        mock_call_claude.return_value = "garbage"

        emitter_instance = MagicMock()
        mock_emitter_cls.return_value = emitter_instance

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "feedback", "exec-002", "C1", "ts1")

        emitter_instance.emit.assert_called_once()
        payload = emitter_instance.emit.call_args.args[1]
        assert payload["learned_preferences_updated"] is False
        assert payload["new_preferences_count"] == 0

    @patch("feedback.feedback_processor._EventEmitter")
    @patch("feedback.feedback_processor.call_claude")
    def test_feedback_received_truncates_long_feedback_text(self, mock_call_claude, mock_emitter_cls):
        """200 文字超の feedback_text は先頭 200 + 「…」に切り詰められる (PII 配慮)"""
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "T", "category": "技術"}
        mock_db.get_user_profile.return_value = None
        mock_call_claude.return_value = json.dumps({"result": json.dumps({"preferences": []})})

        emitter_instance = MagicMock()
        mock_emitter_cls.return_value = emitter_instance

        long_feedback = "あ" * 250
        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", long_feedback, "exec-003", "C1", "ts1")

        payload = emitter_instance.emit.call_args.args[1]
        assert len(payload["reply_text_summary"]) == 201  # 200 + 「…」
        assert payload["reply_text_summary"].endswith("…")

    @patch("feedback.feedback_processor._EventEmitter", None)
    @patch("feedback.feedback_processor.call_claude")
    def test_emit_skipped_when_event_emitter_unavailable(self, mock_call_claude):
        """_EventEmitter が None でも process() は完走する (graceful skip)"""
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {"topic": "T", "category": "技術"}
        mock_db.get_user_profile.return_value = None
        mock_call_claude.return_value = json.dumps({"result": json.dumps({"preferences": []})})

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "feedback", "exec-004", "C1", "ts1")
        # 例外なく完走すれば OK
