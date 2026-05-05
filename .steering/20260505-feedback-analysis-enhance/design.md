# 設計 — フィードバック分析画面の充実 (AC-11 完成)

> 関連: [`requirements.md`](./requirements.md) / [`tasklist.md`](./tasklist.md)

---

## 1. 実装アプローチ

バックエンドのレスポンスに `events`（個別イベントリスト）と `daily_counts`（日別件数）を追加し、
フロントエンドでそれを受け取って折れ線グラフ・Pie チャート・テーブルを描画する。

- 既存の集計フィールド（`total_feedback_count` 等）は変更しない（後方互換）
- `query_event_type()` ヘルパーはそのまま使用し、戻り値を加工して `events` / `daily_counts` を生成
- チャートは既存の `recharts` パターン（`DashboardHome.tsx` の PieChart / BarChart 実装）を踏襲

---

## 2. 変更するファイル

| ファイル | 変更種別 | 内容 |
|---|---|---|
| `src/dashboard_api/get_feedback_aggregation/app.py` | 変更 | `events` / `daily_counts` をレスポンスに追加 |
| `frontend/src/api/types.ts` | 変更 | `FeedbackEvent` 追加、`FeedbackAggregation` に optional フィールド追加 |
| `frontend/src/routes/FeedbackAnalysis.tsx` | 変更 | 折れ線グラフ・Pie チャート・テーブルを追加 |
| `tests/unit/dashboard_api/test_extended_metrics.py` | 変更 | `events` / `daily_counts` の新規テストケース追加 |

新規ファイルは作成しない。新規 npm 依存は追加しない。

---

## 3. バックエンド設計

### 3-1. `get_feedback_aggregation/app.py` の変更

**追加する処理（既存の集計ロジックの後に追記）：**

```python
# daily_counts: タイムスタンプの先頭 10 文字 (YYYY-MM-DD) でグルーピング
from collections import defaultdict
daily: dict[str, int] = defaultdict(int)
for item in items:
    date = (item.get("timestamp") or "")[:10]
    if date:
        daily[date] += 1
daily_counts = [{"date": d, "count": c} for d, c in sorted(daily.items())]

# events: 個別イベントリスト（新着順）
events = []
for item in reversed(items):          # query_event_type は昇順返却 → 反転で新着順
    payload = item.get("payload") or {}
    events.append({
        "execution_id": item.get("execution_id"),
        "timestamp": item.get("timestamp"),
        "subtype": payload.get("subtype", "mention_reply"),
        "reply_text_summary": payload.get("reply_text_summary", ""),
        "learned_preferences_updated": bool(payload.get("learned_preferences_updated")),
        "new_preferences_count": int(payload.get("new_preferences_count") or 0),
        "total_preferences_count": payload.get("total_preferences_count"),
    })
```

**レスポンス（`data` オブジェクトに追加）：**

```json
{
  "data": {
    "period": "7d",
    "total_feedback_count": 5,
    "preferences_updated_count": 3,
    "avg_new_preferences": 1.5,
    "latest_total_preferences": 8,
    "daily_counts": [
      { "date": "2026-04-29", "count": 2 },
      { "date": "2026-05-01", "count": 3 }
    ],
    "events": [
      {
        "execution_id": "exec-abc123",
        "timestamp": "2026-05-01T14:30:00.000Z",
        "subtype": "mention_reply",
        "reply_text_summary": "もう少し詳しく説明して...",
        "learned_preferences_updated": true,
        "new_preferences_count": 2,
        "total_preferences_count": 7
      }
    ]
  }
}
```

---

## 4. フロントエンド設計

### 4-1. `types.ts` の変更

```typescript
// 追加
export interface FeedbackEvent {
  execution_id: string
  timestamp: string
  subtype: string
  reply_text_summary: string
  learned_preferences_updated: boolean
  new_preferences_count: number
  total_preferences_count: number | null
}

export interface DailyCount {
  date: string   // "YYYY-MM-DD"
  count: number
}

// 既存の FeedbackAggregation に optional フィールドを追加
export interface FeedbackAggregation {
  period: string
  total_feedback_count: number
  preferences_updated_count: number
  avg_new_preferences: number | null
  latest_total_preferences: number | null
  // 追加フィールド（optional で後方互換を維持）
  daily_counts?: DailyCount[]
  events?: FeedbackEvent[]
}
```

### 4-2. `FeedbackAnalysis.tsx` のレイアウト構成

