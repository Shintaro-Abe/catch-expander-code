# タスクリスト

## 概要

`design.md` の6フェーズ実装設計に基づくタスクリスト。各タスクは依存関係順に並べている。

---

## Phase 1: インフラ基盤

### 1-1. プロジェクト初期設定

- [x] `pyproject.toml` 作成（Ruff設定、プロジェクトメタデータ）
- [x] `requirements-dev.txt` 作成（pytest, pytest-cov, ruff, pip-audit）
- [x] `.gitignore` 作成（Python, Node.js, AWS SAM, シークレットファイル）
- [x] `samconfig.toml` 作成（SAMデプロイ設定）

### 1-2. SAMテンプレート作成

- [x] `template.yaml` 作成
  - [x] Parameters（Secrets Manager ARN群、NotionDatabaseId、GitHubRepo、DynamoDbTablePrefix）
  - [x] VPC（`AgentVpc`、CIDR: 10.0.0.0/16）
  - [x] パブリックサブネット × 2 AZ（`PublicSubnet1`, `PublicSubnet2`）
  - [x] インターネットゲートウェイ（`InternetGateway`）+ VPCアタッチ
  - [x] ルートテーブル（`PublicRouteTable`）+ ルート + サブネット関連付け
  - [x] セキュリティグループ（`AgentSecurityGroup` インバウンド全拒否、アウトバウンドHTTPS）
  - [x] DynamoDB 5テーブル（UserProfiles, WorkflowExecutions, WorkflowSteps, Deliverables, Sources）
  - [x] ECRリポジトリ（`AgentRepository`）
  - [x] ECSクラスター（`AgentCluster`）
  - [x] ECSタスク定義（`AgentTaskDefinition` 1vCPU, 2GB, ARM64）
  - [x] ECSタスクロール（`AgentTaskRole` DynamoDB+BatchWriteItem, Secrets Manager, ECS）
  - [x] ECSタスク実行ロール（`AgentTaskExecutionRole` ECR pull, CloudWatch Logs, Secrets Manager）
  - [x] Lambda関数（`TriggerFunction` Python 3.13, 256MB, 10秒）
  - [x] API Gateway（`SlackApi` REST API, POST /slack/events）
  - [x] Lambda実行ロール（`TriggerFunctionRole` DynamoDB, ECS RunTask, Secrets Manager, Slack通知用）
  - [x] CloudWatch Logsグループ（`AgentLogGroup`）
  - [x] Outputs（API Gateway URL, ECRリポジトリURI等）

---

## Phase 2: トリガー（Lambda）

### 2-1. Slack署名検証

- [x] `src/trigger/__init__.py` 作成
- [x] `src/trigger/slack_verify.py` 作成
  - [x] `verify_slack_signature()` 関数（HMAC-SHA256）

### 2-2. Lambdaハンドラー

- [x] `src/trigger/app.py` 作成
  - [x] Slack署名検証の呼び出し
  - [x] URL Verification チャレンジ対応
  - [x] イベントタイプ判定（app_mention / message.im）
  - [x] SlackへACKメッセージ投稿（chat.postMessage）
  - [x] DynamoDBに実行レコード作成（status: received）
  - [x] ECS RunTask API呼び出し（環境変数でパラメータ受け渡し）
  - [x] 200 OK返却

### 2-3. Lambda依存パッケージ

- [x] `src/trigger/requirements.txt` 作成（boto3, slack_sdk, aws-lambda-powertools）

### 2-4. Phase 1〜2 段階デプロイ・動作確認

- [x] `sam build` + `sam deploy` でインフラ + Lambda をデプロイ
- [x] Slack → API Gateway → Lambda → ACK応答の動作確認
- [x] ECS RunTask が起動されることの確認

---

## Phase 3: エージェント基盤（ECS）

### 3-1. Dockerfile

- [x] `src/agent/Dockerfile` 作成
  - [x] ベースイメージ: node:22-slim
  - [x] 非rootユーザー作成（appuser）
  - [x] Claude Code CLIインストール（npm install -g）
  - [x] Python 3 + pip インストール
  - [x] アプリケーションコードのコピー
  - [x] Python依存パッケージインストール
  - [x] USER appuser + ENTRYPOINT

