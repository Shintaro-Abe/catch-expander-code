# 設計: Codex CLI 認証ローテーション

## 実装アプローチ

Claude Code OAuth の既存方式（`_setup_claude_credentials` / `_writeback_claude_credentials`）をベースに、Codex 向けの対称的な実装を追加する。

## 変更するコンポーネント

### `src/agent/main.py`

| 追加/変更 | 内容 |
|----------|------|
| 追加 | `_write_secret_file(path, content)` — atomic 0600 ファイル作成ヘルパー（`os.open` + `os.O_CREAT` + mode=0o600）。Claude・Codex 両方が共用 |
| 変更 | `_setup_claude_credentials` — `write_text` + `chmod` を `_write_secret_file` に置換（chmod race 解消） |
| 追加 | `_setup_codex_credentials(secret_value) -> str` — `~/.codex/auth.json` を 0600 で作成、初期 hash を返す |
| 変更 | `_writeback_claude_credentials` — 書き戻し前に Secrets Manager 現在値を再取得して optimistic concurrency チェックを追加 |
| 追加 | `_writeback_codex_credentials(secret_arn, initial_hash)` — 同上の Codex 版 |
| 変更 | `main()` — Codex 認証は orchestrator パスのみで実行（`TASK_TYPE=feedback` は不要） |

### `template.yaml`

| 変更 | 内容 |
|------|------|
| 追加 | `CodexAuthSecretArn` パラメータ |
| 追加 | `AgentTaskRole` に `catch-expander/codex-auth` への `GetSecretValue` + `PutSecretValue` |
| 追加 | `AgentTaskDefinition` に `CODEX_AUTH_SECRET_ARN` 環境変数 |
| **削除** | `AgentTaskExecutionRole` への `GetSecretValue`（実行ロールではなくタスクロールが読む） |

### `samconfig.toml`（gitignore対象、ローカルのみ）

`parameter_overrides` に `CodexAuthSecretArn=arn:aws:secretsmanager:...` を追加。

### `tests/unit/agent/test_main.py`

| 追加 | 内容 |
|------|------|
| `TestWriteSecretFile` | atomic 0600 作成、chmod ではなく `os.open` を使うことを確認 |
| `TestSetupCodexCredentials` | ファイル書き込み・ディレクトリ作成・パーミッション・hash 返却 |
| `TestWritebackCodexCredentials` | 変化なしスキップ・put 実行・並行更新スキップ・ファイル不在・例外スワロー |
| `TestMain` 拡張 | feedback パスで Codex 認証未実施・シークレット取得数確認 |

## データ構造の変更

`~/.codex/auth.json` の内容:
```json
{
  "auth_mode": "chatgpt",
  "tokens": {"id_token": "...", "access_token": "...", "refresh_token": "...", "account_id": "..."},
  "last_refresh": "...",
  "client_id": "app_EMoamEEZ73f0CkXaXp7hrann"
}
```

## 影響範囲の分析

- **影響あり**: `main.py`（エントリポイント）、`template.yaml`（インフラ）、テストコード
- **影響なし**: `orchestrator.py`、`feedback_processor.py`、Lambda 関数群
- **既存テストへの影響**: `TestMain` 全テストに `CODEX_AUTH_SECRET_ARN` 環境変数と対応する mock が必要

## セキュリティ設計

- `_write_secret_file`: `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` で原子的に 0600 を付与。`write_text` + `chmod` の race を排除
- Optimistic concurrency: writeback 前に Secrets Manager を再読みし、別タスクが先に更新していれば skip
- `feedback` タスクは Codex 不要なため blast radius を最小化
