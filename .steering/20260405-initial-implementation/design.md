# 実装設計

## 1. 実装アプローチ

### 実装順序

インフラ → トリガー → エージェント基盤 → 外部連携 → プロンプト → テスト → 結合の順で実装する。各レイヤーが下位レイヤーに依存するため、ボトムアップで進める。

```
Phase 1: インフラ基盤
  template.yaml（SAM）+ VPC + ECR + DynamoDB + Secrets Manager

Phase 2: トリガー（Lambda）
  Slack署名検証 → ACK応答 → ECSタスク起動

Phase 3: エージェント基盤（ECS）
  Dockerfile → 認証復元 → オーケストレーター骨格

Phase 4: 外部連携モジュール
  Slack通知 → DynamoDB状態管理 → Notion格納 → GitHub格納

Phase 5: エージェントプロンプト + オーケストレーターロジック
  専門プロンプト4種 → Claude Code CLI呼び出し → ワークフロー制御

Phase 6: テスト + 結合
  ユニットテスト → 結合テスト → E2Eテスト（Slackから実トピック投入）
```

### 開発方針

| 方針 | 説明 |
|------|------|
| 段階的デプロイ | Phase 1〜2完了時点でSlack→Lambda→ACK応答のみを先にデプロイし動作確認 |
| ローカルテスト優先 | `sam local invoke` でLambdaをローカルテスト。ECSはローカルDocker実行で確認 |
| プロンプト駆動開発 | エージェントのロジックはプロンプトに集約。Pythonコードはプロンプト読み込みとCLI呼び出しに専念 |

## 2. ファイル別の実装設計

### 2.1 `template.yaml`（SAMテンプレート）

すべてのAWSリソースを1つのSAMテンプレートに定義する。

#### 定義するリソース

| リソース | SAM/CFnタイプ | 論理名 |
|---------|-------------|--------|
| Lambda関数 | `AWS::Serverless::Function` | `TriggerFunction` |
| API Gateway | `AWS::Serverless::Api` | `SlackApi` |
| DynamoDB（プロファイル） | `AWS::DynamoDB::Table` | `UserProfilesTable` |
| DynamoDB（実行状態） | `AWS::DynamoDB::Table` | `WorkflowExecutionsTable` |
| DynamoDB（ステップ） | `AWS::DynamoDB::Table` | `WorkflowStepsTable` |
| DynamoDB（成果物） | `AWS::DynamoDB::Table` | `DeliverablesTable` |
| DynamoDB（出典） | `AWS::DynamoDB::Table` | `SourcesTable` |
| ECSクラスター | `AWS::ECS::Cluster` | `AgentCluster` |
| ECSタスク定義 | `AWS::ECS::TaskDefinition` | `AgentTaskDefinition` |
| ECRリポジトリ | `AWS::ECR::Repository` | `AgentRepository` |
| VPC | `AWS::EC2::VPC` | `AgentVpc` |
| パブリックサブネット × 2 | `AWS::EC2::Subnet` | `PublicSubnet1`, `PublicSubnet2` |
| インターネットゲートウェイ | `AWS::EC2::InternetGateway` | `InternetGateway` |
| ルートテーブル | `AWS::EC2::RouteTable` | `PublicRouteTable` |
| セキュリティグループ | `AWS::EC2::SecurityGroup` | `AgentSecurityGroup` |
| Lambda実行ロール | `AWS::IAM::Role` | `TriggerFunctionRole` |
| ECSタスクロール | `AWS::IAM::Role` | `AgentTaskRole` |
| ECSタスク実行ロール | `AWS::IAM::Role` | `AgentTaskExecutionRole` |
| Secrets Manager（参照のみ） | パラメータで ARN を受け取る | - |
| CloudWatch Logsグループ | `AWS::Logs::LogGroup` | `AgentLogGroup` |

#### パラメータ

```yaml
Parameters:
  SlackBotTokenSecretArn:
    Type: String
  SlackSigningSecretArn:
    Type: String
  NotionTokenSecretArn:
    Type: String
  GitHubTokenSecretArn:
    Type: String
  ClaudeOAuthSecretArn:
    Type: String
  NotionDatabaseId:
    Type: String
  GitHubRepo:
    Type: String
  DynamoDbTablePrefix:
    Type: String
    Default: catch-expander
```

### 2.2 `src/trigger/app.py`（Lambdaハンドラー）

