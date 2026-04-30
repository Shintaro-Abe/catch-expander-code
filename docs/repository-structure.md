# リポジトリ構造定義書

## 1. リポジトリ概要

| 項目 | 内容 |
|------|------|
| リポジトリ名 | `Catch-Expander` |
| 用途 | アプリケーションコード + IaC + ドキュメント |
| ブランチ戦略 | `main` のみ（個人開発のため） |

## 2. ディレクトリ構成

```
Catch-Expander/
├── CLAUDE.md                          # プロジェクトメモリ（Claude Code用）
├── README.md                          # プロジェクト概要
├── pyproject.toml                     # Pythonプロジェクト設定（Ruff等）
├── requirements-dev.txt               # 開発用依存パッケージ
├── uv.lock                            # uvロックファイル
├── samconfig.toml                     # SAMデプロイ設定
├── template.yaml                      # SAMテンプレート（IaC）
│
├── docs/                              # 永続的ドキュメント
│   ├── product-requirements.md        # プロダクト要求定義書
│   ├── functional-design.md           # 機能設計書
│   ├── architecture.md                # 技術仕様書
│   ├── credential-setup.md            # クレデンシャル取得手順書
│   ├── repository-structure.md        # リポジトリ構造定義書（本書）
│   ├── development-guidelines.md      # 開発ガイドライン
│   └── glossary.md                    # ユビキタス言語定義
│
├── obsidian/                          # 調査・意思決定の記録
│   └── 2026-04-04_agent-runtime-selection-claude-code-cli-on-ecs.md  # エージェント実行基盤の選定調査
│
├── .steering/                         # 作業単位のステアリングファイル
│   └── YYYYMMDD-タイトル/
│       ├── requirements.md
│       ├── design.md
│       └── tasklist.md
│
├── src/                               # アプリケーションコード
│   ├── trigger/                       # Lambda トリガー関数
│   │   ├── __init__.py
│   │   ├── app.py                     # Lambdaハンドラー
│   │   ├── slack_verify.py            # Slack署名検証
│   │   └── requirements.txt           # Python依存パッケージ
│   │
│   ├── token_monitor/                 # Claude OAuth トークン失効監視 Lambda
│   │   ├── __init__.py
│   │   ├── handler.py                 # EventBridge定期トリガーで発火
│   │   └── requirements.txt
│   │
│   └── agent/                         # ECS エージェントアプリケーション
│       ├── __init__.py
│       ├── Dockerfile                 # ECS Fargate 用コンテナ定義
│       ├── main.py                    # ECSタスクエントリポイント（TASK_TYPE分岐）
│       ├── orchestrator.py            # オーケストレーターエージェント
│       ├── requirements.txt           # Python依存パッケージ
│       ├── prompts/                   # サブエージェント用プロンプト
│       │   ├── orchestrator.md
│       │   ├── researcher.md
│       │   ├── generator.md
│       │   └── reviewer.md
│       ├── feedback/                  # フィードバック学習（F8）
│       │   ├── __init__.py
│       │   └── feedback_processor.py  # フィードバック解析・プロファイル更新
│       ├── storage/                   # 外部ストレージ連携
│       │   ├── __init__.py
│       │   ├── notion_client.py       # Notion API操作
│       │   └── github_client.py       # GitHub API操作
│       ├── state/                     # 状態管理
│       │   ├── __init__.py
│       │   └── dynamodb_client.py     # DynamoDB操作
│       └── notify/                    # 通知
│           ├── __init__.py
│           └── slack_client.py        # Slack通知送信
│
├── tests/                             # テスト
│   ├── __init__.py
│   ├── unit/                          # ユニットテスト
│   │   ├── __init__.py
│   │   ├── trigger/
│   │   │   ├── __init__.py
│   │   │   ├── conftest.py            # botocore[crt]依存問題の回避
│   │   │   └── test_app.py
│   │   ├── token_monitor/
│   │   │   ├── __init__.py
│   │   │   ├── conftest.py
│   │   │   └── test_app.py
│   │   └── agent/
│   │       ├── __init__.py
│   │       ├── test_main.py
│   │       ├── test_orchestrator.py
│   │       ├── test_feedback_processor.py  # F8 フィードバック学習テスト
│   │       ├── test_dynamodb_client.py     # source_id 一意化（M1）
│   │       ├── test_notion_client.py
│   │       ├── test_github_client.py
│   │       └── test_slack_client.py
│   └── integration/                   # 統合テスト
│       ├── __init__.py
│       ├── conftest.py                # _load_prompt モック（プロンプトファイル不在対応）
│       └── test_workflow.py
│
└── .github/                           # GitHub設定
    └── workflows/
```

> **備考**
> `.devcontainer/` と `.claude/` はローカル開発環境・エディタ固有設定のため
> `.gitignore` で追跡対象外としている（公開リポジトリにはアプリケーションコードのみを含める方針）。

