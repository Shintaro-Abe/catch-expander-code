# requirements.md — Frontend Token Monitor カードを last_check_at 表示に切り替え

## 背景

前日（2026-05-14）にバックエンド側を修正済み（commit `83bd80b`）。本作業（2026-05-15）はその UI 反映:
- `src/token_monitor/handler.py`: still_valid 経路に `oauth_refresh_skipped` emit を追加
- `src/dashboard_api/get_token_monitor_health/app.py`: `skip_count` / `last_skip_at` / `last_check_at` 3 フィールドを API レスポンスに追加

しかし frontend SPA（`frontend/src/routes/DashboardHome.tsx` Token Monitor カード）は依然として
`last_refresh_at`（completed のみの最新タイムスタンプ）を「最終更新」として表示している。
そのため「Lambda は 30 分ごとに動いているのに、ダッシュボード上では数時間〜数日前で固定」という
ユーザーから見える症状が解消していない。

## ユーザーストーリー

- **As a** 運用者（個人開発者）
- **I want** Token Monitor カードの「最終更新」がリフレッシュ実行有無に依らず最新のチェック時刻を示してほしい
- **So that** Lambda の死活確認をダッシュボードだけで完結できる

## 受け入れ条件

### AC-1: 型定義の追加
- `frontend/src/api/types.ts` の `TokenMonitorHealth` interface に以下 3 フィールドを追加
  - `skip_count: number`
  - `last_skip_at: string | null`
  - `last_check_at: string | null`
- 既存フィールド（`last_refresh_at` 等）は維持（後方互換のため）

### AC-2: 表示の切り替え
- `frontend/src/routes/DashboardHome.tsx` Token Monitor カードの「最終更新」行
  - ラベル: 「最終更新」→「**最終チェック**」
  - 値: `fmtRelative(token.last_refresh_at)` → `fmtRelative(token.last_check_at)`
- 行数・カードの見た目はそのまま維持（最小変更原則）

### AC-3: 既存テスト・型チェック
- `npm run typecheck`（または相当）がエラーなく通る
- 既存の vitest テストがあれば全 pass

### AC-4: 視覚確認
- dev デプロイ後、CloudFront URL でログインし Token Monitor カードに「最終チェック」が
  当日の TokenMonitor 実行時刻に近い値で表示されることを確認

## 制約事項

- **UI レイアウトは変更しない**: 「スキップ回数」行や「最終スキップ時刻」を別表示として
  追加することは今回スコープ外（最小変更）。
- **新 3 フィールド全てを type に入れる**: 表示は `last_check_at` のみ使うが、
  API レスポンスに含まれる以上、型整合のため 3 つとも追加する。
- **frontend のデプロイは別経路**（memory: `feedback_frontend_deploy_separate_from_sam.md`）:
  `npm run build` → `aws s3 sync` → CloudFront invalidation の 3 ステップが必要。
- **コストインパクト ゼロ**: 静的アセット差し替えのみ。

## スコープ外

- スキップ回数 / 最終失敗時刻の追加表示（仕様判断保留）
- バックエンドの API レスポンス変更（既に完了済み）
- 他カード（Cost / API Health 等）の改修
- Playwright E2E 自動化
