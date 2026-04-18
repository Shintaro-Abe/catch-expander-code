import json
from unittest.mock import MagicMock, patch

import pytest


class TestCallClaude:
    """call_claude関数のテスト"""

    @patch("orchestrator.subprocess.run")
    def test_call_claude_success(self, mock_run):
        from orchestrator import call_claude

        mock_run.return_value = MagicMock(stdout='{"result": "test output"}')
        result = call_claude("test prompt")
        assert result == '{"result": "test output"}'

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd

    @patch("orchestrator.subprocess.run")
    def test_call_claude_with_allowed_tools(self, mock_run):
        from orchestrator import call_claude

        mock_run.return_value = MagicMock(stdout="{}")
        call_claude("prompt", allowed_tools=["WebSearch", "WebFetch"])

        cmd = mock_run.call_args[0][0]
        assert "--allowedTools" in cmd
        assert "WebSearch,WebFetch" in cmd

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.subprocess.run")
    def test_call_claude_retries_on_failure(self, mock_run, mock_sleep):
        import subprocess

        from orchestrator import call_claude

        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "claude"),
            subprocess.CalledProcessError(1, "claude"),
            MagicMock(stdout='{"result": "ok"}'),
        ]
        result = call_claude("prompt")
        assert result == '{"result": "ok"}'
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.subprocess.run")
    def test_call_claude_raises_after_max_retries(self, mock_run, mock_sleep):
        import subprocess

        from orchestrator import call_claude

        mock_run.side_effect = subprocess.CalledProcessError(1, "claude")
        with pytest.raises(subprocess.CalledProcessError):
            call_claude("prompt")
        assert mock_run.call_count == 3


class TestNamespaceSourceIds:
    """_namespace_source_ids関数のテスト"""

    def test_applies_step_id_prefix_to_sources(self):
        from orchestrator import _namespace_source_ids

        result = {
            "step_id": "research-1",
            "summary": "...",
            "sources": [
                {"source_id": "src-001", "url": "https://a"},
                {"source_id": "src-002", "url": "https://b"},
            ],
        }
        _namespace_source_ids(result, "research-1")
        assert result["sources"][0]["source_id"] == "research-1:src-001"
        assert result["sources"][1]["source_id"] == "research-1:src-002"

    def test_corrects_wrong_step_id(self):
        """LLM が別の step_id を返してきた場合、指定値に強制書き換えする"""
        from orchestrator import _namespace_source_ids

        result = {
            "step_id": "research-1",  # LLM が誤って research-1 を返した
            "sources": [{"source_id": "src-001"}],
        }
        _namespace_source_ids(result, "research-2")
        assert result["step_id"] == "research-2"
        assert result["sources"][0]["source_id"] == "research-2:src-001"

    def test_handles_missing_sources(self):
        """sources キーがない、あるいは空リストでも例外を起こさない"""
        from orchestrator import _namespace_source_ids

        result1 = {"step_id": "research-1", "summary": "no sources key"}
        _namespace_source_ids(result1, "research-1")
        assert result1["step_id"] == "research-1"
        assert "sources" not in result1

        result2 = {"step_id": "research-1", "sources": []}
        _namespace_source_ids(result2, "research-1")
        assert result2["sources"] == []

    def test_idempotent_when_prefix_already_applied(self):
        """既に prefix が付いている source_id は再付与しない"""
        from orchestrator import _namespace_source_ids

        result = {
            "step_id": "research-1",
            "sources": [
                {"source_id": "research-1:src-001"},
                {"source_id": "src-002"},
            ],
        }
        _namespace_source_ids(result, "research-1")
        assert result["sources"][0]["source_id"] == "research-1:src-001"
        assert result["sources"][1]["source_id"] == "research-1:src-002"

    def test_skips_non_dict_result(self):
        from orchestrator import _namespace_source_ids

        # 例外を発生させずに no-op で戻ることを確認
        _namespace_source_ids(None, "research-1")  # type: ignore[arg-type]
        _namespace_source_ids("not a dict", "research-1")  # type: ignore[arg-type]

    def test_skips_source_without_source_id(self):
        """source_id が欠けている出典はスキップする（ログのみ）"""
        from orchestrator import _namespace_source_ids

        result = {
            "step_id": "research-1",
            "sources": [
                {"url": "https://a"},  # source_id なし
                {"source_id": "src-002", "url": "https://b"},
            ],
        }
        _namespace_source_ids(result, "research-1")
        assert "source_id" not in result["sources"][0]
        assert result["sources"][1]["source_id"] == "research-1:src-002"


