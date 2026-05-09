# 設計: ダッシュボード Tier 2 完成 + 画面 5 タブ統合

## 全体方針

要件は 4 層（reviewer prompt / orchestrator emit / backend Lambda / frontend SPA）にまたがる。1 PR で全層を投入すると差分が大きくレビュー困難になり、`memory/project_review_loop_recurring_patch_site.md` の警告対象（`_run_review_loop` 周辺）にも触れるため、**フェーズ分割で段階リリース** する。データ層が空のまま UI を作っても意味が無いので、データ層を先に投入する。

### フェーズ構成

| Phase | 範囲 | 効用 | 完了条件 |
|---|---|---|---|
| **P1: データパイプライン** | reviewer prompt + orchestrator emit + 既存 backend 拡張（破壊的変更なし） | 新規 review_completed イベントから `issue_category` が events テーブルに蓄積開始 | 1 件以上の新規実行で events テーブルに `issue_category` が含まれた payload を確認 |
| **P2: 画面 4 完成** | backend get_review_quality 拡張 + ReviewQuality.tsx の 5 グラフ追加 + フィルタ UI | AC-5 完全達成 | E2E でフィルタチップ・カテゴリ円グラフ・繰り返し検出表が動作 |
| **P3: 画面 5 タブ統合** | shadcn tabs 導入 + ErrorAndHealth.tsx 再構成 + 4 タブ実装 | AC-6 完全達成 | E2E で 4 タブ全て表示・データ取得 |

各 Phase の完了後に独立コミット可能（依存方向: P1 → P2 → P3）。本 steering 配下の tasklist で各 Phase をタスクグループ化する。

## Phase 1: データパイプライン

### 1.1 reviewer prompt 拡張

**変更ファイル**: `src/agent/prompts/reviewer.md`

- 出力 JSON スキーマの `issues[]` 要素に `issue_category` フィールドを追加
- 値域: `terraform_schema` / `iam_action` / `syntax` / `api_version` / `other`
- 分類ヒューリスティクスをプロンプトに記述:
  ```
  - terraform 構文・provider 引数・resource プロパティの誤り → terraform_schema
  - 存在しない IAM アクション・ロール権限不備 → iam_action
  - 一般的なコード構文・型・lint 違反 → syntax
  - 古い API バージョン・廃止 SDK 参照 → api_version
  - 上記いずれにも該当しない → other
  ```
- 既存の severity / fix_instruction フィールドは変更しない（互換維持）

#### 出力例

```json
{
  "issues": [
    {
      "item": "main.tf line 14",
      "severity": "error",
      "issue_category": "terraform_schema",
      "description": "duplicate expiration block",
      "fix_instruction": "remove duplicate expiration on line 14"
    }
  ]
}
```

### 1.2 orchestrator emit 拡張

**変更ファイル**: `src/agent/orchestrator.py:1437-1450 周辺`

- `_run_review_loop` 内の `review_completed` 発火部で、`issues_count` に加えて新規フィールドを追加:
  - `issue_categories`: `dict[str, int]` 形式のカテゴリ別件数（例: `{"terraform_schema": 2, "iam_action": 1}`）
  - 既存 `issues` リストはそのまま payload に残し、各 issue dict に LLM が出した `issue_category` を保持
- **下位互換**: LLM が `issue_category` を出さなかった場合は `"other"` でフォールバック（KeyError 防止）
- `_run_review_loop` 周辺は再発パッチ密集地点のため、**`isinstance` ガードを必須**:
  ```python
  raw_issues = review_result.get("issues", [])
  if not isinstance(raw_issues, list):
      raw_issues = []
  category_counts: dict[str, int] = {}
  for issue in raw_issues:
      if not isinstance(issue, dict):
          continue
      cat = issue.get("issue_category")
      if not isinstance(cat, str) or cat not in _VALID_CATEGORIES:
          cat = "other"
      category_counts[cat] = category_counts.get(cat, 0) + 1
  ```

### 1.3 backend 軽微拡張（破壊的変更なし）

**変更ファイル**: `src/dashboard_api/get_review_quality/app.py`

- レスポンスに `issue_categories` 集計を追加（過去データは欠損のため null 許容）
- 既存フィールドは変更せず追加のみ

