# 開発ガイドライン

## 1. コーディング規約

### Python

| 項目 | 規約 |
|------|------|
| バージョン | 3.13 |
| スタイル | PEP 8 準拠 |
| フォーマッター | Ruff（format） |
| リンター | Ruff（lint） |
| 型ヒント | 必須（全関数の引数・戻り値に付与） |
| docstring | Google スタイル |
| 最大行長 | 120文字 |

#### Ruff設定

```toml
# pyproject.toml
[tool.ruff]
target-version = "py313"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I", "W", "UP", "B", "SIM", "S"]

[tool.ruff.format]
quote-style = "double"
```

#### import順序

```python
# 1. 標準ライブラリ
import json
import os
from datetime import datetime

# 2. サードパーティ
import boto3
from slack_sdk import WebClient

# 3. ローカル
from storage.notion_client import NotionClient
```

### IaC（SAMテンプレート）

| 項目 | 規約 |
|------|------|
| 形式 | YAML |
| インデント | 2スペース |
| リソース論理名 | PascalCase（例: `TriggerFunction`, `WorkflowExecutionsTable`） |
| パラメータ名 | PascalCase（例: `SlackBotTokenSecretArn`） |

### Dockerfile

| 項目 | 規約 |
|------|------|
| ベースイメージ | バージョンを明示（`node:22-slim`、`latest`は使用しない） |
| 実行ユーザー | 非rootユーザーで実行（`USER`命令で指定） |
| レイヤー順序 | 変更頻度の低い順（OS依存 → パッケージ → アプリコード） |
| マルチステージ | 不要（ビルドステップがないため） |

## 2. 命名規則

### Python

| 対象 | 規則 | 例 |
|------|------|-----|
| ファイル名 | snake_case | `notion_client.py` |
| クラス名 | PascalCase | `NotionClient` |
| 関数名 | snake_case | `create_page` |
| 変数名 | snake_case | `execution_id` |
| 定数 | UPPER_SNAKE_CASE | `MAX_RETRY_COUNT` |
| プライベート | `_` プレフィックス | `_build_blocks` |

### エージェントプロンプト

| 対象 | 規則 | 例 |
|------|------|-----|
| ファイル名 | エージェント名.md | `orchestrator.md`, `researcher.md` |

### AWSリソース

| 対象 | 規則 | 例 |
|------|------|-----|
| SAM論理名 | PascalCase | `TriggerFunction` |
| DynamoDBテーブル | kebab-case（プレフィックス付き） | `catch-expander-workflow-executions` |
| Secrets Manager | スラッシュ区切り（プレフィックス付き） | `catch-expander/slack-bot-token` |
| CloudWatch Logsグループ | AWSデフォルトに従う | `/aws/lambda/catch-expander-trigger` |
| IAMロール | PascalCase + Role | `CatchExpanderTriggerRole` |

### 環境変数

| 規則 | 例 |
|------|-----|
| UPPER_SNAKE_CASE | `SLACK_BOT_TOKEN`, `NOTION_DATABASE_ID` |

## 3. エラーハンドリング規約

### 基本方針

- 外部API呼び出し（Slack, Notion, GitHub）は必ず例外処理で囲む
- リトライは最大3回、指数バックオフで実行
- 内部ロジックの想定外エラーはキャッチせずそのまま伝播させる

### ログ出力

| レベル | 用途 |
|--------|------|
| `INFO` | ワークフローの開始・完了、主要ステップの通過 |
| `WARNING` | リトライ発生、部分的な失敗、フォールバック実行 |
| `ERROR` | 外部API呼び出し失敗、ワークフロー中断 |

Lambda環境ではAWS Lambda Powertools for Pythonの `Logger` を使用し、ECS環境では標準 `logging` を使用する。

```python
# Lambda（AWS Lambda Powertools）
from aws_lambda_powertools import Logger

logger = Logger(service="catch-expander-trigger")
logger.info("Task started", extra={"execution_id": execution_id})

# ECS（標準 logging）
import logging

logger = logging.getLogger("catch-expander-agent")

logger.info("Workflow started", extra={"execution_id": execution_id, "topic": topic})
```

### ログへのシークレット漏洩防止

