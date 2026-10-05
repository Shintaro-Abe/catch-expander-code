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
| 実行診断役 | Execution Diagnostician | 実行 ID を 1 つ受け取り、その実行の記録を読み取り専用のカスタムツール 5 個で自分で選んで調べ、失敗の原因を診断結果として返す役。ワークフローの外で動き、サブエージェントには含めない。手動実行と、失敗時の自動起動（機能フラグ `EXECUTION_DIAGNOSIS`、既定オフ）から使う（`src/agent/diagnosis/`） | `diagnosis`（パッケージ）/ `diagnose_execution` |
| 修正役 | Fixer | 受け渡し用の診断結果を 1 つ受け取り、作業用の複製の中でカスタムツール 7 個を使って再現テストと修正を作り、合否の検査をすべて通ったものだけを作業用ブランチにコミットする役。本番のイメージには入らず、開発者が隔離された環境で手で起動する（`src/fixer/`） | `fixer`（パッケージ）/ `run_fix` |

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
| 学習済み好み | Learned Preferences | ユーザーの過去フィードバックから抽出された嗜好情報。適用スコープの決定的フィルタを通過したものだけが次回の deliverables 生成 prompt に注入される | `learned_preferences` |
| 好み | Preference | ユーザーが特定のスタイル・形式・出典源等に対して持つ嗜好。F8 フィードバックを通じて学習対象になる | `preference` |
| 適用スコープ | Preference Scope | 好みが prompt に注入される条件（トピックカテゴリ × 成果物区分の 2 次元）。抽出時に 1 回だけ LLM が付与し、適用時はコードが決定的にフィルタする（ADR 0002） | `scope` |
| 汎用好み | General Preference | 適用スコープの両次元が空（または `scope` 欠損）の好み。全パイプライン段階の prompt に注入される（例:「結論を先に簡潔に書く」） | `is_general` |
| 段階的絞り込み | Progressive Narrowing | パイプライン進行に応じてフィルタ条件が増える好みの適用方式。①トピック解析 = 汎用のみ / ②ワークフロー設計 = 汎用 + カテゴリ一致（成果物スコープ付きは緩和提示）/ ③生成 = カテゴリ + 成果物区分の完全フィルタ | `_render_prefs_for_*` |
| 成果物区分 | Scope Deliverable Kind | 適用スコープの成果物次元の語彙（6 値: コード / 調査レポート / アーキテクチャ設計書 / 比較表 / 料金見積もり / 手順書）。ワークフロー内部の成果物タイプ（7 値）とは**別レイヤーの語彙** | `SCOPE_DELIVERABLES` |

#### 成果物区分（6 値）と成果物タイプ（7 値）の対応

好みスコープは「ユーザーの認識の語彙」、成果物タイプは「ワークフロー内部の実行語彙」。
フィルタ時にコード側の展開マップ（`SCOPE_DELIVERABLE_EXPANSION`）で対応付ける。

| 成果物区分（scope） | 成果物タイプ（deliverable_type） |
|-------------------|--------------------------------|
| `code`（コード） | `iac_code` + `program_code` |
| `research_report` | `research_report` |
| `architecture_design` | `architecture_design` |
| `comparison_table` | `comparison_table` |
| `cost_estimate` | `cost_estimate` |
| `procedure_guide` | `procedure_guide` |

### 実行の診断と修正

実行診断役と修正役に関わる用語。使い方は `docs/execution-diagnostician.md` と `docs/execution-fixer.md`。