```python
# 追加部分
# Codex 1 回目 P1-1 / 2 回目 P2-B / P2-C を反映した最終形
seen_issue_categories_payload = False
issue_category_totals: dict[str, int] = {}
for item in items:
    payload = item.get("payload") or {}
    cats = payload.get("issue_categories")
    if not isinstance(cats, dict):
        continue
    seen_issue_categories_payload = True
    for k, v in cats.items():
        if not isinstance(k, str):
            continue
        count = _coerce_count(v)  # int / Decimal を受容、bool / 負数 / 非整数 / NaN / Infinity を除外
        if count is None:
            continue
        issue_category_totals[k] = issue_category_totals.get(k, 0) + count

# レスポンスに追加
# 全件 {} (新 pipeline 動作中、件数 0) → {} を返す
# 全件 field 欠損 (past data のみ) → None を返す
return json_response(200, {
    "data": {
        ...,  # 既存フィールド
        "issue_categories": issue_category_totals if seen_issue_categories_payload else None,
    },
})
```

### 1.4 P1 完了条件

- [ ] `tests/unit/agent/test_orchestrator.py` で `_run_review_loop` のカテゴリ集計ロジックを検証
- [ ] 非 dict reviewer 応答 (`_parse_claude_response` の返却値が None / list / scalar) は fallback dict 化し、`review_completed` emit と最終 `quality_metadata.notes` に流れる (Codex 1 回目 P1-3 対応)
- [ ] backend `_coerce_count` が DDB の `Decimal` を受容し、bool / 負数 / 非整数 / 非有限値 (NaN / Infinity) は除外する (Codex 1〜2 回目 P1-1 / P2-B 対応)
- [ ] `issue_categories` レスポンスは新 pipeline 動作中の全 `{}` ケースは `{}`、past data 欠損ケースは `None` として区別する (Codex 2 回目 P2-C 対応)
- [ ] dev デプロイ → Slack トピック投入 → events テーブル `payload.issue_categories` を直接 query して 1 件以上確認
- [ ] 既存 ReviewQuality.tsx は触らない（壊れない）

---

## Phase 2: 画面 4 完成

### 2.1 backend get_review_quality 拡張

**変更ファイル**: `src/dashboard_api/get_review_quality/app.py`

#### 追加機能

| 機能 | 実装方針 |
|---|---|
| カテゴリ別フィルタ | `?category=` クエリパラメータ。未修正一覧を該当カテゴリのみに絞る |
| pass 率推移時系列 | review_completed events を日次バケット化、各日の `pass_count / total` を返す |
| fix loop 発火回数分布 | 各 review_completed の `iteration` 値を集計（0 / 1 / 2 の度数）|
| 同種 issue 繰り返し検出 | 同一 `execution_id` 内で同じ category の issue が複数 iteration で残存しているケースを抽出 |
| トピック別 pass 率 | events テーブルから `workflow_planned` イベント (execution_id, topic_category) と `review_completed` を join、topic_category 別 pass 率を返す |

#### レスポンススキーマ

```json
{
  "data": {
    "period_days": 30,
    "total_reviews": 42,
    "pass_count": 35,
    "pass_rate": 0.833,
    "issue_categories": {
      "terraform_schema": 14,
      "iam_action": 11,
      "syntax": 9,
      "api_version": 6,
      "other": 2
    },
    "pass_rate_timeseries": [
      {"date": "2026-05-01", "pass": 3, "total": 4},
      {"date": "2026-05-02", "pass": 5, "total": 5}
    ],
    "fix_loop_distribution": {
      "0": 28,
      "1": 11,
      "2": 3
    },
    "recurring_issues": [
      {
        "execution_id": "exec-abc123",
        "category": "iam_action",
        "occurrence_count": 3,
        "max_iteration_reached": 2,
        "last_seen_at": "2026-05-07T14:32:00Z",
        "sample_description": "s3:GeneratePresignedUrl is not a valid IAM action"
      }
    ],
    "pass_rate_by_topic": [
      {"topic_category": "技術", "pass": 25, "total": 28, "rate": 0.893},
      {"topic_category": "時事", "pass": 7, "total": 10, "rate": 0.700}
    ],
    "unfixed_code_issues": [
      {
        "execution_id": "exec-abc123",
        "timestamp": "2026-05-07T14:32:00Z",
        "iteration": 2,
        "code_related_unfixed_count": 3,
        "issue_category": "iam_action"
      }
    ]
  }
}
```

#### クエリ最適化

- DynamoDB events テーブルは `event_type=review_completed` の GSI クエリで取得
- topic_category 取得は同一 execution_id に対する追加クエリ（events テーブルの execution_id PK 直接 query、N+1 に近いが events 件数 < 100/月想定で許容）
- 過去データに `issue_categories` が無いケースは集計から除外（null 許容）

### 2.2 frontend ReviewQuality.tsx 拡張

**変更ファイル**: `frontend/src/routes/ReviewQuality.tsx`