```
lambda_handler(event, context)
  │
  ├── Slack署名検証（slack_verify.py）
  │     失敗 → 403返却
  │
  ├── URL Verification チャレンジ対応
  │     type == "url_verification" → challenge返却
  │
  ├── イベントタイプ判定
  │     app_mention or message.im 以外 → 200返却（無視）
  │
  ├── Slackへ ACK メッセージ投稿
  │     chat.postMessage("受け付けました...")
  │
  ├── DynamoDB に実行レコード作成
  │     status: "received"
  │
  ├── ECS RunTask API 呼び出し（非同期）
  │     環境変数でexecution_id, user_id, topic, channel, thread_tsを渡す
  │
  └── 200 OK 返却
```

#### `src/trigger/slack_verify.py`

```python
def verify_slack_signature(signing_secret: str, timestamp: str, body: str, signature: str) -> bool:
    """Slack署名検証（HMAC-SHA256）"""
```

### 2.3 `src/agent/main.py`（エントリーポイント）

ECSタスク起動時に実行される。認証復元 → オーケストレーター起動が責務。

```
main()
  │
  ├── 環境変数から実行パラメータ取得
  │     EXECUTION_ID, USER_ID, TOPIC, SLACK_CHANNEL, SLACK_THREAD_TS
  │
  ├── Secrets Manager から認証情報取得
  │     ├── Claude Code OAuth → ~/.claude/ に配置
  │     ├── Slack Bot Token
  │     ├── Notion Token
  │     └── GitHub Token
  │
  ├── DynamoDB ステータス更新（analyzing）
  │
  ├── orchestrator.run(execution_id, user_id, topic, ...) 呼び出し
  │
  ├── 正常完了 → DynamoDB ステータス更新（completed）
  │
  └── 異常終了 → DynamoDB ステータス更新（failed）+ Slackエラー通知
```

### 2.4 `src/agent/orchestrator.py`（オーケストレーター）

Claude Code CLIを呼び出してマルチエージェントワークフローを制御する。

```
run(execution_id, user_id, topic, ...)
  │
  ├── プロファイル取得（DynamoDB）
  │
  ├── Claude Code CLI 呼び出し: トピック解析
  │     入力: topic + user_profile
  │     出力: JSON（category, intent, perspectives, deliverable_types）
  │     DynamoDB更新（planning）、Slack通知
  │
  ├── Claude Code CLI 呼び出し: ワークフロー設計
  │     入力: 解析結果 + user_profile
  │     出力: JSON（research_steps[], generate_steps[], qa_steps[]）
  │     DynamoDB更新（researching）、Slack通知（計画）
  │
  ├── リサーチャー並列起動（ThreadPoolExecutorでsubprocess並列）
  │     for step in research_steps:
  │       call_claude(researcher_prompt + step指示) → 並列実行
  │     全完了待ち → 結果統合
  │     DynamoDB更新、Slack通知（調査完了）
  │
  ├── ジェネレーター起動
  │     call_claude(generator_prompt + 調査結果 + 計画 + profile)
  │     code_filesがnullの場合 → _build_code_generation_prompt()で追加呼び出し
  │     DynamoDB更新（generating）、Slack通知
  │
  ├── レビュアー起動
  │     call_claude(reviewer_prompt + 成果物 + 出典 + チェックリスト, model="opus")
  │     不合格 → ジェネレーター修正 → 再レビュー（最大2回）
  │     DynamoDB更新（reviewing）
  │
  ├── 格納処理
  │     DynamoDB更新（storing）
  │     Notion: ページ作成（notion_client.py）
  │     GitHub: コードpush（該当時のみ、github_client.py）
  │     Slack通知（完了）
  │
  └── 品質メタデータ付与 → 完了
```

#### Claude Code CLI呼び出しの実装パターン

```python
import subprocess
import json

def call_claude(prompt: str, allowed_tools: list[str] | None = None, model: str = "sonnet") -> str:
    """Claude Code CLIを呼び出し、結果を返す。
    通常ステップはsonnet、レビュアーのみopusを指定。
    """
    cmd = ["claude", "-p", "-", "--model", model, "--output-format", "json"]
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, check=True)
    return result.stdout
```

#### サブエージェント並列実行の実装パターン

