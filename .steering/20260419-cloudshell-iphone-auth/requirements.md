# 要求内容: iPhone から AWS CloudShell 経由で Claude OAuth 再認証を行う

> **⚠️ 注記 (2026-04-26 追加): この方針は 2026-04-25 に廃止**
>
> 本 steering の解決方針（CloudShell + iPhone での手動再認証）は `.steering/20260425-auth-redesign-aipapers/` の Token Refresher Lambda（自動リフレッシュ）方式により完全に置き換えられました。
> 廃止理由は 3 つ:
> 1. Claude Code 2.1.118 で CloudShell では `~/.claude/.credentials.json` が生成されない（既存 `sync-claude.sh` が動作不能）
> 2. `sync-claude.sh` が「ファイル sync 成功」と「OAuth refresh 成功」を区別できない設計欠陥
> 3. 上記により、ユーザーが手順を完走しても 401 エラーが継続する事故が 2026-04-23 に発生
>
> 現行の認証手順は `.steering/20260425-auth-redesign-aipapers/initial-setup.md` を参照してください。
> 関連ナレッジ: `feedback_auth_procedure_design.md`（認証手順書は refresh 経路そのものを検証する必要あり）。
> 本ファイルは履歴として保持されます。

## 概要

PC の DevContainer にアクセスできない状況（外出先・iPhone のみ所持時）でも、Claude OAuth トークンの再認証と Secrets Manager への反映を実施できるようにする。

## 背景

- 現状、Claude OAuth トークンは約 24 時間で失効する
- 再認証には DevContainer 内で `claude login` を実行する必要がある
- DevContainer は PC 起動が前提であり、iPhone のみの状況では再認証経路が存在しない
- Token Monitor Lambda は失効を検知して通知するが、再認証できないと ECS タスクが停止する

## ユーザーストーリー

- 開発者として、PC にアクセスできない場所からでも Claude OAuth の再認証を行いたい
- 開発者として、iPhone Safari だけで完結する再認証経路がほしい
- 開発者として、静的な AWS アクセスキー/シークレットキーを iPhone やクラウド環境に保存したくない

## 受け入れ条件

- iPhone Safari から AWS Console にログインし、CloudShell を開ける
- CloudShell 上で `claude login` を実行して認証できる
- 認証後、`aws secretsmanager put-secret-value` で `catch-expander/claude-oauth` を更新できる
- 更新後の `expiresAt` が Secrets Manager に正しく反映される
- 静的 AWS アクセスキーを発行・保存しない
- 使用する IAM 権限は、必要最小限（対象シークレットへの `PutSecretValue` + CloudShell 利用）に限定する
- 既存の DevContainer 経由の再認証経路は壊さない（併存）
- 手順が `docs/credential-setup.md` に記載されている

## 制約事項

- CloudShell の 1GB 制限内で収まる構成とする
- `claude` CLI は Node.js 経由で `~/.local` に配置し、CloudShell の持続ホームディレクトリを活用する
- 既存の DevContainer watcher との同期競合を起こさない（CloudShell からの更新は単発・手動）
- IAM ポリシーの変更は個人ユーザー/SSO 範囲に留め、`template.yaml`（SAM）には含めない