| ルール | 説明 |
|--------|------|
| トークン・シークレットをログに出力しない | APIトークン、OAuth認証情報、Signing Secretをログに含めない |
| 外部APIのリクエスト/レスポンスをそのまま出力しない | HTTPライブラリの例外にはURLやAuthorizationヘッダーが含まれる場合がある。例外をログ出力する際はメッセージのみ記録し、ヘッダー情報を除外する |
| ユーザープロファイルの個人情報をログに含めない | `user_id`（Slack ID）は可、ロールや技術スタック等の詳細は不可 |
| ログに出力してよい情報 | `execution_id`, `user_id`, `topic`, `status`, `step_name`, エラーメッセージ（シークレット除去済み） |

## 4. テスト規約

### テストフレームワーク

| 項目 | ツール |
|------|--------|
| テストランナー | pytest |
| モック | unittest.mock（標準ライブラリ） |
| カバレッジ | pytest-cov |

### テスト方針

| テスト種別 | 対象 | 実行タイミング |
|-----------|------|--------------|
| ユニットテスト | 各モジュールの関数・クラス | コード変更時 |
| 統合テスト | ワークフロー全体（外部APIはモック） | デプロイ前 |

### テストファイルの配置

```
src/agent/orchestrator.py      → tests/unit/agent/test_orchestrator.py
src/agent/storage/notion_client.py → tests/unit/agent/test_notion_client.py
src/trigger/app.py             → tests/unit/trigger/test_app.py
```

### テスト命名

```python
# テスト関数: test_<対象メソッド>_<状況>_<期待結果>
def test_create_page_success_returns_page_url():
    ...

def test_create_page_api_error_retries_three_times():
    ...
```

### 実行コマンド

```bash
# 全テスト実行
pytest

# カバレッジ付き
pytest --cov=src --cov-report=term-missing

# 特定ディレクトリ
pytest tests/unit/agent/
```

## 5. Git規約

### ブランチ戦略

個人開発のため `main` ブランチのみで運用する。

### コミットメッセージ

Conventional Commits形式に従う。

```
<type>: <description>

[body]
```

| type | 用途 |
|------|------|
| `feat` | 新機能の追加 |
| `fix` | バグ修正 |
| `docs` | ドキュメントの追加・変更 |
| `refactor` | リファクタリング（機能変更なし） |
| `test` | テストの追加・変更 |
| `chore` | ビルド設定・CI・依存パッケージの変更 |

```
# 例
feat: add Notion page creation
fix: handle Slack API timeout with retry
docs: add credential setup guide
refactor: extract DynamoDB client from orchestrator
test: add unit tests for slack_verify
chore: update SAM template for ECS task definition
```

### コミット単位

- 1つの論理的な変更を1コミットにまとめる
- ドキュメントとコードの変更は別コミットにする
- 動作しない中間状態でコミットしない

## 6. 依存パッケージ管理

### Python

| 項目 | 方式 |
|------|------|
| パッケージ管理 | `requirements.txt`（pip） |
| バージョン固定 | `==` で厳密に固定 |
| 配置場所 | 各コンポーネントのディレクトリ配下 |

```
# src/trigger/requirements.txt
boto3==1.35.0
slack_sdk==3.33.0
aws-lambda-powertools==3.4.0
```

Lambda（`src/trigger/`）には `requirements.txt` を配置する。ECSエージェント（`src/agent/`）用の依存パッケージはデプロイ時に別途管理する（リポジトリ外）。

### 開発用依存

```
# requirements-dev.txt（リポジトリルート）
pytest==8.3.0
pytest-cov==6.0.0
ruff==0.8.0
pip-audit==2.7.0
```

## 7. セキュリティ規約

| ルール | 説明 |
|--------|------|
| シークレットをコードに含めない | 環境変数またはSecrets Manager経由で取得 |
| `.gitignore` でシークレットファイルを除外 | `.env`, `credentials.json`, `*.pem` |
| 依存パッケージの脆弱性確認 | `pip-audit` でデプロイ前に確認（`pip install pip-audit` で導入） |
| 入力値のバリデーション | Slackイベントの署名検証、想定外のイベントタイプの拒否 |
| ログへのシークレット漏洩防止 | トークン・認証情報・個人情報をログに出力しない（3. エラーハンドリング規約参照） |
| 静的セキュリティ解析 | Ruffの `"S"` ルール（bandit）で危険なコードパターンを検出 |
| コンテナの非root実行 | Dockerfile内で `USER` 命令を使い、非rootユーザーでアプリケーションを実行 |