#### 追加コンポーネント構成

```
<ReviewQuality>
  <Header (期間切替・既存)>
  <KpiRow (3 カード・既存)>
  <Grid 2 列>
    <PassRateTrendCard>            ← 新規 (Recharts LineChart)
    <FixLoopDistributionCard>      ← 新規 (Recharts BarChart 横)
  </Grid>
  <Grid 2 列>
    <IssueCategoryPieCard>         ← 新規 (Recharts PieChart, クリックでフィルタ更新)
    <PassRateByTopicCard>          ← 新規 (Recharts BarChart)
  </Grid>
  <RecurringIssuesCard>            ← 新規 (Table)
  <UnfixedIssuesCard>              ← 既存テーブルにフィルタチップ追加
    <CategoryFilterChips>          ← 新規 (Badge ベース)
    <Table (既存)>                 ← issue_category 列を 1 列追加
  </UnfixedIssuesCard>
</ReviewQuality>
```

#### URL 状態管理

- React Router の `useSearchParams` で `?days=&category=` を管理
- カテゴリ未指定時は全件、指定時は該当カテゴリのみテーブル絞り込み
- 円グラフのスライスクリックで `setSearchParams({...prev, category: "iam_action"})` を呼ぶ
- 「すべて」チップで `category` パラメータを削除

#### バンドルサイズ抑制

- recharts はすでに導入済み (3.8.1)。新規ライブラリは追加しない
- 既存 components/ui を再利用（Card / Table / Skeleton）。新規 shadcn コンポーネントは P2 段階では追加しない

### 2.3 P2 完了条件

- [ ] `tests/unit/dashboard_api/test_get_review_quality.py` で 5 つの新フィールドのテスト追加
- [ ] `frontend/src/test/` に ReviewQuality.test.tsx 追加（モックレスポンス + チャート描画 + フィルタ動作）
- [ ] E2E で円グラフクリック → URL 更新 → 一覧フィルタが連動することを確認

---

## Phase 3: 画面 5 タブ統合

### 3.1 shadcn Tabs 追加

**新規ファイル**: `frontend/src/components/ui/tabs.tsx`

- shadcn/ui の `tabs` コンポーネントを `npx shadcn@latest add tabs` で導入
- 依存追加: `@radix-ui/react-tabs`（Tailwind Tabs プリミティブ）

### 3.2 ErrorList.tsx → ErrorAndHealth.tsx へのリネーム再構成

**変更ファイル**:
- `frontend/src/routes/ErrorList.tsx` → `frontend/src/routes/ErrorAndHealth.tsx` にリネーム
- `frontend/src/App.tsx` のルート定義を更新
- ナビゲーション側のラベルを「エラー」→「エラー & 健全性」に更新

#### タブ構成

```
<ErrorAndHealth>
  <Tabs defaultValue="errors">
    <TabsList>
      <TabsTrigger value="errors">エラー</TabsTrigger>
      <TabsTrigger value="api-health">API 健全性</TabsTrigger>
      <TabsTrigger value="token-monitor">Token Monitor</TabsTrigger>
      <TabsTrigger value="recurrence">再発検出</TabsTrigger>
    </TabsList>
    <TabsContent value="errors"><ErrorsTab /></TabsContent>
    <TabsContent value="api-health"><ApiHealthTab /></TabsContent>
    <TabsContent value="token-monitor"><TokenMonitorTab /></TabsContent>
    <TabsContent value="recurrence"><RecurrenceTab /></TabsContent>
  </Tabs>
</ErrorAndHealth>
```

#### 各タブの実装

- **ErrorsTab**: 既存 ErrorList.tsx の `<Card>` 部分をそのまま移植（変更なし）
- **ApiHealthTab**: 既存 `/api/v1/metrics/api-health` を呼び、サブタイプ別折れ線・p50/p95/p99・rate_limit_hit を表示
- **TokenMonitorTab**: 既存 `/api/v1/metrics/token-monitor` を呼び、時系列折れ線 + 残存時間ヒストグラム + 失敗時バナー
- **RecurrenceTab**: 新規エンドポイント `/api/v1/metrics/recurrence?days=` を呼び、エラータイプ別時系列クラスター + stage 別失敗頻度 + 7 日 3 回ルールに基づくハイライト

### 3.3 backend Tier 2.3 (再発検出) Lambda 追加

**新規 Lambda**: `src/dashboard_api/get_recurrence/`

- events テーブルから `error` イベントを取得
- error_type 別に時系列バケット化
- 7 日以内に同じ error_type が 3 回以上 → ハイライト対象
- stage (orchestrator / generator / reviewer / storage / etc.) 別の失敗頻度集計