```python
import concurrent.futures

def run_researchers(steps: list[dict], researcher_prompt: str) -> list[dict]:
    """リサーチャーエージェントを並列実行"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(steps)) as executor:
        futures = {
            executor.submit(call_claude, f"{researcher_prompt}\n\n調査指示:\n{json.dumps(step)}"): step
            for step in steps
        }
        results = []
        for future in concurrent.futures.as_completed(futures):
            results.append(json.loads(future.result()))
    return results
```

### 2.5 `src/agent/notify/slack_client.py`

```python
class SlackClient:
    def __init__(self, bot_token: str):
        self.client = WebClient(token=bot_token)

    def post_progress(self, channel: str, thread_ts: str, message: str) -> None:
        """進捗通知をスレッドに投稿"""

    def post_completion(self, channel: str, thread_ts: str, summary: str, notion_url: str, github_url: str | None) -> None:
        """完了通知をスレッドに投稿"""

    def post_error(self, channel: str, thread_ts: str, error_message: str) -> None:
        """エラー通知をスレッドに投稿"""
```

### 2.6 `src/agent/state/dynamodb_client.py`

```python
class DynamoDbClient:
    def __init__(self, table_prefix: str):
        self.dynamodb = boto3.resource("dynamodb")

    # ユーザープロファイル
    def get_user_profile(self, user_id: str) -> dict | None:
    def put_user_profile(self, profile: dict) -> None:

    # ワークフロー実行
    def create_execution(self, execution: dict) -> None:
    def update_execution_status(self, execution_id: str, status: str) -> None:
    def get_execution(self, execution_id: str) -> dict:

    # ワークフローステップ
    def put_step(self, step: dict) -> None:
    def update_step_status(self, execution_id: str, step_id: str, status: str, result: dict | None = None) -> None:

    # 成果物
    def put_deliverable(self, deliverable: dict) -> None:

    # 出典
    def put_sources(self, execution_id: str, sources: list[dict]) -> None:
        """URLで重複排除し、各エントリに新規UUIDのsource_idを付与してbatch_writer()で一括登録。
        IAM要件: dynamodb:BatchWriteItem が必要。
        """
```

### 2.7 `src/agent/storage/notion_client.py`

```python
class NotionClient:
    def __init__(self, token: str, database_id: str):
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}

    def create_page(self, title: str, category: str, content_blocks: list[dict],
                    github_url: str | None, slack_user: str) -> tuple[str, str]:
        """成果物ページを作成し、(page_url, page_id) タプルを返す。
        content_blocksが100件超の場合は100件ずつチャンク分割して投稿。
        """

    def update_page_status(self, page_id: str, status: str) -> None:
        """ページステータスを更新"""

    def append_blocks(self, page_id: str, blocks: list[dict]) -> None:
        """ページにブロックを追記（推敲・品質メタデータ追加用）"""
```

### 2.8 `src/agent/storage/github_client.py`

```python
class GitHubClient:
    def __init__(self, token: str, repo: str):
        self.headers = {"Authorization": f"Bearer {token}"}

    def push_files(self, directory_name: str, files: dict[str, str]) -> str:
        """ファイル群をリポジトリにpushし、ディレクトリURLを返す
        files: {ファイルパス: ファイル内容} の辞書
        """

    def create_readme(self, directory_name: str, content: str, notion_url: str) -> None:
        """README.mdを作成（Notionページへのリンク含む）"""
```

### 2.9 エージェント専門プロンプト

各プロンプトはMarkdownファイルとして `src/agent/prompts/` に配置し、orchestrator.pyからファイル読み込みして使用する。

#### `orchestrator.md` の構成

```markdown
# オーケストレーター

## 役割
あなたはCatch Expanderのオーケストレーターエージェントです。
トピックを解析し、最適なワークフローを設計してください。

## トピック解析
入力テキストから以下をJSON形式で出力:
- category: カテゴリ（技術/時事/ビジネス/学術/カルチャー）
- intent: ユーザーの意図
- perspectives: 深掘り観点のリスト
- deliverable_types: 成果物タイプのリスト

## ワークフロー設計
（ユーザープロファイルとの組み合わせルール、成果物タイプ選択基準等）
```

#### `researcher.md` の構成

```markdown
# リサーチャー

## 役割
あなたは調査専門のリサーチャーエージェントです。

## ソース優先順位
（カテゴリ別のソース優先順位表）

## 出力形式
- 要約テキスト
- 出典リスト（URL, タイトル, 公開日, ソース種別, 優先度）

## 制約
- 出典URLのないファクトは記載しない
- ソース優先順位に従ってソースを選択する
```

