# T2-2: Claude Design ブリーフ — Catch-Expander 監視ダッシュボード

## T2-2 の作業手順

### ステップ 1: デザインシステム確立

1. Claude Design を開く（claude.ai/design または Claude アプリ内 Labs セクション）
2. 以下の「**A. デザインシステム確立プロンプト**」を貼り付けて送信
3. 生成されたデザインシステム（色・タイポ・コンポーネント）を確認
4. 気に入らない点があれば調整指示を追加してイテレーション

### ステップ 2: 画面ごとのモック生成（T2-3 〜 T2-7 で使用）

デザインシステム確立後、同じセッション内で以下の順に各画面を依頼する:

1. **画面 1**: ダッシュボードトップ（B-1 プロンプト）
2. **画面 2**: 実行一覧（B-2 プロンプト）
3. **画面 3**: 実行詳細（B-3 プロンプト）
4. **画面 4**: レビュー品質（B-4 プロンプト）
5. **画面 5**: エラー一覧（B-5 プロンプト）

### ステップ 3: handoff

1. 各画面のモックを確認し、問題なければ Claude Design の「Export / Handoff」機能でバンドル化
2. バンドルを `frontend/` ディレクトリに配置（コミット前に必ずレビュー）
3. Claude Code（本セッション）に「T2-3 を実装して」と指示

---

## A. デザインシステム確立プロンプト

以下をそのまま Claude Design に貼り付ける。

---

```
You are designing a web UI for "Catch-Expander", an AI-agent monitoring dashboard
for personal use by a single developer. The agent autonomously researches topics,
generates Notion documents and GitHub code files, and posts results to Slack.

This dashboard shows operational observability: execution history, cost metrics,
review quality scores, API health, and error logs.

## Design System Requirements

### Tone & Mood
- Professional, data-dense monitoring dashboard (think Datadog / Linear)
- Dark mode-first (the developer reads this late at night)
- Calm, low-cognitive-load visual hierarchy
- No decorative elements — every pixel serves information

### Color Palette
Base: neutral gray (shadcn/ui "neutral" theme as starting point)
- Background: very dark gray (#0a0a0a or similar)
- Surface/card: slightly lighter (#141414 – #1a1a1a)
- Border: subtle (#2a2a2a)
- Primary text: near-white (#e8e8e8)
- Muted text: medium gray (#888)
- Primary action: use ONE accent color. Suggestions: indigo (#6366f1), violet (#7c3aed),
  or a calm teal (#14b8a6). Pick whichever looks best with the dark background.
- Status colors (keep muted, not neon):
  - Success / completed: green (#22c55e or similar calm green)
  - Failed / error: red (#ef4444)
  - Running / in-progress: amber/yellow (#f59e0b)
  - Pending: neutral gray

### Typography
- Font: Geist (already installed via @fontsource-variable/geist)
- Body: 14px / line-height 1.5
- Labels / metadata: 12px, muted color
- Headings: 18–24px, medium weight
- Numbers / metrics: tabular nums, mono font for values

### Component Conventions (shadcn/ui based)
- Card: slightly elevated surface, 8px border-radius, 1px border
- Badge: small pill for status (success / failed / running / pending)
- Table: zebra-stripe alternate rows subtly, sticky header
- Sidebar: collapsible on mobile, 220px wide on desktop
- All interactive elements: clear hover state, focus ring for a11y

### Layout
Desktop (≥768px):
- Fixed sidebar (left, 220px): logo + nav links
- Main content area: full width minus sidebar
- Max content width: 1280px

Mobile (<768px):
- Sidebar collapses to hamburger menu
- Single column layout

### Navigation (Sidebar)
- [Logo] Catch-Expander 監視
- ─────────────────
- 📊 ダッシュボード  → /dashboard
- 📋 実行一覧        → /executions
- ⭐ レビュー品質    → /review-quality
- ⚠ エラー           → /errors
- ─────────────────
- [User avatar] username  [logout]

Generate the design system tokens (colors, spacing, typography, shadows) as CSS custom
properties compatible with Tailwind v4, following the shadcn/ui pattern of CSS variable
names (--background, --foreground, --primary, etc.). Also show a sample Card, Badge,
and Table component rendered in this system.
```

---

## B-1: ダッシュボードトップ画面プロンプト

```
Using the design system above, design Screen 1: Dashboard Top (/dashboard).

## Data displayed

Period selector: [24h | 7d | 30d] toggle (top-right of content area)

### Row 1: KPI Cards (4 cards in a row)
1. Total Executions: integer count, sub-label "this period"
2. Success Rate: percentage (e.g., "94.2%"), sub-label with success/total counts
3. Avg Duration: seconds (e.g., "143s"), sub-label "per execution"
4. Total Cost: USD (e.g., "$0.48"), sub-label "tokens: 142,300"

### Row 2: Two charts side by side
Left (60% width): "Execution Timeline" — bar chart, x-axis = date, y-axis = count,
  stacked bars: success (green) / failed (red)
Right (40% width): "Review Pass Rate" — line chart or area chart, x-axis = date,
  y-axis = pass rate %

### Row 3: Two cards side by side
Left: "API Health by Service" — table with columns: Service | Calls | Success Rate | Avg Latency | Rate Limits
  Services: anthropic, notion, github, slack
Right: "Token Monitor" — shows last oauth refresh timestamp, success/failure counts,
  last failure reason if any

### Row 4: Recent Executions (last 5)
Compact table: Execution ID (truncated) | Status badge | Topic (truncated 40 chars) | Duration | Started At

## Notes
- All charts use Recharts-compatible layout (declarative React components)
- Loading state: skeleton placeholders for each card/chart
- Empty state: centered message "No data for this period"
```

