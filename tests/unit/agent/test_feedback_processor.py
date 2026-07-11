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

        # scope 未付与（移行前レコード）は [汎用] ラベルで提示される
        assert "0: [汎用] 箇条書きにする" in prompt
        assert "1: [汎用] コードを短くする" in prompt

    def test_prompt_shows_scope_labels_for_existing_prefs(self):
        processor = self._make_processor()
        existing = [
            {
                "text": "module分割する",
                "created_at": "2026-01-01T00:00:00Z",
                "scope": {"categories": [], "deliverables": ["code"]},
            },
        ]
        prompt = processor._build_extraction_prompt("topic", "cat", existing, "feedback")

        assert "0: [コード] module分割する" in prompt

    def test_prompt_contains_scope_instructions_and_enums(self):
        processor = self._make_processor()
        prompt = processor._build_extraction_prompt("t", "c", [], "f")

        assert '"scope"' in prompt
        assert "categories" in prompt
        assert "deliverables" in prompt
        # 両 enum の値が提示される
        for cat in ["技術", "時事", "ビジネス", "学術", "カルチャー"]:
            assert cat in prompt
        for kind in [
            "code",
            "research_report",
            "architecture_design",
            "comparison_table",
            "cost_estimate",
            "procedure_guide",
        ]:
            assert kind in prompt
        # 迷ったら狭くの指示
        assert "狭く" in prompt

    def test_prompt_shows_source_deliverables_as_scope_kinds(self):
        processor = self._make_processor()
        prompt = processor._build_extraction_prompt(
            "t", "技術", [], "f", ["iac_code", "program_code", "research_report"]
        )

        # workflow 語彙は scope 区分の日本語ラベルに逆マップ（iac/program → コード に収束）
        assert "生成した成果物: コード、調査レポート" in prompt

    def test_prompt_shows_unknown_when_no_source_deliverables(self):
        processor = self._make_processor()
        prompt = processor._build_extraction_prompt("t", "c", [], "f")

        assert "生成した成果物: 不明" in prompt

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

    def test_replace_overwrites_scope(self):
        """置換時は text だけでなく scope も新しい値で上書きされる（スコープ訂正の経路）"""
        processor = self._make_processor()
        existing = [
            {
                "text": "module分割する",
                "created_at": "2026-01-01T00:00:00Z",
                "scope": {"categories": ["技術"], "deliverables": ["code"]},
            }
        ]
        new_prefs = [
            {
                "text": "module分割する",
                "replaces_index": 0,
                "scope": {"categories": [], "deliverables": ["code"]},
            }
        ]

        result = processor._merge_preferences(existing, new_prefs)

        assert len(result) == 1
        assert result[0]["scope"] == {"categories": [], "deliverables": ["code"]}

    def test_new_item_without_scope_gets_general_scope(self):
        processor = self._make_processor()
        result = processor._merge_preferences([], [{"text": "好み", "replaces_index": None}])

        assert result[0]["scope"] == {"categories": [], "deliverables": []}

    def test_default_max_items_is_twenty(self):
        """20260706-preference-scope: 保存上限は 10 → 20（FIFO 維持）"""
        processor = self._make_processor()
        existing = [{"text": f"好み{i}", "created_at": "2026-01-01T00:00:00Z"} for i in range(20)]
        new_prefs = [{"text": "21件目", "replaces_index": None}]

        result = processor._merge_preferences(existing, new_prefs)

        assert len(result) == 20
        assert result[0]["text"] == "好み1"  # 最古が削除される
        assert result[-1]["text"] == "21件目"

    def test_non_string_text_is_skipped(self):
        processor = self._make_processor()
        result = processor._merge_preferences([], [{"text": None, "replaces_index": None}])

        assert result == []

    def test_non_int_replaces_index_appends(self):
        """Codex Pass 1 P2: LLM 出力由来の str / bool index は置換せず追加扱い"""
        processor = self._make_processor()
        existing = [
            {"text": "既存0", "created_at": "2026-01-01T00:00:00Z"},
            {"text": "既存1", "created_at": "2026-01-01T00:00:00Z"},
        ]
        for bad_index in ("0", "1", True, 1.0, [0]):
            result = processor._merge_preferences(existing, [{"text": "追加", "replaces_index": bad_index}])
            assert len(result) == 3, f"replaces_index={bad_index!r} で置換されてしまった"
            assert result[0]["text"] == "既存0"
            assert result[1]["text"] == "既存1"
            assert result[2]["text"] == "追加"


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
            {"preferences": [{"text": "Terraformはmodule分割する", "replaces_index": None}]}, ensure_ascii=False
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

        mock_call_claude.return_value = json.dumps({"preferences": []}, ensure_ascii=False)

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
        mock_call_claude.return_value = json.dumps({"preferences": []}, ensure_ascii=False)

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
            {"preferences": [{"text": "好み", "replaces_index": None}]}, ensure_ascii=False
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
        mock_call_claude.return_value = json.dumps({"preferences": []}, ensure_ascii=False)

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
                "preferences": [
                    {"text": "好み1", "replaces_index": None},
                    {"text": "好み2", "replaces_index": None},
                    {"text": "好み3", "replaces_index": None},
                    {"text": "好み4（切り捨て）", "replaces_index": None},
                ]
            },
            ensure_ascii=False,
        )

        mock_slack = MagicMock()
        processor = self._make_processor(mock_slack=mock_slack, mock_db=mock_db)
        processor.process("U1", "feedback", "exec-001", "C1", "ts1")

        saved_prefs = mock_db.put_user_profile.call_args[0][0]["learned_preferences"]
        assert len(saved_prefs) == 3

        # Slack通知に渡すのも3件以内
        notified_prefs = mock_slack.post_feedback_result.call_args[0][2]
        assert len(notified_prefs) == 3