### 3-2. エージェント依存パッケージ

- [x] `src/agent/requirements.txt` 作成（boto3, slack_sdk, python-json-logger, requests）

### 3-3. エントリーポイント

- [x] `src/agent/__init__.py` 作成
- [x] `src/agent/main.py` 作成
  - [x] 環境変数からの実行パラメータ取得
  - [x] Secrets Managerから認証情報取得（Claude OAuth, Slack, Notion, GitHub）
  - [x] Claude OAuth認証情報を `~/.claude/` に配置
  - [x] DynamoDBステータス更新（analyzing）
  - [x] orchestrator.run() 呼び出し
  - [x] 正常完了 → completed / 異常終了 → failed + Slackエラー通知

### 3-4. オーケストレーター骨格

- [x] `src/agent/orchestrator.py` 骨格作成
  - [x] `call_claude()` 関数（Claude Code CLI呼び出し、subprocess）
  - [x] `run()` 関数の骨格（Phase 5で本実装）

### 3-5. コンテナビルド・動作確認

- [x] ローカルDocker build + 起動テスト
- [x] ECRへpush
- [x] ECSタスクとしての起動確認（認証復元まで）

---

## Phase 4: 外部連携モジュール

### 4-1. Slack通知クライアント

- [x] `src/agent/notify/__init__.py` 作成
- [x] `src/agent/notify/slack_client.py` 作成
  - [x] `SlackClient.__init__(bot_token)`
  - [x] `post_progress(channel, thread_ts, message)` → 進捗通知
  - [x] `post_completion(channel, thread_ts, summary, notion_url, github_url)` → 完了通知
  - [x] `post_error(channel, thread_ts, error_message)` → エラー通知

### 4-2. DynamoDB状態管理クライアント

- [x] `src/agent/state/__init__.py` 作成
- [x] `src/agent/state/dynamodb_client.py` 作成
  - [x] `DynamoDbClient.__init__(table_prefix)`
  - [x] `get_user_profile(user_id)` / `put_user_profile(profile)`
  - [x] `create_execution(execution)` / `update_execution_status(execution_id, status)` / `get_execution(execution_id)`
  - [x] `put_step(step)` / `update_step_status(execution_id, step_id, status, result)`
  - [x] `put_deliverable(deliverable)`
  - [x] `put_sources(execution_id, sources)` ※URLで重複排除、UUID再生成、batch_writer()で一括登録

### 4-3. Notion格納クライアント

- [x] `src/agent/storage/__init__.py` 作成
- [x] `src/agent/storage/notion_client.py` 作成
  - [x] `NotionClient.__init__(token, database_id)`
  - [x] `create_page(title, category, content_blocks, github_url, slack_user)` → `(page_url, page_id)` のタプルを返却（100ブロック超の場合はチャンク分割投稿）
  - [x] `update_page_status(page_id, status)`
  - [x] `append_blocks(page_id, blocks)` → 推敲・品質メタデータ追加用

### 4-4. GitHub格納クライアント

- [x] `src/agent/storage/github_client.py` 作成
  - [x] `GitHubClient.__init__(token, repo)`
  - [x] `push_files(directory_name, files)` → ディレクトリURL返却
  - [x] `create_readme(directory_name, content, notion_url)`

---

## Phase 5: エージェントプロンプト + オーケストレーターロジック

### 5-1. エージェント専門プロンプト作成

- [x] `src/agent/prompts/orchestrator.md` 作成
  - [x] 役割定義
  - [x] トピック解析ルール（カテゴリ・意図・観点・成果物タイプの判断基準）
  - [x] ワークフロー設計ルール（プロファイル反映、成果物タイプ選択基準）
  - [x] 出力JSON形式の定義
- [x] `src/agent/prompts/researcher.md` 作成
  - [x] 役割定義
  - [x] カテゴリ別ソース優先順位表
  - [x] 調査手法・要約ルール
  - [x] 出力形式（要約 + 出典リスト）+ JSON-only指示
  - [x] 制約（出典なしファクト禁止）
- [x] `src/agent/prompts/generator.md` 作成
  - [x] 役割定義
  - [x] 成果物タイプ別構造化ルール（Notionブロック形式、コードファイル単位）
  - [x] ユーザープロファイル反映ルール
  - [x] 推敲プロセス + JSON-only指示
