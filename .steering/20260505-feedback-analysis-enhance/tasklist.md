# タスクリスト — フィードバック分析画面の充実 (AC-11 完成)

> 関連: [`requirements.md`](./requirements.md) / [`design.md`](./design.md)

---

## 進捗サマリ

- [x] T1: バックエンド拡張 (`get_feedback_aggregation/app.py`)
- [x] T2: 型定義更新 (`types.ts`)
- [x] T3: フロントエンド実装 (`FeedbackAnalysis.tsx`)
- [x] T4: ユニットテスト追加 (`test_extended_metrics.py`)
- [x] T5: ビルド確認 + デプロイ

---

## T1: バックエンド拡張

### 内容

`src/dashboard_api/get_feedback_aggregation/app.py` に `events` / `daily_counts` を追加する。

### 完了条件

- [x] `daily_counts`: イベントのタイムスタンプ先頭 10 文字 (`YYYY-MM-DD`) でグルーピングし、日付昇順のリストを返す
- [x] `events`: 個別 `feedback_received` イベントを新着順（タイムスタンプ降順）で返す
  - フィールド: `execution_id` / `timestamp` / `subtype` / `reply_text_summary` / `learned_preferences_updated` / `new_preferences_count` / `total_preferences_count`
- [x] 既存フィールド（`total_feedback_count` / `preferences_updated_count` / `avg_new_preferences` / `latest_total_preferences`）は変更しない
- [x] フィードバックが 0 件のとき `events: []` / `daily_counts: []` を返す

---

## T2: 型定義更新

### 内容

`frontend/src/api/types.ts` に `FeedbackEvent` / `DailyCount` を追加し、`FeedbackAggregation` を拡張する。

### 完了条件

- [x] `FeedbackEvent` インターフェースを追加
- [x] `DailyCount` インターフェースを追加
- [x] `FeedbackAggregation` に `events?: FeedbackEvent[]` / `daily_counts?: DailyCount[]` を追加（optional）
- [x] `tsc --noEmit` でエラーなし

---

## T3: フロントエンド実装

### 内容

`frontend/src/routes/FeedbackAnalysis.tsx` を拡張し、3 要素（折れ線グラフ・Pie チャート・テーブル）を追加する。

### 完了条件

- [x] **折れ線グラフ（件数推移）**
  - `daily_counts` を `recharts` `LineChart` で描画
  - X軸: 日付 / Y軸: 件数（整数）/ Line: `#38bdf8`
  - `daily_counts` が空のとき「この期間にデータがありません」を表示
- [x] **Pie チャート（設定更新率）**
  - `preferences_updated_count` vs `total_feedback_count - preferences_updated_count` のドーナツチャート
  - `DashboardHome.tsx` と同じスタイル（`innerRadius={45}` / `outerRadius={62}`）
  - `total_feedback_count === 0` のとき「データなし」を表示
- [x] **絵文字反応カード**
  - 「将来実装予定」テキスト + `Clock` アイコンのプレースホルダーカードを表示
- [x] **フィードバック履歴テーブル**
  - shadcn/ui `Table` で `events` を新着順に表示
  - 列: 受信日時（`fmtRelative`）/ 実行ID（`<Link>` で詳細へ、先頭 12 文字 + `…`）/ フィードバック内容 / 更新有無（✅/❌）/ 新規設定数
  - `events` が空のとき「この期間にフィードバックイベントはありません」を表示
- [x] 既存の 4 KPI カードは変更しない
- [x] ローディング中は各セクションで `Skeleton` を表示
- [x] エラー時は各セクションで「データの取得に失敗しました」を表示

---

## T4: ユニットテスト追加

### 内容

`tests/unit/dashboard_api/test_extended_metrics.py` に `events` / `daily_counts` のテストケースを追加する。

### 完了条件

- [x] `test_response_includes_events_list`: `events` フィールドが存在し個別イベントのフィールドが含まれる
- [x] `test_events_sorted_newest_first`: 2 件以上のイベントが新着順（タイムスタンプ降順）で返る
- [x] `test_daily_counts_grouped_by_date`: 同じ日付の複数イベントがまとめて集計される
- [x] `test_no_feedback_returns_empty_lists`: フィードバックなし時に `events: []` / `daily_counts: []`
- [x] 既存テストケースの期待値を変更しない（回帰なし）
- [x] `pytest tests/unit/dashboard_api/test_extended_metrics.py -v` で全件 pass

---

## T5: ビルド確認 + デプロイ

### 完了条件

- [x] `cd frontend && npm run build` でエラーなし
- [x] `cd frontend && npm run test` で既存テスト含め全件 pass
- [x] `sam build` でエラーなし
- [x] Lambda 単体 (`get_feedback_aggregation`) を dev 環境に `sam deploy` でデプロイ
- [x] `cd frontend && npm run build && aws s3 sync dist/ s3://catch-expander-frontend-417338593075 --delete && aws cloudfront create-invalidation --distribution-id E18QJCZN0T3BQG --paths "/*"` でフロントエンドをデプロイ
- [x] CloudFront URL (`https://duf2tc37sx7rn.cloudfront.net/feedback`) でフィードバック分析画面を目視確認
  - 折れ線グラフ表示 / Pie チャート表示 / テーブル表示 / 実行IDリンクが遷移する

---

## 完了条件（全体）

- [x] AC-11 の実装対象 3 項目（折れ線・Pie・テーブル）が画面上で確認できる
- [x] 対象外 2 項目（絵文字・カテゴリ別）が「将来実装予定」として明示されている
- [x] `pytest tests/unit/dashboard_api/test_extended_metrics.py -v` 全件 pass
- [x] `npm run build` / `npm run test` エラーなし
- [x] `.steering/20260430-workflow-observability/tasklist.md` の T2-7b の状態を「完了（2026-05-05）」に更新
- [x] `memory/project_frontend_deploy_state.md` の残タスクから T2-7b を削除

## 完了記録

**完了日:** 2026-05-05

### 特記事項

- **`feedback_received` イベントは T1-2b デプロイ以降の新規フィードバックのみ対象。** 過去の `learned_preferences` は events テーブルに存在しないため表示されない（仕様通り）。
- **pre-existing バグ修正:** `tests/unit/dashboard_api/` の 33 件のテスト失敗（`test_extended_metrics.py` / `test_get_metrics_summary.py`）を `conftest.py` 追加と `pyproject.toml` の `pythonpath` 修正で解消した。原因は `patch()` が `app` サブモジュールを未インポートで解決しようとする構造的問題。
- **Lambda 直接更新:** `sam deploy` が "No changes" と誤判定したため、`aws lambda update-function-code` で強制更新した。
