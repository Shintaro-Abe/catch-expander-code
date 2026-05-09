# 要求内容: ダッシュボード Tier 2 完成 + 画面 5 タブ統合

## 起票背景

`.steering/20260430-workflow-observability/` の Phase 2 で T2-6 / T2-7 / T2-16 が `[x]` 完了マークで閉じられたが、2026-05-09 の手動 E2E 確認で以下の **設計と実装の乖離** が判明した。本 steering でこれを解消する。

### 確認した未実装項目

#### 画面 4 (レビュー品質) — AC-5

`.steering/20260430-workflow-observability/requirements.md:233-241` の受け入れ条件 7 項目中 **5 項目が未実装**:

| # | AC-5 項目 | 実装状況 |
|---|---|---|
| 1 | 期間内のレビュー pass 率推移が折れ線グラフで表示される | ❌ 未実装（KPI カードのみ）|
| 2 | fix loop 発火回数 (0 回 / 1 回 / 2 回) の分布が棒グラフで表示される | ❌ 未実装 |
| 3 | `code_related_unfixed_count > 0` の実行を抽出した一覧 | ✅ 実装済み |
| 4 | 各行から実行詳細画面へ遷移可能 | ✅ 実装済み |
| 5 | **Tier 2.2**: コード関連 issue のカテゴリ別集計 (terraform_schema / iam_action / syntax / api_version / その他) が円グラフ or 横棒グラフで表示される | ❌ 未実装 |
| 6 | **Tier 2.2**: 「同種 issue の繰り返し検出」テーブル — 同一 fix loop 内で類似 issue が解消されないまま MAX_REVIEW_LOOPS に到達したケースをハイライト | ❌ 未実装 |
| 7 | **Tier 2.2**: トピックカテゴリ別 (技術 / 時事 等) のレビュー pass 率比較 (棒グラフ) | ❌ 未実装 |

**根本原因**: `issue_category` データが reviewer prompt → orchestrator emit → backend → frontend の **全 4 層で欠落**。`grep -r "issue_category" src/ frontend/src/` でゼロヒット確認済み。

#### 画面 5 (エラー & 健全性) — AC-6

設計上はタブ式 4 区画（エラー / API 健全性 / token_monitor / 再発検出）だが、`/errors` 単独画面 + メインダッシュボードの token_monitor カードで部分実装。

| 設計 | 現状 |
|---|---|
| エラー一覧タブ | ✅ `/errors` で単独画面実装 |
| API 健全性タブ (Tier 1.2) | ❌ UI 未実装。データ層 (`/api/v1/metrics/api-health`) は実装済み |
| token_monitor タブ (Tier 1.4) | ⚠️ メインダッシュボードのカードで代替実装 (時系列グラフ・残存時間ヒストグラムは未実装) |
| 再発検出タブ (Tier 2.3) | ❌ UI 未実装。データ層も未実装 |

## ユーザーストーリー

### US-1: 対症療法アンチパターン早期検知

**As a** Catch-Expander の運用者として、
**I want** 同種のレビュー指摘 / エラーが時系列で集中・反復していないかを画面 4 で検出したい、
**So that** 構造的な問題（プロンプト不備 / 外部依存の劣化等）を `obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` の "3 commit ルール" 圏内に入る前に発見できる。

→ `requirements.md:177-181` (US-9) の再起票。設計のみで実装が伴わなかったため、本 steering で実機運用可能な形に落とし込む。

### US-2: token_monitor 健全性の時系列把握

**As a** Catch-Expander の運用者として、
**I want** 直近 30 日の OAuth refresh 履歴をメインダッシュボードのカードではなく、エラー & 健全性画面の専用タブで時系列・残存時間ヒストグラム付きで参照したい、
**So that** Claude OAuth トークンの逼迫タイミングと過去の失敗パターンを一画面で把握できる。

### US-3: API 健全性の集約把握

**As a** Catch-Expander の運用者として、
**I want** Notion / GitHub / Slack / Anthropic API の成功率・レイテンシ・rate limit ヒット数を 1 タブにまとめて見たい、
**So that** 外部依存の劣化（Cloudflare 429 多発・Notion 新 API 仕様変更等）を早期検出できる。

→ `requirements.md:163-176` (US-7) の再起票。

### US-4: tasklist の真実性

**As a** プロジェクトメンテナとして、
**I want** tasklist.md の `[x]` マークが実装の事実と一致していることを保証したい、
**So that** 「完了済み」の主張で意思決定（次の steering 起票・本番影響評価）が誤らない。

## 受け入れ条件

### AC-1: 画面 4 (レビュー品質) Tier 2.2 完成

#### データ層

- [ ] `src/agent/prompts/reviewer.md` に **各 issue に `issue_category` を含める** 指示を追加
- [ ] reviewer の JSON 出力スキーマに `issues[].issue_category` フィールドを追加（値域: `terraform_schema` / `iam_action` / `syntax` / `api_version` / `other`）
- [ ] `src/agent/orchestrator.py` の `review_completed` イベント emit で payload に各 issue の category を含める
- [ ] `src/dashboard_api/get_review_quality/app.py` を拡張:
  - issue_category 別集計 (count by category) をレスポンスに追加
  - 「同種 issue 繰り返し検出」のサーバーサイドロジック追加
  - トピックカテゴリ別 pass 率の集計追加
  - `?category=` クエリパラメータでのフィルタ受付

#### UI 層

