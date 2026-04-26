# 要求内容: Claude OAuth トークン失効監視

> **⚠️ 注記 (2026-04-26 追加): 設計方針が拡張・置換**
>
> 本 steering で実装した Token Monitor Lambda（失効を Slack に**通知のみ**・自動リフレッシュなし）は、その後 `.steering/20260425-auth-redesign-aipapers/` により **Token Refresher Lambda（自動リフレッシュ）** に再設計されました。
> - `src/token_monitor/handler.py` の本体ロジックは ai-papers-digest 方式（`platform.claude.com/v1/oauth/token` を `refresh_token` で叩いて Secrets Manager に書き戻す）に全面書き換え（commit `947bc3b`）
> - 「制約事項: トークンの自動リフレッシュは行わない（通知のみ）」「ECS タスクロールへの `PutSecretValue` 権限付与は行わない」は撤回されました
> - EventBridge スケジュール（12 時間ごと）と Slack 失敗通知の枠組みは継続利用
> 本ファイルは履歴として保持されます。

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
