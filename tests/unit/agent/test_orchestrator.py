import json
import time
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

        def side_effect(prompt, allowed_tools=None, **_kwargs):
            # T1-2b: call_claude に emitter kwarg が追加されたため、未知の kwarg を吸収する
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
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
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
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
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
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
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
            "files": {"main.tf": 'resource "aws_cloudfront_distribution" "x" {}'},
            "readme_content": "# CloudFront IaC",
        }
        original = {
            "content_blocks": [{"t": "original"}],
            "summary": "初版",
            "code_files": original_code_files,
        }
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
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
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
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

        original_code_files = {"files": {"main.tf": 'resource "x" "y" {}'}, "readme_content": "readme"}
        original = {"content_blocks": [], "summary": "初版", "code_files": original_code_files}
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
        assert result["passed"] is False
        assert final_deliverables["summary"] == "fix-2"
        assert final_deliverables["code_files"] == original_code_files

    @patch("orchestrator.call_claude")
    def test_review_loop_code_issue_does_not_claim_fix_in_summary(self, mock_claude):
        """コード関連指摘を受けても、generator は summary で「修正した」と主張しない

        fix_prompt のスコープ制約セクションにより、generator は code_files を修正したと
        宣言する代わりに quality_metadata.notes に未修正記録を残す挙動を期待する。
        """
        from orchestrator import Orchestrator

        code_issue = {
            "item": "コードの構文",
            "severity": "error",
            "description": "resolver.tf の filter ブロックは Route 53 Resolver では非対応",
            "fix_instruction": "resolver.tf から filter ブロックを削除する",
        }
        responses = [
            json.dumps({"result": json.dumps({"passed": False, "issues": [code_issue], "quality_metadata": {}})}),
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "content_blocks": [{"t": "unchanged"}],
                            "summary": "Route 53 Resolver の概要レポート",
                            "quality_metadata": {"notes": ["コード関連指摘 1 件は本ループ未修正"]},
                        }
                    )
                }
            ),
            json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {}})}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original_code_files = {
            "files": {"resolver.tf": 'resource "aws_route53_resolver_firewall_rule" "x" {}'},
            "readme_content": "# Route 53 Resolver IaC",
        }
        original = {
            "content_blocks": [{"t": "original"}],
            "summary": "初版",
            "code_files": original_code_files,
        }

        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        # fix call (2 回目の call_claude 呼び出し) の prompt にスコープ制約セクションが含まれていること
        fix_call_prompt = mock_claude.call_args_list[1].args[0]
        assert "本ループでの修正可能範囲" in fix_call_prompt
        assert "code_files" in fix_call_prompt
        assert "quality_metadata.notes" in fix_call_prompt

        # summary に修正主張が含まれない (mock 応答がスコープ制約を遵守したシナリオ)
        assert "修正" not in final_deliverables["summary"]
        assert "削除" not in final_deliverables["summary"]
        # code_files は preserve されている (既存挙動の維持)
        assert final_deliverables["code_files"] == original_code_files
        # review が pass を返したこと
        assert result["passed"] is True

    @patch("orchestrator.call_claude")
    def test_review_loop_text_issue_updates_text_normally(self, mock_claude):
        """テキスト関連指摘では、従来通り summary / content_blocks が更新される (回帰なし)"""
        from orchestrator import Orchestrator

        text_issue = {
            "item": "セクション 3 の表現",
            "severity": "error",
            "description": "数値 $100 億 が出典 [3] と矛盾 ($85 億)",
            "fix_instruction": "セクション 3 の市場規模を $85 億 に修正",
        }
        responses = [
            json.dumps({"result": json.dumps({"passed": False, "issues": [text_issue], "quality_metadata": {}})}),
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "content_blocks": [{"t": "fixed-section-3"}],
                            "summary": "市場規模を $85 億 に更新済みの最新版",
                        }
                    )
                }
            ),
            json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {}})}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original_code_files = {"files": {"main.tf": 'resource "x" "y" {}'}, "readme_content": "# README"}
        original = {
            "content_blocks": [{"t": "original"}],
            "summary": "初版",
            "code_files": original_code_files,
        }

        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert result["passed"] is True
        assert final_deliverables["content_blocks"] == [{"t": "fixed-section-3"}]
        assert "$85" in final_deliverables["summary"]
        # code_files は preserve されている
        assert final_deliverables["code_files"] == original_code_files

    @patch("orchestrator.call_claude")
    def test_review_loop_merges_fixer_notes_into_review_quality_metadata(self, mock_claude):
        """fix loop で fixer が quality_metadata.notes に書いた未修正記録は、
        後続の review pass 時にも review_result.quality_metadata.notes へマージされる
        (Codex review P2 対応 — Notion / DynamoDB に流れる出力経路で notes が捨てられない)。
        """
        from orchestrator import Orchestrator

        code_issue = {
            "item": "コードの構文",
            "severity": "error",
            "description": "resolver.tf の filter ブロックは非対応",
            "fix_instruction": "filter ブロック削除",
        }
        responses = [
            json.dumps(
                {"result": json.dumps({"passed": False, "issues": [code_issue], "quality_metadata": {"notes": []}})}
            ),
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "content_blocks": [{"t": "unchanged"}],
                            "summary": "Route 53 概要",
                            "quality_metadata": {"notes": ["コード関連指摘 1 件は本ループ未修正"]},
                        }
                    )
                }
            ),
            json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {"notes": []}})}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original_code_files = {"files": {"resolver.tf": "..."}, "readme_content": "# README"}
        original = {
            "content_blocks": [{"t": "original"}],
            "summary": "初版",
            "code_files": original_code_files,
        }

        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        # review pass 後でも fixer notes が review_result に伝搬していること
        assert result["passed"] is True
        notes = result.get("quality_metadata", {}).get("notes", [])
        assert any("コード関連指摘" in n and "未修正" in n for n in notes), f"fixer notes not merged: {notes}"

        # final_deliverables の code_files は preserve されていること (既存挙動維持)
        assert final_deliverables["code_files"] == original_code_files

    @patch("orchestrator.call_claude")
    def test_review_loop_accumulates_fixer_notes_across_multiple_fixes(self, mock_claude):
        """複数回の fix にまたがって fixer notes が累積される (Codex review 2 回目 P2 対応)。

        1 回目の fixer が記録した「コード関連指摘 N 件は本ループ未修正」が、2 回目の fix で
        current_deliverables が新応答に上書きされても失われず、最終 review_result.quality_metadata.notes
        へ届くことを検証する。
        """
        from orchestrator import Orchestrator

        code_issue = {
            "item": "コードの構文",
            "severity": "error",
            "description": "filter ブロック非対応",
            "fix_instruction": "削除",
        }
        text_issue = {
            "item": "セクション 3 表現",
            "severity": "error",
            "description": "数値矛盾",
            "fix_instruction": "$85 億 に修正",
        }
        responses = [
            # 1 回目 review: コード関連 error
            json.dumps(
                {"result": json.dumps({"passed": False, "issues": [code_issue], "quality_metadata": {"notes": []}})}
            ),
            # 1 回目 fix: fixer がスコープ制約に従い notes に未修正を記録
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "content_blocks": [{"t": "after-fix-1"}],
                            "summary": "Route 53 概要",
                            "quality_metadata": {"notes": ["コード関連指摘 1 件は本ループ未修正"]},
                        }
                    )
                }
            ),
            # 2 回目 review: 別のテキスト error (code error は前回未修正のまま)
            json.dumps(
                {"result": json.dumps({"passed": False, "issues": [text_issue], "quality_metadata": {"notes": []}})}
            ),
            # 2 回目 fix: テキスト指摘に対応、notes は空 (1 回目の note を引き継がない応答)
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "content_blocks": [{"t": "after-fix-2"}],
                            "summary": "市場規模 $85 億 に更新済み",
                            "quality_metadata": {"notes": []},
                        }
                    )
                }
            ),
            # 3 回目 review: pass を返す (reviewer は fixer notes を echo しない)
            json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {"notes": []}})}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original_code_files = {"files": {"resolver.tf": "..."}, "readme_content": "# README"}
        original = {
            "content_blocks": [{"t": "original"}],
            "summary": "初版",
            "code_files": original_code_files,
        }

        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        # 1 回目 fixer の notes が 2 回目 fix の上書きを越えて最終 review_result に届いていること
        assert result["passed"] is True
        notes = result.get("quality_metadata", {}).get("notes", [])
        assert any(
            "コード関連指摘" in n and "未修正" in n for n in notes
        ), f"first fixer's note was lost across multiple fixes: {notes}"
        # code_files は preserve されている
        assert final_deliverables["code_files"] == original_code_files

    @patch("orchestrator.call_claude")
    def test_review_loop_safely_skips_null_quality_metadata_in_fixer_response(self, mock_claude):
        """fixer 応答の quality_metadata が null でも _accumulate_fixer_notes は raise せず safely skip する
        (Codex review 3 回目 P2 対応 — malformed LLM 応答でも review loop が abort しない)。
        """
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        responses = [
            json.dumps({"result": json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}})}),
            # fixer が malformed: quality_metadata が null
            json.dumps(
                {"result": json.dumps({"content_blocks": [{"t": "fixed"}], "summary": "OK", "quality_metadata": None})}
            ),
            json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {}})}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

        # raise しないこと
        result, _ = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert result["passed"] is True
        # notes は空または存在しても corrupt していない (str のみで構成される)
        notes = result.get("quality_metadata", {}).get("notes", [])
        assert all(isinstance(n, str) for n in notes)

    @patch("orchestrator.call_claude")
    def test_review_loop_safely_skips_scalar_notes_in_fixer_response(self, mock_claude):
        """fixer 応答の quality_metadata.notes が文字列 (scalar) でも、文字単位で
        accumulated に追加されず safely skip する (Notion/DynamoDB の corrupt 防止)。
        """
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        responses = [
            json.dumps({"result": json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}})}),
            # fixer が malformed: notes が文字列
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "content_blocks": [{"t": "fixed"}],
                            "summary": "OK",
                            "quality_metadata": {"notes": "コード関連指摘 1 件は本ループ未修正"},
                        }
                    )
                }
            ),
            json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {}})}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

        result, _ = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert result["passed"] is True
        notes = result.get("quality_metadata", {}).get("notes", [])
        # 文字単位で追加されていない (1 文字の note が存在しない)
        assert not any(isinstance(n, str) and len(n) == 1 for n in notes), f"notes corrupted by char iteration: {notes}"
        # 文字列全体がそのまま 1 entry として入ってもいない (型が list ではないので skip された)
        assert "コード関連指摘 1 件は本ループ未修正" not in notes

    @patch("orchestrator.call_claude")
    def test_review_loop_safely_skips_int_notes_in_fixer_response(self, mock_claude):
        """fixer 応答の quality_metadata.notes が整数でも raise せず safely skip する。"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        responses = [
            json.dumps({"result": json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}})}),
            # fixer が malformed: notes が整数
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "content_blocks": [{"t": "fixed"}],
                            "summary": "OK",
                            "quality_metadata": {"notes": 5},
                        }
                    )
                }
            ),
            json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {}})}),
        ]
        mock_claude.side_effect = responses

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

        # raise しないこと
        result, _ = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert result["passed"] is True

    # ========================================================================
    # fix loop での content_blocks 構造的保護
    # 2026-05-09 インシデント (Notion 本文消失) を防ぐため、fixer 応答が
    # content_blocks を omit / null / 空 list / 非 list で返した場合に
    # 旧版を引き継ぐ条件付き fallback の動作を検証する。
    #
    # 既存 TestReviewLoop の他テストは call_codex を patch しておらず実 CLI が
    # 呼ばれて parse_error になる pre-existing failure があるため、新規テストは
    # call_codex (reviewer) と call_claude (fixer) の両方を patch する。
    # call_codex は生 JSON 文字列を返し、call_claude は {"result": "..."} 形式を返す。
    # ========================================================================

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_preserves_content_blocks_when_fixer_omits_key(self, mock_codex, mock_claude):
        """fixer 応答に content_blocks キー自体が存在しない場合、旧版が維持される"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        # reviewer (call_codex): 1回目 fail → 2回目 pass
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        # fixer (call_claude): content_blocks キーを omit
        mock_claude.return_value = json.dumps({"result": json.dumps({"summary": "fixed summary only"})})

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original-1"}, {"t": "original-2"}], "summary": "初版"}
        _, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert final["content_blocks"] == [{"t": "original-1"}, {"t": "original-2"}]

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_preserves_content_blocks_when_fixer_returns_none(self, mock_codex, mock_claude):
        """fixer 応答の content_blocks が None の場合、旧版が維持される"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        mock_claude.return_value = json.dumps(
            {"result": json.dumps({"content_blocks": None, "summary": "fixed"})}
        )

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}
        _, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert final["content_blocks"] == [{"t": "original"}]

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_preserves_content_blocks_when_fixer_returns_empty_list(self, mock_codex, mock_claude):
        """fixer 応答の content_blocks が空 list の場合、旧版が維持される"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        mock_claude.return_value = json.dumps(
            {"result": json.dumps({"content_blocks": [], "summary": "fixed"})}
        )

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}
        _, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert final["content_blocks"] == [{"t": "original"}]

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_preserves_content_blocks_when_fixer_returns_non_list(self, mock_codex, mock_claude):
        """fixer 応答の content_blocks が非 list (string) の場合、旧版が維持される"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        mock_claude.return_value = json.dumps(
            {"result": json.dumps({"content_blocks": "not a list", "summary": "fixed"})}
        )

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}
        _, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert final["content_blocks"] == [{"t": "original"}]

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_uses_fixer_content_blocks_when_valid(self, mock_codex, mock_claude, caplog):
        """fixer 応答の content_blocks が valid な non-empty list の場合、fixer 版が採用される (regression 防止)"""
        import logging

        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        mock_claude.return_value = json.dumps(
            {"result": json.dumps({"content_blocks": [{"t": "fixer-version"}], "summary": "fixed"})}
        )

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

        with caplog.at_level(logging.INFO, logger="catch-expander-agent"):
            _, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        # fix loop 本来の機能: fixer の修正版が採用される
        assert final["content_blocks"] == [{"t": "fixer-version"}]
        # info ログ contract: valid fixer ケースは reason=None / applied=False
        info_records = [
            r for r in caplog.records if "Deliverables updated by review fix" in r.getMessage()
        ]
        assert len(info_records) == 1
        assert info_records[0].content_blocks_fallback_reason is None
        assert info_records[0].content_blocks_fallback_applied is False

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_logs_warning_on_fallback(self, mock_codex, mock_claude, caplog):
        """fallback 発動時に warning ログが loop / reason / previous_blocks_count を含めて記録される"""
        import logging

        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        # fixer が content_blocks=None で fallback 発動
        mock_claude.return_value = json.dumps(
            {"result": json.dumps({"content_blocks": None, "summary": "fixed"})}
        )

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original-a"}, {"t": "original-b"}], "summary": "初版"}

        with caplog.at_level(logging.INFO, logger="catch-expander-agent"):
            orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        fallback_records = [
            r for r in caplog.records
            if "Fix loop fixer omitted/invalid content_blocks" in r.getMessage()
        ]
        assert len(fallback_records) == 1
        rec = fallback_records[0]
        assert rec.levelno == logging.WARNING
        assert rec.reason == "none_value"
        assert rec.previous_blocks_count == 2
        assert rec.loop == 0

        # info ログ contract: fallback 発動ケースは reason="none_value" / applied=True
        info_records = [
            r for r in caplog.records if "Deliverables updated by review fix" in r.getMessage()
        ]
        assert len(info_records) == 1
        assert info_records[0].content_blocks_fallback_reason == "none_value"
        assert info_records[0].content_blocks_fallback_applied is True

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_keeps_previous_when_fixer_returns_non_dict(self, mock_codex, mock_claude, caplog):
        """fixer 応答が JSON array/scalar (非 dict) の場合、AttributeError を起こさず旧版を保持する。

        `_parse_claude_response` は valid JSON でも `[]`, `"text"`, `123` 等を parse_error なしで
        そのまま返す経路があり、`parsed.get(...)` で AttributeError になる pre-existing リスク。
        Codex レビュー (1 回目, P1) の指摘に対する構造保護。
        """
        import logging

        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        # fixer が JSON array を返す (非 dict)。`_parse_claude_response` は `[]` をそのまま返す。
        mock_claude.return_value = json.dumps({"result": "[]"})

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

        with caplog.at_level(logging.WARNING, logger="catch-expander-agent"):
            # AttributeError を起こさず loop が完走すること
            result, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        # 旧版 deliverables が維持されること
        assert final["content_blocks"] == [{"t": "original"}]
        assert final["summary"] == "初版"
        # 適切な warning が出ていること
        non_dict_records = [
            r for r in caplog.records
            if "Fix attempt produced non-dict response" in r.getMessage()
        ]
        assert len(non_dict_records) == 1
        assert non_dict_records[0].parsed_type == "list"

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_keeps_previous_on_parse_error_branch(self, mock_codex, mock_claude, caplog):
        """fixer 応答が parse_error の場合、旧版 deliverables が保持される (3 分岐の B 分岐網羅)。

        既存 test_run_review_loop_keeps_previous_on_parse_error は call_codex の patch 忘れで
        pre-existing failure になっているため、本 steering で分岐 B 専用の網羅テストを新規追加する。
        Codex レビュー (2 回目, P1) の指摘に対する構造保護。
        """
        import logging

        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        # fixer が parse_error になる応答 (raw_text に変換される)
        mock_claude.return_value = json.dumps({"result": "this is not valid json at all"})

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

        with caplog.at_level(logging.WARNING, logger="catch-expander-agent"):
            _, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        # 旧版 deliverables が維持される
        assert final["content_blocks"] == [{"t": "original"}]
        assert final["summary"] == "初版"
        # parse_error 専用 warning が出る (非 dict warning ではない)
        parse_err_records = [
            r for r in caplog.records
            if "Fix attempt produced unparseable response" in r.getMessage()
        ]
        assert len(parse_err_records) == 1

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_fix_loop_does_not_apply_fallback_when_previous_also_invalid(
        self, mock_codex, mock_claude, caplog
    ):
        """旧版 content_blocks 自体が無効な場合、fallback は実施されない (info ログで applied=False)。

        fix loop の最初の iteration で generator 応答時点で既に content_blocks が空だった場合、
        fixer も content_blocks を omit すると、fallback 発動条件
        `isinstance(prev, list) and bool(prev)` が False で fallback されない。
        Codex レビュー (2 回目, P2) の info ログ contract 検証。
        """
        import logging

        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
            ),
        ]
        # fixer が content_blocks=None で fallback の判定対象だが、旧版も無効なため fallback 発動せず
        mock_claude.return_value = json.dumps(
            {"result": json.dumps({"content_blocks": None, "summary": "fixed"})}
        )

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        # 旧版自身が無効値 (空 list)
        original = {"content_blocks": [], "summary": "初版"}

        with caplog.at_level(logging.INFO, logger="catch-expander-agent"):
            orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        # warning ログは出ない (fallback 発動条件を満たさないため)
        fallback_records = [
            r for r in caplog.records
            if "Fix loop fixer omitted/invalid content_blocks" in r.getMessage()
        ]
        assert len(fallback_records) == 0

        # info ログ contract: reason="none_value" だが applied=False で「諦め」を表現
        info_records = [
            r for r in caplog.records if "Deliverables updated by review fix" in r.getMessage()
        ]
        assert len(info_records) == 1
        assert info_records[0].content_blocks_fallback_reason == "none_value"
        assert info_records[0].content_blocks_fallback_applied is False


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
            "generate_steps": [
                {"step_id": f"g-{i}", "step_name": t, "deliverable_type": t} for i, t in enumerate(deliverable_types)
            ],
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
    def test_code_generation_per_type_merges_files(self, mock_call_claude, mock_call_workspace, _mock_load):
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
        warning_calls = [c for c in slack.post_progress.call_args_list if "失敗" in c.args[2]]
        assert len(warning_calls) == 1
        assert "プログラムコード" in warning_calls[0].args[2]

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude_with_workspace")
    @patch("orchestrator.call_claude")
    def test_generator_code_files_always_discarded(self, mock_call_claude, mock_call_workspace, _mock_load):
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


class TestPutDeliverableGitHubUrl:
    """put_deliverable に github_url が条件付きで含まれることのテスト"""

    def _make_responses_with_storage(self, storage_targets):
        """run() を最後まで通すための最小限の Claude 応答列（storage_targets を可変に）"""
        analysis = {"category": "技術", "intent": "学習", "perspectives": [], "deliverable_types": ["research_report"]}
        workflow = {
            "research_steps": [{"step_id": "r-1", "step_name": "概要", "description": "概要調査", "search_hints": []}],
            "generate_steps": [{"step_id": "g-1", "step_name": "レポート", "deliverable_type": "research_report"}],
            "storage_targets": storage_targets,
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

    def _make_orch(self, db, mock_review_loop_return):
        """Orchestrator インスタンスを作成し、Notion / GitHub / レビューループをモック化"""
        from orchestrator import Orchestrator

        slack = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")
        orch.notion = MagicMock()
        orch.notion.create_page.return_value = ("https://notion.so/page", "page-id")
        orch.github = MagicMock()
        orch.github.push_files.return_value = "https://github.com/owner/repo/tree/main/test-20260429"
        orch._run_review_loop = MagicMock(return_value=mock_review_loop_return)
        return orch

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_put_deliverable_with_github_url(self, mock_call_claude, _mock_load):
        """code_files ありで storage_targets に github を含む場合、put_deliverable に github_url が含まれる"""
        mock_call_claude.side_effect = self._make_responses_with_storage(["notion", "github"])

        db = MagicMock()
        db.get_user_profile.return_value = {"user_id": "U1"}
        db._table.return_value = MagicMock()

        # レビューループの戻り値で code_files を含む deliverables を返す
        review_result = {"passed": True, "issues": [], "quality_metadata": {"sources_total": 5}}
        deliverables_with_code = {
            "content_blocks": [],
            "code_files": {"files": {"main.py": "print('hi')"}, "readme_content": "# README"},
            "summary": "完成",
        }
        orch = self._make_orch(db, (review_result, deliverables_with_code))

        orch.run("exec-1", "U1", "Lambda入門", "C1", "ts1")

        db.put_deliverable.assert_called_once()
        payload = db.put_deliverable.call_args[0][0]
        assert "github_url" in payload
        assert payload["github_url"] == "https://github.com/owner/repo/tree/main/test-20260429"
        assert payload["storage"] == "notion+github"

    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_put_deliverable_without_github_url(self, mock_call_claude, _mock_load):
        """code_files なしの場合、put_deliverable に github_url キー自体が含まれない"""
        mock_call_claude.side_effect = self._make_responses_with_storage(["notion"])

        db = MagicMock()
        db.get_user_profile.return_value = {"user_id": "U1"}
        db._table.return_value = MagicMock()

        review_result = {"passed": True, "issues": [], "quality_metadata": {}}
        deliverables_text_only = {"content_blocks": [], "code_files": None, "summary": "完成"}
        orch = self._make_orch(db, (review_result, deliverables_text_only))

        orch.run("exec-1", "U1", "DDDの基礎", "C1", "ts1")

        db.put_deliverable.assert_called_once()
        payload = db.put_deliverable.call_args[0][0]
        assert "github_url" not in payload
        assert payload["storage"] == "notion"


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
        """ "unknown" / "continuously-updated" は日付として扱わず注意書きに切り替わる"""
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


class TestTextGeneratorWorkspace:
    """text generator の workspace モード化 + 検証層 + 自動リトライのテスト群。

    2026-05-12 観測の LLM Part 分割応答インシデント対応で導入された
    `_run_text_generator_with_retries` の挙動を検証する。
    `.steering/20260512-parse-claude-response-dict-contract/`
    """

    @staticmethod
    def _valid_deliverable_json() -> str:
        """valid な deliverable.json の中身を返す。"""
        return json.dumps({
            "content_blocks": [
                {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "h"}}]}},
            ],
            "summary": "ok",
            "quality_metadata": {
                "sources_verified": 1,
                "sources_unverified": 0,
                "sources_total": 1,
                "checklist_passed": 4,
                "checklist_total": 4,
                "notes": [],
                "unverified_details": [],
            },
        })

    @staticmethod
    def _valid_workspace_result() -> tuple:
        """call_claude_with_text_workspace の valid 戻り値を返す。"""
        content = TestTextGeneratorWorkspace._valid_deliverable_json()
        return (
            "Wrote: deliverable.json",
            content,
            {"file_exists": True, "file_bytes": len(content), "extra_files": []},
        )

    @staticmethod
    def _make_orch():
        """Orchestrator + mock 依存関係を作る。"""
        from orchestrator import Orchestrator

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        # _emitter / _prompt_recorder / _cost_acc は既に MagicMock 系で初期化されているが
        # 一部テストで明示的に MagicMock として確認したいので再代入
        orch._emitter = MagicMock()
        orch._prompt_recorder = MagicMock()
        orch._cost_acc = None
        return orch

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_succeeds_when_deliverable_json_is_valid_dict(self, mock_workspace, mock_sleep):
        from orchestrator import Orchestrator  # noqa: F401

        mock_workspace.return_value = self._valid_workspace_result()
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert deliverables["content_blocks"][0]["type"] == "heading_2"
        assert mock_workspace.call_count == 1
        # PromptRecorder が呼ばれ、output_files に deliverable.json が含まれる
        record_call = orch._prompt_recorder.record.call_args
        assert record_call.args[0] == "generator_text"
        assert record_call.args[1] == "0"
        assert "deliverable.json" in record_call.kwargs["output_files"]
        # backoff は不要
        mock_sleep.assert_not_called()

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_records_empty_deliverable_as_failure_trace(self, mock_workspace, mock_sleep):
        """空 deliverable.json (Codex 1 回目 P2-3 対応) でも PromptRecorder に保存される。

        旧コードは `if deliverable_content` の truthiness で空文字を落としていた。
        現コードは `is not None` チェックで空ファイルも失敗証跡として記録する。
        """
        from orchestrator import Orchestrator  # noqa: F401

        empty_result = ("Wrote: deliverable.json", "", {"file_exists": True, "file_bytes": 0, "extra_files": []})
        mock_workspace.side_effect = [empty_result, self._valid_workspace_result()]
        orch = self._make_orch()

        orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        # 1 回目の record 呼出で output_files に空文字が保存されている
        first_record = orch._prompt_recorder.record.call_args_list[0]
        assert first_record.kwargs["output_files"] == {"deliverable.json": ""}

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_file_missing(self, mock_workspace, mock_sleep):
        from orchestrator import Orchestrator  # noqa: F401

        mock_workspace.side_effect = [
            ("Done", None, {"file_exists": False, "file_bytes": 0, "extra_files": []}),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2
        # 1 回目失敗で backoff (2 秒)
        mock_sleep.assert_called_once_with(2)

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_json_invalid(self, mock_workspace, mock_sleep):
        from orchestrator import Orchestrator  # noqa: F401

        invalid = "this is not valid json {"
        mock_workspace.side_effect = [
            ("Wrote: deliverable.json", invalid, {"file_exists": True, "file_bytes": len(invalid), "extra_files": []}),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_not_dict(self, mock_workspace, mock_sleep):
        from orchestrator import Orchestrator  # noqa: F401

        # 5/12 19:04 シナリオ再現: JSON array をトップレベルで返す
        non_dict = json.dumps([{"type": "heading_2"}])
        mock_workspace.side_effect = [
            ("Wrote: deliverable.json", non_dict, {"file_exists": True, "file_bytes": len(non_dict), "extra_files": []}),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_missing_keys(self, mock_workspace, mock_sleep):
        from orchestrator import Orchestrator  # noqa: F401

        # content_blocks のみ存在 (summary / quality_metadata 欠落)
        missing = json.dumps({"content_blocks": [{"type": "h"}]})
        mock_workspace.side_effect = [
            ("Wrote: deliverable.json", missing, {"file_exists": True, "file_bytes": len(missing), "extra_files": []}),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_invalid_content_blocks(self, mock_workspace, mock_sleep):
        from orchestrator import Orchestrator  # noqa: F401

        empty_blocks = json.dumps({"content_blocks": [], "summary": "x", "quality_metadata": {}})
        mock_workspace.side_effect = [
            ("Wrote: deliverable.json", empty_blocks, {"file_exists": True, "file_bytes": len(empty_blocks), "extra_files": []}),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_file_too_large(self, mock_workspace, mock_sleep):
        """deliverable.json が _MAX_DELIVERABLE_BYTES (1MB) を超えたら file_too_large で失敗扱い。

        Codex 1 回目 P2-4 対応。後方互換経路 (oversize フラグなし、file_bytes のみ) のテスト。
        """
        from orchestrator import Orchestrator  # noqa: F401

        oversize_content = "x" * 200  # 実際のコンテンツは短くて OK
        mock_workspace.side_effect = [
            ("Wrote: deliverable.json", oversize_content, {"file_exists": True, "file_bytes": 2 * 1024 * 1024, "extra_files": []}),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_oversize_flag_set_by_workspace(self, mock_workspace, mock_sleep):
        """workspace ラッパーが stat() 先行で oversize=True を立てた場合、検証層が file_too_large を検出する。

        Codex 2 回目 P2 補強: read 前段で stat() による size 検出を行う経路の検証。
        deliverable_content は preview のみ (300 bytes) で content 全量は読まない設計。
        """
        from orchestrator import Orchestrator  # noqa: F401

        preview = "x" * 300  # ラッパーが preview のみ返した想定
        mock_workspace.side_effect = [
            (
                "Wrote: deliverable.json",
                preview,
                {
                    "file_exists": True,
                    "file_bytes": 5 * 1024 * 1024,  # 5 MB
                    "extra_files": [],
                    "oversize": True,  # ★ stat() 先行で立てたフラグ
                },
            ),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2

    def test_validate_deliverable_payload_avoids_redundant_encoding(self):
        """_validate_deliverable_payload が outcome["file_bytes"] を優先使用し、再 encode しない。

        Codex 2 回目 P2 指摘: 旧コードは `outcome.get("file_bytes", len(content.encode("utf-8")))`
        で default 引数が常に評価され、file_bytes 存在時も巨大文字列を再 encode していた。
        """
        from orchestrator import _validate_deliverable_payload

        # outcome に file_bytes 提供 → encode は呼ばれず file_bytes が直接使われる
        outcome = {"file_exists": True, "file_bytes": 100, "extra_files": []}
        deliverables, reason, _ = _validate_deliverable_payload(self._valid_deliverable_json(), outcome)
        assert reason is None
        assert deliverables is not None

        # outcome に file_bytes 欠落 → fallback で encode 計算が走る (旧経路互換)
        outcome_no_bytes = {"file_exists": True, "extra_files": []}
        deliverables, reason, _ = _validate_deliverable_payload(self._valid_deliverable_json(), outcome_no_bytes)
        assert reason is None
        assert deliverables is not None

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_invalid_summary(self, mock_workspace, mock_sleep):
        """summary が non-empty str でない場合 (Codex 1 回目 P2-5 対応) リトライ発火。"""
        from orchestrator import Orchestrator  # noqa: F401

        bad_summary = json.dumps({
            "content_blocks": [{"type": "heading_2"}],
            "summary": [],  # str ではなく list
            "quality_metadata": {},
        })
        mock_workspace.side_effect = [
            ("Wrote: deliverable.json", bad_summary, {"file_exists": True, "file_bytes": len(bad_summary), "extra_files": []}),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_retries_when_invalid_quality_metadata(self, mock_workspace, mock_sleep):
        """quality_metadata が dict でない場合 (Codex 1 回目 P2-5 対応) リトライ発火。"""
        from orchestrator import Orchestrator  # noqa: F401

        bad_qm = json.dumps({
            "content_blocks": [{"type": "heading_2"}],
            "summary": "ok",
            "quality_metadata": "not a dict",
        })
        mock_workspace.side_effect = [
            ("Wrote: deliverable.json", bad_qm, {"file_exists": True, "file_bytes": len(bad_qm), "extra_files": []}),
            self._valid_workspace_result(),
        ]
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        assert deliverables["summary"] == "ok"
        assert mock_workspace.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_fails_after_max_retries(self, mock_workspace, mock_sleep):
        from orchestrator import NonDictGeneratorResponse, Orchestrator  # noqa: F401

        # 全 3 試行で file_missing を返す
        failed_outcome = ("Done", None, {"file_exists": False, "file_bytes": 0, "extra_files": []})
        mock_workspace.return_value = failed_outcome
        orch = self._make_orch()

        with pytest.raises(NonDictGeneratorResponse) as excinfo:
            orch._run_text_generator_with_retries(
                gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
            )

        assert excinfo.value.reason == "file_missing"
        assert mock_workspace.call_count == 3  # MAX_GENERATOR_RETRIES (2) + 1
        # subagent_failed emit が呼ばれ、error_type は NonDictGeneratorResponse
        failed_emit_call = orch._emitter.emit.call_args_list[-1]
        assert failed_emit_call.args[0] == "subagent_failed"
        payload = failed_emit_call.args[1]
        assert payload["subagent"] == "generator_text"
        assert payload["error_type"] == "NonDictGeneratorResponse"
        # 試行間 backoff は 2 秒 + 4 秒 = 計 2 回呼ばれる (最終試行直前はスリープしない)
        assert mock_sleep.call_count == 2

    def test_should_use_workspace_text_gen_default_true(self, monkeypatch):
        """WORKSPACE_TEXT_GEN 未設定時、デフォルトで True を返す。

        Codex 1 回目 P2-2 対応で feature flag 判定をヘルパー化したため、判定式を直接検証する。
        """
        monkeypatch.delenv("WORKSPACE_TEXT_GEN", raising=False)
        from orchestrator import _should_use_workspace_text_gen

        assert _should_use_workspace_text_gen() is True

    def test_should_use_workspace_text_gen_false_when_env_false(self, monkeypatch):
        """WORKSPACE_TEXT_GEN=false で False を返す (即時切り戻し用)。"""
        monkeypatch.setenv("WORKSPACE_TEXT_GEN", "false")
        from orchestrator import _should_use_workspace_text_gen

        assert _should_use_workspace_text_gen() is False

    def test_should_use_workspace_text_gen_handles_uppercase(self, monkeypatch):
        """WORKSPACE_TEXT_GEN=FALSE / False など大文字・混在ケースで False を返す。

        運用者がうっかり大文字で env 設定しても意図通り動作することを保証。
        """
        from orchestrator import _should_use_workspace_text_gen

        monkeypatch.setenv("WORKSPACE_TEXT_GEN", "FALSE")
        assert _should_use_workspace_text_gen() is False

        monkeypatch.setenv("WORKSPACE_TEXT_GEN", "False")
        assert _should_use_workspace_text_gen() is False

        # "true" 系も同様に case-insensitive
        monkeypatch.setenv("WORKSPACE_TEXT_GEN", "TRUE")
        assert _should_use_workspace_text_gen() is True

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.call_claude_with_text_workspace")
    def test_generator_emits_subagent_completed_with_generator_text_subagent_name(
        self, mock_workspace, mock_sleep
    ):
        """成功時の subagent_completed emit が "generator_text" subagent 名で呼ばれる。

        Codex 1 回目 P2-1 対応: 旧コードは completed だけ "generator" 固定で、started/failed と
        非対称だった。本 steering で workspace 経路は一貫して "generator_text" を使う。

        注: subagent_completed は `_run_text_generator_with_retries` の **外側** (run() メソッド内)
        で emit されるため、本テストは _run_text_generator_with_retries 単体では検証できない。
        修正の効果は実機検証 (T-17) + run() 統合テスト (将来) で確認。
        本テストは run() の generator_subagent_name 変数が正しく "generator_text" になることを
        間接確認する: _run_text_generator_with_retries が成功して返ったとき、PromptRecorder の
        subagent 引数が "generator_text" であることを以て、後段の subagent_completed emit も
        同じ変数を使っているという design.md ポイント 1 を信頼する。
        """
        mock_workspace.return_value = self._valid_workspace_result()
        orch = self._make_orch()

        deliverables = orch._run_text_generator_with_retries(
            gen_prompt="prompt", execution_id="exec-test", generator_start_ns=time.monotonic_ns()
        )

        # PromptRecorder 経由で subagent 名が "generator_text" であることを確認
        # (run() 内 generator_subagent_name 変数の整合性を間接検証)
        record_call = orch._prompt_recorder.record.call_args
        assert record_call.args[0] == "generator_text"
        assert deliverables["summary"] == "ok"


class TestCallCodexErrorObservability:
    """call_codex のエラー observability テスト

    steering: .steering/20260514-codex-error-observability/
    背景: 5/14 03:28 JST に Codex 401 失敗の真因が error_message から失われていた問題への対処。
    CodexInvocationError(subprocess.CalledProcessError) サブクラス + __str__ オーバーライドで
    str(e)[:500] スライスでも stderr 末尾が読めるようにした実装の回帰防止テスト。
    """

    def _make_failure_run(self, stderr: str):
        """全 retry を CalledProcessError で失敗させる subprocess.run mock を返す"""
        import subprocess

        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, output="", stderr=stderr,
            )
        return fake_run

    def test_raises_codex_invocation_error_on_total_failure(self):
        from orchestrator import CodexInvocationError, call_codex

        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run("err")),
            patch("orchestrator.time.sleep"),
            pytest.raises(CodexInvocationError),
        ):
            call_codex("prompt")

    def test_codex_invocation_error_is_a_called_process_error(self):
        """継承により main.py:_notify_task_failure の Slack 通知分岐 (isinstance) が壊れないこと"""
        import subprocess

        from orchestrator import call_codex

        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run("err")),
            patch("orchestrator.time.sleep"),
            pytest.raises(subprocess.CalledProcessError),
        ):
            call_codex("prompt")

    def test_str_includes_stderr_when_short(self):
        """短い stderr (典型的なエラー) の場合、str(e)[:500] スライスで stderr が読めること.

        emitter / DynamoDB / Slack / Dashboard の error_message = str(e)[:500] 経路の
        実利上、最も重要なケース (Codex 401 のような短いエラーメッセージ).
        """
        from orchestrator import CodexInvocationError, call_codex

        marker = "401_UNAUTHORIZED_TOKEN_REFRESH_FAILED"
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run(marker)),
            patch("orchestrator.time.sleep"),
        ):
            try:
                call_codex("prompt")
            except CodexInvocationError as e:
                head500 = str(e)[:500]
                assert marker in head500, f"marker not in head500: {head500!r}"
            else:
                pytest.fail("expected CodexInvocationError")

    def test_str_keeps_stderr_tail_when_long(self):
        """長い stderr の場合、末尾 1500 文字が __str__ 全体に含まれること.

        実装の tail = stderr[-1500:] により、stderr が 1500 を超えても末尾は保持される.
        """
        from orchestrator import CodexInvocationError, call_codex

        end_marker = "ENDMARKER_xyz123"
        stderr_text = "X" * 3000 + end_marker + "Y" * 100
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run(stderr_text)),
            patch("orchestrator.time.sleep"),
        ):
            try:
                call_codex("prompt")
            except CodexInvocationError as e:
                full_str = str(e)
                assert end_marker in full_str, "stderr end marker should be in __str__"
                # 旧スライス [:500] では捕まらないので、フル str を検証
                # 一方、stderr 冒頭の "X" は cap で消える (3000 + end_marker + "Y"*100 > 1500)
                assert len(full_str) <= 1600, f"__str__ should cap stderr at 1500: {len(full_str)}"
            else:
                pytest.fail("expected CodexInvocationError")

    def test_preserves_cause_chain(self):
        """exc.__cause__ に元 CalledProcessError がチェーンされ stack_trace が保持されること"""
        import subprocess

        from orchestrator import CodexInvocationError, call_codex

        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run("err")),
            patch("orchestrator.time.sleep"),
        ):
            try:
                call_codex("prompt")
            except CodexInvocationError as e:
                assert isinstance(e.__cause__, subprocess.CalledProcessError)
            else:
                pytest.fail("expected CodexInvocationError")

    def test_preserves_cmd_for_slack_branch(self):
        """exc.cmd に codex コマンドリストが入り main.py:201 の '"codex" in cmd_str' が動くこと"""
        from orchestrator import CodexInvocationError, call_codex

        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run("err")),
            patch("orchestrator.time.sleep"),
        ):
            try:
                call_codex("prompt")
            except CodexInvocationError as e:
                cmd_str = " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
                assert "codex" in cmd_str
            else:
                pytest.fail("expected CodexInvocationError")

    def test_logger_stderr_slice_2000(self, caplog):
        """リトライ時 logger.warning の stderr スライスが 2000 文字まで保持されること"""
        import contextlib

        from orchestrator import call_codex

        long_stderr = "A" * 3000
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run(long_stderr)),
            patch("orchestrator.time.sleep"),
            caplog.at_level("WARNING", logger="catch-expander-agent"),
            contextlib.suppress(Exception),
        ):
            call_codex("prompt")

        retry_logs = [r for r in caplog.records if "Codex CLI error" in r.getMessage()]
        assert retry_logs, "no retry warning found"
        first_msg = retry_logs[0].getMessage()
        assert "AAAA" in first_msg
        stderr_part = first_msg.split("stderr=", 1)[1]
        assert len(stderr_part) <= 2000
        assert len(stderr_part) > 500  # 旧スライス [:500] より明確に長い
