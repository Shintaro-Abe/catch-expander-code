# ユビキタス言語定義

## 1. ドメイン用語

本プロジェクトで統一して使用する用語を定義する。ドキュメント・コード・会話のすべてで同じ用語を使用する。

### コアコンセプト

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| トピック | Topic | ユーザーがSlackで送信する関心事のテキスト。キーワード、短文、質問形式のいずれか | `topic` |
| 成果物 | Deliverable | エージェントが生成する最終出力物の総称。レポート、コード、設計書等を含む | `deliverable` |
| ワークフロー | Workflow | トピックに対してエージェントが自律的に設計する調査・生成・品質担保の一連の処理手順 | `workflow` |
| ワークフロー実行 | Workflow Execution | 1つのトピックに対するワークフローの実行インスタンス | `execution` |
| ワークフローステップ | Workflow Step | ワークフロー内の個々の処理単位（例: 概要調査、コード生成、ソース検証） | `step` |
| フェーズ | Phase | ワークフローの大区分。調査・生成・品質担保の3フェーズで構成される | `phase` |
| カテゴリ | Category | トピックの分野分類。技術、時事、ビジネス、学術、カルチャー等 | `category` |
| 意図 | Intent | ユーザーがトピックで知りたいことの方向性。技術導入検討、情報キャッチアップ等 | `intent` |
| 深掘り観点 | Perspective | トピックに対する調査の切り口。概要、実装方法、コスト、リスク等 | `perspective` |
| 出典 | Source | 成果物内の事実主張を裏付ける情報源。URL・タイトル・公開日を含む | `source` |
| 品質メタデータ | Quality Metadata | 成果物の信頼性を示す付属情報。検証ステータス、情報鮮度、レビュー結果 | `quality_metadata` |

### エージェント

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| オーケストレーター | Orchestrator | 全体の司令塔エージェント。トピック解析、ワークフロー設計、サブエージェント管理、格納処理を担当 | `orchestrator` |
| リサーチャー | Researcher | 調査専門のサブエージェント。Web検索による情報収集・要約・出典記録を担当。並列で複数起動される | `researcher` |
| ジェネレーター | Generator | 成果物生成専門のサブエージェント。調査結果をもとにレポート・コード・設計書を生成・推敲 | `generator` |
| レビュアー | Reviewer | 品質検証専門のサブエージェント。ソース検証・チェックリスト評価・品質メタデータ付与を担当 | `reviewer` |
| サブエージェント | Sub-agent | オーケストレーターから起動されるエージェントの総称。リサーチャー、ジェネレーター、レビュアー | `agent`（Claude Code Agentツール） |
| 専門プロンプト | Specialized Prompt | 各エージェントの役割に特化したシステムプロンプト | `prompt` |

### ユーザー関連

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| ユーザープロファイル | User Profile | ユーザーの職種・技術スタック・関心領域等の背景情報。成果物のカスタマイズに使用 | `user_profile` |
| ロール | Role | ユーザーの職種・役割（例: クラウドエンジニア） | `role` |
| 担当クラウド | Clouds | ユーザーが業務で使用するクラウドベンダ（例: AWS, Google Cloud） | `clouds` |
| 技術スタック | Tech Stack | ユーザーが主に使用する技術（例: Terraform, Python） | `tech_stack` |
| 専門領域 | Expertise | ユーザーが深い知識を持つ分野（例: インフラ設計） | `expertise` |
| 関心領域 | Interests | ユーザーが現在関心のあるテーマ（例: サーバーレス） | `interests` |
| 組織コンテキスト | Organization Context | ユーザーの業務上の背景（例: SaaS企業のインフラチーム） | `org_context` |