class TestParseClaudeResponse:
    """_parse_claude_response関数のテスト"""

    def test_parse_result_field(self):
        from orchestrator import _parse_claude_response

        raw = json.dumps({"result": '{"category": "技術"}'})
        result = _parse_claude_response(raw)
        assert result["category"] == "技術"

    def test_parse_json_code_block(self):
        from orchestrator import _parse_claude_response

        raw = json.dumps({"result": '```json\n{"category": "時事"}\n```'})
        result = _parse_claude_response(raw)
        assert result["category"] == "時事"

    def test_parse_direct_json(self):
        from orchestrator import _parse_claude_response

        raw = '{"category": "ビジネス"}'
        result = _parse_claude_response(raw)
        assert result["category"] == "ビジネス"

    def test_parse_json_after_preamble(self):
        """前置き文の後にJSONが来るケースを正しく抽出できること（戦略4）"""
        from orchestrator import _parse_claude_response

        # Claudeが前置き文をつけた場合のシミュレーション
        prose_with_json = (
            '以下が調査結果です。\n\n{"step_id": "r-1", "summary": "概要", "sources": [], "extra": "data"}'
        )
        raw = json.dumps({"result": prose_with_json})
        result = _parse_claude_response(raw)
        assert result["step_id"] == "r-1"
        assert result["summary"] == "概要"

    def test_parse_json_with_trailing_content(self):
        """JSONの後に余分なテキストがある場合も正しく抽出できること（戦略4 raw_decode）"""
        from orchestrator import _parse_claude_response

        json_with_trailing = (
            '{"step_id": "r-2", "summary": "summary text", "sources": [], "extra": "val"}\n\n'
            "以上が結果です。ご確認ください。"
        )
        raw = json.dumps({"result": json_with_trailing})
        result = _parse_claude_response(raw)
        assert result["step_id"] == "r-2"

    def test_parse_ignores_small_json_fragments(self):
        """キー数2以下の断片的JSONオブジェクトを無視して大きなJSONを返すこと"""
        from orchestrator import _parse_claude_response

        # 最初の {} はキー1個の断片的JSON。その後に本体JSONが来る
        text_with_small_fragment = (
            'The format is {"type": "text"}. '
            "Here is the full output: "
            '{"step_id": "r-3", "summary": "full result", "sources": [], "extra": "more"}'
        )
        raw = json.dumps({"result": text_with_small_fragment})
        result = _parse_claude_response(raw)
        # キー数3以上の大きい方が返ること
        assert result.get("step_id") == "r-3"

    def test_parse_returns_parse_error_when_no_json(self):
        """JSONが全く見つからない場合はparse_error=Trueを返すこと"""
        from orchestrator import _parse_claude_response

        raw = json.dumps({"result": "JSONを含まない純粋なテキストです。"})
        result = _parse_claude_response(raw)
        assert result.get("parse_error") is True
        assert "raw_text" in result

    def test_parse_handles_invalid_outer_json(self):
        """外側のJSONが無効でもクラッシュしないこと"""
        from orchestrator import _parse_claude_response

        result = _parse_claude_response("not json at all")
        assert result.get("parse_error") is True


class TestRunResearchers:
    """リサーチャー並列実行のテスト"""

    @patch("orchestrator.call_claude")
    def test_parallel_execution_all_success(self, mock_claude):
        from orchestrator import Orchestrator

        mock_claude.return_value = json.dumps(
            {"result": json.dumps({"step_id": "r-1", "summary": "結果", "sources": []})}
        )

        slack = MagicMock()
        db = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")

        steps = [
            {"step_id": "r-1", "step_name": "概要", "description": "概要を調査", "search_hints": ["test"]},
            {"step_id": "r-2", "step_name": "論文", "description": "論文を調査", "search_hints": ["paper"]},
        ]
        results = orch._run_researchers("exec-1", steps, "prompt", "技術", "C1", "ts1")

        assert len(results) == 2
        assert mock_claude.call_count == 2

    @patch("orchestrator.call_claude")
    def test_partial_failure_continues(self, mock_claude):
        from orchestrator import Orchestrator

        def side_effect(prompt, allowed_tools=None):
            if "概要" in prompt:
                return json.dumps({"result": json.dumps({"step_id": "r-1", "summary": "ok", "sources": []})})
            raise RuntimeError("Search failed")

        mock_claude.side_effect = side_effect

        slack = MagicMock()
        db = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")

        steps = [
            {"step_id": "r-1", "step_name": "概要", "description": "概要を調査", "search_hints": []},
            {"step_id": "r-2", "step_name": "論文", "description": "論文を調査", "search_hints": []},
        ]
        results = orch._run_researchers("exec-1", steps, "prompt", "技術", "C1", "ts1")

        successful = [r for r in results if not r.get("error")]
        failed = [r for r in results if r.get("error")]
        assert len(successful) >= 1
        assert len(failed) >= 1

    @patch("orchestrator.call_claude")
    def test_all_failure_raises(self, mock_claude):
        from orchestrator import Orchestrator

        mock_claude.side_effect = RuntimeError("All failed")

        slack = MagicMock()
        db = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")

        steps = [{"step_id": "r-1", "step_name": "概要", "description": "test", "search_hints": []}]
        with pytest.raises(RuntimeError, match="All research steps failed"):
            orch._run_researchers("exec-1", steps, "prompt", "技術", "C1", "ts1")