#### 新規 SAM リソース

- `GetRecurrenceFunction` (Lambda)
- API Gateway ルート `GET /api/v1/metrics/recurrence`
- IAM: events テーブル query のみ許可（既存パターン踏襲）

### 3.4 メインダッシュボード Token Monitor カードの維持

- `DashboardHome.tsx` の Token Monitor カードは **削除しない**
- 専用タブと併存させる方針（KPI として常時可視化、深掘りはタブ）

### 3.5 P3 完了条件

- [ ] shadcn tabs 導入後、既存 import alias（`@/components/ui/...`）と整合
- [ ] `tests/unit/dashboard_api/test_get_recurrence.py` 新規追加
- [ ] `frontend/src/test/ErrorAndHealth.test.tsx` で 4 タブの切替テスト
- [ ] E2E で 4 タブ全部にデータが表示されること
- [ ] メインダッシュボードのカードが従来通り表示されることを回帰確認

---

## データ構造の変更まとめ

### events テーブル `review_completed` payload

| フィールド | 既存/新規 | 型 | 備考 |
|---|---|---|---|
| `iteration` | 既存 | int | |
| `passed` | 既存 | bool | |
| `issues_count` | 既存 | int | |
| `fixer_notes_count` | 既存 | int | |
| `code_related_unfixed_count` | 既存 | int | |
| `issues` | 既存 | list[dict] | 各 dict に `issue_category` を追加 |
| `issue_categories` | **新規** | dict[str, int] | カテゴリ別件数 |

### 既存スキーマへの破壊的変更

**なし**。すべて追加フィールドで、past data には欠損として null 許容で扱う。

## 影響範囲分析

| 対象 | 影響 |
|---|---|
| `_run_review_loop` 周辺 | 既存ロジック非変更、emit payload に追加のみ。再発パッチ警戒対象 → P1 で `isinstance` ガード厳守 |
| 既存 ReviewQuality.tsx の挙動 | 新グラフ追加のみ、既存テーブルは列追加 + フィルタチップで挙動変更 |
| 既存 ErrorList.tsx の URL | リネームに伴い `/errors` → `/errors-and-health` または `/errors` 維持で内部だけリネーム。**`/errors` URL は維持し内部のコンポーネント名のみ変更** |
| 既存テスト | ErrorList.test.tsx → ErrorAndHealth.test.tsx に追従 |
| バンドルサイズ | shadcn tabs (`@radix-ui/react-tabs`) は ~5KB。recharts は既存 |
| Lambda コールドスタート | get_review_quality は集計クエリ追加で +50-100ms 想定。許容 |
| DynamoDB 読み取りコスト | events テーブル query 件数は変化なし（フィルタ条件追加のみ）。コスト増は無視可能 |

## 代替案検討（なぜこの設計か）

### 代替案 A: 集計を frontend 側で計算

- pros: backend 改修不要、Lambda コスト増なし
- cons: events 全件を frontend に送る必要があり、件数増で破綻。SPA 側にビジネスロジックが流出
- **却下**: 既存設計（backend で集計）と整合する方が良い

### 代替案 B: issue_category を backend ヒューリスティクスで分類（reviewer prompt は触らない）

- pros: prompt 改修なし、過去データも遡及分類可能
- cons: キーワードマッチの精度が低い（特に terraform_schema vs syntax の境界）。reviewer の意図を反映できない
- **採用しない**: LLM 分類のほうが文脈理解で正確。`requirements.md:455-456` の Open Question 12 でも本案を推奨

### 代替案 C: フェーズ分割せず 1 PR

- pros: PR 数が少ない、マージ手間が少ない
- cons: 差分が 600 行超になり Codex レビューの効果が薄れる。`_run_review_loop` 周辺の再発パッチエリアを慎重に進められない
- **却下**: フェーズ分割の方が安全

## 関連ナレッジ参照

- [`obsidian/2026-04-26_symptomatic-fix-anti-pattern.md`](../../obsidian/2026-04-26_symptomatic-fix-anti-pattern.md) — 「3 commit ルール」アンチパターン回避
- [`obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md`](../../obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md) — フェーズ間 Codex レビューの活用
- `memory/project_review_loop_recurring_patch_site.md` — `_run_review_loop` 修正時の必読リスト
- `memory/feedback_anti_pattern_discipline.md` — プロンプト/パイプライン/型 3 層代替案規律
- 親 steering: [`.steering/20260430-workflow-observability/`](../20260430-workflow-observability/) — Tier 1/2 元設計
