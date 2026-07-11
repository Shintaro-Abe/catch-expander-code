# Codex レビュー依頼: Claude 呼び出しの Agent SDK 移行

## 対象

**未コミットの作業ツリー**（`git diff HEAD` で確認。HEAD = e8b5469）。コミット前レビュー方式。
作業ツリーには preference-scope の Codex 是正（別レビューで 3 パス収束済み: `.audit/2026-07-06_preference-scope.md`）も
intertwined で含まれるが、本レビューの主対象は **Agent SDK 移行**。主変更ファイル:

- `src/agent/orchestrator.py` — `claude -p` subprocess を `claude-agent-sdk` の同期ファサードに置換
- `src/agent/main.py` — `ClaudeInvocationError` except 分岐追加
- `src/agent/feedback/feedback_processor.py` — 厳密パーサ契約への追随
- `src/agent/requirements.txt` / `src/agent/Dockerfile` — 依存追加・pin
- `tests/unit/agent/test_orchestrator.py` / `test_main.py` / `tests/integration/test_workflow.py` — モック移行

設計文書: `.steering/20260706-agent-sdk-migration/{requirements,design,tasklist}.md`、`docs/adr/0001-claude-agent-sdk-sync-facade.md`

## 前提（誤検知防止）

1. **Dockerfile の `npm install -g @anthropic-ai/claude-code` は意図的に残している**。Python SDK は内部で CLI subprocess を起動するため（ADR-0001 参照）。削除提案は不要
2. 認証は Secrets Manager → `~/.claude/.credentials.json` seeding + writeback を**無変更**で維持する設計
3. `call_codex`（OpenAI CLI）と `CodexInvocationError` は対象外・無変更
4. 旧 `TestReviewLoop` 等 24 テストのモック移行（reviewer→`call_codex` / text-gen→`_should_use_workspace_text_gen=False`）は**実施済み**。全スイート 525 passed（`AWS_DEFAULT_REGION=ap-northeast-1` 必須）。モック移行がアサーションを弱めていないかは観点に含めてよい
5. `endpoint_path` の `/claude/cli?...` 文字列は events テーブル / dashboard 集計キーの互換性のため意図的に旧形式を維持

## レビュー観点（優先順）

1. **戻り値契約の一貫性**: `call_claude` 系が `ResultMessage.result` テキストを返す新契約に対し、全 call site（orchestrator 6 箇所 + feedback_processor）と全テストモックが追随できているか。envelope JSON を前提とする残骸がないか
2. **`_parse_claude_response` の厳密契約**: 「dict を返すか `ClaudeResponseParseError` を投げる」に対する違反経路（非 dict を返す、例外を握り潰して None 以外を流す）が残っていないか。呼び出し側 try/except の漏れはないか
3. **リトライセマンティクスのパリティ**: `_run_claude_with_retries` が旧実装と同一の挙動（MAX_CLAUDE_RETRIES=3、指数バックオフ、sonnet 枯渇時のみ advisor 1 回、rate limit 時は advisor スキップ）を保っているか。advisor 失敗後の例外伝播は正しいか
4. **エラーマッピング**: `ProcessError` / `CLINotFoundError` / `CLIConnectionError` / `is_error` ResultMessage の分類漏れ。`subscription_limit` 検出（`_result_indicates_usage_limit`）の誤検知・見逃しパターン
5. **emit 契約の維持**: `api_call_completed`（subtype/anthropic、duration、tokens）と `rate_limit_hit` のフィールド・発火タイミングが旧実装と等価か。finally ブロックでの emit 漏れ・二重 emit はないか
6. **asyncio.run の安全性**: ThreadPoolExecutor ワーカー内での `asyncio.run` 実行に問題がないか（イベントループ競合、リソースリーク）
7. **main.py 分岐順序**: `ClaudeInvocationError` 分岐を `CalledProcessError` より先に置いたことによる意図しないマスクがないか

## 出力形式

P1（機能破壊）/ P2（品質・保守性）/ P3（軽微）で分類し、各指摘に該当ファイル・行と修正案を付けること。指摘ゼロならその旨を明記。
