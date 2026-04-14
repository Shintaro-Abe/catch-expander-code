# 要求内容: Claude OAuth トークン失効監視

## 概要

Claude OAuth トークンが Secrets Manager 上で失効状態になったとき、開発者へ Slack で通知する仕組みを実装する。

## ユーザーストーリー

- 開発者として、Claude OAuth トークンが失効していることを自動的に知りたい
- 通知を受け取ったら DevContainer で `claude` コマンドを実行して再ログインする

## 受け入れ条件

- 12 時間ごとに自動チェックが走ること
- Secrets Manager の `expiresAt` が現在時刻より 24 時間以上過去の場合に「失効」と判定すること
- 失効時は Catch Expander の Slack チャンネルへ通知を投稿すること
- 通知には有効期限と再ログイン手順を含むこと
- トークンが有効な場合は通知しないこと

## 制約事項

- トークンの自動リフレッシュは行わない（通知のみ）
- ECS タスクロールへの `PutSecretValue` 権限付与は行わない
- AWS のマネージドサービス（Lambda + EventBridge）のみで完結させる
