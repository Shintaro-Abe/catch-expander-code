# 設計: Claude OAuth トークン失効監視

## 実装アプローチ

### アーキテクチャ

```
EventBridge Scheduler (rate: 12時間)
    → Lambda (catch-expander-token-monitor)
        → Secrets Manager: catch-expander/claude-oauth を読み取り
        → expiresAt を解析
        → 失効判定 (now_ms > expiresAt + 24h)
        → 失効時: Secrets Manager から Slack Bot Token を読み取り
        → Slack チャンネルへ通知
```

### 失効判定ロジック

- Secrets Manager の JSON: `{"claudeAiOauth": {"expiresAt": <ms>}}`
- 閾値: `expiresAt + 24h < now` → 失効と判定
- accessToken は 8h で失効するため、24h 経過 = devcontainer が 16h 以上未起動

### 変更するコンポーネント

| コンポーネント | 変更内容 |
|---|---|
| `template.yaml` | `SlackNotificationChannelId` パラメータ追加、`TokenMonitorFunction` リソース追加 |
| `src/token_monitor/app.py` | 新規 Lambda 関数コード |
| `src/token_monitor/requirements.txt` | 依存ライブラリ |
| `tests/unit/token_monitor/test_app.py` | ユニットテスト |

### 影響範囲

- 既存の ECS タスク、Trigger Lambda: 変更なし
- IAM: TokenMonitorFunction 専用のロールを SAM が自動生成（GetSecretValue のみ）
- デプロイ: `sam deploy` 時に `SlackNotificationChannelId` の値が必要

## Slack 通知メッセージ設計

```
⚠️ Claude OAuth トークンの確認が必要です。

有効期限: 2026-04-14 08:00 UTC （16 時間前に期限切れ）

DevContainer を起動して `claude` コマンドを実行し、再ログインしてください。
再ログイン後、トークンは自動的に Secrets Manager へ同期されます。
```
