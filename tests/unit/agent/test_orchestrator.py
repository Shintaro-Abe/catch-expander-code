import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _fake_result_message(
    result_text: str,
    *,
    is_error: bool = False,
    subtype: str = "success",
    usage: dict | None = None,
    total_cost_usd: float = 0.0,
):
    """ResultMessage 相当のスタブ (20260706 Agent SDK 移行)。

    _query_claude_sync を patch した先で参照される属性のみ持てばよい。
    MagicMock は is_error が truthy になる罠があるため SimpleNamespace を使う。
    """
    return SimpleNamespace(
        result=result_text,
        is_error=is_error,
        subtype=subtype,
        usage=usage if usage is not None else {"input_tokens": 10, "output_tokens": 5},
        total_cost_usd=total_cost_usd,
    )


class TestCallClaude:
    """call_claude 関数のテスト (Agent SDK 同期ファサード)"""

    @patch("orchestrator._query_claude_sync")
    def test_call_claude_success_returns_result_text(self, mock_query):
        from orchestrator import call_claude

        mock_query.return_value = _fake_result_message("test output")
        result = call_claude("test prompt")
        assert result == "test output"

        prompt_arg, options = mock_query.call_args.args
        assert prompt_arg == "test prompt"
        assert options.model == "sonnet"

    @patch("orchestrator._query_claude_sync")
    def test_call_claude_with_allowed_tools(self, mock_query):
        from orchestrator import call_claude

        mock_query.return_value = _fake_result_message("{}")
        call_claude("prompt", allowed_tools=["WebSearch", "WebFetch"])

        options = mock_query.call_args.args[1]
        assert options.allowed_tools == ["WebSearch", "WebFetch"]

    @patch("orchestrator._query_claude_sync")
    def test_call_claude_uses_claude_code_system_prompt_preset(self, mock_query):
        """SDK デフォルトは空システムプロンプトのため、旧 CLI と等価な preset 指定を検証する
        (design.md パリティ注意点)。"""
        from orchestrator import call_claude

        mock_query.return_value = _fake_result_message("ok")
        call_claude("prompt")

        options = mock_query.call_args.args[1]
        assert options.system_prompt == {"type": "preset", "preset": "claude_code"}

    @patch("orchestrator.time.sleep")
    @patch("orchestrator._query_claude_sync")
    def test_call_claude_retries_on_process_error(self, mock_query, mock_sleep):
        from claude_agent_sdk import ProcessError
        from orchestrator import call_claude

        mock_query.side_effect = [
            ProcessError("boom", exit_code=1, stderr="err"),
            ProcessError("boom", exit_code=1, stderr="err"),
            _fake_result_message("ok"),
        ]
        result = call_claude("prompt")
        assert result == "ok"
        assert mock_query.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator._query_claude_sync")
    def test_call_claude_raises_after_max_retries_and_advisor(self, mock_query, mock_sleep):
        """3 リトライ枯渇後に advisor (Opus) を 1 回試行し、それも失敗したら
        ClaudeInvocationError を送出する。"""
        from claude_agent_sdk import ProcessError
        from orchestrator import CLAUDE_ADVISOR_MODEL, ClaudeInvocationError, call_claude

        mock_query.side_effect = ProcessError("boom", exit_code=1, stderr="err")
        with pytest.raises(ClaudeInvocationError):
            call_claude("prompt")
        # MAX_CLAUDE_RETRIES (3) + advisor 1 回
        assert mock_query.call_count == 4
        advisor_options = mock_query.call_args_list[3].args[1]
        assert advisor_options.model == CLAUDE_ADVISOR_MODEL

    @patch("orchestrator.time.sleep")
    @patch("orchestrator._query_claude_sync")
    def test_call_claude_rate_limit_skips_advisor(self, mock_query, mock_sleep):
        """stderr が 429 を示す場合、advisor エスカレーションせず rate_limited=True で失敗する
        (旧実装セマンティクスの維持)。"""
        from claude_agent_sdk import ProcessError
        from orchestrator import ClaudeInvocationError, call_claude

        mock_query.side_effect = ProcessError("boom", exit_code=1, stderr="HTTP 429 rate limit")
        with pytest.raises(ClaudeInvocationError) as excinfo:
            call_claude("prompt")
        assert mock_query.call_count == 3  # advisor なし
        assert excinfo.value.rate_limited is True

    @patch("orchestrator.time.sleep")
    @patch("orchestrator._query_claude_sync")
    def test_call_claude_subscription_limit_in_error_result(self, mock_query, mock_sleep):
        """サブスク使用上限が「正常終了 + is_error 応答テキスト」で返るケースを
        rate limit として扱う (旧 stderr スニッフィングでは検出漏れだった新規捕捉)。"""
        from orchestrator import ClaudeInvocationError, call_claude

        mock_query.return_value = _fake_result_message(
            "You've reached your usage limit. Try again later.", is_error=True, subtype="error_during_execution"
        )
        with pytest.raises(ClaudeInvocationError) as excinfo:
            call_claude("prompt")
        assert mock_query.call_count == 3  # rate limit 扱いのため advisor なし
        assert excinfo.value.rate_limited is True

    @patch("orchestrator.time.sleep")
    @patch("orchestrator._query_claude_sync")
    def test_call_claude_advisor_failure_updates_final_exception(self, mock_query, mock_sleep):
        """Sonnet 通常失敗 ×3 の後 advisor だけ usage limit で失敗した場合、
        最終例外は advisor 側の失敗 (rate_limited=True) を反映する (Codex Pass 1 P2-1)。"""
        from claude_agent_sdk import ProcessError
        from orchestrator import ClaudeInvocationError, call_claude

        mock_query.side_effect = [
            ProcessError("boom", exit_code=1, stderr="err"),
            ProcessError("boom", exit_code=1, stderr="err"),
            ProcessError("boom", exit_code=1, stderr="err"),
            _fake_result_message(
                "You've reached your usage limit. Try again later.",
                is_error=True,
                subtype="error_during_execution",
            ),
        ]
        with pytest.raises(ClaudeInvocationError) as excinfo:
            call_claude("prompt")
        assert mock_query.call_count == 4  # 3 リトライ + advisor 1 回
        assert excinfo.value.rate_limited is True

    @patch("orchestrator.time.sleep")
    @patch("orchestrator._query_claude_sync")
    def test_call_claude_no_result_message_is_retried(self, mock_query, mock_sleep):
        """ResultMessage なしのストリーム終端は即時 abort せず
        リトライ共通経路に正規化される (Codex Pass 1 P2-2)。"""
        from orchestrator import ClaudeInvocationError, call_claude

        mock_query.side_effect = [
            ClaudeInvocationError("no ResultMessage received from Agent SDK"),
            _fake_result_message("ok"),
        ]
        assert call_claude("prompt") == "ok"
        assert mock_query.call_count == 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator._query_claude_sync")
    def test_call_claude_cli_not_found_fails_fast(self, mock_query, mock_sleep):
        """CLI 不在は環境異常のためリトライせず即時 fail する。"""
        from claude_agent_sdk import CLINotFoundError
        from orchestrator import ClaudeInvocationError, call_claude

        mock_query.side_effect = CLINotFoundError("claude not found")
        with pytest.raises(ClaudeInvocationError):
            call_claude("prompt")
        assert mock_query.call_count == 1
        mock_sleep.assert_not_called()

    @patch("orchestrator._query_claude_sync")
    def test_call_claude_accumulates_cost_from_result_message(self, mock_query):
        from orchestrator import call_claude

        mock_query.return_value = _fake_result_message(
            "ok",
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 40,
            },
            total_cost_usd=0.25,
        )
        cost_acc = {
            "total_cost_usd": 0.0,
            "total_tokens_used": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        }
        call_claude("prompt", cost_acc=cost_acc)
        assert cost_acc["total_cost_usd"] == 0.25
        assert cost_acc["total_tokens_used"] == 200
        assert cost_acc["total_input_tokens"] == 100
        assert cost_acc["total_output_tokens"] == 50