| 用語（日本語） | 用語（英語） | 定義 | コード上の命名 |
|--------------|------------|------|--------------|
| カスタムツール | Custom Tool | 実行診断役・修正役が自分で定義して持つツール。Agent SDK のインプロセスの MCP サーバーとして登録し、モデルにはこれだけを使わせる（組み込みツールは外す） | `create_sdk_mcp_server` / `build_server` / `TOOL_NAMES`（ツール名は `mcp__diag__*`・`mcp__fixer__*`） |
| 診断結果 | Diagnosis | 実行診断役が返す、固定の構造の結果。失敗した段階・原因の分類・原因の性質・確からしさ・推奨する対処・要約と、コードが照合した根拠・例外の型・コードの場所を持つ | `Diagnosis` |
| 受け渡し用の診断結果 | Diagnosis Handoff | 診断結果のうち、決まった値と、形を検証した識別子だけを取り出した JSON（`schema_version: 1`）。実行診断役から修正役へ渡す唯一の入力で、自由記述と記録の中身は含まない | `DiagnosisHandoff`（出す側）/ `HandoffInput`（受ける側） |
| 根拠 ID | Evidence ID | カスタムツールが、記録を返すたびに付ける識別子（`EX-1`・`ST-<n>`・`EV-<n>`・`PR-<役>-<番号>`）。診断結果の根拠は、ツールが実際に返した根拠 ID とコードが照合し、合わないものは取り除く | `EvidenceRef` / `evidence_id` |
| コードの場所 | Code Location | 失敗に関わるコードの位置（パス・関数名・行番号）。スタックトレースからコードが取り出し、実際のコードにその関数・その行があるものだけを残す。修正役にとっては手がかりにすぎない | `CodeLocation` / `code_locations`（修正役で検証し直したものは `VerifiedLocation`） |
| 「修正役に渡せる」の判定 | Fixer Eligibility | 診断結果を修正役に渡してよいかを、コードが決める真偽値。原因の性質がコードの不具合、確からしさが高、対処がコードの修正、などをすべて満たすときだけ真。修正役の側でも計算し直す | `fixer_eligible` / `is_fixer_eligible` / `is_eligible` |
| 足取り | Trace | 役が 1 回の実行で呼んだカスタムツールの記録。順番・結果の分類・所要時間などをコードが残し、モデルの申告には頼らない | `trace` / `ToolCall`（実行診断役）/ `FixToolCall`（修正役） |
| 作業用の複製 | Workspace | 修正役が作業する、基準のコミットを書き出した一時ディレクトリ（`.git` を持たない）。修正役の読み書きは、この中だけに限る | `Workspace` |
| 再現テスト | Reproduction Test | 修正役が新しく足すテストファイル。修正前に失敗し、修正後に通ることを、合否の検査（G4〜G6）が確かめる | `new_test_files` |
| 合否の検査 | Gate | 修正役の変更を成果物として認めるかを、モデルの申告でなく、コードが決める 10 個の検査（G1〜G10）。範囲・既存のテスト・大きさ・差分の中身の検査と、検査用の複製で実際にテストとリントを走らせる検査から成る | `gates` / `GateResult` / `GATE_IDS` |
| 検査用の複製 | Gate Tree | 合否の検査 G5〜G8 のために、コードが基準のコミットと検査を通ったファイルだけから組み立て直す複製。使う実行の直前に 1 つずつ作り、前後で中身を照合する | `_GateTrees` / `build_tree` |
| 対象外の場所 | Forbidden Paths | 修正役が書けず、変更があれば合否の検査 G2 で不合格にする場所。インフラの定義、CI、規律の文書、依存の定義、認証に関わるコード、実行診断役と修正役のコードなど | `FORBIDDEN_PREFIXES` / `FORBIDDEN_PATHS` / `FORBIDDEN_NAMES` / `is_forbidden` |
| 作業用ブランチ | Fix Branch | 修正役が、合否の検査をすべて通った変更をコミットする手元のブランチ（`fix/diagnosed-<実行 ID>`）。プッシュと取り込みは人が行う | `BRANCH_PREFIX` |

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
| イベントテーブル | Events Table | 観測イベントログを格納する DynamoDB テーブル（`{prefix}-events`）。PK: `execution_id`、SK: タイムスタンプ+種別。TTL 5年。ダッシュボード API のデータソース | `events` |
| Lambda オーソライザー | Lambda Authorizer | API Gateway HTTP API の全エンドポイントを保護する Lambda 関数。リクエストに付与された JWT cookie を検証し、認証済みリクエストのみ通過させる（`src/dashboard_api/authorizer/`） | `authorizer` |
| 実行ID | execution_id | ワークフロー実行インスタンスを一意に識別する文字列（例: `exec-20260404-001`）。DynamoDB 全テーブルの PK として使用される | `execution_id` |
| フィードバック受信イベント | feedback_received | ユーザーが完了通知スレッドにフィードバックを投稿したことを記録するイベント種別。`{prefix}-events` テーブルに記録され、`get_feedback_aggregation` Lambda がダッシュボード表示用に集計する | `feedback_received` |
| 診断完了イベント | diagnosis_completed | 実行診断役が終了時に 1 件残すイベント種別。終了理由・原因の分類・確からしさ・足取りのツール名の並び・受け渡し用の診断結果などを持ち、自由記述と記録の中身は含まない。`{prefix}-events` テーブルに記録される | `diagnosis_completed` |
| SPA | SPA（Single Page Application） | 初回ロード時に HTML / JS / CSS をまとめて取得し、以降はページ遷移なしで動作するウェブアプリケーション方式。本プロジェクトのダッシュボードは React + Vite 構成の SPA として S3 / CloudFront で配信される | - |
| JWT | JWT（JSON Web Token） | ユーザー認証情報を JSON 形式で署名したトークン。Slack OAuth 認証成功後に `auth_callback` Lambda が発行し、HttpOnly cookie としてブラウザに保存される。Lambda Authorizer が API リクエストごとに検証する | `jwt` |
| Slack OAuth | Slack OAuth | Slack のユーザー認証フロー（OAuth 2.0）。ダッシュボードはユーザーを Slack の認可エンドポイントへリダイレクトし、コールバックで得たコードでユーザー情報を取得・JWT を発行する | - |
| Agent SDK | Claude Agent SDK | Claude Code を型付きインターフェース（`query()` / `ClaudeAgentOptions` / `ResultMessage`）で呼び出す公式 Python SDK。純粋な HTTP クライアントではなく、内部で Claude Code CLI を subprocess として起動する | `claude_agent_sdk` |
| 同期ファサード | Sync Facade | `call_claude` 系の同期シグネチャを維持したまま、内部で `asyncio.run(query(...))` を実行する呼び出し層。orchestrator の ThreadPoolExecutor 並列と共存する | `call_claude` |
| envelope パース | Envelope Parse | 旧 CLI（`--output-format json`）の stdout JSON から `result` キーを取り出す外側のパース。Agent SDK 移行で消滅する | - |
| フェンス抽出 | Fence Extraction | モデル出力テキストから ```json コードブロックを剥がして JSON を取り出す内側のパース。researcher / reviewer プロンプトがフェンス必須指示のため Agent SDK 移行後も残存する | `_parse_claude_response` |

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
| エージェント（4種） vs 実行診断役・修正役 | 上の「4種」は、ワークフローを実行するオーケストレーターとサブエージェント3種。実行診断役と修正役は、ワークフローの外で動く別の役で、4種にもサブエージェントにも含めない。指すときは、役の名前で呼ぶ |
| 修正役 vs フィクサー（review fix loop） | 「修正役」（`src/fixer/`）は、診断結果をもとにこのリポジトリのコードの修正案を作る役。「フィクサー注記」の fixer は、レビューループの中で成果物を直す LLM 呼び出し（`reviewer_fix`）を指し、別のもの |
| 診断結果 vs 受け渡し用の診断結果 | 「診断結果」はモデルが書いた要約などの自由記述を含み、人が読む。「受け渡し用の診断結果」は固定項目だけで、修正役が受け取るのはこちらだけ |
| 作業用の複製 vs 検査用の複製 vs サンドボックス | 「作業用の複製」は修正役のモデルが読み書きする場所。「検査用の複製」は合否の検査のためにコードが組み立て直す場所で、モデルは触れない。「サンドボックス」はワークスペースモードのコード生成用の一時ディレクトリで、修正役とは無関係 |
| ワークフロー vs ステップ | 「ワークフロー」は全体の処理計画。「ステップ」はワークフロー内の個々の処理単位 |
| プロファイル vs アカウント | 「プロファイル」はユーザーの背景情報（DynamoDB）。Slackアカウントやクラウドアカウントとは別の概念 |
