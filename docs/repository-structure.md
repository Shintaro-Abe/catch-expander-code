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
├── knowledge/                         # 調査・意思決定の記録
│   └── agent-runtime-selection.md     # エージェント実行基盤の選定調査
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
│   └── agent/                         # ECS エージェントアプリケーション
│       ├── __init__.py
│       ├── orchestrator.py            # オーケストレーターエージェント
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
│   │   └── agent/
│   │       ├── __init__.py
│   │       ├── test_orchestrator.py
│   │       ├── test_feedback_processor.py  # F8 フィードバック学習テスト
│   │       ├── test_notion_client.py
│   │       ├── test_github_client.py
│   │       └── test_slack_client.py
│   └── integration/                   # 統合テスト
│       ├── __init__.py
│       ├── conftest.py                # _load_prompt モック（プロンプトファイル不在対応）
│       └── test_workflow.py
│
├── .devcontainer/                     # Dev Container設定
│   ├── devcontainer.json
│   └── post_create.sh
│
├── .github/                           # GitHub設定
│   └── workflows/
│
└── .claude/                           # Claude Code設定
    ├── settings.local.json
    └── commands/
```

> **デプロイ時に別途作成するファイル（リポジトリ外管理）**
> `src/agent/Dockerfile`、`src/agent/main.py`、`src/agent/prompts/*.md`、`src/agent/requirements.txt` は
> デプロイ時にリポジトリ外で作成・管理される。設計仕様は `docs/architecture.md` を参照のこと。

## 3. ディレクトリの役割

### `docs/` — 永続的ドキュメント

アプリケーション全体の「何を作るか」「どう作るか」を定義する恒久的なドキュメント。基本設計や方針が変わらない限り更新されない。

### `knowledge/` — 調査・意思決定の記録

技術選定や設計判断の調査過程・根拠を記録する。コードやgit履歴からは読み取れない「なぜその判断をしたか」を残す場所。

### `.steering/` — 作業単位のステアリングファイル

特定の開発作業における要求・設計・タスクを管理する一時的なファイル。命名規則は `YYYYMMDD-タイトル/`。詳細はCLAUDE.mdを参照。

### `src/trigger/` — Lambda トリガー関数

Slackイベントを受信し、署名検証・ACK応答・ECSタスク起動を行うLambda関数。SAMテンプレートから参照される。

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
| 調査記録は `knowledge/` に配置 | 技術選定・意思決定の根拠 |
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
| `src/agent/orchestrator.py` | Claude Code CLIの呼び出し、サブエージェント管理、ワークフロー制御 |