def _real_result_message(result_text: str, *, is_error: bool = False, subtype: str = "success"):
    """isinstance(message, ResultMessage) を通す必要がある _sdk_query 層テスト用の本物の ResultMessage。

    _query_claude_sync を patch する上位層テストは _fake_result_message (SimpleNamespace) で足りるが、
    _query_claude_sync 自体のストリーム消費を検証するにはスタブでは通らない (T22)。
    """
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype=subtype,
        duration_ms=1000,
        duration_api_ms=800,
        is_error=is_error,
        num_turns=1,
        session_id="test-session",
        result=result_text,
        usage={"input_tokens": 10, "output_tokens": 5},
    )


def _fake_sdk_stream(messages, tail_exc=None):
    """_sdk_query 互換のフェイク async generator を返す。

    tail_exc は全 message を yield し切った後に raise する。実 SDK が
    「is_error result → CLI の意図的な非ゼロ終了 → エラーフレームを素の Exception として
    raise」する挙動 (SDK query.py receive_messages) を模す。ResultMessage で即 return
    していれば tail_exc には到達しない。
    """

    async def _gen(*, prompt, options):
        for m in messages:
            yield m
        if tail_exc is not None:
            raise tail_exc

    return _gen


class TestQueryClaudeSync:
    """_query_claude_sync のストリーム消費セマンティクスのテスト (T22)。

    exec-20260711154440-67fed1d5 の障害対応: ストリームを最後まで回すと、is_error result 後の
    意図的な非ゼロ終了 (エラーフレーム = 素の Exception) が捕捉済み ResultMessage を破棄し、
    is_error 分岐 (usage-limit 検出 / 型付きリトライ) が全て素通しになっていた。
    """

    def test_returns_result_message_before_trailing_error_frame(self):
        """is_error ResultMessage の後にエラーフレーム Exception が控えていても、
        ResultMessage 受信時点で即 return し例外に到達しない。"""
        from orchestrator import _build_claude_options, _query_claude_sync

        msg = _real_result_message("Claude AI usage limit reached|1783785000", is_error=True)
        stream = _fake_sdk_stream(
            [msg], tail_exc=Exception("Claude Code returned an error result: success")
        )
        with patch("orchestrator._sdk_query", stream):
            result = _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
        assert result is msg

    def test_early_return_closes_stream_generator(self):
        """ResultMessage での早期 return 後も aclosing により generator の finally
        (= SDK 側 transport cleanup 相当) が実行される (Codex Pass 1 P2-1/P2-2)。"""
        from orchestrator import _build_claude_options, _query_claude_sync

        cleanup = {"ran": False}
        msg = _real_result_message("ok")

        async def _gen(*, prompt, options):
            try:
                yield msg
                yield _real_result_message("should not be consumed")
            finally:
                cleanup["ran"] = True

        with patch("orchestrator._sdk_query", _gen):
            result = _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
        assert result is msg
        assert cleanup["ran"] is True

    def test_stream_error_before_result_is_normalized(self):
        """ResultMessage 到達前の素の Exception (エラーフレーム) は ClaudeInvocationError に
        正規化され、リトライ共通経路に乗る。"""
        from orchestrator import ClaudeInvocationError, _build_claude_options, _query_claude_sync

        stream = _fake_sdk_stream([], tail_exc=Exception("Fatal error in message reader"))
        with patch("orchestrator._sdk_query", stream), pytest.raises(ClaudeInvocationError) as excinfo:
            _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
        assert "Fatal error in message reader" in excinfo.value.stderr

    def test_sdk_typed_errors_propagate_unwrapped(self):
        """ClaudeSDKError 系 (CLINotFoundError 等) はラップせず素通しし、
        上位の即時 fail / stderr 429 判定を温存する。"""
        from claude_agent_sdk import CLINotFoundError
        from orchestrator import _build_claude_options, _query_claude_sync

        stream = _fake_sdk_stream([], tail_exc=CLINotFoundError("claude not found"))
        with patch("orchestrator._sdk_query", stream), pytest.raises(CLINotFoundError):
            _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))

    def test_stream_end_without_result_raises_invocation_error(self):
        """正常終端で ResultMessage が 1 件もない場合は従来どおり ClaudeInvocationError。"""
        from orchestrator import ClaudeInvocationError, _build_claude_options, _query_claude_sync

        stream = _fake_sdk_stream([])
        with patch("orchestrator._sdk_query", stream), pytest.raises(ClaudeInvocationError) as excinfo:
            _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
        assert "no ResultMessage" in str(excinfo.value)

    @patch("orchestrator.time.sleep")
    def test_call_claude_usage_limit_with_nonzero_exit_is_rate_limited(self, mock_sleep):
        """本番障害の end-to-end 再現: usage limit の is_error result + 非ゼロ終了が
        素の Exception ではなく rate_limited=True の ClaudeInvocationError に分類され、
        advisor エスカレーションもしない。"""
        from orchestrator import ClaudeInvocationError, call_claude

        msg = _real_result_message("Claude AI usage limit reached|1783785000", is_error=True)
        stream = _fake_sdk_stream(
            [msg], tail_exc=Exception("Claude Code returned an error result: success")
        )
        with patch("orchestrator._sdk_query", stream), pytest.raises(ClaudeInvocationError) as excinfo:
            call_claude("prompt")
        assert excinfo.value.rate_limited is True

    @patch("orchestrator.time.sleep")
    def test_call_claude_stream_429_error_frame_is_rate_limited(self, mock_sleep):
        """途中失敗のエラーフレーム (SDK reader が ProcessError を変換したケース) でも
        stderr 429 判定が効き rate_limited=True で失敗する (T22 補強)。"""
        from orchestrator import ClaudeInvocationError, call_claude

        stream = _fake_sdk_stream([], tail_exc=Exception("API Error: 429 Too Many Requests"))
        with patch("orchestrator._sdk_query", stream), pytest.raises(ClaudeInvocationError) as excinfo:
            call_claude("prompt")
        assert excinfo.value.rate_limited is True


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
    """_parse_claude_response 関数のテスト (厳密契約: dict を返すか ClaudeResponseParseError)。

    20260706 Agent SDK 移行で入力は「モデルの最終応答テキスト」になった。
    旧 envelope パース (CLI stdout JSON の result キー取り出し) のテストは削除済み。
    """

    def test_parse_json_code_block(self):
        from orchestrator import _parse_claude_response

        text = '```json\n{"category": "時事"}\n```'
        result = _parse_claude_response(text)
        assert result["category"] == "時事"

    def test_parse_generic_code_block(self):
        from orchestrator import _parse_claude_response

        text = '```\n{"category": "学術"}\n```'
        result = _parse_claude_response(text)
        assert result["category"] == "学術"

    def test_parse_direct_json(self):
        from orchestrator import _parse_claude_response

        text = '{"category": "ビジネス"}'
        result = _parse_claude_response(text)
        assert result["category"] == "ビジネス"

    def test_parse_json_after_preamble(self):
        """前置き文の後にJSONが来るケースを正しく抽出できること（戦略4）"""
        from orchestrator import _parse_claude_response

        text = '以下が調査結果です。\n\n{"step_id": "r-1", "summary": "概要", "sources": [], "extra": "data"}'
        result = _parse_claude_response(text)
        assert result["step_id"] == "r-1"
        assert result["summary"] == "概要"

    def test_parse_json_with_trailing_content(self):
        """JSONの後に余分なテキストがある場合も正しく抽出できること（戦略4 raw_decode）"""
        from orchestrator import _parse_claude_response

        text = (
            '{"step_id": "r-2", "summary": "summary text", "sources": [], "extra": "val"}\n\n'
            "以上が結果です。ご確認ください。"
        )
        result = _parse_claude_response(text)
        assert result["step_id"] == "r-2"

    def test_parse_ignores_small_json_fragments(self):
        """キー数2以下の断片的JSONオブジェクトを無視して大きなJSONを返すこと"""
        from orchestrator import _parse_claude_response

        text = (
            'The format is {"type": "text"}. '
            "Here is the full output: "
            '{"step_id": "r-3", "summary": "full result", "sources": [], "extra": "more"}'
        )
        result = _parse_claude_response(text)
        assert result.get("step_id") == "r-3"

    def test_parse_raises_when_no_json(self):
        """JSON が全く見つからない場合は ClaudeResponseParseError を送出すること
        (旧: {"raw_text": ..., "parse_error": True} を黙って返していた)"""
        from orchestrator import ClaudeResponseParseError, _parse_claude_response

        with pytest.raises(ClaudeResponseParseError) as excinfo:
            _parse_claude_response("JSONを含まない純粋なテキストです。")
        assert excinfo.value.text_preview

    def test_parse_raises_on_json_array(self):
        """valid JSON でも dict でない値 (array/scalar) は返さず raise すること
        (旧実装の型契約違反経路の根絶)"""
        from orchestrator import ClaudeResponseParseError, _parse_claude_response

        with pytest.raises(ClaudeResponseParseError):
            _parse_claude_response("[]")
        with pytest.raises(ClaudeResponseParseError):
            _parse_claude_response('"just a string"')
        with pytest.raises(ClaudeResponseParseError):
            _parse_claude_response("123")

    def test_parse_raises_on_non_str_input(self):
        from orchestrator import ClaudeResponseParseError, _parse_claude_response

        with pytest.raises(ClaudeResponseParseError):
            _parse_claude_response(None)  # type: ignore[arg-type]

    def test_parse_fenced_array_falls_through_to_outer_dict(self):
        """フェンス内が array でも、フェンス外に dict があれば戦略4で拾うこと"""
        from orchestrator import _parse_claude_response

        text = '```json\n[1, 2]\n```\n{"step_id": "r-4", "summary": "s", "sources": [], "extra": 1}'
        result = _parse_claude_response(text)
        assert result["step_id"] == "r-4"


