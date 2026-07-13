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

## Phase 6: T20 E2E 失敗（exec-20260711154440-67fed1d5）の是正

> **経緯**: 2026-07-11 15:44 UTC の実機 E2E で、リサーチ 5 本成功後に text generator が
> 3 回連続即死 → `NonDictGeneratorResponse`。根本原因は `_query_claude_sync` が SDK ストリームを
> 最後まで回すため、CLI が「is_error result → 意図的な非ゼロ終了」をした際に SDK のエラーフレームが
> 素の `Exception` として raise され（SDK `query.py:852`）、捕捉済み ResultMessage を破棄すること。
> これにより `_attempt_claude_query` の is_error 分岐（usage-limit 検出 / rate_limit_hit emit /
> 型付きリトライ・advisor 経路）が実運用でデッドコード化していた（構造修正を選択、症状パッチは不採用）。

- [x] T22: `_query_claude_sync` を「最初の ResultMessage で return」に変更（SDK `receive_response` と同セマンティクス）。ResultMessage 到達前にストリームが素の `Exception` を raise した場合は `ClaudeInvocationError` に正規化してリトライ経路に乗せる（`ClaudeSDKError` 系は従来どおり素通し — CLINotFoundError/CLIConnectionError の即時 fail と ProcessError の stderr 429 判定を温存）。P2-2 正規化分岐にも stderr 429 判定を追加（SDK reader が途中 ProcessError をエラーフレーム化するため）
- [x] T23: 回帰テスト追加（`TestQueryClaudeSync` 6 件）— (1) is_error ResultMessage 後にエラーフレーム Exception が来ても ResultMessage が返る、(2) 本番障害の E2E 再現: usage limit が `rate_limited=True` に分類され advisor スキップ、(3) ResultMessage なしの素の Exception → 正規化、(4) CLINotFoundError 素通し、(5) 空ストリーム → ClaudeInvocationError、(6) エラーフレーム内 429 の rate limit 分類。**全 533 passed**、変更ファイルの新規 lint ゼロ（11 件は HEAD 比較で pre-existing と同一集合を確認）
- [x] T24: Dockerfile から未使用の `@anthropic-ai/claude-code@2.1.207` を除去（本番ログ全呼び出しで SDK 同梱 CLI 2.1.191 使用を確認、PATH claude への依存コードなし）。Dockerfile コメント / architecture.md（基盤表・ランタイム表・Dockerfile 抜粋）を同梱 CLI 前提に訂正
> **E2E 再実行 2 回の結果 (2026-07-12)**:
> exec-20260712003302: 401 認証失敗（ローカル Claude Code セッションが refresh token を
> ローテーションしシークレット側が失効。さらに CLI がログアウトした空 credentials を
> writeback がシークレットに書き戻し破壊 → T27）。
> exec-20260712005527: 再同期後 56 分ほぼ完走（解析/設計/リサーチ 5 本/text generator/
> コード生成 2 種/fix loop まで実機成功 = T20 の大半を実証）、最終盤に
> `You've hit your session limit · resets 2am (UTC)` で失敗。新 CLI の上限文言
> "session limit" が `_USAGE_LIMIT_RESULT_PATTERNS` に無くレート制限扱いにならなかった → T26。

- [x] T26: `_USAGE_LIMIT_RESULT_PATTERNS` に "session limit" を追加（advisor スキップ / Slack レート制限文言の分類を回復）+ 本番観測文言での回帰テスト（`test_call_claude_session_limit_in_error_result`）
- [x] T27: `_writeback_claude_credentials` に空 credentials ガード（`_claude_credentials_look_valid`: accessToken / refreshToken が空 or JSON 不正 or claudeAiOauth 非 dict なら writeback をスキップ）+ 回帰テスト 3 件、既存フィクスチャを claudeAiOauth 実形状に更新。**全 538 passed**、新規 lint ゼロ。コミット 8ebf97e + e0ce396。**Codex ゲート: Pass 1 指摘ゼロで収束**（補足 1 件は isinstance ガードで対応。`.audit/2026-07-12_session-limit-writeback-guard.md`。Pass 2 はユーザー判断でスキップ）
> **4 回目 E2E (exec-20260713114159, 2026-07-13 11:42-13:04)**: text generator リトライ回復
> (2 回検証失敗→3 回目成功)・コード生成・レビューループ (unparseable fix 2 回とも前版維持で継続)・
> **GitHub push 成功 (新 PAT)**・Notion ページ作成成功まで到達。ブロック追記の 400
> (`code.language` が Notion 許容 enum 外) で失敗 → T28。SDK 移行とは無関係の pre-existing バグ
> (generator プロンプト例が非対応の "terraform" を教示 + notion_client に language 検証層なし)。

- [x] T28: Notion code block language の正規化層 — `notion_client` に `_normalize_code_languages`（許容 enum + alias マップ、未知は "plain text" に縮退、非 mutate）を追加し `create_page` / `append_blocks` に適用。`prompts/generator.md` の例を `terraform` → `hcl` に訂正 + 許容値の指示行を追加。回帰テスト 10 件（本番障害の terraform 再現・payload 検証含む）→ **全 548 passed**、新規 lint ゼロ
- [ ] T25: 品質ゲート — ruff / 全テスト green ✔ → secret scan（初回 102 件は全件精査で FP、`.gitleaks.toml` 整備 202efa0）✔ → commit（46b9784）→ push ✔ → **Codex レビューゲート収束**（Pass 1: P2×2 → aclosing 化 + 回帰テストで是正（ec5cb6d、534 passed）、Pass 2: 指摘ゼロ。`.audit/2026-07-11_sdk-stream-error-result.md`）✔ → 再デプロイ完了（2026-07-12、ユーザー実行。**検証**: stack UPDATE_COMPLETE + AgentImageUri=ec5cb6d + task definition `catch-expander-agent:16`）✔ → T26/T27 デプロイ完了（2026-07-12、ユーザー実行。**検証**: stack UPDATE_COMPLETE + AgentImageUri=e0ce396 + task definition `catch-expander-agent:17`）✔ → 残り: E2E 再実行（T20 残分 / preference-scope T8-4。**前提: シークレット再同期 + 使用上限の回復**）

## 完了条件

requirements.md の受け入れ条件 1〜7 がすべて観測事実つきで verified になること。
