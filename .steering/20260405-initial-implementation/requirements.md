# 初回実装の要求

## 概要

Catch ExpanderのMVP（初回リリース）を実装する。`docs/product-requirements.md` のスコープ定義に基づき、F1〜F7の基本機能を1ユーザー・1ワークスペースで動作させる。

## 実装対象

### インフラストラクチャ

| 対象 | 内容 | 参照 |
|------|------|------|
| SAMテンプレート | Lambda, API Gateway, DynamoDB, ECS（タスク定義・クラスター）, IAMロール, Secrets Manager, CloudWatch | `architecture.md` 全体 |
| VPC | パブリックサブネット × 2 AZ, セキュリティグループ | `architecture.md` 2. ネットワーク構成 |
| ECR | コンテナイメージリポジトリ | `architecture.md` 1. 実行基盤 |
| Dockerイメージ | Claude Code CLI + Python ラッパー | `architecture.md` 3. コンテナ設計 |

### Lambda トリガー関数

| 対象 | 内容 | 参照 |
|------|------|------|
| Slackイベント受信 | API Gateway経由でSlackイベントを受信 | `functional-design.md` 4.1 |
| Slack署名検証 | `X-Slack-Signature` によるリクエスト検証 | `architecture.md` 7. セキュリティ設計 |
| ACK応答 | Slackへ即座に受付メッセージを投稿（3秒以内） | `product-requirements.md` NFR-1 |
| ECSタスク起動 | RunTask APIでFargateタスクを非同期起動 | `architecture.md` 4. Lambda設計 |
| DynamoDB書き込み | ワークフロー実行レコードの作成（status: received） | `functional-design.md` 3.1 状態管理 |

### ECS エージェントアプリケーション

| 対象 | 内容 | 参照 |
|------|------|------|
| 認証復元 | Secrets ManagerからClaude Code OAuth認証情報を取得し復元 | `credential-setup.md` 4.3 |
| オーケストレーター | トピック解析、ワークフロー設計、サブエージェント管理、格納処理 | `functional-design.md` 3.1 |
| リサーチャー | Web検索による情報収集・要約・出典記録（並列実行） | `functional-design.md` 3.2 |
| ジェネレーター | 成果物生成・推敲 | `functional-design.md` 3.3 |
| レビュアー | ソース検証・チェックリスト評価・品質メタデータ付与 | `functional-design.md` 3.4 |
| Notion格納 | 成果物ページの作成（Notion API） | `functional-design.md` 4.2 |
| GitHub格納 | コード成果物のpush（GitHub API、コード成果物がある場合のみ） | `functional-design.md` 4.3 |
| Slack通知 | 進捗通知・完了通知の送信 | `functional-design.md` 4.1 |
| 状態管理 | DynamoDBへのワークフロー状態の読み書き | `functional-design.md` 3.1 状態管理 |
| エラーハンドリング | リトライ、部分成果物出力、フォールバック | `functional-design.md` 6. エラーハンドリング |

### エージェント専門プロンプト

| 対象 | 内容 | 参照 |
|------|------|------|
| オーケストレーター用 | トピック解析・ワークフロー設計・判断に特化 | `functional-design.md` 3.1 |
| リサーチャー用 | 調査・要約・出典記録に特化、ソース優先順位ルール含む | `functional-design.md` 3.2 |
| ジェネレーター用 | 文書/コード生成に特化、成果物タイプ別構造化ルール含む | `functional-design.md` 3.3 |
| レビュアー用 | 品質検証・ファクトチェックに特化、カテゴリ別チェックリスト含む | `functional-design.md` 3.4 |

## 実装対象外（MVP以降）

| 対象 | 理由 |
|------|------|
| F8: フィードバック学習 | `product-requirements.md` スコープ定義でMVP対象外 |
| F9: 成果物履歴管理 | `product-requirements.md` スコープ定義でMVP対象外 |
| 複数ユーザー対応 | MVPは1ユーザー・1ワークスペース |
| 定期リサーチ | 将来拡張 |
| CI/CDパイプライン | 初回は手動デプロイ（`sam deploy`） |

## 受け入れ条件

`product-requirements.md` の AC-1〜AC-5 を満たすこと。以下に要約する。

### AC-1: 基本フロー

- [ ] Slackでメッセージを送信すると3秒以内に処理開始通知が返る
- [ ] エージェントがトピックのカテゴリ・観点・成果物タイプを自律的に判断する
- [ ] ワークフローを動的に構築し実行する
- [ ] 成果物がNotion（+ 該当時GitHub）に格納される
- [ ] Slackに完了通知（サマリー + リンク）が届く

### AC-2: 成果物の適切性

- [ ] 技術トピック: 概要、論文参照、ユースケース、クラウド別実装手順、料金情報、IaCコード、設計書が含まれる
- [ ] 時事トピック: 背景、ニュースダイジェスト、予測シナリオが含まれる
- [ ] 成果物タイプはトピックに応じてエージェントが自律的に判断する

### AC-3: ユーザープロファイル

- [ ] プロファイル情報を登録できる
- [ ] 成果物にプロファイルが反映される

### AC-4: 進捗通知

- [ ] 処理開始通知がSlackに送られる
- [ ] 主要ステップごとに進捗が通知される
- [ ] エラー時はユーザーに通知される

### AC-5: 品質担保

- [ ] 全事実主張に出典URLが記載されている
- [ ] 出典URLの実在と整合性が検証されている
- [ ] カテゴリ別セルフレビューチェックリストを全項目合格している
- [ ] 品質メタデータが成果物末尾に記載されている

## 制約事項

| 制約 | 内容 |
|------|------|
| 実行環境 | ECS Fargate（ap-northeast-1） |
| LLM | Claude Opus（Maxプラン、Claude Code CLI経由） |
| IaC | AWS SAM（template.yaml） |
| 言語 | Python 3.13 |
| デプロイ | 手動（`sam deploy` + `docker push`） |
| 環境 | dev環境のみ（単一環境） |

## 事前準備（実装開始前に必要）

| 項目 | 内容 | 参照 |
|------|------|------|
| Slack App作成 | Bot Token + Signing Secret取得 | `credential-setup.md` 1 |
| Notion Integration作成 | Integration Token取得 + 成果物DB作成 | `credential-setup.md` 2 |
| GitHub PAT作成 | Fine-grained PAT取得 + リポジトリ作成 | `credential-setup.md` 3 |
| Claude Code認証 | MaxプランOAuthトークン取得 | `credential-setup.md` 4 |
| AWS CLI設定 | デプロイ用のAWS認証情報 | - |