#### `generator.md` の構成

```markdown
# ジェネレーター

## 役割
あなたは成果物生成専門のジェネレーターエージェントです。

## 成果物タイプ別の構造化ルール
（テキスト成果物: Notionブロック形式、コード成果物: ファイル単位）

## ユーザープロファイルの反映ルール
（担当クラウド、技術スタック等に基づくカスタマイズ）
```

#### `reviewer.md` の構成

```markdown
# レビュアー

## 役割
あなたは品質検証専門のレビュアーエージェントです。
ジェネレーターとは独立した視点で成果物を評価してください。

## 第1層: ソース検証
（出典URLの実在確認、内容整合性チェック）

## 第2層: チェックリスト評価
（カテゴリ別チェックリスト）

## 第3層: 品質メタデータ生成
（出力JSON形式の定義）

## 出力形式
- passed: true/false
- issues: [{項目, 修正指示}]
- quality_metadata: {検証ステータス, 情報鮮度, レビュー結果}
```

### 2.10 Dockerfile

```dockerfile
FROM node:22-slim

# 非rootユーザー作成
RUN groupadd -r appuser && useradd -r -g appuser -m appuser

# Claude Code CLIのインストール
RUN npm install -g @anthropic-ai/claude-code

# Python
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# アプリケーションコード
COPY src/agent/ /app/
WORKDIR /app

# Python依存パッケージ
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

# 非rootユーザーで実行
USER appuser

ENTRYPOINT ["python3", "main.py"]
```

## 3. データフロー

### トリガー → エージェント間のデータ受け渡し

ECSタスク起動時にコンテナの環境変数として渡す。

| 環境変数 | 内容 | 設定元 |
|---------|------|--------|
| `EXECUTION_ID` | ワークフロー実行ID | Lambda（生成） |
| `USER_ID` | Slack User ID | Lambda（Slackイベントから取得） |
| `TOPIC` | トピックテキスト | Lambda（Slackイベントから取得） |
| `SLACK_CHANNEL` | 通知先チャンネルID | Lambda（Slackイベントから取得） |
| `SLACK_THREAD_TS` | ACKメッセージのthread_ts | Lambda（ACK応答のレスポンスから取得） |

### エージェント間のデータ受け渡し

Claude Code CLIの `--output-format json` でJSON形式のレスポンスを取得し、Pythonで解析して次のエージェントに渡す。

```
オーケストレーター
  │
  │ call_claude(topic解析プロンプト) → JSON
  │ call_claude(WF設計プロンプト)   → JSON
  │
  ├── call_claude(リサーチャープロンプト + step指示) → JSON（要約 + 出典）
  ├── call_claude(リサーチャープロンプト + step指示) → JSON（要約 + 出典）
  ├── ...
  │
  │ 結果統合（Python）
  │
  ├── call_claude(ジェネレータープロンプト + 統合結果) → JSON（成果物）
  │
  └── call_claude(レビュアープロンプト + 成果物) → JSON（合否 + 修正指示）
```

## 4. エラーハンドリング設計

### リトライ戦略

| 対象 | リトライ回数 | バックオフ | フォールバック |
|------|:----------:|-----------|--------------|
| Claude Code CLI呼び出し | 3回 | 指数（2s, 4s, 8s） | エラー通知 + 部分成果物出力 |
| Notion API | 3回 | 指数（1s, 2s, 4s） | Slackに成果物テキストを直接投稿 |
| GitHub API | 3回 | 指数（1s, 2s, 4s） | Notionのコードブロックに格納 |
| Slack API（通知） | 3回 | 指数（1s, 2s, 4s） | ログ出力のみ（通知失敗は致命的でない） |
| DynamoDB | 3回 | 指数（1s, 2s, 4s） | ログ出力（状態管理の失敗はワークフロー継続） |

### 部分成果物出力

リサーチャーの一部が失敗した場合、成功したステップの結果のみでジェネレーターに進む。失敗情報は品質メタデータに記載する。

```python
results = run_researchers(steps, prompt)
failed_steps = [s for s in results if s.get("error")]
successful_results = [s for s in results if not s.get("error")]

if not successful_results:
    # 全ステップ失敗 → エラー通知して終了
    raise WorkflowError("All research steps failed")

# 部分結果でジェネレーターに進む
# failed_stepsの情報は品質メタデータに含める
```