class TestProcessScopeValidation:
    """process() での適用スコープ検証（design §3.4 非対称フォールバック）"""

    def _make_processor(self, mock_slack=None, mock_db=None):
        from feedback.feedback_processor import FeedbackProcessor

        slack = mock_slack or MagicMock()
        db = mock_db or MagicMock()
        return FeedbackProcessor(slack_client=slack, db_client=db)

    def _make_db(self):
        mock_db = MagicMock()
        mock_db.get_execution.return_value = {
            "topic": "AIパイプライン構築",
            "category": "技術",
            "deliverable_types": ["research_report", "iac_code"],
        }
        mock_db.get_user_profile.return_value = None
        return mock_db

    @staticmethod
    def _claude_response(preferences):
        return json.dumps({"preferences": preferences}, ensure_ascii=False)

    @patch("feedback.feedback_processor.call_claude")
    def test_valid_scope_is_saved(self, mock_call_claude):
        mock_db = self._make_db()
        mock_call_claude.return_value = self._claude_response(
            [
                {
                    "text": "コードはmodule分割する",
                    "scope": {"categories": [], "deliverables": ["code"]},
                    "replaces_index": None,
                }
            ]
        )

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "コードが良かった", "exec-001", "C1", "ts1")

        saved = mock_db.put_user_profile.call_args[0][0]["learned_preferences"]
        assert saved[0]["scope"] == {"categories": [], "deliverables": ["code"]}

    @patch("feedback.feedback_processor.call_claude")
    def test_intentional_empty_scope_saved_as_general(self, mock_call_claude):
        mock_db = self._make_db()
        mock_call_claude.return_value = self._claude_response(
            [
                {
                    "text": "結論を先に簡潔に書く",
                    "scope": {"categories": [], "deliverables": []},
                    "replaces_index": None,
                }
            ]
        )

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "簡潔で良かった", "exec-001", "C1", "ts1")

        saved = mock_db.put_user_profile.call_args[0][0]["learned_preferences"]
        assert saved[0]["scope"] == {"categories": [], "deliverables": []}

    @patch("feedback.feedback_processor.call_claude")
    def test_missing_scope_falls_back_to_source_execution(self, mock_call_claude):
        """LLM が scope を出さなかった場合は元実行のカテゴリ・成果物区分に縮退（迷ったら狭く）"""
        mock_db = self._make_db()
        mock_call_claude.return_value = self._claude_response(
            [{"text": "好み", "replaces_index": None}]
        )

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "feedback", "exec-001", "C1", "ts1")

        saved = mock_db.put_user_profile.call_args[0][0]["learned_preferences"]
        assert saved[0]["scope"]["categories"] == ["技術"]
        assert saved[0]["scope"]["deliverables"] == ["code", "research_report"]

    @patch("feedback.feedback_processor.call_claude")
    def test_invalid_enum_values_fall_back_to_source(self, mock_call_claude):
        mock_db = self._make_db()
        mock_call_claude.return_value = self._claude_response(
            [
                {
                    "text": "好み",
                    "scope": {"categories": ["でたらめ"], "deliverables": ["iac_code"]},
                    "replaces_index": None,
                }
            ]
        )

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "feedback", "exec-001", "C1", "ts1")

        saved = mock_db.put_user_profile.call_args[0][0]["learned_preferences"]
        assert saved[0]["scope"]["categories"] == ["技術"]
        assert saved[0]["scope"]["deliverables"] == ["code", "research_report"]

    @patch("feedback.feedback_processor.call_claude")
    def test_unparseable_response_treated_as_empty(self, mock_call_claude):
        """Codex Pass 1 P1 対応（Agent SDK 厳密契約版）: JSON array / scalar / 非 JSON 応答は
        ClaudeResponseParseError となり「好みなし」として graceful に処理される"""
        for garbage in ("[]", '"text"', "42", "not a json", '[{"text": "x"}]'):
            mock_db = self._make_db()
            mock_call_claude.return_value = garbage

            mock_slack = MagicMock()
            processor = self._make_processor(mock_slack=mock_slack, mock_db=mock_db)
            processor.process("U1", "feedback", "exec-001", "C1", "ts1")

            mock_slack.post_feedback_unextracted.assert_called_once()
            mock_slack.post_error.assert_not_called()

    @patch("feedback.feedback_processor.call_claude")
    def test_malformed_existing_prefs_are_normalized(self, mock_call_claude):
        """Codex Pass 1 P2: 既存 learned_preferences の string 要素 / malformed dict で落ちない"""
        mock_db = self._make_db()
        mock_db.get_user_profile.return_value = {
            "user_id": "U1",
            "learned_preferences": [
                "legacy string",
                {"text": None},
                {"no_text": True},
                {"text": "正常な既存好み", "created_at": "2026-01-01T00:00:00Z"},
            ],
        }
        mock_call_claude.return_value = self._claude_response(
            [{"text": "新しい好み", "scope": {"categories": [], "deliverables": []}, "replaces_index": None}]
        )

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "feedback", "exec-001", "C1", "ts1")

        prompt = mock_call_claude.call_args[0][0]
        assert "正常な既存好み" in prompt
        assert "legacy string" not in prompt  # 正規化で除外

        saved = mock_db.put_user_profile.call_args[0][0]["learned_preferences"]
        assert [p["text"] for p in saved] == ["正常な既存好み", "新しい好み"]

    @patch("feedback.feedback_processor.call_claude")
    def test_non_dict_preference_entries_are_filtered(self, mock_call_claude):
        mock_db = self._make_db()
        mock_call_claude.return_value = self._claude_response(
            [
                "文字列ゴミ",
                {"text": "有効な好み", "scope": {"categories": [], "deliverables": []}, "replaces_index": None},
            ]
        )

        mock_slack = MagicMock()
        processor = self._make_processor(mock_slack=mock_slack, mock_db=mock_db)
        processor.process("U1", "feedback", "exec-001", "C1", "ts1")

        saved = mock_db.put_user_profile.call_args[0][0]["learned_preferences"]
        assert len(saved) == 1
        assert saved[0]["text"] == "有効な好み"


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
            {"preferences": [{"text": "新しい好み", "replaces_index": None}]}, ensure_ascii=False
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
        # 20260706-preference-scope: scope 欠損 → 元実行値に縮退するのでスコープ付き扱い
        assert payload["new_general_count"] == 0
        assert payload["new_scoped_count"] == 1

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
        mock_call_claude.return_value = json.dumps({"preferences": []}, ensure_ascii=False)

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
        mock_call_claude.return_value = json.dumps({"preferences": []}, ensure_ascii=False)

        processor = self._make_processor(mock_db=mock_db)
        processor.process("U1", "feedback", "exec-004", "C1", "ts1")
        # 例外なく完走すれば OK