- [x] `src/agent/prompts/reviewer.md` 作成
  - [x] 役割定義
  - [x] 第1層: ソース検証ルール
  - [x] 第2層: カテゴリ別チェックリスト
  - [x] 第3層: 品質メタデータ生成ルール
  - [x] 出力形式（passed, issues, quality_metadata）+ JSON-only指示

### 5-2. オーケストレーター本実装

- [x] `src/agent/orchestrator.py` 本実装
  - [x] プロファイル取得（DynamoDB）
  - [x] トピック解析（Claude Code CLI呼び出し）
  - [x] ワークフロー設計（Claude Code CLI呼び出し）
  - [x] リサーチャー並列実行（ThreadPoolExecutor）
  - [x] 結果統合
  - [x] ジェネレーター起動
  - [x] コード成果物独立生成（code_filesがnullの場合のフォールバック、`_build_code_generation_prompt()`）
  - [x] レビュアー起動 + レビューループ（最大2回、Opusモデル指定）
  - [x] Notion格納（+ GitHub格納：コード成果物がある場合）
  - [x] 品質メタデータ付与
  - [x] 各ステップでのDynamoDB更新 + Slack進捗通知
  - [x] エラーハンドリング（リトライ、部分成果物出力、フォールバック）
  - [x] 4戦略JSONパーサー（前置き文・trailing content・外側JSON失敗への対応）

---

## Phase 6: テスト + 結合

### 6-1. ユニットテスト

- [x] `tests/unit/trigger/test_app.py` 作成（10テスト全パス）
  - [x] Slack署名検証の成功/失敗テスト
  - [x] URL Verificationチャレンジのテスト
  - [x] イベントタイプ判定のテスト（unknown / bot / subtype付きmessage / 空トピック）
  - [x] ACK応答 + ECS RunTask呼び出しのテスト（app_mention / DM）
  - [x] X-Slack-Retry-Numリトライ無視のテスト
  - ※ conftest.py で sys.modules swap により botocore[crt] 依存問題を回避
- [x] `tests/unit/agent/test_orchestrator.py` 作成（17テスト全パス）
  - [x] call_claude()のテスト（subprocess.runモック）
  - [x] _parse_claude_response()の各戦略テスト（前置き文・trailing content・断片JSON無視・parse_error等）
  - [x] run_researchers()の並列実行テスト
  - [x] レビューループのテスト（初回合格・修正→合格・全失敗）
  - [x] エラーハンドリング・部分成果物テスト
- [x] `tests/unit/agent/test_slack_client.py` 作成
  - [x] 各通知メソッドのテスト
  - [x] Slack APIエラー時のリトライテスト
- [x] `tests/unit/agent/test_notion_client.py` 作成（7テスト全パス）
  - [x] ページ作成のテスト（基本 / GitHub URL付き / 100ブロック超チャンク分割）
  - [x] ブロック追記のテスト（append_blocks）
  - [x] ステータス更新のテスト（update_page_status）
  - [x] APIエラー時のリトライテスト（5xx：3回リトライ / 4xx：即raise）
- [x] `tests/unit/agent/test_github_client.py` 作成
  - [x] ファイルpushのテスト
  - [x] APIエラー時のリトライテスト

### 6-2. 統合テスト

- [x] `tests/integration/test_workflow.py` 作成
  - [x] ワークフロー全体のE2Eフロー（外部APIはモック）
  - [x] 部分失敗時のフォールバック動作

### 6-3. 品質チェック

- [x] Ruffリント + フォーマット実行（全ファイル）
- [x] 型ヒントの確認
- [x] pip-audit による脆弱性チェック

### 6-4. E2Eテスト（実環境）

- [x] コンテナイメージを ECR へ push
- [x] `sam deploy` で全リソースデプロイ
- [x] Slackから実トピック投入 → 完了通知まで確認
- [x] Notionに成果物ページが作成されていることを確認
- [x] 品質メタデータが付与されていることを確認
- [x] GitHubにコード成果物がpushされていることを確認（技術トピック）
- [x] DynamoDBに実行レコード・出典情報が登録されていることを確認