---

## B-2: 実行一覧画面プロンプト

```
Using the design system above, design Screen 2: Execution List (/executions).

## Data displayed

### Filters bar (top)
- Search input: "Search by topic..." (filters client-side on topic text)
- Status filter: [All | Success | Failed | Running] (tab or dropdown)
- Period selector: [24h | 7d | 30d]

### Table
Columns:
1. Status: colored badge (success/failed/running/pending)
2. Execution ID: monospace, first 8 chars + "..." (links to /executions/:id)
3. Topic: truncated at 60 chars
4. Started At: relative time ("2 hours ago") + full datetime on hover
5. Duration: seconds (e.g., "143s") or "—" if still running
6. Tokens: integer or "—"
7. Cost: "$0.01" or "—"

### Pagination
Simple "← Previous | Page 2 of 5 | Next →" at bottom

## Notes
- Clicking a row navigates to /executions/:id
- Running executions have a subtle pulsing indicator on the status badge
- Table rows are 44px height, compact but readable
```

---

## B-3: 実行詳細画面プロンプト

```
Using the design system above, design Screen 3: Execution Detail (/executions/:id).

## Layout

### Header section
- ← Back to list (link)
- Execution ID: monospace full ID
- Status badge (large)
- Topic: full text, 20px heading
- Metadata row: "Started: 2026-04-30 02:28:13 | Duration: 156s | Tokens: 18,420 | Cost: $0.06"
- Deliverables: 📝 Notion (link) | 💻 GitHub (link)  (shown only if present)

### Collapsible section: Workflow Plan
Collapsed by default. When expanded shows JSON-like key-value:
- planned_subagents: researcher, generator, reviewer
- topic_category: 技術
- expected_deliverable_type: iac_code
- planning_summary: "..." (truncated with expand)

### Timeline section (main content)
Title: "Timeline (N events)"

Each event is a row:
┌──────────────────────────────────────────────────────┐
│ ⏺ 02:28:13  topic_received                     [+]  │
│   channel: C12345  topic: "AWS Route 53..."          │
└──────────────────────────────────────────────────────┘

Event types and their visual treatment:
- topic_received: blue dot
- workflow_planned: purple dot
- subagent_started: gray dot (indented slightly)
- subagent_completed: green dot (indented)
- subagent_failed: red dot (indented)
- research_completed: teal dot
- review_completed: show pass/fail indicator
- execution_completed: large green/red dot, final summary
- error: red background highlight

Payload is collapsed by default, expandable via [+] button.
When expanded, show as formatted key-value pairs (not raw JSON).

## Notes
- The timeline is the centerpiece — it should feel like a clear log viewer
- Mobile: timeline items stack vertically, metadata becomes 2-column grid
```

---

## B-4: レビュー品質画面プロンプト

```
Using the design system above, design Screen 4: Review Quality (/review-quality).

## Data displayed

Period selector: [24h | 7d | 30d]

### Summary cards (top row, 4 cards)
1. Total Reviews: count
2. Pass Rate: percentage
3. Avg Issues per Review: decimal (e.g., "2.4")
4. Recurring Issues: count of issues seen ≥2 times

### Charts row
Left: "Pass Rate Trend" — area chart over time
Right: "Issue Category Distribution" — horizontal bar chart or donut
  Categories: logic_error, missing_validation, code_quality, security, other

### Issues table
Title: "Reviews with Unresolved Issues"
Columns:
1. Execution ID (link)
2. Topic (truncated)
3. Pass/Fail badge
4. Issues count
5. Unresolved (code_related_unfixed_count)
6. Date

Rows highlighted in amber if unresolved > 0.
```

---

## B-5: エラー一覧画面プロンプト

```
Using the design system above, design Screen 5: Error List (/errors).

## Data displayed

Period selector: [24h | 7d | 30d]

### Summary cards (top row, 3 cards)
1. Total Errors: count
2. Affected Executions: count
3. Most Common Error Type: string (e.g., "notion_api_error")

### Error cards list
Each error is a card:
┌──────────────────────────────────────────────────────────┐
│ ⚠ notion_api_error               2026-04-30 03:14        │
│ Execution: exec-abc123 (link)   Subagent: generator       │
│ Message: "Rate limit exceeded on Notion API /v1/pages"    │
│ [▶ Stack Trace]  ← collapsible                           │
└──────────────────────────────────────────────────────────┘

Errors are grouped by error_type by default.
"Group by: [Error Type | Execution | Date]" toggle at top.

### Recurring error banner
If same error_message appears ≥3 times in the period, show a yellow banner:
"⚠ 'Rate limit exceeded on Notion API' has occurred 5 times this period."
```

---

## handoff 後の Claude Code への指示テンプレート

画面モックが完成し handoff バンドルを受け取ったら、以下を本セッションに貼り付ける:

```
T2-3 を実装してください。
Claude Design の handoff バンドルを [frontend/ 内のパス] に配置しました。
設計書の仕様（.steering/20260430-workflow-observability/design.md §5.4）に従い、
TanStack Query / Zustand 統合・API client 結線・アクセシビリティ補強を補完してください。
```

---

## 参照ドキュメント

- 詳細仕様: `.steering/20260430-workflow-observability/design.md`
- 要件: `.steering/20260430-workflow-observability/requirements.md`
- API エンドポイント一覧: `design.md §3.2`
- セキュリティ要件: `design.md §4.6`