class TestRunResearchers:
    """リサーチャー並列実行のテスト"""

    @patch("orchestrator.call_claude")
    def test_parallel_execution_all_success(self, mock_claude):
        from orchestrator import Orchestrator

        mock_claude.return_value = json.dumps({"step_id": "r-1", "summary": "結果", "sources": []})

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
                return json.dumps({"step_id": "r-1", "summary": "ok", "sources": []})
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
    @patch("orchestrator.call_codex")
    def test_review_passes_first_time(self, mock_codex, mock_claude):
        from orchestrator import Orchestrator

        mock_codex.return_value = json.dumps(
            {
                "passed": True,
                "issues": [],
                "quality_metadata": {"sources_verified": 5, "sources_unverified": 0},
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
        assert mock_codex.call_count == 1
        assert mock_claude.call_count == 0

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_review_loop_fix_then_pass(self, mock_codex, mock_claude):
        from orchestrator import Orchestrator

        # reviewer (call_codex): 1回目 不合格 → 2回目 合格
        mock_codex.side_effect = [
            json.dumps(
                {
                    "passed": False,
                    "issues": [
                        {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
                    ],
                    "quality_metadata": {},
                }
            ),
            json.dumps(
                {
                    "passed": True,
                    "issues": [],
                    "quality_metadata": {"sources_verified": 5, "sources_unverified": 0},
                }
            ),
        ]
        # fixer (call_claude): 修正後の成果物
        mock_claude.side_effect = [json.dumps({"content_blocks": [], "summary": "fixed"})]

        slack = MagicMock()
        db = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")

        result, final_deliverables = orch._run_review_loop(
            "prompt", {"content_blocks": []}, [], "技術", "gen_prompt", "C1", "ts1"
        )
        assert result["passed"] is True
        assert final_deliverables.get("summary") == "fixed"
        assert mock_codex.call_count == 2
        assert mock_claude.call_count == 1

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_run_review_loop_returns_fixed_deliverables_on_passed(self, mock_codex, mock_claude):
        """修正後に合格した場合、修正済み成果物を返す"""
        from orchestrator import Orchestrator

        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps(
                {
                    "passed": False,
                    "issues": [
                        {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
                    ],
                    "quality_metadata": {},
                }
            ),
            json.dumps(
                {
                    "passed": True,
                    "issues": [],
                    "quality_metadata": {"sources_verified": 3, "sources_unverified": 0},
                }
            ),
        ]
        # fixer (call_claude): 修正版成果物
        mock_claude.side_effect = [json.dumps({"content_blocks": [{"t": "fixed"}], "summary": "修正版"})]

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
        assert result["passed"] is True
        assert final_deliverables["content_blocks"] == [{"t": "fixed"}]
        assert final_deliverables["summary"] == "修正版"

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_run_review_loop_returns_fixed_deliverables_on_max_loop(self, mock_codex, mock_claude):
        """ループ上限到達でも、最後に適用された修正版を返す"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        failing_review = {"passed": False, "issues": [issue], "quality_metadata": {}}

        # reviewer (call_codex): 3 回とも不合格
        mock_codex.side_effect = [
            json.dumps(failing_review),
            json.dumps(failing_review),
            json.dumps(failing_review),
        ]
        # fixer (call_claude): 2 回の修正
        mock_claude.side_effect = [
            json.dumps({"content_blocks": [], "summary": "fix-1"}),
            json.dumps({"content_blocks": [], "summary": "fix-2"}),
        ]

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original = {"content_blocks": [], "summary": "初版"}
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
        assert result["passed"] is False
        assert final_deliverables["summary"] == "fix-2"
        notes = result["quality_metadata"]["notes"]
        assert any("レビュー修正上限" in n for n in notes)

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_run_review_loop_keeps_previous_on_parse_error(self, mock_codex, mock_claude):
        """fix 応答が parse_error の場合、前回成果物を保持する"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        failing_review = {"passed": False, "issues": [issue], "quality_metadata": {}}
        passing_review = {
            "passed": True,
            "issues": [],
            "quality_metadata": {"sources_verified": 1, "sources_unverified": 0},
        }

        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps(failing_review),
            json.dumps(passing_review),
        ]
        # fixer (call_claude): 不正な JSON → parse_error になる
        mock_claude.side_effect = ["this is not valid json at all"]

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
        assert result["passed"] is True
        # parse_error のため original を保持
        assert final_deliverables == original

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_review_loop_preserves_code_files_on_fix_success(self, mock_codex, mock_claude):
        """レビュー修正成功時も code_files は保持される（generator は text のみ返す契約のため）"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {
                    "passed": True,
                    "issues": [],
                    "quality_metadata": {"sources_verified": 1, "sources_unverified": 0},
                }
            ),
        ]
        # fixer (call_claude): 修正レスポンスは text 成果物のみ（code_files を含まない = 実運用の挙動）
        mock_claude.side_effect = [json.dumps({"content_blocks": [{"t": "fixed"}], "summary": "修正版"})]

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
    @patch("orchestrator.call_codex")
    def test_review_loop_no_code_files_when_absent(self, mock_codex, mock_claude):
        """元の成果物に code_files が無ければ、修正後にもキーが現れない"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps(
                {
                    "passed": True,
                    "issues": [],
                    "quality_metadata": {"sources_verified": 1, "sources_unverified": 0},
                }
            ),
        ]
        # fixer (call_claude): 修正版
        mock_claude.side_effect = [json.dumps({"content_blocks": [{"t": "fixed"}], "summary": "修正版"})]

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
        assert result["passed"] is True
        assert "code_files" not in final_deliverables

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_review_loop_preserves_code_files_across_multiple_fixes(self, mock_codex, mock_claude):
        """複数回の修正を跨いでも code_files は保持される"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "wrong", "fix_instruction": "fix"}
        failing_review = {"passed": False, "issues": [issue], "quality_metadata": {}}

        # reviewer (call_codex): 3 回とも不合格
        mock_codex.side_effect = [
            json.dumps(failing_review),
            json.dumps(failing_review),
            json.dumps(failing_review),
        ]
        # fixer (call_claude): 2 回の修正
        mock_claude.side_effect = [
            json.dumps({"content_blocks": [], "summary": "fix-1"}),
            json.dumps({"content_blocks": [], "summary": "fix-2"}),
        ]

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")

        original_code_files = {"files": {"main.tf": 'resource "x" "y" {}'}, "readme_content": "readme"}
        original = {"content_blocks": [], "summary": "初版", "code_files": original_code_files}
        result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")
        assert result["passed"] is False
        assert final_deliverables["summary"] == "fix-2"
        assert final_deliverables["code_files"] == original_code_files

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_review_loop_code_issue_does_not_claim_fix_in_summary(self, mock_codex, mock_claude):
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
        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [code_issue], "quality_metadata": {}}),
            json.dumps({"passed": True, "issues": [], "quality_metadata": {}}),
        ]
        # fixer (call_claude): コード指摘には修正主張せず notes に記録
        mock_claude.side_effect = [
            json.dumps(
                {
                    "content_blocks": [{"t": "unchanged"}],
                    "summary": "Route 53 Resolver の概要レポート",
                    "quality_metadata": {"notes": ["コード関連指摘 1 件は本ループ未修正"]},
                }
            ),
        ]

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

        # fix call (call_claude の唯一の呼び出し = fixer) の prompt にスコープ制約セクションが含まれていること
        fix_call_prompt = mock_claude.call_args_list[0].args[0]
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
    @patch("orchestrator.call_codex")
    def test_review_loop_text_issue_updates_text_normally(self, mock_codex, mock_claude):
        """テキスト関連指摘では、従来通り summary / content_blocks が更新される (回帰なし)"""
        from orchestrator import Orchestrator

        text_issue = {
            "item": "セクション 3 の表現",
            "severity": "error",
            "description": "数値 $100 億 が出典 [3] と矛盾 ($85 億)",
            "fix_instruction": "セクション 3 の市場規模を $85 億 に修正",
        }
        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [text_issue], "quality_metadata": {}}),
            json.dumps({"passed": True, "issues": [], "quality_metadata": {}}),
        ]
        # fixer (call_claude): テキスト修正版
        mock_claude.side_effect = [
            json.dumps(
                {
                    "content_blocks": [{"t": "fixed-section-3"}],
                    "summary": "市場規模を $85 億 に更新済みの最新版",
                }
            ),
        ]

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
    @patch("orchestrator.call_codex")
    def test_review_loop_merges_fixer_notes_into_review_quality_metadata(self, mock_codex, mock_claude):
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
        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps(
                {"passed": False, "issues": [code_issue], "quality_metadata": {"notes": []}}
            ),
            json.dumps({"passed": True, "issues": [], "quality_metadata": {"notes": []}}),
        ]
        # fixer (call_claude): notes に未修正記録
        mock_claude.side_effect = [
            json.dumps(
                {
                    "content_blocks": [{"t": "unchanged"}],
                    "summary": "Route 53 概要",
                    "quality_metadata": {"notes": ["コード関連指摘 1 件は本ループ未修正"]},
                }
            ),
        ]

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
    @patch("orchestrator.call_codex")
    def test_review_loop_accumulates_fixer_notes_across_multiple_fixes(self, mock_codex, mock_claude):
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
        # reviewer (call_codex): 1回目コード error → 2回目テキスト error → 3回目 pass
        mock_codex.side_effect = [
            # 1 回目 review: コード関連 error
            json.dumps(
                {"passed": False, "issues": [code_issue], "quality_metadata": {"notes": []}}
            ),
            # 2 回目 review: 別のテキスト error (code error は前回未修正のまま)
            json.dumps(
                {"passed": False, "issues": [text_issue], "quality_metadata": {"notes": []}}
            ),
            # 3 回目 review: pass を返す (reviewer は fixer notes を echo しない)
            json.dumps({"passed": True, "issues": [], "quality_metadata": {"notes": []}}),
        ]
        # fixer (call_claude): 2 回の修正
        mock_claude.side_effect = [
            # 1 回目 fix: fixer がスコープ制約に従い notes に未修正を記録
            json.dumps(
                {
                    "content_blocks": [{"t": "after-fix-1"}],
                    "summary": "Route 53 概要",
                    "quality_metadata": {"notes": ["コード関連指摘 1 件は本ループ未修正"]},
                }
            ),
            # 2 回目 fix: テキスト指摘に対応、notes は空 (1 回目の note を引き継がない応答)
            json.dumps(
                {
                    "content_blocks": [{"t": "after-fix-2"}],
                    "summary": "市場規模 $85 億 に更新済み",
                    "quality_metadata": {"notes": []},
                }
            ),
        ]

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
    @patch("orchestrator.call_codex")
    def test_review_loop_safely_skips_null_quality_metadata_in_fixer_response(self, mock_codex, mock_claude):
        """fixer 応答の quality_metadata が null でも _accumulate_fixer_notes は raise せず safely skip する
        (Codex review 3 回目 P2 対応 — malformed LLM 応答でも review loop が abort しない)。
        """
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps({"passed": True, "issues": [], "quality_metadata": {}}),
        ]
        # fixer (call_claude) が malformed: quality_metadata が null
        mock_claude.side_effect = [
            json.dumps(
                {"content_blocks": [{"t": "fixed"}], "summary": "OK", "quality_metadata": None}
            ),
        ]

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

        # raise しないこと
        result, _ = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        assert result["passed"] is True
        # notes は空または存在しても corrupt していない (str のみで構成される)
        notes = result.get("quality_metadata", {}).get("notes", [])
        assert all(isinstance(n, str) for n in notes)

    @patch("orchestrator.call_claude")
    @patch("orchestrator.call_codex")
    def test_review_loop_safely_skips_scalar_notes_in_fixer_response(self, mock_codex, mock_claude):
        """fixer 応答の quality_metadata.notes が文字列 (scalar) でも、文字単位で
        accumulated に追加されず safely skip する (Notion/DynamoDB の corrupt 防止)。
        """
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps({"passed": True, "issues": [], "quality_metadata": {}}),
        ]
        # fixer (call_claude) が malformed: notes が文字列
        mock_claude.side_effect = [
            json.dumps(
                {
                    "content_blocks": [{"t": "fixed"}],
                    "summary": "OK",
                    "quality_metadata": {"notes": "コード関連指摘 1 件は本ループ未修正"},
                }
            ),
        ]

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
    @patch("orchestrator.call_codex")
    def test_review_loop_safely_skips_int_notes_in_fixer_response(self, mock_codex, mock_claude):
        """fixer 応答の quality_metadata.notes が整数でも raise せず safely skip する。"""
        from orchestrator import Orchestrator

        issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
        # reviewer (call_codex): 不合格 → 合格
        mock_codex.side_effect = [
            json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
            json.dumps({"passed": True, "issues": [], "quality_metadata": {}}),
        ]
        # fixer (call_claude) が malformed: notes が整数
        mock_claude.side_effect = [
            json.dumps(
                {
                    "content_blocks": [{"t": "fixed"}],
                    "summary": "OK",
                    "quality_metadata": {"notes": 5},
                }
            ),
        ]

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
    # call_codex は生 JSON 文字列を返し、call_claude は最終応答テキスト (フェンス付き or 生 JSON) を返す。
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
        mock_claude.return_value = json.dumps({"summary": "fixed summary only"})

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
        mock_claude.return_value = json.dumps({"content_blocks": None, "summary": "fixed"})

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
        mock_claude.return_value = json.dumps({"content_blocks": [], "summary": "fixed"})

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
        mock_claude.return_value = json.dumps({"content_blocks": "not a list", "summary": "fixed"})

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
        mock_claude.return_value = json.dumps({"content_blocks": [{"t": "fixer-version"}], "summary": "fixed"})

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
        mock_claude.return_value = json.dumps({"content_blocks": None, "summary": "fixed"})

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

        20260706 Agent SDK 移行で `_parse_claude_response` は厳密契約 (dict or raise) になり、
        旧「非 dict をそのまま返す」経路は消滅した。本テストは非 dict 応答が
        ClaudeResponseParseError → unparseable 分岐で旧版保持されることを検証する。
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
        # fixer が JSON array を返す (非 dict)。厳密契約化後の `_parse_claude_response` は
        # `[]` を返さず ClaudeResponseParseError を送出し、unparseable 分岐に集約される
        # (20260706 Agent SDK 移行: 旧「非 dict をそのまま返す」経路の根絶)。
        mock_claude.return_value = "[]"

        orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
        original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

        with caplog.at_level(logging.WARNING, logger="catch-expander-agent"):
            # AttributeError を起こさず loop が完走すること
            result, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

        # 旧版 deliverables が維持されること
        assert final["content_blocks"] == [{"t": "original"}]
        assert final["summary"] == "初版"
        # 適切な warning が出ていること (非 dict も unparseable に集約された)
        unparseable_records = [
            r for r in caplog.records
            if "Fix attempt produced unparseable response" in r.getMessage()
        ]
        assert len(unparseable_records) == 1

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
        # fixer が JSON 抽出不能な応答を返す (ClaudeResponseParseError → 旧版保持)
        mock_claude.return_value = "this is not valid json at all"

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
        mock_claude.return_value = json.dumps({"content_blocks": None, "summary": "fixed"})

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

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_codex")
    @patch("orchestrator.call_claude_with_workspace")
    @patch("orchestrator.call_claude")
    def test_code_generation_per_type_merges_files(
        self, mock_call_claude, mock_call_workspace, mock_call_codex, _mock_load, _mock_ws
    ):
        """iac_code と program_code が個別 workspace 呼び出しでマージされる"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code", "program_code"])
        text_deliverables = {"content_blocks": [], "summary": "完成"}
        # reviewer は call_codex 経由
        mock_call_codex.return_value = json.dumps({"passed": True, "issues": [], "quality_metadata": {}})

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
        # text 系は call_claude 経由 (review は call_codex に移動したため含めない)
        mock_call_claude.side_effect = [
            json.dumps(analysis),
            json.dumps(workflow),
            json.dumps(research),
            json.dumps(text_deliverables),
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

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_codex")
    @patch("orchestrator.call_claude_with_workspace")
    @patch("orchestrator.call_claude")
    def test_code_generation_partial_failure_keeps_successful_types(
        self, mock_call_claude, mock_call_workspace, mock_call_codex, _mock_load, _mock_ws
    ):
        """一方のタイプが workspace 失敗でも、もう一方は push される + Slack 部分失敗通知"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code", "program_code"])
        text_deliverables = {"content_blocks": [], "summary": "完成"}
        # reviewer は call_codex 経由
        mock_call_codex.return_value = json.dumps({"passed": True, "issues": [], "quality_metadata": {}})

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
            json.dumps(analysis),
            json.dumps(workflow),
            json.dumps(research),
            json.dumps(text_deliverables),
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

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_codex")
    @patch("orchestrator.call_claude_with_workspace")
    @patch("orchestrator.call_claude")
    def test_generator_code_files_always_discarded(
        self, mock_call_claude, mock_call_workspace, mock_call_codex, _mock_load, _mock_ws
    ):
        """ジェネレーターが誤って code_files を返しても、workspace 経由の値で上書きされる"""
        from orchestrator import Orchestrator

        analysis, workflow, research = self._make_analysis_and_workflow(["iac_code"])
        text_deliverables = {
            "content_blocks": [],
            "code_files": {"files": {"LEAK.tf": "# must not be used"}, "readme_content": ""},
            "summary": "完成",
        }
        # reviewer は call_codex 経由
        mock_call_codex.return_value = json.dumps({"passed": True, "issues": [], "quality_metadata": {}})

        mock_call_workspace.side_effect = [
            (
                "Wrote: main.tf",
                {"main.tf": "# iac"},
                {"files_kind": "valid", "files_count": 1, "files_total_bytes": 6, "rejected": []},
            ),
        ]
        mock_call_claude.side_effect = [
            json.dumps(analysis),
            json.dumps(workflow),
            json.dumps(research),
            json.dumps(text_deliverables),
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
        """run() を最後まで通すための最小限の Claude 応答列

        20260706-preference-scope: reviewer は call_codex 経由のため review 応答は
        ここに含めない (feedback_test_patches_call_codex_and_claude)。
        """
        analysis = {"category": "技術", "intent": "学習", "perspectives": [], "deliverable_types": ["research_report"]}
        workflow = {
            "research_steps": [{"step_id": "r-1", "step_name": "概要", "description": "概要調査", "search_hints": []}],
            "generate_steps": [{"step_id": "g-1", "step_name": "レポート", "deliverable_type": "research_report"}],
            "storage_targets": ["notion"],
        }
        research = {"step_id": "r-1", "summary": "概要", "sources": []}
        deliverables = {"content_blocks": [], "code_files": None, "summary": "完成"}
        # Agent SDK 移行後の call_claude は素のテキスト（bare JSON / フェンス付き JSON）を返す
        return [
            json.dumps(analysis, ensure_ascii=False),
            json.dumps(workflow, ensure_ascii=False),
            json.dumps(research, ensure_ascii=False),
            json.dumps(deliverables, ensure_ascii=False),
        ]

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_codex")
    @patch("orchestrator.call_claude")
    def test_learned_preferences_included_in_prompts(self, mock_call_claude, mock_call_codex, _mock_load, _mock_ws):
        """learned_preferences が1件以上ある場合、最初の call_claude 呼び出しに好みが含まれる"""
        from orchestrator import Orchestrator

        mock_call_claude.side_effect = self._make_minimal_responses()
        mock_call_codex.return_value = json.dumps({"passed": True, "issues": [], "quality_metadata": {}})

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

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_codex")
    @patch("orchestrator.call_claude")
    def test_empty_learned_preferences_not_included_in_prompts(
        self, mock_call_claude, mock_call_codex, _mock_load, _mock_ws
    ):
        """learned_preferences が空の場合、profile_text に好みセクションが含まれない"""
        from orchestrator import Orchestrator

        mock_call_claude.side_effect = self._make_minimal_responses()
        mock_call_codex.return_value = json.dumps({"passed": True, "issues": [], "quality_metadata": {}})

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


class TestRenderPrefsSections:
    """20260706-preference-scope: 段階的絞り込みの各レンダラー (design §3.1)"""

    GENERAL = {"text": "結論を先に簡潔に書く", "created_at": "2026-01-01T00:00:00Z"}
    CODE_SCOPED = {
        "text": "コードはmodule分割する",
        "created_at": "2026-01-01T00:00:00Z",
        "scope": {"categories": [], "deliverables": ["code"]},
    }
    JIJI_SCOPED = {
        "text": "複数の政治的立場の出典を併記する",
        "created_at": "2026-01-01T00:00:00Z",
        "scope": {"categories": ["時事"], "deliverables": []},
    }

    def test_analysis_includes_general_only(self):
        from orchestrator import _render_prefs_for_analysis

        section = _render_prefs_for_analysis([self.GENERAL, self.CODE_SCOPED, self.JIJI_SCOPED])
        assert "結論を先に簡潔に書く" in section
        assert "module分割" not in section
        assert "政治的立場" not in section
        assert "必ず反映" in section

    def test_analysis_empty_when_no_general(self):
        from orchestrator import _render_prefs_for_analysis

        assert _render_prefs_for_analysis([self.CODE_SCOPED]) == ""

    def test_workflow_splits_deliverable_scoped_into_soft_section(self):
        from orchestrator import _render_prefs_for_workflow

        section = _render_prefs_for_workflow([self.GENERAL, self.CODE_SCOPED, self.JIJI_SCOPED], "時事")
        # 汎用 + カテゴリ一致は反映指示
        assert "結論を先に簡潔に書く" in section
        assert "政治的立場" in section
        # 成果物スコープ付きはラベル + 緩和文言の別小節
        assert "[コード] コードはmodule分割する" in section
        assert "該当する成果物を選ぶ場合" in section

    def test_workflow_drops_category_mismatch(self):
        from orchestrator import _render_prefs_for_workflow

        section = _render_prefs_for_workflow([self.JIJI_SCOPED], "技術")
        assert section == ""

    def test_workflow_unknown_category_falls_back_to_general_only(self):
        from orchestrator import _render_prefs_for_workflow

        section = _render_prefs_for_workflow([self.GENERAL, self.JIJI_SCOPED], "不明カテゴリ")
        assert "結論を先に簡潔に書く" in section
        assert "政治的立場" not in section

    def test_generation_full_filter_excludes_code_scope_for_report(self):
        """受け入れ条件 1: 時事 research_report の生成に code スコープ好みが混入しない"""
        from orchestrator import _render_prefs_for_generation

        section = _render_prefs_for_generation(
            [self.GENERAL, self.CODE_SCOPED, self.JIJI_SCOPED], "時事", ["research_report"]
        )
        assert "module分割" not in section
        # 汎用 + カテゴリ一致はラベル付きで注入され、強い命令を維持
        assert "[汎用] 結論を先に簡潔に書く" in section
        assert "[時事] 複数の政治的立場の出典を併記する" in section
        assert "必ず反映" in section

    def test_generation_code_scope_applies_to_both_code_types(self):
        from orchestrator import _render_prefs_for_generation

        for code_type in ("iac_code", "program_code"):
            section = _render_prefs_for_generation([self.CODE_SCOPED], "技術", [code_type])
            assert "[コード] コードはmodule分割する" in section

    def test_generation_category_mismatch_drops_pref(self):
        from orchestrator import _render_prefs_for_generation

        section = _render_prefs_for_generation([self.JIJI_SCOPED], "技術", ["research_report"])
        assert section == ""

    def test_generation_empty_types_yields_general_nothing(self):
        from orchestrator import _render_prefs_for_generation

        assert _render_prefs_for_generation([self.GENERAL], "技術", []) == ""


class TestProgressiveNarrowingInRun:
    """run() 全体での段階的絞り込み配線テスト（受け入れ条件 1〜3）"""

    def _make_responses(self, category="時事"):
        analysis = {
            "category": category,
            "intent": "調査",
            "perspectives": [],
            "deliverable_types": ["research_report"],
        }
        workflow = {
            "research_steps": [{"step_id": "r-1", "step_name": "概要", "description": "概要調査", "search_hints": []}],
            "generate_steps": [{"step_id": "g-1", "step_name": "レポート", "deliverable_type": "research_report"}],
            "storage_targets": ["notion"],
        }
        research = {"step_id": "r-1", "summary": "概要", "sources": []}
        deliverables = {"content_blocks": [], "code_files": None, "summary": "完成"}
        # Agent SDK 移行後の call_claude は素のテキスト（bare JSON / フェンス付き JSON）を返す
        return [
            json.dumps(analysis, ensure_ascii=False),
            json.dumps(workflow, ensure_ascii=False),
            json.dumps(research, ensure_ascii=False),
            json.dumps(deliverables, ensure_ascii=False),
        ]

    def _run_orchestrator(self, mock_call_claude, mock_call_codex, prefs, category="時事"):
        from orchestrator import Orchestrator

        mock_call_claude.side_effect = self._make_responses(category)
        # reviewer は call_codex（生 JSON）経由 (feedback_test_patches_call_codex_and_claude)
        mock_call_codex.return_value = json.dumps({"passed": True, "issues": [], "quality_metadata": {}})
        db = MagicMock()
        db.get_user_profile.return_value = {
            "user_id": "U1",
            "role": "エンジニア",
            "learned_preferences": prefs,
        }
        db._table.return_value = MagicMock()
        slack = MagicMock()
        orch = Orchestrator(slack, db, "token", "db_id", "gh_token", "owner/repo")
        orch.notion = MagicMock()
        orch.notion.create_page.return_value = ("https://notion.so/page", "page-id")
        orch.github = MagicMock()
        orch.run("exec-1", "U1", "国際情勢まとめ", "C1", "ts1")
        return mock_call_claude.call_args_list

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_codex")
    @patch("orchestrator.call_claude")
    def test_code_scoped_pref_never_reaches_report_generation(  # gitleaks:allow (テスト名の "rt_generation" 部分文字列誤検知)
        self, mock_call_claude, mock_call_codex, _mock_load, _mock_ws
    ):
        """本障害の再現テスト: code スコープ好みは時事レポートの ① と ③ に混入しない"""
        prefs = [
            {
                "text": "コードはmodule分割する",
                "created_at": "2026-01-01T00:00:00Z",
                "scope": {"categories": [], "deliverables": ["code"]},
            },
            {"text": "結論を先に簡潔に書く", "created_at": "2026-01-01T00:00:00Z"},
        ]
        calls = self._run_orchestrator(mock_call_claude, mock_call_codex, prefs)

        analysis_prompt = calls[0][0][0]
        wf_prompt = calls[1][0][0]
        gen_prompt = calls[3][0][0]

        # ① 汎用のみ
        assert "module分割" not in analysis_prompt
        assert "結論を先に簡潔に書く" in analysis_prompt
        # ② 成果物スコープ付きは緩和文言付きで提示（成果物選択の材料）
        assert "module分割" in wf_prompt
        assert "該当する成果物を選ぶ場合" in wf_prompt
        # ③ 完全フィルタ: research_report 生成に code スコープは混入しない
        assert "module分割" not in gen_prompt
        assert "結論を先に簡潔に書く" in gen_prompt

        # プロファイル JSON 経由の素通り（フィルタ無効化）が起きていないことを保証
        assert "learned_preferences" not in gen_prompt

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_codex")
    @patch("orchestrator.call_claude")
    def test_category_scoped_pref_in_matching_category_generation(
        self, mock_call_claude, mock_call_codex, _mock_load, _mock_ws
    ):
        prefs = [
            {
                "text": "複数の政治的立場の出典を併記する",
                "created_at": "2026-01-01T00:00:00Z",
                "scope": {"categories": ["時事"], "deliverables": []},
            },
        ]
        calls = self._run_orchestrator(mock_call_claude, mock_call_codex, prefs, category="時事")

        assert "政治的立場" not in calls[0][0][0]  # ① カテゴリ未確定なので入らない
        assert "政治的立場" in calls[1][0][0]  # ② カテゴリ一致
        assert "政治的立場" in calls[3][0][0]  # ③ カテゴリ一致


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
            json.dumps(analysis),
            json.dumps(workflow),
            json.dumps(research),
            json.dumps(deliverables),
            json.dumps(review),
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

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_put_deliverable_with_github_url(self, mock_call_claude, _mock_load, _mock_ws):
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

    @patch("orchestrator._should_use_workspace_text_gen", return_value=False)
    @patch("orchestrator._load_prompt", return_value="# テスト用プロンプト")
    @patch("orchestrator.call_claude")
    def test_put_deliverable_without_github_url(self, mock_call_claude, _mock_load, _mock_ws):
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
    """Agent SDK の Write ツール経由呼び出しの sandbox 管理 + リトライ"""

    def _fake_query(self, files_to_write: dict[str, str] | None = None, result_text: str = "ok"):
        """_query_claude_sync を差し替える fake。options.cwd を見て指定ファイルを書く。"""
        from pathlib import Path

        def runner(prompt, options):
            sandbox = Path(options.cwd)
            for rel, content in (files_to_write or {}).items():
                target = sandbox / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            return _fake_result_message(result_text)

        return runner

    def test_creates_and_cleans_sandbox(self):
        from orchestrator import call_claude_with_workspace

        captured: dict = {}

        def fake_query(prompt, options):
            captured["cwd"] = options.cwd
            return _fake_result_message("")

        with patch("orchestrator._query_claude_sync", side_effect=fake_query):
            call_claude_with_workspace("p", "iac_code")

        assert captured["cwd"].startswith("/tmp/agent-output-iac_code-")
        # finally で削除済み
        from pathlib import Path

        assert not Path(captured["cwd"]).exists()

    def test_passes_cwd_in_options(self):
        from orchestrator import call_claude_with_workspace

        captured: dict = {}

        def fake_query(prompt, options):
            captured["cwd"] = options.cwd
            return _fake_result_message("")

        with patch("orchestrator._query_claude_sync", side_effect=fake_query):
            call_claude_with_workspace("p", "iac_code")

        assert captured["cwd"] is not None
        assert captured["cwd"].startswith("/tmp/agent-output-iac_code-")

    def test_includes_write_in_allowed_tools(self):
        from orchestrator import call_claude_with_workspace

        captured: dict = {}

        def fake_query(prompt, options):
            captured["allowed_tools"] = options.allowed_tools
            return _fake_result_message("")

        with patch("orchestrator._query_claude_sync", side_effect=fake_query):
            call_claude_with_workspace("p", "iac_code")

        assert "Write" in captured["allowed_tools"]
        assert "Edit" in captured["allowed_tools"]

    def test_returns_collected_files(self):
        from orchestrator import call_claude_with_workspace

        runner = self._fake_query(
            files_to_write={"main.tf": "resource ...", "README.md": "# doc"},
            result_text="Wrote: main.tf, README.md",
        )

        with patch("orchestrator._query_claude_sync", side_effect=runner):
            raw, files, outcome = call_claude_with_workspace("p", "iac_code")

        assert raw == "Wrote: main.tf, README.md"
        assert files == {"main.tf": "resource ...", "README.md": "# doc"}
        assert outcome["files_kind"] == "valid"

    def test_retries_on_process_error(self):
        from claude_agent_sdk import ProcessError
        from orchestrator import call_claude_with_workspace

        call_count = {"n": 0}

        def fake_query(prompt, options):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise ProcessError("boom", exit_code=1, stderr="err")
            return _fake_result_message("")

        with (
            patch("orchestrator._query_claude_sync", side_effect=fake_query),
            patch("orchestrator.time.sleep"),
        ):
            call_claude_with_workspace("p", "iac_code")

        assert call_count["n"] == 2

    def test_advisor_gets_read_tool_after_retries_exhausted(self):
        """sonnet 枯渇後の advisor 試行では allowed_tools に Read が追加される (旧 CLI 実装踏襲)"""
        from claude_agent_sdk import ProcessError
        from orchestrator import CLAUDE_ADVISOR_MODEL, call_claude_with_workspace

        calls: list = []

        def fake_query(prompt, options):
            calls.append(options)
            if len(calls) <= 3:
                raise ProcessError("boom", exit_code=1, stderr="err")
            return _fake_result_message("")

        with (
            patch("orchestrator._query_claude_sync", side_effect=fake_query),
            patch("orchestrator.time.sleep"),
        ):
            call_claude_with_workspace("p", "iac_code")

        assert len(calls) == 4
        advisor_options = calls[3]
        assert advisor_options.model == CLAUDE_ADVISOR_MODEL
        assert advisor_options.allowed_tools == ["Write", "Edit", "Read"]

    def test_cleans_sandbox_on_exception(self):
        from claude_agent_sdk import ProcessError
        from orchestrator import ClaudeInvocationError, call_claude_with_workspace

        captured: dict = {}

        def fake_query(prompt, options):
            captured["cwd"] = options.cwd
            raise ProcessError("boom", exit_code=1, stderr="")

        with (
            patch("orchestrator._query_claude_sync", side_effect=fake_query),
            patch("orchestrator.time.sleep"),
            pytest.raises(ClaudeInvocationError),
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