> **備考**
> `src/agent/Dockerfile`、`src/agent/prompts/*.md`、`src/agent/requirements.txt` は
> いずれもリポジトリ内で管理する。ECS Fargate 用のコンテナイメージは SAM ビルド時に
> これらのファイルを使ってビルドされる。設計仕様は `docs/architecture.md` を参照のこと。

## 3. ディレクトリの役割

### `docs/` — 永続的ドキュメント

アプリケーション全体の「何を作るか」「どう作るか」を定義する恒久的なドキュメント。基本設計や方針が変わらない限り更新されない。

### `obsidian/` — 調査・意思決定の記録

技術選定や設計判断の調査過程・根拠を記録する。コードやgit履歴からは読み取れない「なぜその判断をしたか」を残す場所。

### `.steering/` — 作業単位のステアリングファイル

特定の開発作業における要求・設計・タスクを管理する一時的なファイル。命名規則は `YYYYMMDD-タイトル/`。詳細はCLAUDE.mdを参照。

### `src/trigger/` — Lambda トリガー関数

Slackイベントを受信し、署名検証・ACK応答・ECSタスク起動を行うLambda関数。SAMテンプレートから参照される。

### `src/token_monitor/` — トークンリフレッシャー Lambda

EventBridge の定期スケジュールで 12 時間ごとに発火し、Secrets Manager に保管された Claude OAuth credentials の `expiresAt` を確認する。残り 1 時間以下または失効済みの場合、`refreshToken` を Anthropic OAuth エンドポイントに直接送信して新 access_token を取得し、Secrets Manager を上書きする。refresh が失敗した場合のみ Slack 通知で再認証を促す。

### `src/agent/` — ECS エージェントアプリケーション

Claude Code CLIを使ってマルチAIエージェントを実行するメインアプリケーション。Dockerコンテナとしてビルドされ、ECS Fargateで実行される。

| サブディレクトリ | 役割 |
|----------------|------|
| `feedback/` | フィードバック解析・`learned_preferences` 更新（F8） |
| `storage/` | Notion API・GitHub APIの操作 |
| `state/` | DynamoDBによるワークフロー状態管理 |
| `notify/` | Slack通知の送信 |

### `tests/` — テスト

| サブディレクトリ | 対象 |
|----------------|------|
| `unit/` | 各モジュールの単体テスト |
| `integration/` | ワークフロー全体の統合テスト |

## 4. ファイル配置ルール

### アプリケーションコード

| ルール | 説明 |
|--------|------|
| Lambda関数は `src/trigger/` に配置 | SAMテンプレートの `CodeUri` で参照 |
| ECSアプリケーションは `src/agent/` に配置 | Dockerfileでビルド（Dockerfileはリポジトリ外管理） |
| 外部API連携は機能別サブディレクトリに分離 | `storage/`, `state/`, `notify/` |

### IaC

| ルール | 説明 |
|--------|------|
| SAMテンプレートはリポジトリルートに配置 | `template.yaml`（SAM CLIのデフォルト） |
| SAMデプロイ設定はリポジトリルートに配置 | `samconfig.toml`（SAM CLIのデフォルト） |

### ドキュメント

| ルール | 説明 |
|--------|------|
| 永続的ドキュメントは `docs/` に配置 | プロジェクト全体の設計・方針 |
| 調査記録は `obsidian/` に配置 | 技術選定・意思決定の根拠 |
| 作業単位のドキュメントは `.steering/` に配置 | 個別の開発作業の要求・設計・タスク |

### テスト

| ルール | 説明 |
|--------|------|
| テストファイルは `tests/` 配下に、ソースと同じ構造で配置 | `src/trigger/app.py` → `tests/unit/trigger/test_app.py` |
| テストファイル名は `test_` プレフィックス | pytestの自動検出に準拠 |

## 5. 主要ファイルの説明

### ルートファイル

| ファイル | 説明 |
|---------|------|
| `CLAUDE.md` | Claude Codeのプロジェクトメモリ。開発ルール・プロセスを定義 |
| `README.md` | プロジェクト概要、セットアップ手順 |
| `pyproject.toml` | Ruff設定・プロジェクトメタデータ |
| `requirements-dev.txt` | 開発用依存パッケージ（pytest, ruff, pip-audit） |
| `template.yaml` | SAMテンプレート。Lambda, API Gateway, DynamoDB, ECS等の全AWSリソースを定義 |
| `samconfig.toml` | SAMデプロイのパラメータ設定 |

### エージェントアプリケーション

| ファイル | 説明 |
|---------|------|
| `src/agent/main.py` | ECSタスクエントリポイント。`TASK_TYPE` 環境変数でフィードバック処理とワークフロー実行を分岐 |
| `src/agent/orchestrator.py` | Claude Code CLIの呼び出し、サブエージェント管理、ワークフロー制御 |
