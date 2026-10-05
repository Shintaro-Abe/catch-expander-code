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
│   ├── execution-diagnostician.md     # 実行の診断（diagnosis/）の使い方・手動手順
│   ├── execution-fixer.md             # 診断にもとづく修正（src/fixer/）の使い方・手動手順
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
│   ├── observability/                 # 観測イベント共通ヘルパー
│   │   ├── __init__.py
│   │   └── event_emitter.py           # DynamoDB events テーブルへのイベント書き込みユーティリティ
│   │
│   ├── fixer/                         # 修正役: 診断結果をもとに修正案を作る（隔離された環境で手で動かす。本番のイメージに入れない）
│   │   ├── __init__.py                # run_fix の再公開
│   │   ├── __main__.py                # 手動実行の入口（python -m src.fixer）
│   │   ├── models.py                  # 決まった値・データの型
│   │   ├── handoff_input.py           # 受け渡しの読み込み・形の検証・場所の再検証・条件の再計算
│   │   ├── exception_types.py         # 例外の型の許可一覧
│   │   ├── gitio.py                   # git の操作（決まったサブコマンドだけ）
│   │   ├── workspace.py               # 作業用の複製・パスの検査・変更の取り出し
│   │   ├── tools.py                   # ツールの中身・入力の検証・上限・足取り
│   │   ├── runners.py                 # テストとリントの実行・環境変数の絞り込み
│   │   ├── gates.py                   # 合否の検査・対象外の場所の一覧
│   │   ├── prompt.py                  # 指示文
│   │   ├── sdk_server.py              # Agent SDK へのツールの登録
│   │   ├── runner.py                  # 実行（オプション・時間切れ・検査・コミット）
│   │   └── draft.py                   # プルリクエスト本文の下書き
│   │
│   ├── dashboard_api/                 # ダッシュボード API Lambda 群
│   │   ├── __init__.py
│   │   ├── _common.py                 # 共通ユーティリティ（json_response / error_response / ts_range 等）
│   │   ├── requirements.txt
│   │   ├── authorizer/                # Lambda Authorizer（JWT cookie 検証）
│   │   ├── auth_login/                # Slack OAuth 認可 URL 生成
│   │   ├── auth_callback/             # Slack OAuth コールバック・JWT 発行
│   │   ├── auth_logout/               # JWT cookie 削除
│   │   ├── auth_me/                   # セッション確認
│   │   ├── list_executions/           # 実行一覧
│   │   ├── get_execution/             # 実行詳細
│   │   ├── get_execution_events/      # イベントタイムライン
│   │   ├── get_metrics_summary/       # メトリクス概要
│   │   ├── get_cost_summary/          # コスト集計
│   │   ├── get_api_health/            # 外部 API 健全性
│   │   ├── get_token_monitor_health/  # OAuth トークン更新状況
│   │   ├── get_review_quality/        # レビュー品質
│   │   ├── get_errors/                # エラー一覧
│   │   └── get_feedback_aggregation/  # フィードバック集計
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
│       ├── notify/                    # 通知
│       │   ├── __init__.py
│       │   └── slack_client.py        # Slack通知送信
│       └── diagnosis/                 # 実行診断役: 実行の記録を調べて原因を診断する（読み取り専用）
│           ├── __init__.py            # diagnose_execution の再公開
│           ├── __main__.py            # 手動実行の入口（python -m diagnosis）
│           ├── models.py              # 決まった値・データの型
│           ├── redaction.py           # マスク・識別子の除去・切り詰め
│           ├── records.py             # DynamoDB・S3 の読み取り
│           ├── tools.py               # ツールの中身・入力の検証・上限・根拠の照合
│           ├── frames.py              # スタックトレースのフレームと実際のコードの突き合わせ
│           ├── exception_types.py     # 例外の型の許可一覧
│           ├── handoff.py             # 受け渡し用の結果の組み立て
│           ├── prompt.py              # 指示文（定数）
│           ├── sdk_server.py          # Agent SDK へのツールの登録
│           ├── runner.py              # 実行（オプション・時間切れ・イベント）
│           └── notify.py              # Slack の投稿文の組み立て
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
│   │   ├── dashboard_api/
│   │   │   ├── conftest.py            # patch() サブモジュール解決問題の回避
│   │   │   ├── test_extended_metrics.py
│   │   │   └── test_get_metrics_summary.py
│   │   ├── agent/
│   │   │   ├── __init__.py
│   │   │   ├── test_main.py
│   │   │   ├── test_orchestrator.py
│   │   │   ├── test_feedback_processor.py  # F8 フィードバック学習テスト
│   │   │   ├── test_dynamodb_client.py     # source_id 一意化（M1）
│   │   │   ├── test_notion_client.py
│   │   │   ├── test_github_client.py
│   │   │   ├── test_slack_client.py
│   │   │   └── diagnosis/             # src/agent/diagnosis/ のテスト（偽の DynamoDB・S3・SDK）
│   │   └── fixer/                     # src/fixer/ のテスト（偽の SDK。git は一時ディレクトリの実物）
│   └── integration/                   # 統合テスト
│       ├── __init__.py
│       ├── conftest.py                # _load_prompt モック（プロンプトファイル不在対応）
│       └── test_workflow.py
│
├── frontend/                          # ダッシュボード SPA（Vite + React + TypeScript）
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── src/
│       ├── api/                       # API クライアント・エンドポイント定義・型
│       ├── components/                # UI コンポーネント（shadcn/ui + カスタム）
│       ├── contexts/                  # AuthContext（JWT cookie セッション管理）
│       ├── lib/                       # ユーティリティ（時刻フォーマット等）
│       ├── routes/                    # ページレベルのルートコンポーネント
│       └── test/                      # Vitest セットアップ
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

