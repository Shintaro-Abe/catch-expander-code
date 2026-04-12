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

        result = orch._run_review_loop("prompt", {"content_blocks": []}, [], "技術", "gen_prompt", "C1", "ts1")
        assert result["passed"] is True
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

        result = orch._run_review_loop("prompt", {"content_blocks": []}, [], "技術", "gen_prompt", "C1", "ts1")
        assert result["passed"] is True
        assert mock_claude.call_count == 3