```
┌─ フィードバック分析 ─────────────────────────────── [7d ▼] ─┐
│                                                              │
│  [KPI: 総数]  [KPI: 更新件数]  [KPI: 平均新規]  [KPI: 累計] │  ← 既存維持
│                                                              │
│  ┌── 件数推移（LineChart）────────────────────────────────┐  │
│  │  X軸: 日付  Y軸: 件数  Line: feedback_count           │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌── 設定更新率（PieChart）──┐  ┌── 絵文字反応 ────────────┐  │
│  │  更新あり / 更新なし      │  │  （将来実装予定）         │  │
│  │  ドーナツ + 中央に %     │  │  データ未収集のため       │  │
│  └──────────────────────────┘  │  表示不可                 │  │
│                                └──────────────────────────┘  │
│                                                              │
│  ┌── フィードバック履歴（Table）───────────────────────────┐  │
│  │  日時 / 実行ID / 内容 / 更新 / 新規設定数              │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 4-3. 各コンポーネントの実装方針

#### 折れ線グラフ（LineChart）

- `recharts` の `LineChart` + `XAxis` + `YAxis` + `Line` + `Tooltip` + `ResponsiveContainer`
- `daily_counts` が空または未定義のときは「データなし」テキスト表示
- `DashboardHome.tsx` の `BarChart` と同じ `CartesianGrid` / `Tooltip` スタイルを踏襲

```tsx
<ResponsiveContainer width="100%" height={180}>
  <LineChart data={d.daily_counts ?? []}>
    <CartesianGrid strokeDasharray="3 3" stroke="#2a2a2a" />
    <XAxis dataKey="date" tick={{ fontSize: 11 }} stroke="#52525b" />
    <YAxis allowDecimals={false} tick={{ fontSize: 11 }} stroke="#52525b" />
    <Tooltip contentStyle={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6 }} />
    <Line type="monotone" dataKey="count" stroke="#38bdf8" strokeWidth={2} dot={{ r: 3 }} />
  </LineChart>
</ResponsiveContainer>
```

#### Pie チャート（設定更新率）

- `DashboardHome.tsx` の `PieChart`（ドーナツ型、`innerRadius={45}` / `outerRadius={62}`）と同じパターン
- データ: `[{ name: "更新あり", value: preferences_updated_count }, { name: "更新なし", value: total - updated }]`
- `total_feedback_count === 0` のときは「データなし」表示

#### 絵文字反応カード

- `Card` コンポーネントを使い「将来実装予定」テキストのみ表示
- `Clock` アイコン（lucide-react）+ muted テキストカラー

#### フィードバック履歴テーブル

- shadcn/ui の `Table` / `TableBody` / `TableRow` 等（`DashboardHome.tsx` と同パターン）
- `execution_id` は `<Link to={/executions/${e.execution_id}}>` でリンク（先頭12文字 + `…`）
- `timestamp`: `fmtRelative()` ユーティリティで相対時刻表示（既存関数）
- `learned_preferences_updated`: ✅ / ❌ アイコン（`CheckCircle2` / `XCircle`）
- `events` が空配列のときは「この期間にフィードバックイベントはありません」

---

## 5. テスト設計

`tests/unit/dashboard_api/test_extended_metrics.py` に以下を追加：

| テストケース | 確認内容 |
|---|---|
| `test_response_includes_events_list` | `events` フィールドが存在し個別イベントが含まれる |
| `test_events_sorted_newest_first` | 複数イベントが新着順（タイムスタンプ降順）で返る |
| `test_daily_counts_grouped_by_date` | 同じ日付の複数イベントがまとめて集計される |
| `test_no_feedback_returns_empty_lists` | フィードバックなし時は `events: []` / `daily_counts: []` |

既存テスト（`test_no_feedback_returns_zeros_and_nulls` / `test_aggregates_feedback_counts` 等）の期待値は変更しない。

---

## 6. 影響範囲分析

| 対象 | 影響 |
|---|---|
| 既存の `FeedbackAggregation` 型 | optional フィールド追加のみ → 既存コードに影響なし |
| 他の routes コンポーネント | 変更なし |
| バックエンドの他 Lambda | 変更なし |
| `_common.py` | 変更なし |
| 既存ユニットテスト | `events` / `daily_counts` は optional なので既存期待値に影響なし |
| デプロイ | Lambda のみ更新（SAM template 変更なし） |
