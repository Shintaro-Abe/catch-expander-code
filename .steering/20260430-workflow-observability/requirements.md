# 要求 — ワークフロー監視ダッシュボード (専用 Web UI)

## 概要

利用者が Slack 経由で投入したトピックに対する Catch-Expander の処理過程 (自動生成されたワークフロー、収集コンテキスト、各サブエージェントの実行、レビュー結果、成果物格納) を、**専用 Web UI** で可視化する新機能。AWS コンソールに依存せず、利用者・運用者がブラウザで直接ブラウズ・検索・詳細確認できる UX を提供する。

採用方向の概略: 専用 Web UI ベース (S3 + CloudFront でホストする SPA + API Gateway + Lambda + 専用 events ストア)。代替案 (CloudWatch Dashboard / Slack コマンド / Notion 統合) との比較と採用根拠は「[代替案の比較と採用方針](#代替案の比較と採用方針)」節を参照。

---

## 背景 / 目的

### 現状の課題

- ECS Fargate 上の orchestrator が標準出力に出すログは CloudWatch Logs に集約されているが、構造化されておらず、特定の `execution_id` に対する全イベントを横断的に確認するのが難しい。
- ワークフロー設計の妥当性 (どんなサブエージェントが起動したか・何が context として渡されたか・レビューが正しく機能したか) を可視化する手段が欠如している。
- AWS コンソールにログインしないと中身を見られないため、運用者・利用者が気軽にアクセスできず、品質傾向の継続観測が滞りがち。
- `obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` の実機検証ログ追記運用も人手による Notion 結果の目視確認が前提で、再現性・網羅性に欠ける。

### 利用者の声 (2026-04-30)

> Slackからトピックが送られて、どのようなワークフローが自動生成されたのか、ワークフローにはどのようなコンテキストが含まれていたのか、レビューは正しく行われていたのかなど、各イベントをダッシュボードで拾えるようにしたい。

> 専用の UI ができるイメージで合いますか? (案 C 採用に直結する確認)

### 採用するアプローチ (要約)

専用 SPA + API のフルスタック構成。詳細は `design.md` で確定するが、本要求書の前提は以下:

- フロントエンド: 静的 SPA (S3 + CloudFront)
- バックエンド: REST API (API Gateway + Lambda)
- データソース: 既存 DynamoDB (workflows / deliverables) + 新規 events テーブル (構造化イベント時系列)
- 認証: Slack OAuth (Slack workspace メンバーのみ閲覧可)
- 既存インフラへの侵襲: orchestrator / Lambda Trigger に **構造化イベント書き込みコール** を追加する程度

採用の経緯と却下した代替案 (案 A: CloudWatch Dashboard / 案 B: Slack コマンド / 案 D: Notion 統合) との比較は次節を参照。

---

## 代替案の比較と採用方針

### 検討した 4 案の概要

監視ダッシュボードの実装方法として、設計検討段階で以下の 4 つを評価した。本節では各案を初出時に説明し、最終的に **案 C** を採用した経緯を記す。

| 案 | 名称 | 概要 | 主な技術スタック |
|---|---|---|---|
| **A** | CloudWatch Dashboard | AWS 標準の Dashboard を構築し、CloudWatch Logs Insights のクエリ結果ウィジェットで集計指標と log 抽出を可視化 | CloudWatch Logs (構造化 JSON) + CloudWatch Dashboard ウィジェット + Logs Insights クエリ |
| **B** | DynamoDB events + Slack `monitor` コマンド | 既存 Slack 統合を拡張し、`/monitor` 等のスラッシュコマンドで直近実行をテキスト表示 | DynamoDB events テーブル + Slack Bot コマンド処理 (Lambda) |
| **C** | 専用 Web UI **(採用)** | 独立した SPA + REST API を構築し、ブラウザでアクセスする専用ダッシュボード | 静的 SPA (S3 + CloudFront) + REST API (API Gateway + Lambda) + 専用 events DDB + Slack OAuth |
| **D** | Notion ダッシュボード | 既存 Notion 統合を拡張し、実行ごとに Notion 上に監視ページを自動生成 | Notion API + 専用 DB (Notion 内) |

### 主要観点での比較

| 観点 | 案 A | 案 B | **案 C** | 案 D |
|---|---|---|---|---|
| 工数 | 1〜2 日 | 2〜3 日 | 1〜2 週間 | 3〜4 日 |
| 月額追加コスト | +$5 以内 | +$3 以内 | +$30 以内 | +$1 以内 |
| アクセス手段 | AWS Console | Slack | **専用 URL (ブラウザ)** | Notion アプリ |
| **ワークフロー JSON / コンテキスト中身の閲覧 UX** | △ セル truncation 多発 | ✗ Slack テキスト | **◎ 整形・ツリー展開・検索** | △ Notion 制約 |
| 集計指標表示 (件数 / 成功率) | ◎ | △ | ○ | △ |
| 時系列タイムライン (イベント詳細) | ✗ | ✗ | **◎** | △ |
| 既存インフラへの侵襲度 | 低 | 低 | 中 (新規 5 リソース) | 低 |
| 「専用 UI 感」 (利用者期待への適合) | ✗ AWS Console UI | ✗ Slack 内 | **◎ ブランド/カスタム自由** | △ Notion UI |
| 認証の柔軟性 | AWS IAM 必須 | Slack 内蔵 | **Slack OAuth 等で自由設計** | Notion 共有設定依存 |

### 採用案 (案 C) の選定理由

利用者からの確認「**専用の UI ができるイメージで合いますか**」(2026-04-30) を受けて、要件は単なる集計表示ではなく **ワークフローやコンテキストの中身を快適に閲覧できる UX** が必須と確定した。

- **案 A** (CloudWatch Dashboard) は集計指標表示には強いが、Logs Insights ウィジェットのセル幅制限 (~256 文字) で長文 JSON が省略表示され、全文確認には AWS Console 内でドリルダウンが必要。「専用 UI」の利用者期待に合致しない。
- **案 B** (Slack コマンド) は Slack 内テキスト表示が前提のため時系列タイムライン描画や JSON 整形ができず、可視化レベルが要件未満。
- **案 D** (Notion ダッシュボード) は Notion が既に成果物格納先として使われており、観測ストアと役割が混濁する。Notion API の集計能力にも限界があり、「ダッシュボード」用途には不適。
- **案 C** (専用 Web UI) は工数とコストはかかるが、上記 3 つで満たせない「中身ブラウズ UX」を確実に満たし、**ブランド・認証・操作性すべてカスタム可能**。専用 URL によるアクセスで利用者が抱く「専用 UI」イメージに直接整合する。

→ **要件適合度を最優先し、案 C を採用**。工数/コスト懸念は本要求書の NFR-1 (パフォーマンス +5%以内) と NFR-2 (月額 +$30 以内) で上限を制約。

### 補完的な将来拡張余地

案 B (Slack コマンド) は採用していないが、案 C と **相補関係** で将来追加できる:

- 例: Slack で `/monitor exec-abc123` を実行 → 当該実行の専用ダッシュボード URL が自動 DM される
- 例: 直近 5 件の実行サマリ + ダッシュボード詳細 URL を Slack に投稿
- 本要求書のスコープ外。別 steering で取り扱う

---

## スコープ

### 含む (本タスクで実装)

#### 機能面

- **専用 Web UI** (6 画面: ダッシュボードトップ / 実行一覧 / 実行詳細 / レビュー品質 / エラー & 健全性 / フィードバック分析)
- **Slack OAuth 認証** によるアクセス制御
- **構造化イベント書き込み** (orchestrator / Lambda Trigger / sub-agents / fix loop / 格納処理 / token_monitor / feedback_processor)
- **events ストア** (新規 DynamoDB テーブル、TTL 90 日)
- **API エンドポイント群** (実行一覧 / 詳細 / メトリクス / イベント取得 / コスト集計 / API 健全性 / フィードバック集計)
- **拡張観測機能** (Tier 1 + Tier 2):
  - Tier 1.1: コスト追跡 (Anthropic API トークンコスト + AWS インフラコスト + サブエージェント別内訳)
  - Tier 1.2: API 連携健全性 (Notion / GitHub / Slack / Anthropic API の成功率・レイテンシ・rate limit ヒット)
  - Tier 1.3: 段階別レイテンシ分解 (research / generator / reviewer / storage の処理時間内訳)
  - Tier 1.4: token_monitor 健全性 (OAuth refresh 成否、トークン残存時間)
  - Tier 2.1: ソース品質指標 (domain authority 分布 / freshness / diversity)
  - Tier 2.2: レビュー指摘の再発パターン検出 (issue カテゴリ別集計、同種指摘の繰り返し)
  - Tier 2.3: エラー再発パターン検出 (時系列クラスター、stage 別失敗頻度)
  - Tier 2.4: F8 フィードバック集計 (絵文字反応、メンション返信、learned_preferences 更新頻度)

#### インフラ面

- SAM template への新リソース追加 (S3 / CloudFront / API Gateway / Lambda / DynamoDB events / IAM ロール)
- フロントエンドのビルドパイプライン (Vite + 静的アセット → S3)
- ドキュメント更新 (`docs/architecture.md` / `docs/functional-design.md` / `docs/glossary.md`)

### 含まない (将来候補、別 steering)

- リアルタイム push 通知 (WebSocket / Server-Sent Events)。UI は polling or 手動 refresh で十分
- 過去 90 日を超える長期履歴 (TTL で削除)
- ML ベースの異常検知 / アラート
- Slack `monitor` コマンド (UI と相補的だが別タスク)
- モバイルアプリ (ネイティブ)。Web レスポンシブで最低限の縮小対応のみ
- 多言語対応 (日本語のみ)
- ユーザーごとのカスタマイズ (個人設定保存)

---

## ユーザーストーリー

### US-1: 全体観測

**As a** Catch-Expander の運用者として、
**I want** ダッシュボードトップで「直近 24 時間の実行件数 / 成功率 / 平均処理時間 / レビュー pass 率」を一目で把握したい、
**So that** 異常な傾向を早期発見し、改善優先度を判断できる。

### US-2: 個別実行のトレース

**As a** Catch-Expander の運用者として、
**I want** 任意の実行を選択して、その全過程 (ワークフロー設計 → context 構築 → サブエージェント実行 → レビュー → 格納) を時系列タイムラインで確認したい、
**So that** 「この成果物の挙動が気になる」と利用者から指摘されたときに、5 分以内に該当実行を追跡できる。

### US-3: ワークフロー内容の参照

**As a** Catch-Expander の運用者として、
**I want** orchestrator が自動生成したワークフロー (起動した sub-agent の listing と各 sub-agent の入力/出力) と収集された research コンテキスト (sources / excerpts) を整形済みで閲覧したい、
**So that** トピックに対する処理が妥当だったかを質的に評価できる。

### US-4: レビュー品質の追跡

**As a** Catch-Expander の運用者として、
**I want** レビューペインで pass 率推移・fix loop 発火回数・「コード関連指摘 N 件は本ループ未修正」累積をグラフ表示したい、
**So that** 5 件目 review_loop パッチ (`8c5b220`) の効果継続性や、案 A 起票判断条件 (致命未修正の累積) を客観データで把握できる。

### US-5: エラーの素早い検索

**As a** Catch-Expander の運用者として、
**I want** エラー一覧画面で直近 7 日のエラーを stack trace 付きでブラウズしたい、
**So that** 原因調査の初動を 5 分以内に完了させ、利用者への影響範囲を判断できる。

### US-6: コスト把握 (Tier 1.1)

**As a** Catch-Expander の運用者として、
**I want** Anthropic API トークン使用量と AWS インフラコストを 1 実行単位 / 月次集計で把握したい、
**So that** Anthropic 月額コストの予算内収まりを確認し、コスト効率の悪い実行 (失敗続きで無駄遣い) を特定できる。

### US-7: 連携 API の健全性監視 (Tier 1.2)

**As a** Catch-Expander の運用者として、
**I want** Notion / GitHub / Slack / Anthropic 各 API の呼び出し成功率・レスポンスタイム・rate limit ヒットを可視化したい、
**So that** 過去観測した Cloudflare 429 等の事象 (`obsidian/2026-04-25_python-urllib-cloudflare-429-user-agent.md`) の再発を早期検知し、外部依存に起因する不具合を切り分けられる。

### US-8: ボトルネックとソース品質の特定 (Tier 1.3 + 2.1)

**As a** Catch-Expander の運用者として、
**I want** 1 実行内の段階別レイテンシ分解 (research / generator / reviewer / storage の比率) と、収集された research source の品質指標 (domain authority / freshness / diversity) を確認したい、
**So that** どの段階に時間がかかっているか、収集された情報源が信頼に足るかを質的に評価できる。

### US-9: レビュー / エラーの再発パターン検出 (Tier 2.2 + 2.3)

**As a** Catch-Expander の運用者として、
**I want** 同種のレビュー指摘 / エラーが時系列で集中・反復していないかを検出したい、
**So that** 構造的な問題 (プロンプト不備 / 外部依存の劣化等) を対症療法アンチパターンに陥る前に発見できる (`obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` 連動)。

### US-10: F8 フィードバック傾向の把握 (Tier 2.4)

**As a** Catch-Expander の運用者として、
**I want** F8 フィードバック (絵文字反応 / メンション返信) の集計と `learned_preferences` 更新頻度を観測したい、
**So that** 利用者満足度の定量指標を把握し、F8 学習機構の効果を測定できる。

---

## 受け入れ条件 (AC)

### AC-1: 認証 / アクセス制御

- [ ] ダッシュボード URL に未認証でアクセスすると、Slack OAuth ログイン画面にリダイレクトされる
- [ ] Slack workspace メンバーのみ閲覧可能 (workspace ID で制限、他の workspace ユーザーは拒否)
- [ ] 認証セッションは 24 時間で expire、再ログインを要求
- [ ] ログアウト機能が UI に存在する

### AC-2: ダッシュボードトップ画面

- [ ] 「直近 24 時間の実行件数」が status 別 (success / partial / failed) に円グラフ or 棒グラフで表示される
- [ ] 「成功率」「平均処理時間」「レビュー pass 率」「エラー率」がスコアカード形式で 1 ページ目に並ぶ
- [ ] 期間切替 (24h / 7d / 30d) のセレクタがある
- [ ] ページ初回表示は 1 秒以内 (cold start を除く、warm 状態)
- [ ] **Tier 1.1 拡張**: 「期間内の総トークン使用量 / 総コスト概算 ($)」スコアカード + 月次推移折れ線グラフ
- [ ] **Tier 1.1 拡張**: サブエージェント別 (researcher / generator / reviewer) のトークン使用量内訳 (積み上げ棒グラフ or ドーナツチャート)
- [ ] **Tier 1.3 拡張**: 全実行平均の段階別レイテンシ分解 (積み上げ棒グラフで research / generator / reviewer / storage の比率)

### AC-3: 実行一覧画面

- [ ] テーブル形式で直近 N 件 (デフォルト 50 件) の実行が表示される
- [ ] 列: `実行時刻` / `execution_id` / `topic (先頭 80 文字)` / `status` / `処理時間` / `レビュー結果`
- [ ] 検索/絞り込み: 期間 (date range)、status (multi-select)、topic キーワード (部分一致)
- [ ] 行クリックで実行詳細画面へ遷移
- [ ] ページネーション (前/次、または無限スクロール)

### AC-4: 実行詳細画面

- [ ] URL に `execution_id` が含まれる (共有可能)
- [ ] 上部にメタ情報 (timestamp / topic / status / 処理時間 / final_deliverable_url 等)
- [ ] 中央に時系列タイムライン: 各イベント (ワークフロー設計 / context 構築 / サブエージェント呼び出し / レビュー / 格納) を縦並びで表示
- [ ] 各イベントは展開可能で、payload (JSON) を整形表示 (ツリー or シンタックスハイライト付きコードブロック)
- [ ] 「ワークフロー」セクションには起動された sub-agent リストと各 sub-agent の役割・入出力概要
- [ ] 「コンテキスト」セクションには research_results の sources (URL / title / excerpt) を Markdown 整形で表示
- [ ] 「レビュー結果」セクションには各 fix loop iteration の issues / fixer notes / pass/fail を順次表示
- [ ] 「成果物」セクションには Notion URL / GitHub URL / code_files の有無を表示
- [ ] **Tier 1.1 拡張**: 上部メタ情報に「本実行のトークン使用量 / コスト概算」を表示
- [ ] **Tier 1.3 拡張**: 「段階別レイテンシ分解」セクション — research / generator / reviewer / storage の処理時間 (絶対値 + 比率) を可視化 (横棒グラフ)
- [ ] **Tier 2.1 拡張**: 「コンテキスト」セクションの sources 一覧に、各 source の `domain_authority` ラベル (公式 docs / 公式 blog / 第三者 blog / forum / unknown) と `published_at` (取得可能な場合) を追加
- [ ] **Tier 2.1 拡張**: sources 一覧の上に「ソース品質サマリ」(domain authority 分布の小型ドーナツ + 平均 freshness + 重複ドメイン率) を表示

### AC-5: レビュー品質画面

- [ ] 期間内のレビュー pass 率推移が折れ線グラフで表示される
- [ ] fix loop 発火回数 (0 回 / 1 回 / 2 回) の分布が棒グラフで表示される
- [ ] **`code_related_unfixed_count > 0` の実行を抽出した一覧** がテーブルで見える (案 A 起票判断のデータソース、`memory/project_review_loop_recurring_patch_site.md` 連動)
- [ ] 各行から実行詳細画面へ遷移可能
- [ ] **Tier 2.2 拡張**: コード関連 issue のカテゴリ別集計 (terraform_schema / iam_action / syntax / api_version / その他) が円グラフ or 横棒グラフで表示される
- [ ] **Tier 2.2 拡張**: 「同種 issue の繰り返し検出」テーブル — 同一 fix loop 内で類似 issue が解消されないまま MAX_REVIEW_LOOPS に到達したケースをハイライト
- [ ] **Tier 2.2 拡張**: トピックカテゴリ別 (技術 / 時事 等) のレビュー pass 率比較 (棒グラフ)

### AC-6: エラー & 健全性画面 (Tier 1.2 + 1.4 + 2.3 統合)

#### エラー一覧 (基本)

- [ ] 直近 7 日のエラー (status=failed の実行 + ECS タスク異常終了等) が時系列で表示される
- [ ] 各エラーは展開可能で、stack trace / error_message / 関連 execution_id が見える
- [ ] エラータイプ別の集計 (例: ParseError / NotionAPIError / Cloudflare429 等) が円グラフで見える

#### Tier 2.3 拡張: 再発パターン検出

- [ ] 同種 `error_type` の集中発生検出 — 1 時間に 3 回以上発生したエラータイプをハイライト表示
- [ ] stage 別 (notion_storage / generator / reviewer / etc.) の失敗頻度ランキング
- [ ] エラー復旧成功率 (`is_recoverable=True` のうち実際にリトライ成功した割合)

#### Tier 1.2 拡張: 連携 API 健全性タブ

- [ ] Notion / GitHub / Slack / Anthropic 各 API の直近 7 日の呼び出し成功率がタブ別に表示される
- [ ] 各 API のレスポンスタイム p50 / p95 / p99 が時系列折れ線グラフで表示される
- [ ] **rate limit ヒット件数** (Cloudflare 429 / Slack rate limit / Anthropic 429 等) が時系列で表示される
- [ ] 異常な失敗率 (>10%) の API はバナーで警告表示

#### Tier 1.4 拡張: token_monitor 健全性タブ

- [ ] 直近 30 日の OAuth refresh 成功 / 失敗の時系列が表示される
- [ ] refresh 直前のトークン残存時間ヒストグラム (refresh タイミングの妥当性検証用)
- [ ] refresh 失敗時の Slack 通知到達率 (フォールバック観測)
- [ ] 直近の refresh 失敗があった場合、バナーで「ECS タスク認証エラーリスクあり」を警告

### AC-7: イベント観測ポイント (データソース要件)

UI で表示する内容を支えるため、以下のイベントが events テーブルに永続化される必要がある (詳細スキーマは design.md):

#### 基本イベント (元の 9 種)

- [ ] `topic_received`: Slack トピック受領 (execution_id / topic / user_id (匿名化) / channel_id)
- [ ] `workflow_planned`: ワークフロー自動生成完了 (execution_id / planned_subagents / topic_category / expected_deliverable_type)
- [ ] `research_completed`: コンテキスト構築完了 (execution_id / sources_summary / total_tokens_used) **+ Tier 2.1: 各 source に `domain_authority` / `published_at` を含める**
- [ ] `subagent_started` / `subagent_completed` / `subagent_failed`: 各サブエージェントの開始/終了/失敗 (duration_ms / output_summary / tokens_used を含む)
- [ ] `review_completed`: レビュー終了 (iteration / passed / issues_count / fixer_notes_count / **`code_related_unfixed_count`**) **+ Tier 2.2: 各 issue に `issue_category` (terraform_schema / iam_action / syntax / api_version / other) を含める**
- [ ] `notion_stored` / `github_stored`: 格納完了 (URL / code_files の有無)
- [ ] `slack_notified`: 完了通知送信
- [ ] `execution_completed`: 実行全体終了 (status / total_duration_ms / total_tokens_used / final_deliverable_url) **+ Tier 1.1: `total_cost_usd` (推計) を含める**
- [ ] `error`: 例外発生 (error_type / error_message / stack_trace / 関連 execution_id)

#### Tier 1/2 で追加するイベント (4 種)

- [ ] **`api_call_completed`** (Tier 1.2): 外部 API 呼び出しの成否と所要時間 (subtype: `notion` / `github` / `slack` / `anthropic`、success / duration_ms / response_status_code / endpoint_path)
- [ ] **`rate_limit_hit`** (Tier 1.2): rate limit 検出 (subtype + retry_after など)、Cloudflare 429 / Slack rate limit / Anthropic 429 を区別
- [ ] **`oauth_refresh_completed`** / **`oauth_refresh_failed`** (Tier 1.4): token_monitor からの emit (status / token_remaining_seconds_before / error_message)
- [ ] **`feedback_received`** (Tier 2.4): F8 フィードバック受信 (subtype: `emoji_reaction` / `mention_reply`、emoji 種別 / reply_text_summary / 関連 execution_id / `learned_preferences_updated` (bool))

### AC-8: パフォーマンス

- [ ] 実行一覧画面: 50 件取得 + 表示完了が **1 秒以内** (warm 状態)
- [ ] 実行詳細画面: イベント 30 件取得 + 表示完了が **2 秒以内** (warm 状態)
- [ ] ダッシュボードトップのスコアカード/グラフ: 初回表示が **1.5 秒以内**
- [ ] API レスポンス自体は p95 で **500ms 以内**

### AC-9: 既存挙動の回帰なし

- [ ] 既存の Slack 進捗通知 / Notion 格納 / GitHub 格納 / DynamoDB 書き込み / F8/F9 機能には一切影響しない
- [ ] `tests/unit/` 全件 pass を維持 (既存ケースの期待値は変更しない)
- [ ] 構造化イベント書き込み追加によるテスト変更は新規ケースのみ
- [ ] orchestrator の総処理時間増加は **+5% 以内** (events 書き込みは非同期 / batch で実装)

### AC-10: ドキュメント更新

- [ ] `docs/architecture.md` に新リソース (S3 / CloudFront / API Gateway / Lambda / DynamoDB events) と構成図が追記される
- [ ] `docs/functional-design.md` に「監視ダッシュボード」節が追加され、画面構成 / API / データフローが図示される
- [ ] `docs/glossary.md` に新用語 (events table / Slack OAuth / SPA 等の関連語) を追加
- [ ] `docs/credential-setup.md` に Slack OAuth クライアント ID/シークレット作成手順を追記
- [ ] `docs/repository-structure.md` のツリーに `frontend/` (or 同等の) ディレクトリを追加
- [ ] README に「ダッシュボード URL」と「アクセス方法」を記載

### AC-11: フィードバック分析画面 (Tier 2.4)

- [ ] 直近 30 日の F8 絵文字反応の分布 (👍 / 👎 / その他) が円グラフで表示される
- [ ] 直近 30 日のメンション返信の件数推移が折れ線グラフで表示される
- [ ] `learned_preferences` の更新頻度が時系列で表示される
- [ ] フィードバックを受けた成果物の execution_id 一覧 (実行詳細へリンク) がテーブルで見える
- [ ] フィードバックに紐づくトピックカテゴリ別の満足度 (例: 技術 vs 時事 で 👍 比率) を比較できる

---

## 非機能要件 (NFR)

### NFR-1: パフォーマンス

- 構造化イベント書き込みのオーバーヘッドは既存実行時間の **+5% 以内** (非同期 batch 書き込みで実現)
- フロントエンド初回ロード (cold) は **3 秒以内** (静的アセット + CloudFront キャッシュ)
- フロントエンド画面遷移は **300ms 以内** (SPA の特性で達成)

### NFR-2: コスト

- 月額追加コストは **$30 以内** を目標:
  - S3 + CloudFront: <$1
  - API Gateway: ~$3.50/1M req (低トラフィック想定で $1〜2)
  - Lambda: 無料枠内 (1M req/月以下を想定)
  - DynamoDB events テーブル: ~$5〜10 (TTL 90 日、PAY_PER_REQUEST)
  - データ転送: <$5
- 1 ヶ月運用後にコスト実測で見直し

### NFR-3: セキュリティ (個人利用ベースライン)

#### 前提とする脅威モデル

- **利用者は単独 (本人のみ)**
- **Slack workspace は個人開発用** (機密業務情報は含まない前提)
- **コンプライアンス要件なし** (監査ログ・SOC2 等不要)
- **運用期間 1 年以上**
- 守るべき脅威: 「外部攻撃者による不正アクセス」「自身の credentials 漏洩 (PC 紛失等)」「AWS 課金爆発」「長期運用での鍵陳腐化」に絞る

#### 実装するセキュリティ対策 (S1〜S4 + 既存)

**[既存維持] 基本的な機密性管理**

- 構造化イベントには以下を含めない:
  - Slack Bot Token / Notion Token / GitHub PAT (Secrets Manager 経由のみで扱う)
  - 利用者の Slack user ID は SHA-256 ハッシュ化、events には hash のみ保存
- API Gateway は Slack OAuth トークン検証必須、未認証は 401
- CloudFront はカスタムヘッダ (`X-Origin-Verify`、**静的値で許容**) で API Gateway 直叩きを制限

**[S1] CSP / セキュリティヘッダー設定 (CloudFront Response Headers Policy)**

- `Content-Security-Policy: default-src 'self'; script-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'`
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Referrer-Policy: no-referrer`

**[S2] IAM 最小権限ポリシー**

- 各 Lambda は必要最小限のアクション + リソース ARN に限定
- `dynamodb:*` / `secretsmanager:*` / `s3:*` 等の **`*` ワイルドカード禁止**
- Read 系 Lambda: `dynamodb:Query` / `GetItem` のみ、特定テーブル ARN
- Auth 系 Lambda: 該当 Secrets Manager の特定 ARN のみ
- ECS Task Role: events テーブルへの `PutItem` のみ (既存の workflows / deliverables への権限は変更しない)

**[S3] OAuth state の単一回限り化 + フィンガープリントバインド**

- `auth_login` で生成した state を DDB に保存する際、IP アドレス + User-Agent のハッシュも一緒に記録
- `auth_callback` で受け取った state を照合する際、IP / UA も一致確認
- 一致したら state を **照合直後に DDB から削除** (一回限り)
- 不一致または既に削除済みの state は 400 応答

**[S4] 年 1 回の手動鍵ローテーション**

- 運用手順に以下を明記:
  - **JWT 署名鍵** を年 1 回 (毎年 1 月) 手動ローテーション
  - **Slack OAuth client_secret** を年 1 回手動ローテーション
  - **Slack workspace の MFA を有効化** (アクセス防御の最大要素)
  - **PC 紛失時** は Slack 設定画面からセッションを revoke
  - ダッシュボード URL を **公開リポジトリのソースコードに書かない** (難読化のため)

#### スキップする対策 (個人利用では過剰)

- **API Gateway / Lambda Authorizer のレート制限**: 自分しか使わない
- **JWT blacklist (ログアウト時の token 即時失効)**: 自分のみ利用、24h セッション expire で受容
- **CloudFront Origin Verify の自動ローテーション**: 静的値で十分
- **PII フィルタリング**: topic は自分の入力なので不要
- **events 書き込み失敗時のエラーログ payload マスキング**: 自分の Slack 内容なので受容
- **監査ログ / Dependabot / SRI 等の運用機能**: 個人開発で過剰投資

これらは将来チーム利用化や業務利用に切り替える際に **追加要件として再評価** する (本要求書「将来追加候補」セクション参照)。

### NFR-4: 可用性

- 静的フロントエンドは S3 + CloudFront のため SLA 99.9%
- API は API Gateway + Lambda のため SLA 99.95%
- events DDB は PAY_PER_REQUEST で読み書きスケール自動
- 障害時の挙動: orchestrator の events 書き込みが失敗しても **既存ワークフローは継続** (write は best-effort、失敗時は logging.error のみ、Slack 通知や Notion 格納はブロックしない)

### NFR-5: 互換性

- 既存の `logging.info()` / `logging.error()` 呼び出しは維持。新規 events 書き込みは別 API 経由 (events テーブル direct write)
- ブラウザ対応: 直近 2 メジャーバージョンの Chrome / Edge / Safari / Firefox。IE 非対応

### NFR-6: 観測の網羅性

- 5 件目パッチ (`_run_review_loop` 5 回連続パッチ領域) で導入した accumulator / preserve / isinstance ガード経路の発火状況も観測ポイントとして含める (AC-7 の `review_completed` イベントの `code_related_unfixed_count` がこれに対応)

---

## 制約事項

| 制約 | 内容 |
|---|---|
| インフラ | 既存 AWS アカウント / SAM template に追記する形で構築 (新 AWS アカウント不要) |
| 言語 (Backend) | Python 3.13 (既存と統一) |
| 言語 (Frontend) | TypeScript + React + Vite (詳細は design.md で確定) |
| デザイン | 一般的なダッシュボード UI で OK (Tailwind + shadcn/ui 等の標準ライブラリ前提)。ブランド固有要件なし |
| モバイル対応 | PC ブラウザ最適化、レスポンシブで最低限の縮小対応 (タブレット程度まで)。スマホ専用画面は設けない |
| デプロイ | `sam deploy` 経由のみ。フロントエンドビルドは GitHub Actions で自動化 |
| プロンプト/エージェント設計 | `prompts/*.md` および sub-agent の役割分担は **変更しない** (events 書き込みは orchestrator のラッパーのみ) |
| 後方互換性 | 既存の Slack コマンド (履歴コマンド等) は完全に挙動維持 |

---

## オープン論点

実装着手前 (= design.md 起草時) に確定させる論点:

1. **認証方式の詳細**: Slack OAuth の実装パス (Slack OAuth v2 + Sign in with Slack)、セッション管理 (Cognito / JWT in cookie / その他)、workspace 制限の実装方法
2. **フロントエンド技術スタックの確定**: Vite + React + TypeScript + Tailwind + shadcn/ui を前提とするが、状態管理ライブラリ (TanStack Query 推奨) / UI コンポーネントライブラリの最終選定
3. **events テーブルのスキーマ**: PK/SK 設計、GSI の必要性 (execution_id 検索、時刻範囲検索、status 別検索)
4. **events 書き込みの実装**: orchestrator から DDB に直接書く / 専用 Lambda 経由 / SQS バッファリング、どれが適切か
5. **API 構成**: REST (API Gateway HTTP API) / GraphQL (AppSync) / 静的に Lambda Functions URL — どれを採用するか
6. **デプロイ運用**: フロントエンドビルドアセットの S3 デプロイを SAM で完結するか / 別の GitHub Actions ステップで分けるか
7. **モバイル/タブレット詳細**: レスポンシブの最小幅と機能制限の有無
8. **デザインシステム選定**: shadcn/ui / Material UI / Chakra UI / カスタム — UI ライブラリの最終選定
9. **トピック/コンテキスト中の PII 取り扱い**: Slack 投稿に含まれる可能性のある個人情報のフィルタリング方針 (events 書き込み時にマスクするか、UI 表示時に警告のみか)
10. **コスト見積もり検証**: 月額 $30 以内の試算根拠を SAM deploy 直前に再検証
11. **Anthropic API 単価設定** (Tier 1.1): モデル別単価をどこで管理するか (環境変数 / Secrets Manager / コード固定)。料金体系変更時の追従コスト評価
12. **issue カテゴリ簡易分類のヒューリスティック** (Tier 2.2): キーワードマッチで `terraform_schema` / `iam_action` / `syntax` / `api_version` 等に分類する精度許容ライン
13. **domain authority 分類リスト** (Tier 2.1): 公式 docs / 公式 blog / 第三者 blog の判定ルール (URL ホワイトリスト / 動的判定)

---

## 将来追加候補の監視観点 (Tier 3-4)

本 MVP には含まないが、運用が固まった後に独立タスクで検討する候補:

### Tier 3: 検討余地あり (要件次第)

- **利用パターン分析**: DAU (distinct user_id_hash 数) / 時間帯ヒートマップ / トピックカテゴリ分布 / F9 履歴コマンド使用回数
- **認証・セキュリティ監視**: Slack OAuth callback 失敗回数 / JWT 検証失敗回数 / workspace 境界違反 / API rate limit 自身ヒット
- **同時実行数 / 並列度モニタ**: 月間実行数が 100 件超に増えた段階で必要

### Tier 4: 規模拡大時の候補

- **異常検知 (sudden spike detection)**: ML ベースの異常検知 / アラート
- **A/B テスト基盤**: モデル / プロンプトバージョン比較
- **容量計画 / 予測メトリクス**: AWS 制限到達予測

採用判断のトリガー:
- Tier 3 → チーム利用化、月次実行数 50+ への成長、セキュリティ監査要件
- Tier 4 → 月次実行数 500+、複数プロンプトバージョン並行運用、本番事故を契機

---

## 関連ドキュメント / メモリ

- 過去議論: 本セッションでの設計検討 (案 A vs C の比較、利用者フィードバック「専用 UI ができるイメージ」を踏まえて 案 C 採用)
- 関連メモリ: `memory/project_review_loop_recurring_patch_site.md` — `code_related_unfixed_count` の累積観測は案 A (review_loop の) 起票判断条件のデータソース
- 関連 obsidian: `obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` — 実機検証ログの自動化候補
- 関連 steering: なし (本タスクは独立な新機能、既存実装には侵襲なし)
