# タスクリスト: Codex CLI 認証ローテーション

## ステータス凡例
- [ ] 未着手
- [x] 完了

## タスク

### P0: コア実装

- [x] `_write_secret_file` ヘルパー追加（atomic 0600）
- [x] `_setup_claude_credentials` を `_write_secret_file` に置換（chmod race 解消）
- [x] `_setup_codex_credentials` 追加
- [x] `_writeback_claude_credentials` に optimistic concurrency チェック追加
- [x] `_writeback_codex_credentials` 追加
- [x] `main()` で Codex 認証を orchestrator パスのみに限定
- [x] `template.yaml` に `CodexAuthSecretArn` パラメータ・IAM 権限・環境変数追加
- [x] `samconfig.toml` に `CodexAuthSecretArn` の実際の ARN 設定（ローカルのみ）

### P1: Codex レビュー指摘対応

- [x] **P1**: `_writeback_claude_credentials` に optimistic concurrency（並行タスク上書き防止）
- [x] **P2**: `TestSetupClaudeCredentials` に 0600 パーミッションテスト追加
- [x] **P2**: `TestWritebackClaudeCredentials` に並行更新スキップテスト追加
- [x] **P2**: `TestSetupCodexCredentials` 追加（4テスト）
- [x] **P2**: `TestWritebackCodexCredentials` 追加（5テスト）
- [x] **P2**: `AgentTaskExecutionRole` から `GetSecretValue` 削除（最小権限）
- [x] **P3**: feedback タスクのスコープ確認（Codex 認証はオーケストレーターパスのみ）

### P2: ドキュメント更新

- [x] `docs/architecture.md` — LLM モデル表・シークレット表・Token Monitor 説明・コスト表更新
- [x] `docs/functional-design.md` — Opus → GPT-5.5 Codex 全箇所（エージェント表・シーケンス図×2・レビュアー説明・モデル表・呼び出し表）
- [x] `docs/credential-setup.md` — セクション 4.5 追加・Token Refresher 注記・シークレット一覧更新
- [x] `.steering/20260503-codex-auth-rotation/` ディレクトリ作成

### P3: デプロイ

- [x] `sam build` + `sam deploy`（インフラ変更: `CodexAuthSecretArn` パラメータ・IAM 権限・環境変数）
- [x] GitHub Actions によるコンテナイメージビルド・ECR push（Python コード変更）

## 完了条件

1. ECS タスク起動時に `~/.codex/auth.json` が Secrets Manager から復元される
2. タスク終了時に変化があれば書き戻される
3. 単体テスト 36 件全通過
4. `sam deploy` でインフラ変更が適用される
5. ドキュメントが現状のコードと齟齬なく更新される