### 品質担保

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| ソース検証 | Source Verification | 出典URLの実在確認と成果物記述との整合性チェック（品質担保の第1層） | `source_verification` |
| セルフレビュー | Self Review | カテゴリ別チェックリストによる成果物の品質評価（品質担保の第2層） | `self_review` |
| チェックリスト | Checklist | カテゴリごとに定義された品質評価項目のリスト | `checklist` |
| ソース優先順位 | Source Priority | カテゴリ別に定義された情報ソースの信頼度ランキング（1〜6） | `source_priority` |
| 未検証 | Unverified | 信頼できるソースで裏付けが取れていない事実主張の状態 | `unverified` |
| レビューループ | Review Loop | レビュアーが不合格→ジェネレーターが修正→再レビューの繰り返し（最大2回） | - |
| フィクサー注記 | Fixer Notes | review fix loop で fixer (LLM) が `quality_metadata.notes` に記録する正直な実行ログ。「コード関連指摘 N 件は本ループ未修正」等が代表例 | `fixer_notes` |
| 保護フィールドリスト | Preserved Deliverable Fields | fix loop で text 成果物を再生成しても上書きしないフィールド集合。現在は `("code_files",)` | `_PRESERVED_DELIVERABLE_FIELDS` |

### コード生成

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| ワークスペースモード | Workspace Mode | コード生成時に Claude が sandbox ディレクトリで `.tf` `.py` 等を Write ツール直書きする方式。JSON エスケープを回避 | `call_claude_with_workspace` |
| サンドボックス | Sandbox | workspace モードで一時作成される独立ディレクトリ（`/tmp/agent-output-{type}-{uuid}`）。タスク完了後に `shutil.rmtree` で削除される | `sandbox` |

### フィードバック学習

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| フィードバックプロセッサー | Feedback Processor | F8 フィードバック（Slack 絵文字反応 + メンション返信）を解析し、`learned_preferences` を更新するモジュール（`src/agent/feedback/`） | `feedback_processor` |
| 学習済み好み | Learned Preferences | ユーザーの過去フィードバックから抽出された嗜好情報。次回の deliverables 生成時に prompt 経由で反映される | `learned_preferences` |
| 好み | Preference | ユーザーが特定のスタイル・形式・出典源等に対して持つ嗜好。F8 フィードバックを通じて学習対象になる | `preference` |

## 2. 成果物タイプ

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| 調査レポート | Research Report | 概要・背景・分析等のテキスト成果物 | `research_report` |
| IaCコード | IaC Code | Terraform、CloudFormation等のインフラコード | `iac_code` |
| プログラムコード | Program Code | Python、TypeScript等のアプリケーションコード | `program_code` |
| アーキテクチャ設計書 | Architecture Design | 構成図・データフロー等の設計ドキュメント | `architecture_design` |
| 比較表 | Comparison Table | ベンダ比較、技術比較等の表形式成果物 | `comparison_table` |
| 料金見積もり | Cost Estimate | クラウドサービスのコスト試算 | `cost_estimate` |
| 手順書 | Procedure Guide | ステップバイステップの実装/運用手順 | `procedure_guide` |

## 3. インフラ・システム用語

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| トリガー関数 | Trigger Function | Slackイベントを受信しECSタスクを起動するLambda関数 | `trigger` |
| トークンモニター関数 | Token Monitor | Claude OAuth トークンの失効を定期監視し、失効時に Slack へ再ログイン依頼を通知する Lambda 関数 | `token_monitor` |
| エージェントコンテナ | Agent Container | Claude Code CLIを実行するECS Fargateタスク | `agent` |
| プロファイルDB | Profile DB | ユーザープロファイルを格納するDynamoDBテーブル | `user_profiles` |
| 成果物DB | Deliverables DB | Notionのデータベース。全トピックの成果物ページを格納 | `notion_database` |
| コード成果物リポジトリ | Code Repository | コード成果物を格納するGitHubのPrivateリポジトリ | `github_repo` |
| ACK応答 | ACK Response | トピック受信時にSlackへ即座に返す受付確認メッセージ | `ack` |
| 完了通知 | Completion Notification | ワークフロー完了時にSlackへ送信するサマリーとリンク | `completion_notification` |
| 進捗通知 | Progress Notification | ワークフロー実行中にSlackへ送信するステップ完了報告 | `progress_notification` |
| 履歴コマンド | History Command | Slack で `履歴` または `history [keyword]` 投稿で過去の完了成果物を一覧表示する F9 機能（`src/trigger/app.py`） | `_handle_history_command` |

