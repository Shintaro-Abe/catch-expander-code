# Requirements: Claude 呼び出しの Agent SDK 移行

## 背景

エージェントコンテナ内の全 Claude 呼び出しは `claude -p` subprocess + stdout JSON パース + stderr 文字列での 429 判定で構成されている。この構造は以下の問題を生んできた:

- `_parse_claude_response` の型契約違反（6 call sites 中 5 つが同型バグ、`.steering/20260512-parse-claude-response-dict-contract` 参照）
- stderr の `"429"` 文字列スニッフィングという脆弱なレートリミット検出
- envelope JSON（`{"result": ..., "usage": ...}`）の 6 段フォールバックパースという保守負債

これを Python `claude-agent-sdk` の型付きインターフェースに置き換える。設計判断の経緯と却下代替案は `docs/adr/0001-claude-agent-sdk-sync-facade.md` に記録済み。

## 目的（優先順）

1. **構造化 I/O と型付きエラー** — `ResultMessage` / `ProcessError` による型付き処理で文字列スニッフィングを排除
2. **リトライ・レート検出の改善** — 検出層を型化し、サブスク使用上限が正常応答テキストで返るケースを新規捕捉
3. **SDK 機能活用の土台** — hooks / session 継続は本作業に含めず、後続 steering の前提を作る

## ユーザーストーリー

- 運用者として、Claude 呼び出しの失敗原因（レート上限 / プロセス異常 / パース失敗）がログ上で型として区別できてほしい。文字列マッチの取りこぼしで誤分類されたくない
- 開発者として、`call_claude` の戻り値が「何の型か」をシグネチャだけで判断できてほしい。envelope JSON か生テキストかを call site ごとに推測したくない

## 受け入れ条件

1. `orchestrator.py` の Claude 呼び出し 3 wrapper（`call_claude` / `call_claude_with_workspace` / `call_claude_with_text_workspace`）が `claude-agent-sdk` の `query()` 経由で動作する（subprocess 直叩きの排除）
2. `call_claude` は最終テキスト `str` を返す契約に変更され、`_parse_claude_response` は「dict を返すか `ClaudeResponseParseError` を投げる」厳密契約になる（非 dict を黙って返す経路の根絶）
3. リトライ回数（`MAX_CLAUDE_RETRIES=3`）・指数バックオフ・Sonnet→Opus エスカレーション・rate limit 時はエスカレーションしない、の既存セマンティクスが維持される
4. `rate_limit_hit` / `api_call_completed` イベントの emit 契約（subtype、usage トークン数）が維持される
5. 認証は現行の Secrets Manager → `~/.claude/.credentials.json` seeding + writeback が無変更で動作する（実機 ECS で読み取り・writeback 両方を確認）
6. ユニットテスト・統合テストが新契約で green（pre-existing failure 26 件は除く）
7. 実機検証: dev 環境の ECS タスクで実トピックのワークフローが完走し、CloudWatch Logs で researcher（WebSearch）/ generator（Write ツール + sandbox）/ fix loop の各経路の実動作を確認

## 制約事項

- **認証機構は変更しない** — `setup-token` 化は本作業のスコープ外（ADR-0001 の却下案参照）
- **同期ファサード維持** — orchestrator の同期構造・`ThreadPoolExecutor` 並列・既存シグネチャは不変。async 化しない
- **パリティのみ** — hooks / session 継続 / streaming は含めない
- **`call_codex`（OpenAI CLI）は無変更** — reviewer 経路は対象外
- **Dockerfile の `npm install -g @anthropic-ai/claude-code` は残す** — SDK は内部で CLI を起動する（削除は誤り。ADR-0001 に明記済み）
- 実装完了後の commit + push 後は CLAUDE.md §4 の Codex レビューゲートに従う（本 steering の承認スキップはドキュメント作成のみに適用、build / deploy の承認要件は不変）
