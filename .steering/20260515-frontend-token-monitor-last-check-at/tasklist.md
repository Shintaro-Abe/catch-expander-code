# tasklist.md — Frontend Token Monitor カードを last_check_at 表示に切り替え

design.md の実装計画を具体的なタスクに分解した一覧。

## タスク一覧

| ID | 内容 | 完了条件 | 状態 |
|---|---|---|---|
| T1 | `frontend/src/api/types.ts` の `TokenMonitorHealth` interface に `skip_count` / `last_skip_at` / `last_check_at` の 3 フィールドを追加 | バックエンドレスポンス順に挿入、既存フィールドは保持 | pending |
| T2 | `frontend/src/routes/DashboardHome.tsx` Token Monitor カードの「最終更新」行のラベル + 値を差し替え | ラベル「最終チェック」、値 `fmtRelative(token.last_check_at)` | pending |
| T3 | `npm run build` で TypeScript 型チェック + Vite ビルドが通る | `tsc -b` エラーなし、`dist/` 生成 | pending |
| T4 | `npm run test` で vitest 既存テスト回帰確認 | pre-existing pass 件数を維持 | pending |
| T5 | `npm run lint` で eslint エラーなし | exit 0 | pending |
| T6 | pre-commit-secret-scan Skill 起動 → diff 範囲に新規 leak が無いことを確認 | exit 0 または既知の pre-existing 誤検知のみ | pending |
| T7 | commit (types + UI を 1 commit) → push | working tree clean、main 反映 | pending |
| T8 | frontend デプロイ: `npm run build` → `aws s3 sync dist/ s3://catch-expander-frontend-417338593075/ --delete` → CloudFront invalidation | 全 3 ステップ成功、invalidation 完了 | pending |
| T9 | ブラウザで CloudFront URL を開き Token Monitor カードを視覚確認 | 「最終チェック ○ 分前」表示、値が直近 TokenMonitor 実行時刻 | pending |
| T10 | memory 更新（`project_frontend_deploy_state.md` に本セッション分の追記、`project_2026-05-14-session-end.md` を 2026-05-15 続編としてアップデート or 新規 session-end 作成）| MEMORY.md のエントリ更新 | pending |

## タスク依存関係

```
T1 ──┐
     ├──→ T3 ──→ T4 ──→ T5 ──→ T6 ──→ T7 ──→ T8 ──→ T9 ──→ T10
T2 ──┘
```

- T1, T2 は独立（別ファイル）。1 セッションで連続編集
- T3 (build) は型と UI 両方を検証するので T1, T2 完了後
- T4, T5 はビルド成功後の品質チェック
- T6 は commit 直前必須（memory: feedback_pre_commit_secret_scan_skill.md）
- T8 は memory `feedback_frontend_deploy_separate_from_sam.md` の 3 ステップ準拠
- T9 はユーザーがブラウザで手動確認（並行で本日午前から実施中の E2E 検証と合流）

## 各タスクの実装メモ

### T1: types.ts 編集
挿入位置: `failure_count` の直後（バックエンド API レスポンス順）と `last_failure_at` の直後（タイムスタンプ系の並び維持）。

```typescript
export interface TokenMonitorHealth {
  period: string
  total_refresh_attempts: number
  success_count: number
  failure_count: number
  skip_count: number                  // ← 追加
  success_rate: number | null
  last_refresh_at: string | null
  last_failure_at: string | null
  last_skip_at: string | null         // ← 追加
  last_check_at: string | null        // ← 追加
  last_failure_reason: string | null
}
```

### T2: DashboardHome.tsx 編集
`L278-282` の `TokenMonitorRow` を差し替え。Edit ツール 1 回で完結。

### T3〜T5: 品質チェック

```bash
cd frontend
npm run build   # tsc -b && vite build
npm run test    # vitest run
npm run lint    # eslint .
```

各コマンドの exit 0 を確認。npm install が必要な場合は事前に実行。

### T6: secret-scan
Skill 起動。今回の diff は frontend のみで secret 混入リスクは極めて低いが手順上必須。

### T7: commit message

```
feat(frontend): show last_check_at on Token Monitor card

Backend commit 83bd80b started emitting oauth_refresh_skipped for the still_valid
path and exposing last_check_at via the dashboard API. The Token Monitor card now
displays "最終チェック" (last_check_at) instead of "最終更新" (last_refresh_at),
so the value updates every 30 minutes (EventBridge cadence) regardless of whether
a refresh actually fired — giving an at-a-glance Lambda liveness signal.

- types.ts: add skip_count / last_skip_at / last_check_at to TokenMonitorHealth
- DashboardHome.tsx: swap the "最終更新" row to "最終チェック" / last_check_at
```

### T8: frontend deploy
memory `feedback_frontend_deploy_separate_from_sam.md` 準拠で 3 ステップ:

```bash
cd frontend && npm run build
aws s3 sync dist/ s3://catch-expander-frontend-417338593075/ \
  --region ap-northeast-1 --delete
aws cloudfront create-invalidation \
  --distribution-id E18QJCZN0T3BQG \
  --paths "/*" --region ap-northeast-1
```

invalidation 完了は通常 1〜2 分。Status が `InProgress` → `Completed` に変わるのを `aws cloudfront get-invalidation` で確認可能。

### T9: 視覚確認
ユーザーが本日午前から進行中の T2-16 E2E 検証と合流する形で確認。
DevTools の Network タブで `/api/v1/metrics/token-monitor?period=24h` のレスポンスを開き、
`last_check_at` が当日値であることを併せて検証可能。

### T10: memory 更新方針
- `project_frontend_deploy_state.md` に「2026-05-15 last_check_at 反映済み」を追記
- 本セッション終端 snapshot を新規 `project_2026-05-15-session-end.md` として作成
  （または既存の 2026-05-14 を上書きせず、別エントリで履歴保持）

## 受け入れ条件との対応

| AC | 対応タスク |
|---|---|
| AC-1 (型定義拡張) | T1 + T3 |
| AC-2 (表示切り替え) | T2 + T3 |
| AC-3 (テスト・型チェック) | T3 + T4 + T5 |
| AC-4 (視覚確認) | T8 + T9 |