class TestReviewLoop:
    """レビューループのテスト"""

    @patch("orchestrator.call_claude")
    def test_review_passes_first_time(self, mock_claude):
        from orchestrator import Orchestrator

        mock_claude.return_value = json.dumps(
            {
                "result": json.dumps(
                    {
                        "passed": True,
                        "issues": [],
                        "quality_metadata": {"sources_verified": 5, "sources_unverified": 0},
                    }
                )
            }
        )

        slack = MagicMock()
        db = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")

        result, final_deliverables = orch._run_review_loop(
            "prompt", {"content_blocks": []}, [], "技術", "gen_prompt", "C1", "ts1"
        )
        assert result["passed"] is True
        assert final_deliverables == {"content_blocks": []}
        assert mock_claude.call_count == 1

    @patch("orchestrator.call_claude")
    def test_review_loop_fix_then_pass(self, mock_claude):
        from orchestrator import Orchestrator

        responses = [
            # 1回目レビュー: 不合格
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "passed": False,
                            "issues": [
                                {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
                            ],
                            "quality_metadata": {},
                        }
                    )
                }
            ),
            # 修正後の成果物
            json.dumps({"result": json.dumps({"content_blocks": [], "summary": "fixed"})}),
            # 2回目レビュー: 合格
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "passed": True,
                            "issues": [],
                            "quality_metadata": {"sources_verified": 5, "sources_unverified": 0},
                        }
                    )
                }
            ),
        ]
        mock_claude.side_effect = responses

        slack = MagicMock()
        db = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")

        result, final_deliverables = orch._run_review_loop(
            "prompt", {"content_blocks": []}, [], "技術", "gen_prompt", "C1", "ts1"
        )
        assert result["passed"] is True
        assert final_deliverables.get("summary") == "fixed"
        assert mock_claude.call_count == 3

    @patch("orchestrator.call_claude")
    def test_run_review_loop_returns_fixed_deliverables_on_passed(self, mock_claude):
        """修正後に合格した場合、修正済み成果物を返す"""
        from orchestrator import Orchestrator

        responses = [
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "passed": False,
                            "issues": [
                                {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
                            ],
                            "quality_metadata": {},
                        }
                    )
                }
            ),
            json.dumps({"result": json.dumps({"content_blocks": [{"t": "fixed"}], "summary": "修正版"})}),
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "passed": True,
                            "issues": [],
                            "quality_metadata": {"sources_verified": 3, "sources_unverified": 0},
                        }
                    )
                }
            ),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}
        result, final_deliverables = orch._run_review_loop(
            "prompt", original, [], "技術", "gen_prompt", "C1", "ts1"
        )
        assert result["passed"] is True
        assert final_deliverables["content_blocks"] == [{"t": "fixed"}]
        assert final_deliverables["summary"] == "修正版"

    @patch("orchestrator.call_claude")
    def test_run_review_loop_returns_fixed_deliverables_on_max_loop(self, mock_claude):
        """ループ上限到達でも、最後に適用された修正版を返す"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        failing_review = {"passed": False, "issues": [issue], "quality_metadata": {}}

        responses = [
            json.dumps({"result": json.dumps(failing_review)}),
            json.dumps({"result": json.dumps({"content_blocks": [], "summary": "fix-1"})}),
            json.dumps({"result": json.dumps(failing_review)}),
            json.dumps({"result": json.dumps({"content_blocks": [], "summary": "fix-2"})}),
            json.dumps({"result": json.dumps(failing_review)}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original = {"content_blocks": [], "summary": "初版"}
        result, final_deliverables = orch._run_review_loop(
            "prompt", original, [], "技術", "gen_prompt", "C1", "ts1"
        )
        assert result["passed"] is False
        assert final_deliverables["summary"] == "fix-2"
        notes = result["quality_metadata"]["notes"]
        assert any("レビュー修正上限" in n for n in notes)

    @patch("orchestrator.call_claude")
    def test_run_review_loop_keeps_previous_on_parse_error(self, mock_claude):
        """fix 応答が parse_error の場合、前回成果物を保持する"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        failing_review = {"passed": False, "issues": [issue], "quality_metadata": {}}
        passing_review = {
            "passed": True,
            "issues": [],
            "quality_metadata": {"sources_verified": 1, "sources_unverified": 0},
        }

        responses = [
            json.dumps({"result": json.dumps(failing_review)}),
            # fix response は不正な JSON → parse_error になる
            json.dumps({"result": "this is not valid json at all"}),
            json.dumps({"result": json.dumps(passing_review)}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}
        result, final_deliverables = orch._run_review_loop(
            "prompt", original, [], "技術", "gen_prompt", "C1", "ts1"
        )
        assert result["passed"] is True
        # parse_error のため original を保持
        assert final_deliverables == original


class TestCodeGeneration:
    """コード成果物のタイプ別独立生成テスト（M3）"""

    def _make_analysis_and_workflow(self, deliverable_types: list[str]):
        analysis = {
            "category": "技術",
            "intent": "test",
            "perspectives": [],
            "deliverable_types": deliverable_types,
        }
        workflow = {
            "research_steps": [
                {"step_id": "r-1", "step_name": "概要", "description": "test", "search_hints": []},
            ],
            "generate_steps": [{"step_id": f"g-{i}", "step_name": t, "deliverable_type": t} for i, t in enumerate(deliverable_types)],
            "storage_targets": ["github"],
        }
        research = {"step_id": "r-1", "summary": "調査結果", "sources": []}
        return analysis, workflow, research

    def test_generator_no_longer_returns_code_files(self):
        """generator.md から code_files 関連の出力指示が削除されている"""
        from pathlib import Path

        # プロンプトファイル直読（_load_prompt を経由しないことで CI 環境差異を回避）
        prompt_path = Path(__file__).resolve().parents[3] / "src" / "agent" / "prompts" / "generator.md"
        content = prompt_path.read_text(encoding="utf-8")

        # 出力形式セクションに code_files が含まれていない（フィールド定義として）
        assert '"code_files":' not in content
        # README として code_files 禁止の方針が書かれている
        assert "code_files" in content  # 文脈上の言及は OK（禁止の明示など）
        # コード成果物の構造化ルールセクションが削除されている
        assert "コード成果物の構造化ルール" not in content

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_code_generation_per_type_merges_files(self, mock_call_claude, _mock_load):
        """iac_code と program_code が個別呼び出しでマージされる"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code", "program_code"])
        text_deliverables = {"content_blocks": [], "summary": "完成"}
        review = {"passed": True, "issues": [], "quality_metadata": {}}

        iac_result = {"files": {"main.tf": "# iac"}, "readme_content": "IaC README"}
        prog_result = {"files": {"app.py": "# program"}, "readme_content": "Program README"}

        mock_call_claude.side_effect = [
            json.dumps({"result": json.dumps(analysis)}),
            json.dumps({"result": json.dumps(workflow)}),
            json.dumps({"result": json.dumps(research)}),
            json.dumps({"result": json.dumps(text_deliverables)}),
            json.dumps({"result": json.dumps(iac_result)}),
            json.dumps({"result": json.dumps(prog_result)}),
            json.dumps({"result": json.dumps(review)}),
        ]

        db = MagicMock()
        db.get_user_profile.return_value = None
        db._table.return_value = MagicMock()
        slack = MagicMock()

        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")
        orch.notion = MagicMock()
        orch.notion.create_page.return_value = ("https://notion.so/page", "page-id")
        orch.github = MagicMock()
        orch.github.push_files.return_value = "https://github.com/owner/repo"

        orch.run("exec-1", "U1", "テスト", "C1", "ts1")

        orch.github.push_files.assert_called_once()
        files_arg = orch.github.push_files.call_args.args[1]
        assert "main.tf" in files_arg
        assert "app.py" in files_arg

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_code_generation_partial_failure_keeps_successful_types(self, mock_call_claude, _mock_load):
        """一方のタイプが parse_error でも、もう一方は push される"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code", "program_code"])
        text_deliverables = {"content_blocks": [], "summary": "完成"}
        review = {"passed": True, "issues": [], "quality_metadata": {}}

        iac_result = {"files": {"main.tf": "# iac"}, "readme_content": "IaC README"}
        # program_code は壊れた JSON
        prog_broken = "not a json"

        mock_call_claude.side_effect = [
            json.dumps({"result": json.dumps(analysis)}),
            json.dumps({"result": json.dumps(workflow)}),
            json.dumps({"result": json.dumps(research)}),
            json.dumps({"result": json.dumps(text_deliverables)}),
            json.dumps({"result": json.dumps(iac_result)}),
            json.dumps({"result": prog_broken}),
            json.dumps({"result": json.dumps(review)}),
        ]

        db = MagicMock()
        db.get_user_profile.return_value = None
        db._table.return_value = MagicMock()
        slack = MagicMock()

        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")
        orch.notion = MagicMock()
        orch.notion.create_page.return_value = ("https://notion.so/page", "page-id")
        orch.github = MagicMock()
        orch.github.push_files.return_value = "https://github.com/owner/repo"

        orch.run("exec-1", "U1", "テスト", "C1", "ts1")

        orch.github.push_files.assert_called_once()
        files_arg = orch.github.push_files.call_args.args[1]
        assert "main.tf" in files_arg
        assert "app.py" not in files_arg

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_generator_code_files_always_discarded(self, mock_call_claude, _mock_load):
        """ジェネレーターが誤って code_files を返しても、システム側で破棄される"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code"])
        # 旧仕様のような code_files 混入を模擬
        text_deliverables = {
            "content_blocks": [],
            "code_files": {"files": {"LEAK.tf": "# must not be used"}, "readme_content": ""},
            "summary": "完成",
        }
        review = {"passed": True, "issues": [], "quality_metadata": {}}
        iac_result = {"files": {"main.tf": "# iac"}, "readme_content": "IaC README"}

        mock_call_claude.side_effect = [
            json.dumps({"result": json.dumps(analysis)}),
            json.dumps({"result": json.dumps(workflow)}),
            json.dumps({"result": json.dumps(research)}),
            json.dumps({"result": json.dumps(text_deliverables)}),
            json.dumps({"result": json.dumps(iac_result)}),
            json.dumps({"result": json.dumps(review)}),
        ]

        db = MagicMock()
        db.get_user_profile.return_value = None
        db._table.return_value = MagicMock()
        slack = MagicMock()

        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")
        orch.notion = MagicMock()
        orch.notion.create_page.return_value = ("https://notion.so/page", "page-id")
        orch.github = MagicMock()
        orch.github.push_files.return_value = "https://github.com/owner/repo"

        orch.run("exec-1", "U1", "テスト", "C1", "ts1")

        orch.github.push_files.assert_called_once()
        files_arg = orch.github.push_files.call_args.args[1]
        assert "main.tf" in files_arg
        assert "LEAK.tf" not in files_arg


class TestLearnedPreferencesInProfileText:
    """learned_preferences が profile_text に反映されるテスト"""

    def _make_minimal_responses(self):
        """run() を最後まで通すための最小限の Claude 応答列"""
        analysis = {"category": "技術", "intent": "学習", "perspectives": [], "deliverable_types": ["research_report"]}
        workflow = {
            "research_steps": [{"step_id": "r-1", "step_name": "概要", "description": "概要調査", "search_hints": []}],
            "generate_steps": [{"step_id": "g-1", "step_name": "レポート", "deliverable_type": "research_report"}],
            "storage_targets": ["notion"],
        }
        research = {"step_id": "r-1", "summary": "概要", "sources": []}
        deliverables = {"content_blocks": [], "code_files": None, "summary": "完成"}
        review = {"passed": True, "issues": [], "quality_metadata": {}}
        return [
            json.dumps({"result": json.dumps(analysis)}),
            json.dumps({"result": json.dumps(workflow)}),
            json.dumps({"result": json.dumps(research)}),
            json.dumps({"result": json.dumps(deliverables)}),
            json.dumps({"result": json.dumps(review)}),
        ]

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_learned_preferences_included_in_prompts(self, mock_call_claude, _mock_load):
        """learned_preferences が1件以上ある場合、最初の call_claude 呼び出しに好みが含まれる"""
        from orchestrator import Orchestrator

        mock_call_claude.side_effect = self._make_minimal_responses()

        db = MagicMock()
        db.get_user_profile.return_value = {
            "user_id": "U1",
            "role": "エンジニア",
            "learned_preferences": [
                {"text": "Terraformはmodule分割する", "created_at": "2026-01-01T00:00:00Z"},
            ],
        }
        db._table.return_value = MagicMock()

        slack = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")
        orch.notion = MagicMock()
        orch.notion.create_page.return_value = ("https://notion.so/page", "page-id")
        orch.github = MagicMock()

        orch.run("exec-1", "U1", "Terraform入門", "C1", "ts1")

        # トピック解析プロンプト（最初の呼び出し）に好みセクションが含まれる
        first_prompt = mock_call_claude.call_args_list[0][0][0]
        assert "Terraformはmodule分割する" in first_prompt
        assert "蓄積された好み" in first_prompt

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_empty_learned_preferences_not_included_in_prompts(self, mock_call_claude, _mock_load):
        """learned_preferences が空の場合、profile_text に好みセクションが含まれない"""
        from orchestrator import Orchestrator

        mock_call_claude.side_effect = self._make_minimal_responses()

        db = MagicMock()
        db.get_user_profile.return_value = {
            "user_id": "U1",
            "role": "エンジニア",
            # learned_preferences なし（既存ユーザー）
        }
        db._table.return_value = MagicMock()

        slack = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")
        orch.notion = MagicMock()
        orch.notion.create_page.return_value = ("https://notion.so/page", "page-id")
        orch.github = MagicMock()

        orch.run("exec-1", "U1", "Terraform入門", "C1", "ts1")

        first_prompt = mock_call_claude.call_args_list[0][0][0]
        assert "蓄積された好み" not in first_prompt


class TestBuildQualityMetadataBlock:
    """_build_quality_metadata_block の表示ロジックテスト（S1, S2）"""

    def _block_text(self, blocks: list[dict]) -> str:
        """ブロック列から本文テキストを取り出す"""
        for block in blocks:
            if block.get("type") == "paragraph":
                return block["paragraph"]["rich_text"][0]["text"]["content"]
        return ""

    def _make_orch(self):
        from orchestrator import Orchestrator

        return Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

    # --- S1: sources_total 分岐 ---

    def test_displays_verified_over_total_when_total_present(self):
        """sources_total が指定されていれば verified/total 形式で表示する"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block(
            {
                "sources_verified": 12,
                "sources_total": 49,
                "sources_unverified": 0,
                "newest_source_date": "2026-04-02",
                "oldest_source_date": "2024-11-15",
                "checklist_passed": 5,
                "checklist_total": 5,
            }
        )
        text = self._block_text(blocks)
        assert "出典検証済み: 12/49 件" in text

    def test_falls_back_to_count_only_when_total_missing(self):
        """sources_total が無い場合は従来通り件数のみ表示（後方互換）"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block(
            {
                "sources_verified": 5,
                "sources_unverified": 0,
                "newest_source_date": "2026-04-01",
                "oldest_source_date": "2025-01-01",
                "checklist_passed": 4,
                "checklist_total": 4,
            }
        )
        text = self._block_text(blocks)
        assert "出典検証済み: 5件" in text
        assert "/" not in text.split("出典検証済み:")[1].split("\n")[0]

    def test_falls_back_to_count_only_when_total_smaller_than_verified(self):
        """異常値（total < verified）の場合は分母を信頼せず件数のみ表示"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block(
            {
                "sources_verified": 10,
                "sources_total": 3,  # 異常値
                "sources_unverified": 0,
                "newest_source_date": "2026-04-01",
                "oldest_source_date": "2025-01-01",
                "checklist_passed": 1,
                "checklist_total": 1,
            }
        )
        text = self._block_text(blocks)
        assert "出典検証済み: 10件" in text

    def test_unverified_details_appended_when_present(self):
        """sources_unverified > 0 の場合、未検証セクションが追加される"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block(
            {
                "sources_verified": 4,
                "sources_total": 10,
                "sources_unverified": 2,
                "unverified_details": ["セクション3の市場規模", "セクション5の予測"],
                "newest_source_date": "2026-04-01",
                "oldest_source_date": "2025-01-01",
                "checklist_passed": 5,
                "checklist_total": 5,
            }
        )
        text = self._block_text(blocks)
        assert "未検証の記述: 2件" in text
        assert "セクション3の市場規模" in text

    # --- S2: published_at フォールバック ---

    def test_freshness_displays_date_range_when_both_present(self):
        """正常な日付ペアはそのまま「最新 X / 最古 Y」で表示する"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block(
            {
                "sources_verified": 3,
                "sources_total": 10,
                "newest_source_date": "2026-04-02",
                "oldest_source_date": "2024-11-15",
            }
        )
        text = self._block_text(blocks)
        assert "情報の鮮度: 最新 2026-04-02 / 最古 2024-11-15" in text

    def test_freshness_falls_back_when_both_null(self):
        """newest/oldest が両方 null/欠損の場合、注意書きに切り替わる"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block(
            {
                "sources_verified": 3,
                "sources_total": 10,
                "newest_source_date": None,
                "oldest_source_date": None,
            }
        )
        text = self._block_text(blocks)
        assert "情報の鮮度: 取得日不明のソースが含まれます" in text
        assert "N/A" not in text

    def test_freshness_falls_back_when_keys_missing(self):
        """newest/oldest キー自体が無い場合も注意書きに切り替わる"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block({"sources_verified": 0})
        text = self._block_text(blocks)
        assert "情報の鮮度: 取得日不明のソースが含まれます" in text

    def test_freshness_treats_unknown_marker_as_missing(self):
        """"unknown" / "continuously-updated" は日付として扱わず注意書きに切り替わる"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block(
            {
                "sources_verified": 3,
                "sources_total": 10,
                "newest_source_date": "continuously-updated",
                "oldest_source_date": "unknown",
            }
        )
        text = self._block_text(blocks)
        assert "情報の鮮度: 取得日不明のソースが含まれます" in text

    def test_freshness_shows_partial_when_one_side_missing(self):
        """片側のみ日付が取れた場合は欠損側を「不明」と表示する"""
        orch = self._make_orch()
        blocks = orch._build_quality_metadata_block(
            {
                "sources_verified": 3,
                "sources_total": 10,
                "newest_source_date": "2026-04-02",
                "oldest_source_date": None,
            }
        )
        text = self._block_text(blocks)
        assert "情報の鮮度: 最新 2026-04-02 / 最古 不明" in text


class TestBuildCodeFailureDiagnostics:
    """_build_code_failure_diagnostics の観測項目テスト（Followup-A Phase A）"""

    def test_parse_error_flag_true_when_parsed_marks_parse_error(self):
        from orchestrator import _build_code_failure_diagnostics

        diag = _build_code_failure_diagnostics(
            "raw text", {"parse_error": True, "raw_text": "raw text"}
        )
        assert diag["parse_error"] is True
        assert diag["files_kind"] == "missing"
        assert diag["files_count"] == 0
        assert diag["top_level_keys"] == ["parse_error", "raw_text"]
        assert diag["response_chars"] == len("raw text")
        assert diag["response_preview"] == "raw text"

    def test_missing_files_key_marks_files_kind_missing(self):
        from orchestrator import _build_code_failure_diagnostics

        parsed = {"summary": "ok", "content_blocks": []}
        diag = _build_code_failure_diagnostics("{}", parsed)
        assert diag["parse_error"] is False
        assert diag["files_kind"] == "missing"
        assert diag["files_count"] == 0
        assert "summary" in diag["top_level_keys"]
        assert "content_blocks" in diag["top_level_keys"]

    def test_empty_files_dict_reports_zero_count(self):
        from orchestrator import _build_code_failure_diagnostics

        diag = _build_code_failure_diagnostics(
            '{"files": {}}', {"files": {}, "readme_content": ""}
        )
        assert diag["parse_error"] is False
        assert diag["files_kind"] == "dict"
        assert diag["files_count"] == 0

    def test_files_as_list_reports_list_kind(self):
        from orchestrator import _build_code_failure_diagnostics

        diag = _build_code_failure_diagnostics(
            '{"files": [1, 2, 3]}', {"files": [1, 2, 3]}
        )
        assert diag["files_kind"] == "list"
        assert diag["files_count"] == 3

    def test_response_preview_truncated_to_500_chars(self):
        from orchestrator import _build_code_failure_diagnostics

        long_raw = "x" * 1500
        diag = _build_code_failure_diagnostics(long_raw, {"files": None})
        assert diag["response_chars"] == 1500
        assert len(diag["response_preview"]) == 500
        assert diag["files_kind"] == "NoneType"
        assert diag["files_count"] == 0

    def test_non_dict_parsed_marks_parse_error_true(self):
        from orchestrator import _build_code_failure_diagnostics

        diag = _build_code_failure_diagnostics("[1,2,3]", [1, 2, 3])
        assert diag["parse_error"] is True
        assert diag["files_kind"] == "<not-dict>"
        assert diag["files_count"] == 0
        assert diag["top_level_keys"] == []

    def test_top_level_keys_capped_at_ten(self):
        from orchestrator import _build_code_failure_diagnostics

        parsed = {f"k{i}": i for i in range(15)}
        diag = _build_code_failure_diagnostics("{}", parsed)
        assert len(diag["top_level_keys"]) == 10


class TestNormalizeCodeFilesPayload:
    """_normalize_code_files_payload のスキーマ正規化テスト（Followup-A B-2）"""

    def test_passes_through_already_wrapped(self):
        from orchestrator import _normalize_code_files_payload

        original = {"files": {"main.tf": "..."}, "readme_content": "doc"}
        result = _normalize_code_files_payload(original)
        assert result is original

    def test_wraps_observed_iac_pattern(self):
        """exec-20260418073748-2c27bb2f の iac_code 観測例"""
        from orchestrator import _normalize_code_files_payload

        parsed = {
            "bin/app.ts": "// app entry",
            "lib/api-lambda-stack.ts": "// stack",
            "lambda/handler.ts": "// handler",
            "package.json": "{}",
            "cdk.json": "{}",
        }
        result = _normalize_code_files_payload(parsed)
        assert "files" in result
        assert len(result["files"]) == 5
        assert result["files"]["bin/app.ts"] == "// app entry"
        assert result["files"]["package.json"] == "{}"
        assert "readme_content" not in result

    def test_wraps_observed_program_pattern(self):
        """exec-20260418073748-2c27bb2f の program_code 観測例"""
        from orchestrator import _normalize_code_files_payload

        parsed = {
            "app.py": "import x",
            "stacks/api_gateway_lambda_stack.py": "class Stack: pass",
            "lambda/handler.py": "def handler(): pass",
            "lambda/authorizer.py": "def auth(): pass",
            "requirements.txt": "boto3",
        }
        result = _normalize_code_files_payload(parsed)
        assert len(result["files"]) == 5
        assert result["files"]["app.py"] == "import x"
        assert result["files"]["requirements.txt"] == "boto3"

    def test_preserves_readme_content_when_present(self):
        from orchestrator import _normalize_code_files_payload

        parsed = {
            "main.tf": "resource ...",
            "variables.tf": "variable ...",
            "readme_content": "## Setup\n...",
        }
        result = _normalize_code_files_payload(parsed)
        assert result["files"] == {"main.tf": "resource ...", "variables.tf": "variable ..."}
        assert result["readme_content"] == "## Setup\n..."

    def test_passes_through_parse_error(self):
        from orchestrator import _normalize_code_files_payload

        parsed = {"parse_error": True, "raw_text": "..."}
        result = _normalize_code_files_payload(parsed)
        assert result is parsed

    def test_passes_through_non_dict(self):
        from orchestrator import _normalize_code_files_payload

        for value in [None, "raw text", [1, 2, 3], 42]:
            assert _normalize_code_files_payload(value) is value

    def test_does_not_wrap_when_majority_non_file_keys(self):
        """ファイル風キーが少数派の場合は誤検知を避ける"""
        from orchestrator import _normalize_code_files_payload

        parsed = {
            "main.tf": "resource ...",
            "title": "x",
            "description": "y",
            "category": "z",
            "metadata": "w",
        }
        result = _normalize_code_files_payload(parsed)
        assert result is parsed

    def test_does_not_wrap_with_only_one_file_key(self):
        """ファイル風キーが1つのみだと誤検知の可能性が高いため正規化しない"""
        from orchestrator import _normalize_code_files_payload

        parsed = {"main.tf": "resource ..."}
        result = _normalize_code_files_payload(parsed)
        assert result is parsed

    def test_wraps_when_value_is_string_only(self):
        """値が string のものだけ files に含める（dict や list は除外）"""
        from orchestrator import _normalize_code_files_payload

        parsed = {
            "main.tf": "resource ...",
            "variables.tf": "variable ...",
            "config.json": {"nested": "object"},
        }
        result = _normalize_code_files_payload(parsed)
        # config.json は値が dict なので files から除外、結果として 2 ファイル のみ
        # ただし非ファイルキー扱いになるため比率 2/3 = 0.67 で閾値 0.8 未満 → 正規化しない
        assert result is parsed

    def test_looks_like_file_path_recognizes_extensions(self):
        from orchestrator import _looks_like_file_path

        assert _looks_like_file_path("main.tf")
        assert _looks_like_file_path("app.py")
        assert _looks_like_file_path("README.md")
        assert _looks_like_file_path("config.yaml")

    def test_looks_like_file_path_recognizes_paths(self):
        from orchestrator import _looks_like_file_path

        assert _looks_like_file_path("lib/stack.ts")
        assert _looks_like_file_path("a/b/c/d.go")
        assert _looks_like_file_path("Dockerfile")
        assert not _looks_like_file_path("")
        assert not _looks_like_file_path("plain_text")
        assert not _looks_like_file_path("title.x")  # unknown extension
