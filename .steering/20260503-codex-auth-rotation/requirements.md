# 要求内容: Codex CLI 認証ローテーション

## 変更・追加する機能

レビュアーエージェントで使用する Codex CLI（GPT-5.5）の OAuth 認証情報について、Claude Code OAuth と同じローテーション方式を実装する。

## ユーザーストーリー

- ECS Fargate タスクは起動のたびにエフェメラルなファイルシステムで起動するため、Codex CLI の認証情報（`~/.codex/auth.json`）がタスク間で引き継がれない。
- Codex CLI がタスク実行中にアクセストークンを自動 refresh した場合、新しいトークンが次回起動時にも使えるよう Secrets Manager に書き戻す必要がある。
- `TASK_TYPE=feedback` のパスではレビュアーを使わないため、Codex 認証情報の取得・配置は不要。

## 受け入れ条件

1. ECS タスク起動時に `catch-expander/codex-auth` から `~/.codex/auth.json` を 0600 権限で復元する
2. タスク終了時にファイルの変化を検知し、変化があれば Secrets Manager に書き戻す（ベストエフォート）
3. 書き戻し前に Secrets Manager の現在値を再取得し、並行タスクが既に更新していればスキップする（optimistic concurrency）
4. `TASK_TYPE=feedback` パスでは Codex 認証情報は取得・配置しない
5. ファイル作成は原子的に 0600 権限で行い、chmod race condition を回避する
6. 単体テストで各関数の正常系・異常系をカバーする

## 制約事項

- Token Refresher Lambda は Claude OAuth のみが対象。Codex は Lambda による定期 refresh 不要（TTL 約 10 日、CLI が自動 refresh）
- `samconfig.toml` はgitignore対象のため、`CodexAuthSecretArn` パラメータはローカルに設定
