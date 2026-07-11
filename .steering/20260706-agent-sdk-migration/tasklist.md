# Tasklist: Claude 呼び出しの Agent SDK 移行

> **進捗ステータス (2026-07-11 更新)**: Phase 1-3 実装済み。Phase 4 のテスト移行を実施中
> （旧 21 テストが `call_codex` 未 patch / 旧 `subprocess` 前提で実 SDK に到達しハングしていた
> 問題を修正中）。commit / Codex ゲート / deploy（Phase 5）は未着手・ユーザー承認待ち。
> preference-scope steering と作業ツリー上で intertwined（orchestrator.py / main.py を共有）。

## Phase 1: 依存とヘルパー

- [x] T1: `claude-agent-sdk==0.2.110` を `src/agent/requirements.txt` に pin 追加。SDK は CLI を同梱せず PATH 上の `claude` を subprocess 起動するため Dockerfile の `@anthropic-ai/claude-code` は残し、devcontainer で検証済みの `@2.1.207` に pin（`&& claude --version` で build 時検証）
- [x] T2: devcontainer で `claude-agent-sdk==0.2.110` インストール済み・`import orchestrator` 成功を確認（`.credentials.json` 実疎通は実機 ECS の T20 で確認）
- [x] T3: `_query_claude_sync()`（`orchestrator.py:540`）と `ClaudeInvocationError`（`:477`）を新設

## Phase 2: wrapper 差し替え

- [x] T4: `call_claude`（`:698`）を SDK 化 — `ResultMessage.result` テキストを返す。リトライ / advisor エスカレーション / emitter 契約は維持
- [x] T5: rate limit 検出の型化 — `_claude_stderr_indicates_rate_limit`（ProcessError.stderr）+ `_result_indicates_usage_limit`（is_error 応答テキスト、subtype `subscription_limit`）
- [x] T6: `call_claude_with_workspace`（`:979`）を SDK 化（`cwd=sandbox`, `allowed_tools=["Write","Edit"]`）
- [x] T7: `call_claude_with_text_workspace`（`:1047`）を SDK 化（同上）
- [x] T8: `_accumulate_cost`（`:453`）を `ResultMessage.usage` / `total_cost_usd` 入力に変更

## Phase 3: パーサ構造修正と call site 追随

- [x] T9: `_parse_claude_response`（`:777`）を厳密契約に修正 — envelope 段削除、4 戦略でフェンス抽出、失敗時 `ClaudeResponseParseError` 送出
- [x] T10: orchestrator 内 call sites を新契約（str 受け取り + try/except）に更新
- [x] T11: `feedback_processor.py` を新契約に追随
- [x] T12: `main.py` に `ClaudeInvocationError` except 分岐を追加（codex 用 `CalledProcessError` 分岐は温存）

## Phase 4: テスト（2026-07-11 完了）

- [x] T13: `test_orchestrator.py` の wrapper 単体テストを `_query_claude_sync` patch + `ResultMessage` スタブ（`_fake_result_message`）に書き換え。**旧 TestReviewLoop(15) / TestCodeGeneration(3) / TestPutDeliverableGitHubUrl(2) の 21 テストが未移行で実 SDK/Codex に到達しハングしていたのを修正 → `test_orchestrator.py` 125 passed**（アサーションは維持、reviewer→call_codex / fixer→call_claude の分離のみ）
- [x] T14: 統合テスト `tests/integration/test_workflow.py` の 3 テストを `_should_use_workspace_text_gen=False` + `call_codex`（reviewer）+ `call_claude`（fixer/text）の分離 patch に移行 → 3 passed。`test_feedback_processor.py` / `test_main.py` は既に移行済み・green
- [x] T15: 新規テスト追加済み — `ClaudeResponseParseError` 送出、subscription-limit 検出（`test_call_claude_subscription_limit_in_error_result`）、`CLINotFoundError` 即時失敗（`test_call_claude_cli_not_found_fails_fast`）
- [x] T16: **全テスト実行し green 確認 → `525 passed`（`AWS_DEFAULT_REGION=ap-northeast-1` 設定 + `--timeout=30 --timeout-method=signal`）**。従来の「pre-existing 26 failures」の実体は本 SDK 移行のテストハング(24) + devcontainer リビルドによる region 環境変数消失だったと判明。**残る lint 11 件（orchestrator.py SIM108/SIM105/E501×2、test_orchestrator.py S108×2/E501×5）は e8b5469 の同ファイルに ruff を掛けて同一集合であることを確認済みの pre-existing**（別 chore で対応可）。`dynamodb_client.py` の F821/F401 も unrelated pre-existing。※本移行作業で新規混入していた test_main.py の E501 1 件は 2026-07-11 に修正済み（変更ファイルに新規 lint ゼロを再確認）

## Phase 5: レビューと検証（CLAUDE.md 規律に従う）

- [x] T17: pre-commit-secret-scan (gitleaks 8.30.1, no leaks) → **2 論理コミット** (4fa1184 pref-scope 是正 / b62a438 SDK 移行) → push 完了（ユーザー決定によりコミット前レビュー方式に変更、レビュー収束後にコミット）
- [x] T18: Codex レビューゲート — **収束 (2026-07-11)**: Pass 1 (gpt-5.4): P1×0 / P2×2 → 両方是正 + 回帰テスト 2 件追加、527 passed。Pass 2 (gpt-5.5): **指摘ゼロ**、是正 2 件も「意図どおり」確認（`.audit/2026-07-06_agent-sdk-migration.md`）。方式: read-only sandbox + 差分埋め込み（bwrap が devcontainer で不可のため）
- [x] T19: `sam build` / `sam deploy` 完了（2026-07-11、ユーザー実行）。agent image は CI push 済みの b62a438 タグを指定。stack UPDATE_COMPLETE + task definition :15 反映を検証済み
- [ ] T20: 実機検証（`/deploy-verify` + design.md の環境前提 4 項目）:
  - [ ] ECS タスクで実トピック投入 → ワークフロー完走
  - [ ] CloudWatch Logs で researcher（WebSearch）/ generator（sandbox Write）/ fix loop の実動作確認
  - [ ] `.credentials.json` 認証成功と終了時 writeback の発火確認
  - [ ] `api_call_completed` / `rate_limit_hit` イベントが events テーブルに従来契約で記録されることを確認
- [x] T21: `docs/architecture.md` の CLI 記述を SDK 構成に整合更新（エージェント基盤表 / §4.5 出力フォーマット）、セッションハンドオフ memo 更新。※ Dockerfile の CLI インストール・Node.js ランタイム・認証記述は SDK 移行後も事実として正しいため温存

## 完了条件

requirements.md の受け入れ条件 1〜7 がすべて観測事実つきで verified になること。
