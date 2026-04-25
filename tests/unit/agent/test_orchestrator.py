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

    @patch("orchestrator.call_claude")
    def test_review_loop_preserves_code_files_on_fix_success(self, mock_claude):
        """レビュー修正成功時も code_files は保持される（generator は text のみ返す契約のため）"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        responses = [
            json.dumps({"result": json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}})}),
            # 修正レスポンスは text 成果物のみ（code_files を含まない = 実運用の挙動）
            json.dumps({"result": json.dumps({"content_blocks": [{"t": "fixed"}], "summary": "修正版"})}),
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "passed": True,
                            "issues": [],
                            "quality_metadata": {"sources_verified": 1, "sources_unverified": 0},
                        }
                    )
                }
            ),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original_code_files = {
            "files": {"main.tf": "resource \"aws_cloudfront_distribution\" \"x\" {}"},
            "readme_content": "# CloudFront IaC",
        }
        original = {
            "content_blocks": [{"t": "original"}],
            "summary": "初版",
            "code_files": original_code_files,
        }
        result, final_deliverables = orch._run_review_loop(
            "prompt", original, [], "技術", "gen_prompt", "C1", "ts1"
        )
        assert result["passed"] is True
        assert final_deliverables["summary"] == "修正版"
        assert final_deliverables["content_blocks"] == [{"t": "fixed"}]
        assert final_deliverables["code_files"] == original_code_files

    @patch("orchestrator.call_claude")
    def test_review_loop_no_code_files_when_absent(self, mock_claude):
        """元の成果物に code_files が無ければ、修正後にもキーが現れない"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        responses = [
            json.dumps({"result": json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}})}),
            json.dumps({"result": json.dumps({"content_blocks": [{"t": "fixed"}], "summary": "修正版"})}),
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "passed": True,
                            "issues": [],
                            "quality_metadata": {"sources_verified": 1, "sources_unverified": 0},
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
        assert "code_files" not in final_deliverables

    @patch("orchestrator.call_claude")
    def test_review_loop_preserves_code_files_across_multiple_fixes(self, mock_claude):
        """複数回の修正を跨いでも code_files は保持される"""
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

        original_code_files = {"files": {"main.tf": "resource \"x\" \"y\" {}"}, "readme_content": "readme"}
        original = {"content_blocks": [], "summary": "初版", "code_files": original_code_files}
        result, final_deliverables = orch._run_review_loop(
            "prompt", original, [], "技術", "gen_prompt", "C1", "ts1"
        )
        assert result["passed"] is False
        assert final_deliverables["summary"] == "fix-2"
        assert final_deliverables["code_files"] == original_code_files


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
    @patch("orchestrator.call_claude_with_workspace")
    @patch("orchestrator.call_claude")
    def test_code_generation_per_type_merges_files(
        self, mock_call_claude, mock_call_workspace, _mock_load
    ):
        """iac_code と program_code が個別 workspace 呼び出しでマージされる"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code", "program_code"])
        text_deliverables = {"content_blocks": [], "summary": "完成"}
        review = {"passed": True, "issues": [], "quality_metadata": {}}

        # コード生成は call_claude_with_workspace 経由
        mock_call_workspace.side_effect = [
            (
                "Wrote: main.tf, README.md",
                {"main.tf": "# iac", "README.md": "IaC README"},
                {"files_kind": "valid", "files_count": 2, "files_total_bytes": 30, "rejected": []},
            ),
            (
                "Wrote: app.py, README.md",
                {"app.py": "# program", "README.md": "Program README"},
                {"files_kind": "valid", "files_count": 2, "files_total_bytes": 30, "rejected": []},
            ),
        ]
        # text 系は call_claude 経由
        mock_call_claude.side_effect = [
            json.dumps({"result": json.dumps(analysis)}),
            json.dumps({"result": json.dumps(workflow)}),
            json.dumps({"result": json.dumps(research)}),
            json.dumps({"result": json.dumps(text_deliverables)}),
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
        # README.md は files から分離されて readme_content にマージされる
        assert "README.md" not in files_arg

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude_with_workspace")
    @patch("orchestrator.call_claude")
    def test_code_generation_partial_failure_keeps_successful_types(
        self, mock_call_claude, mock_call_workspace, _mock_load
    ):
        """一方のタイプが workspace 失敗でも、もう一方は push される + Slack 部分失敗通知"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code", "program_code"])
        text_deliverables = {"content_blocks": [], "summary": "完成"}
        review = {"passed": True, "issues": [], "quality_metadata": {}}

        mock_call_workspace.side_effect = [
            (
                "Wrote: main.tf",
                {"main.tf": "# iac"},
                {"files_kind": "valid", "files_count": 1, "files_total_bytes": 6, "rejected": []},
            ),
            # program_code は Write を呼ばずに失敗
            (
                "raw stdout no files",
                {},
                {"files_kind": "none", "files_count": 0, "files_total_bytes": 0, "rejected": []},
            ),
        ]
        mock_call_claude.side_effect = [
            json.dumps({"result": json.dumps(analysis)}),
            json.dumps({"result": json.dumps(workflow)}),
            json.dumps({"result": json.dumps(research)}),
            json.dumps({"result": json.dumps(text_deliverables)}),
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

        # 部分失敗の Slack 通知が送られている
        warning_calls = [
            c for c in slack.post_progress.call_args_list
            if "失敗" in c.args[2]
        ]
        assert len(warning_calls) == 1
        assert "プログラムコード" in warning_calls[0].args[2]

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude_with_workspace")
    @patch("orchestrator.call_claude")
    def test_generator_code_files_always_discarded(
        self, mock_call_claude, mock_call_workspace, _mock_load
    ):
        """ジェネレーターが誤って code_files を返しても、workspace 経由の値で上書きされる"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code"])
        text_deliverables = {
            "content_blocks": [],
            "code_files": {"files": {"LEAK.tf": "# must not be used"}, "readme_content": ""},
            "summary": "完成",
        }
        review = {"passed": True, "issues": [], "quality_metadata": {}}

        mock_call_workspace.side_effect = [
            (
                "Wrote: main.tf",
                {"main.tf": "# iac"},
                {"files_kind": "valid", "files_count": 1, "files_total_bytes": 6, "rejected": []},
            ),
        ]
        mock_call_claude.side_effect = [
            json.dumps({"result": json.dumps(analysis)}),
            json.dumps({"result": json.dumps(workflow)}),
            json.dumps({"result": json.dumps(research)}),
            json.dumps({"result": json.dumps(text_deliverables)}),
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


class TestLooksLikeFilePath:
    """_looks_like_file_path のホワイトリスト判定テスト（_collect_workspace_files から再利用）"""

    def test_recognizes_extensions(self):
        from orchestrator import _looks_like_file_path

        assert _looks_like_file_path("main.tf")
        assert _looks_like_file_path("app.py")
        assert _looks_like_file_path("README.md")
        assert _looks_like_file_path("config.yaml")

    def test_recognizes_paths_and_special_filenames(self):
        from orchestrator import _looks_like_file_path

        assert _looks_like_file_path("lib/stack.ts")
        assert _looks_like_file_path("a/b/c/d.go")
        assert _looks_like_file_path("Dockerfile")
        assert not _looks_like_file_path("")
        assert not _looks_like_file_path("plain_text")
        assert not _looks_like_file_path("title.x")  # unknown extension


# ---------------------------------------------------------------------------
# Workspace mode: _collect_workspace_files
# ---------------------------------------------------------------------------


class TestCollectWorkspaceFiles:
    """sandbox からのファイル収集 + ホワイトリスト + 安全性チェック"""

    def test_collects_whitelisted_files(self, tmp_path):
        from orchestrator import _collect_workspace_files

        (tmp_path / "main.tf").write_text("resource ...")
        (tmp_path / "app.py").write_text("import os")
        (tmp_path / "Dockerfile").write_text("FROM python")

        files, rejected = _collect_workspace_files(tmp_path)

        assert files == {
            "main.tf": "resource ...",
            "app.py": "import os",
            "Dockerfile": "FROM python",
        }
        assert rejected == []

    def test_rejects_unknown_extension(self, tmp_path):
        from orchestrator import _collect_workspace_files

        (tmp_path / "binary.exe").write_text("nope")
        (tmp_path / "main.tf").write_text("resource ...")

        files, rejected = _collect_workspace_files(tmp_path)

        assert "binary.exe" not in files
        assert "main.tf" in files
        assert any(r["path"] == "binary.exe" and r["reason"] == "not_in_whitelist" for r in rejected)

    def test_rejects_oversized_file(self, tmp_path):
        from orchestrator import _MAX_FILE_BYTES, _collect_workspace_files

        big = "x" * (_MAX_FILE_BYTES + 100)
        (tmp_path / "big.tf").write_text(big)

        files, rejected = _collect_workspace_files(tmp_path)

        assert files == {}
        assert any(r["path"] == "big.tf" and r["reason"] == "too_large" for r in rejected)

    def test_rejects_non_utf8(self, tmp_path):
        from orchestrator import _collect_workspace_files

        (tmp_path / "binary.tf").write_bytes(b"\xff\xfe\x00\x00invalid utf-8")

        files, rejected = _collect_workspace_files(tmp_path)

        assert files == {}
        assert any(r["path"] == "binary.tf" and r["reason"] == "not_utf8" for r in rejected)

    def test_rejects_symlink_to_outside(self, tmp_path):
        import os

        from orchestrator import _collect_workspace_files

        target = tmp_path.parent / "outside.txt"
        target.write_text("secret")
        link = tmp_path / "leak.tf"
        os.symlink(target, link)

        files, rejected = _collect_workspace_files(tmp_path)

        assert files == {}
        assert any(r["path"] == "leak.tf" and r["reason"] == "symlink_not_allowed" for r in rejected)
        target.unlink()

    def test_rejects_symlink_to_inside(self, tmp_path):
        import os

        from orchestrator import _collect_workspace_files

        real = tmp_path / "main.tf"
        real.write_text("resource ...")
        link = tmp_path / "alias.tf"
        os.symlink(real, link)

        files, rejected = _collect_workspace_files(tmp_path)

        assert files == {"main.tf": "resource ..."}
        assert any(r["path"] == "alias.tf" and r["reason"] == "symlink_not_allowed" for r in rejected)

    def test_returns_empty_when_sandbox_empty(self, tmp_path):
        from orchestrator import _collect_workspace_files

        files, rejected = _collect_workspace_files(tmp_path)

        assert files == {}
        assert rejected == []

    def test_handles_subdirectory(self, tmp_path):
        from orchestrator import _collect_workspace_files

        sub = tmp_path / "modules" / "cloudfront"
        sub.mkdir(parents=True)
        (sub / "main.tf").write_text("resource ...")

        files, rejected = _collect_workspace_files(tmp_path)

        assert files == {"modules/cloudfront/main.tf": "resource ..."}
        assert rejected == []


# ---------------------------------------------------------------------------
# Workspace mode: _classify_workspace_outcome
# ---------------------------------------------------------------------------


class TestClassifyWorkspaceOutcome:
    """workspace 実行の結末判定（valid / all_empty / no_recognized / none）"""

    def test_valid_when_files_have_content(self):
        from orchestrator import _classify_workspace_outcome

        outcome = _classify_workspace_outcome({"main.tf": "resource ..."}, [])

        assert outcome["files_kind"] == "valid"
        assert outcome["files_count"] == 1
        assert outcome["files_total_bytes"] > 0

    def test_all_empty_when_files_have_zero_bytes(self):
        from orchestrator import _classify_workspace_outcome

        outcome = _classify_workspace_outcome({"main.tf": "", "variables.tf": ""}, [])

        assert outcome["files_kind"] == "all_empty"
        assert outcome["files_count"] == 2
        assert outcome["files_total_bytes"] == 0

    def test_no_recognized_when_only_rejected(self):
        from orchestrator import _classify_workspace_outcome

        rejected = [{"path": "x.exe", "reason": "not_in_whitelist"}]
        outcome = _classify_workspace_outcome({}, rejected)

        assert outcome["files_kind"] == "no_recognized"
        assert outcome["files_count"] == 0
        assert outcome["rejected"] == rejected

    def test_none_when_nothing_written(self):
        from orchestrator import _classify_workspace_outcome

        outcome = _classify_workspace_outcome({}, [])

        assert outcome["files_kind"] == "none"
        assert outcome["files_count"] == 0


# ---------------------------------------------------------------------------
# Workspace mode: call_claude_with_workspace
# ---------------------------------------------------------------------------


class TestCallClaudeWithWorkspace:
    """Claude CLI の Write ツール経由呼び出しの sandbox 管理 + リトライ"""

    def _fake_subprocess_run(self, files_to_write: dict[str, str] | None = None, stdout: str = "ok"):
        """Claude CLI の subprocess.run を差し替える fake。cwd を見て指定ファイルを書く。"""
        from pathlib import Path

        def runner(cmd, **kwargs):
            sandbox = Path(kwargs["cwd"])
            for rel, content in (files_to_write or {}).items():
                target = sandbox / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            result = MagicMock()
            result.stdout = stdout
            result.returncode = 0
            return result

        return runner

    def test_creates_and_cleans_sandbox(self, tmp_path):
        from orchestrator import call_claude_with_workspace

        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cwd"] = kwargs["cwd"]
            result = MagicMock()
            result.stdout = ""
            return result

        with patch("orchestrator.subprocess.run", side_effect=fake_run):
            call_claude_with_workspace("p", "iac_code")

        assert captured["cwd"].startswith("/tmp/agent-output-iac_code-")
        # finally で削除済み
        from pathlib import Path

        assert not Path(captured["cwd"]).exists()

    def test_passes_cwd_to_subprocess(self):
        from orchestrator import call_claude_with_workspace

        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            captured["cmd"] = cmd
            result = MagicMock()
            result.stdout = ""
            return result

        with patch("orchestrator.subprocess.run", side_effect=fake_run):
            call_claude_with_workspace("p", "iac_code")

        assert captured["cwd"] is not None
        assert captured["cwd"].startswith("/tmp/agent-output-iac_code-")

    def test_includes_write_in_allowed_tools(self):
        from orchestrator import call_claude_with_workspace

        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            result = MagicMock()
            result.stdout = ""
            return result

        with patch("orchestrator.subprocess.run", side_effect=fake_run):
            call_claude_with_workspace("p", "iac_code")

        assert "--allowedTools" in captured["cmd"]
        idx = captured["cmd"].index("--allowedTools")
        assert "Write" in captured["cmd"][idx + 1]
        assert "Edit" in captured["cmd"][idx + 1]

    def test_returns_collected_files(self):
        from orchestrator import call_claude_with_workspace

        runner = self._fake_subprocess_run(
            files_to_write={"main.tf": "resource ...", "README.md": "# doc"},
            stdout="Wrote: main.tf, README.md",
        )

        with patch("orchestrator.subprocess.run", side_effect=runner):
            raw, files, outcome = call_claude_with_workspace("p", "iac_code")

        assert raw == "Wrote: main.tf, README.md"
        assert files == {"main.tf": "resource ...", "README.md": "# doc"}
        assert outcome["files_kind"] == "valid"

    def test_retries_on_subprocess_error(self):
        import subprocess

        from orchestrator import call_claude_with_workspace

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr="err")
            result = MagicMock()
            result.stdout = ""
            return result

        with (
            patch("orchestrator.subprocess.run", side_effect=fake_run),
            patch("orchestrator.time.sleep"),
        ):
            call_claude_with_workspace("p", "iac_code")

        assert call_count["n"] == 2

    def test_cleans_sandbox_on_exception(self):
        import subprocess

        from orchestrator import call_claude_with_workspace

        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cwd"] = kwargs["cwd"]
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr="")

        with (
            patch("orchestrator.subprocess.run", side_effect=fake_run),
            patch("orchestrator.time.sleep"),
            pytest.raises(subprocess.CalledProcessError),
        ):
            call_claude_with_workspace("p", "iac_code")

        from pathlib import Path

        assert not Path(captured["cwd"]).exists()