### `src/observability/` — 観測イベント共通ヘルパー

`event_emitter.py` が DynamoDB の `{prefix}-events` テーブルへのイベント書き込みユーティリティを提供する。エージェントコンテナ（ECS）と Lambda トリガー関数の両方からインポートされ、ワークフロー各フェーズのイベントを一元的に記録する。

### `src/fixer/` — 修正役: 診断にもとづく修正（隔離された環境で、手で動かす）

実行診断役（`src/agent/diagnosis/`）が出した受け渡し用の診断結果を 1 つ受け取り、リポジトリの複製の中で、失敗を再現するテストと修正を作る。コードによる合否の検査に通ったものだけを、手元の作業用ブランチにコミットし、プルリクエスト本文の下書きを添える。プッシュと取り込みは人が行う。

**本番のイメージには入れない**: Dockerfile が入れるのは `src/agent/` と `src/observability/` だけで、`template.yaml` からも参照しない。本番の認証情報がない、**隔離された環境**（使い捨てのコンテナか、専用のユーザー）で、人が手で起動する（`python -m src.fixer`）。位置づけは「隔離された環境で、試験的に動かせる段階」で、ふだんの開発の環境では使わない（実際の LLM を使った動作は未検証）。第 1 段階のコード（決まった値、場所の突き合わせ、マスク）は、変更せずに import して使う。使い方と隔離の条件は `docs/execution-fixer.md`、取り込みの手順は `docs/development-guidelines.md` 5 章。

### `src/dashboard_api/` — ダッシュボード API Lambda 群

Slack OAuth 認証・JWT cookie 管理・観測データ取得の API エンドポイントを実装する Lambda 関数群。SAM テンプレートから `AWS::Serverless::HttpApi` にルーティングされる。`_common.py` が全 Lambda 共通のレスポンスヘルパー・クエリユーティリティを提供する。

| サブディレクトリ | 役割 |
|----------------|------|
| `authorizer/` | Lambda Authorizer。JWT cookie を検証し API Gateway を保護 |
| `auth_login/` `auth_callback/` `auth_logout/` `auth_me/` | Slack OAuth フロー |
| `list_executions/` `get_execution/` `get_execution_events/` | 実行データ取得 |
| `get_metrics_summary/` `get_cost_summary/` `get_api_health/` `get_token_monitor_health/` `get_review_quality/` `get_feedback_aggregation/` | メトリクス・分析データ取得 |
| `get_errors/` | エラーイベント取得 |
| `get_my_profile/` | F6 ユーザープロファイル閲覧（`UserProfilesTable` GetItem、read-only） |

### `src/agent/` — ECS エージェントアプリケーション

Claude Code CLIを使ってマルチAIエージェントを実行するメインアプリケーション。Dockerコンテナとしてビルドされ、ECS Fargateで実行される。

| サブディレクトリ | 役割 |
|----------------|------|
| `feedback/` | フィードバック解析・`learned_preferences` 更新（F8） |
| `storage/` | Notion API・GitHub APIの操作 |
| `state/` | DynamoDBによるワークフロー状態管理 |
| `notify/` | Slack通知の送信 |
| `diagnosis/` | 実行診断役。実行 ID を 1 つ受け取り、その実行の記録を読み取り専用のツールで調べて原因を診断する。手動実行（`python -m diagnosis`）と、失敗時の自動起動（`EXECUTION_DIAGNOSIS`、既定オフ）から使う。実際の LLM・AWS では未検証。使い方は `docs/execution-diagnostician.md` |

### `tests/` — テスト

| サブディレクトリ | 対象 |
|----------------|------|
| `unit/` | 各モジュールの単体テスト |
| `integration/` | ワークフロー全体の統合テスト |

### `frontend/` — ダッシュボード SPA

Vite 8 + React 19 + TypeScript で構築したオブザービリティダッシュボード。SAM ではなく `aws s3 sync` + CloudFront でデプロイする。

| サブディレクトリ | 役割 |
|----------------|------|
| `src/api/` | TanStack Query フック・エンドポイント定義・レスポンス型 |
| `src/components/` | shadcn/ui ベースの共通 UI コンポーネント |
| `src/contexts/` | `AuthContext`（`/api/v1/auth/me` ポーリング、5 分間隔） |
| `src/lib/` | 時刻フォーマット等のユーティリティ |
| `src/routes/` | ページコンポーネント（DashboardHome / ExecutionList / ExecutionDetail / ReviewQuality / ErrorList / FeedbackAnalysis） |
| `src/test/` | Vitest グローバルセットアップ |

## 4. ファイル配置ルール

### アプリケーションコード

| ルール | 説明 |
|--------|------|
| Lambda関数は `src/trigger/` に配置 | SAMテンプレートの `CodeUri` で参照 |
| ECSアプリケーションは `src/agent/` に配置 | `src/agent/Dockerfile` で管理（`sam build` 時に Docker daemon が読み取る） |
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