## 4. ステータス

| ステータス | 英語 | 定義 | コード上の値 |
|-----------|------|------|------------|
| 受信済み | Received | Slackメッセージを受信した初期状態 | `received` |
| 解析中 | Analyzing | トピックを解析している状態 | `analyzing` |
| 計画中 | Planning | ワークフローを設計している状態 | `planning` |
| 調査中 | Researching | リサーチャーが情報収集している状態 | `researching` |
| 生成中 | Generating | ジェネレーターが成果物を生成している状態 | `generating` |
| レビュー中 | Reviewing | レビュアーが品質検証している状態 | `reviewing` |
| 格納中 | Storing | 成果物をNotion/GitHubに格納している状態 | `storing` |
| 完了 | Completed | ワークフローが正常に完了した状態 | `completed` |
| 失敗 | Failed | ワークフローがエラーで中断した状態 | `failed` |

## 5. エラー・例外

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| Cloudflare ブロック | Cloudflare Block | Notion API 前段の Cloudflare WAF が ECS Fargate からのリクエストを 403 で拒否する事象。HTML ボディに「Attention Required! \| Cloudflare」または `cdn-cgi/styles/cf.errors.css` のシグネチャを含む | `cloudflare_block` |
| Cloudflare ブロック例外 | Notion Cloudflare Block Error | Cloudflare ブロックを検知したときに `notion_client._request_with_retry` が送出する専用例外。`cf_ray` 属性を保持 | `NotionCloudflareBlockError` |
| トークン要 refresh | Refresh Needed | Claude OAuth トークンの残り有効時間が 1 時間以下、または既に `expiresAt` を超過した状態。Token Refresher が refresh をトリガーする起点 | `_needs_refresh` |
| OAuth 自動延命 | OAuth Auto Refresh | Token Refresher Lambda が `refreshToken` を Anthropic OAuth エンドポイントに直接送信し、新 access_token を Secrets Manager に書き戻す仕組み | `_call_refresh_endpoint` / `_build_updated_credentials` |
| Credentials 書き戻し | Credentials Writeback | ECS タスク終了時に `~/.claude/.credentials.json` の内容変化を検知し、変更があれば Secrets Manager `catch-expander/claude-oauth` に書き戻すベストエフォート処理 | `_writeback_claude_credentials` |

## 6. 略語・略称

| 略語 | 正式名称 | 用途 |
|------|---------|------|
| IaC | Infrastructure as Code | コードによるインフラ管理 |
| PAT | Personal Access Token | GitHub APIの認証トークン |
| ACK | Acknowledgement | 受信確認応答 |
| MCP | Model Context Protocol | Claude Codeのツール拡張プロトコル |
| SAM | Serverless Application Model | AWSのサーバーレスIaCフレームワーク |
| ECR | Elastic Container Registry | AWSのコンテナイメージレジストリ |

## 7. 命名の注意事項

### 使い分けが必要な用語

| 混同しやすい用語 | 正しい使い分け |
|----------------|--------------|
| トピック vs クエリ | 「トピック」はユーザーの入力テキスト。「クエリ」はリサーチャーが生成する検索キーワード。混同しない |
| 成果物 vs レポート | 「成果物」はすべての出力物の総称。「レポート」はテキスト形式の成果物のみを指す |
| エージェント vs サブエージェント | 「エージェント」は4種すべてを指す一般名称。「サブエージェント」はオーケストレーターから起動される3種（リサーチャー、ジェネレーター、レビュアー）を指す |
| ワークフロー vs ステップ | 「ワークフロー」は全体の処理計画。「ステップ」はワークフロー内の個々の処理単位 |
| プロファイル vs アカウント | 「プロファイル」はユーザーの背景情報（DynamoDB）。Slackアカウントやクラウドアカウントとは別の概念 |
