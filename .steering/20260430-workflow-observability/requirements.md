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

- **専用 Web UI** (5 画面: ダッシュボードトップ / 実行一覧 / 実行詳細 / レビュー品質 / エラー一覧)
- **Slack OAuth 認証** によるアクセス制御
- **構造化イベント書き込み** (orchestrator / Lambda Trigger / sub-agents / fix loop / 格納処理)
- **events ストア** (新規 DynamoDB テーブル、TTL 90 日)
- **API エンドポイント群** (実行一覧 / 詳細 / メトリクス / イベント取得)

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

### AC-5: レビュー品質画面

- [ ] 期間内のレビュー pass 率推移が折れ線グラフで表示される
- [ ] fix loop 発火回数 (0 回 / 1 回 / 2 回) の分布が棒グラフで表示される
- [ ] **`code_related_unfixed_count > 0` の実行を抽出した一覧** がテーブルで見える (案 A 起票判断のデータソース、`memory/project_review_loop_recurring_patch_site.md` 連動)
- [ ] 各行から実行詳細画面へ遷移可能

### AC-6: エラー一覧画面

- [ ] 直近 7 日のエラー (status=failed の実行 + ECS タスク異常終了等) が時系列で表示される
- [ ] 各エラーは展開可能で、stack trace / error_message / 関連 execution_id が見える
- [ ] エラータイプ別の集計 (例: ParseError / NotionAPIError / Cloudflare429 等) が円グラフで見える

### AC-7: イベント観測ポイント (データソース要件)

UI で表示する内容を支えるため、以下のイベントが events テーブルに永続化される必要がある (詳細スキーマは design.md):

- [ ] `topic_received`: Slack トピック受領 (execution_id / topic / user_id (匿名化) / channel_id)
- [ ] `workflow_planned`: ワークフロー自動生成完了 (execution_id / planned_subagents / topic_category / expected_deliverable_type)
- [ ] `research_completed`: コンテキスト構築完了 (execution_id / sources_summary / total_tokens_used)
- [ ] `subagent_started` / `subagent_completed` / `subagent_failed`: 各サブエージェントの開始/終了/失敗 (duration_ms / output_summary / tokens_used を含む)
- [ ] `review_completed`: レビュー終了 (iteration / passed / issues_count / fixer_notes_count / **`code_related_unfixed_count`**)
- [ ] `notion_stored` / `github_stored`: 格納完了 (URL / code_files の有無)
- [ ] `slack_notified`: 完了通知送信
- [ ] `execution_completed`: 実行全体終了 (status / total_duration_ms / total_tokens_used / final_deliverable_url)
- [ ] `error`: 例外発生 (error_type / error_message / stack_trace / 関連 execution_id)

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

### NFR-3: セキュリティ

- 構造化イベントには **以下を含めない**:
  - Slack Bot Token / Notion Token / GitHub PAT (Secrets Manager 経由のみで扱う)
  - 利用者の Slack user ID は **匿名化** (ハッシュ化、events table には hash のみ保存)
- raw `topic` は events に含むが、PII を含む可能性があるため、機密プロジェクトで使う場合の注意点として `docs/development-guidelines.md` に追記
- API Gateway は Slack OAuth トークン検証必須、未認証リクエストは 401
- CloudFront はカスタムヘッダー (Origin Verify) で API Gateway のみが応答するよう制限

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

---

## 関連ドキュメント / メモリ

- 過去議論: 本セッションでの設計検討 (案 A vs C の比較、利用者フィードバック「専用 UI ができるイメージ」を踏まえて 案 C 採用)
- 関連メモリ: `memory/project_review_loop_recurring_patch_site.md` — `code_related_unfixed_count` の累積観測は案 A (review_loop の) 起票判断条件のデータソース
- 関連 obsidian: `obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` — 実機検証ログの自動化候補
- 関連 steering: なし (本タスクは独立な新機能、既存実装には侵襲なし)