- [ ] pass 率推移折れ線グラフ
- [ ] fix loop 発火回数分布棒グラフ
- [ ] issue カテゴリ別円グラフ（クリックで下表をフィルタ）
- [ ] 同種 issue 繰り返し検出テーブル（MAX_REVIEW_LOOPS 到達ハイライト付き）
- [ ] トピックカテゴリ別 pass 率比較棒グラフ
- [ ] 既存「未修正コード問題」テーブルにフィルタチップ ([すべて][terraform][iam][syntax][api_ver][other]) を追加
- [ ] URL クエリパラメータ `?days=&category=` でフィルタ状態が共有可能

### AC-2: 画面 5 (エラー & 健全性) タブ統合

- [ ] `/errors` ページをタブ式に再構成 (4 タブ: エラー / API 健全性 / token_monitor / 再発検出)
- [ ] **エラータブ**: 既存 `ErrorList.tsx` の機能をそのまま移植
- [ ] **API 健全性タブ (Tier 1.2)**:
  - [ ] サブタイプ (notion / github / slack / anthropic) 別の成功率時系列折れ線
  - [ ] レイテンシ p50 / p95 / p99 折れ線
  - [ ] rate_limit_hit イベント時系列
  - [ ] 失敗率 >10% でバナー警告
- [ ] **token_monitor タブ (Tier 1.4)**:
  - [ ] 直近 30 日の OAuth refresh 成功 / 失敗時系列
  - [ ] refresh 直前のトークン残存時間ヒストグラム
  - [ ] refresh 失敗時の Slack 通知到達率
  - [ ] 直近の refresh 失敗があった場合のバナー警告
- [ ] **再発検出タブ (Tier 2.3)**:
  - [ ] エラー時系列クラスター可視化
  - [ ] stage 別失敗頻度棒グラフ
  - [ ] 同一 error_type が 7 日以内に 3 回以上発生した場合のハイライト
- [ ] メインダッシュボードの token_monitor カードは **継続表示**（タブとは独立した KPI として維持）

### AC-3: tasklist 真実性

- [x] `.steering/20260430-workflow-observability/tasklist.md` の T2-6 / T2-7 / T2-16 の `[x]` を `[~]` に訂正し、未実装項目と本 steering への引き継ぎを明記（**本 steering 起票時に実施済み**）
- [ ] AC-5 / AC-6 マッピング行に「本 steering で対応予定」の注記追加（**本 steering 起票時に実施済み**）

### AC-4: テスト

- [ ] `tests/unit/agent/test_orchestrator.py` に reviewer 出力の `issue_category` 検証ケース追加
- [ ] `tests/unit/dashboard_api/test_get_review_quality.py` にカテゴリ別集計・フィルタ・繰り返し検出のテスト追加
- [ ] `frontend/src/test/` に画面 4 / 画面 5 の React Testing Library テスト追加
- [ ] 既存テストの回帰なし

### AC-5: ドキュメント整合

- [ ] `docs/functional-design.md` の画面 4 / 画面 5 セクションを最新化
- [ ] `docs/architecture.md` の Lambda API 一覧を `?category=` 等の追加パラメータに合わせて更新
- [ ] `obsidian/` への学び記録（必要に応じて）

## 制約事項

### スコープ制約

- **対象は画面 4 / 画面 5 のみ**。画面 1-3 / 6 は変更しない（既存テスト回帰防止）
- メインダッシュボードの token_monitor カードは **削除せず維持**。タブ統合は追加実装
- 既存の `code_related_unfixed_count > 0` 一覧 UI は **保持**。フィルタ追加で機能拡張する形

### 技術制約

- `issue_category` の **過去データは欠損** (新 emit 開始日以降のみ集計対象)
- 実装期間中の events テーブルへの破壊的スキーマ変更は禁止 (追加フィールドのみ)
- フロントエンドのバンドルサイズ +50KB 以内に抑える

### コスト制約

- Lambda 実行時間増加は集計クエリで <100ms p95
- 新規 AWS リソースの追加は **行わない** (既存 events テーブル + 既存 Lambda の拡張のみ)

### スケジュール制約

- T2-6 / T2-7 が機能して初めて T2-16 の AC-5 が満たされる依存関係。既存 tasklist 全体のクロージング条件として優先度高

## 関連ナレッジ

- [`obsidian/2026-04-26_symptomatic-fix-anti-pattern.md`](../../obsidian/2026-04-26_symptomatic-fix-anti-pattern.md) — 対症療法アンチパターン検知の動機
- [`memory/project_review_loop_recurring_patch_site.md`](../../../home/vscode/.claude/projects/-workspaces-Catch-Expander/memory/project_review_loop_recurring_patch_site.md) — `_run_review_loop` の再発パッチ密集地点。同種 issue 繰り返し検出機能で観測対象
- [`obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md`](../../obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md) — Codex 連続レビュー実施時の参照基盤
- 親 steering: [`.steering/20260430-workflow-observability/`](../20260430-workflow-observability/) (Tier 1/2 観測基盤の元設計)

## 起票の合理性

| 観点 | 判定 |
|---|---|
| 単独 PR 化に適した粒度か | ✅ 4 層 (prompt / emit / backend / frontend) を 1 つの目的で統合する規模、独立 PR が妥当 |
| 既存 steering を再開するべきか | ❌ `20260430-workflow-observability` は Phase 3 まで `[x]` クローズ済み。新規起票が清潔 |
| 個人利用規模で価値があるか | ⚠️ Tier 2.2 のカテゴリ分析は対症療法アンチパターン検知に直結し US-9 を実機運用化するため、設計書からの削除は推奨しない。ただしフェーズ分割で段階リリースは可（後述 design.md で議論） |
| 必須か | 設計書要件としては必須。SLA・法務上は任意 |
