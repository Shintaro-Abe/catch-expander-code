# design.md — Frontend Token Monitor カードを last_check_at 表示に切り替え

## 実装アプローチ

### 全体方針

- TypeScript 型定義の拡張は **add-only**（既存フィールドは保持、後方互換）
- UI 変更は最小 1 行差し替え（ラベル + 値の参照先）
- 既存の `TokenMonitorRow` コンポーネントはそのまま再利用
- vitest テスト追加は **不要**（Token Monitor 関連の既存テスト無し、最小変更で型チェックがあれば十分）

## 1. types.ts の変更

### ファイル
`frontend/src/api/types.ts`

### Before
```typescript
export interface TokenMonitorHealth {
  period: string
  total_refresh_attempts: number
  success_count: number
  failure_count: number
  success_rate: number | null
  last_refresh_at: string | null
  last_failure_at: string | null
  last_failure_reason: string | null
}
```

### After
```typescript
export interface TokenMonitorHealth {
  period: string
  total_refresh_attempts: number
  success_count: number
  failure_count: number
  skip_count: number
  success_rate: number | null
  last_refresh_at: string | null
  last_failure_at: string | null
  last_skip_at: string | null
  last_check_at: string | null
  last_failure_reason: string | null
}
```

### 設計判断
- フィールド順序はバックエンド API のレスポンス順に合わせる（人間が JSON と型を見比べる際に対応が取りやすい）
- 全 3 フィールド追加（表示は `last_check_at` のみ使用するが、型整合のため省略しない）

## 2. DashboardHome.tsx の変更

### ファイル
`frontend/src/routes/DashboardHome.tsx`

### 変更箇所（L278-282 付近）

### Before
```tsx
<TokenMonitorRow
  icon={<RefreshCw size={14} className="text-sky-400" />}
  label="最終更新"
  value={fmtRelative(token.last_refresh_at)}
/>
```

### After
```tsx
<TokenMonitorRow
  icon={<RefreshCw size={14} className="text-sky-400" />}
  label="最終チェック"
  value={fmtRelative(token.last_check_at)}
/>
```

### 設計判断
- アイコン（`RefreshCw`）と class は維持。意味的には「リフレッシュ」アイコンだが「チェック」概念とも違和感なし
- 「最終更新」→「最終チェック」: バックエンドのフィールド名（`last_check_at`）と意味的に整合し、UI 上の文言だけでも「リフレッシュ実行有無に依らない」ことを示唆できる
- 他の Row（成功 / 失敗 / 成功率）は変更しない

## 3. ビルド・テスト

### 3.1 型チェック
```bash
cd frontend && npm run build
```
`tsc -b` が `types.ts` の interface 変更を検知し、`DashboardHome.tsx` の `token.last_check_at` 参照を型レベルで検証する。

### 3.2 テスト
```bash
cd frontend && npm run test
```
既存テストの回帰確認のみ（Token Monitor 関連の新規 vitest テストは追加しない）。

### 3.3 lint
```bash
cd frontend && npm run lint
```

## 4. デプロイ手順

memory `feedback_frontend_deploy_separate_from_sam.md` に従い、SAM とは別経路でデプロイ:

```bash
# 1. ビルド
cd frontend && npm run build

# 2. S3 sync
aws s3 sync dist/ s3://catch-expander-frontend-417338593075/ \
  --region ap-northeast-1 --delete

# 3. CloudFront invalidation
aws cloudfront create-invalidation \
  --distribution-id E18QJCZN0T3BQG \
  --paths "/*" \
  --region ap-northeast-1
```

### バケット名・Distribution ID の根拠
- 直近の `sam deploy` 出力 (2026-05-14 15:21 JST) より:
  - `FrontendBucketName: catch-expander-frontend-417338593075`
  - `FrontendDistributionId: E18QJCZN0T3BQG`
  - `FrontendUrl: https://duf2tc37sx7rn.cloudfront.net`

## 5. 影響範囲

| レイヤ | ファイル | 変更 |
|---|---|---|
| TypeScript 型 | `frontend/src/api/types.ts` | +3 フィールド（add-only） |
| React UI | `frontend/src/routes/DashboardHome.tsx` | label + value の参照先差し替え（1 ヶ所） |
| 既存 frontend テスト | （変更なし） | 該当する vitest テストが無いため不要 |
| バックエンド | （変更なし） | 既に commit 83bd80b で完了 |
| AWS リソース | （変更なし） | S3 アセット差し替え + CloudFront invalidation のみ |

## 6. 視覚確認手順

1. CloudFront invalidation 完了を待つ（1〜2 分）
2. ブラウザで `https://duf2tc37sx7rn.cloudfront.net` を開く
3. Slack OAuth でログイン
4. DashboardHome の Token Monitor カードを確認:
   - ラベル「**最終チェック**」が表示されている
   - 値が当日（または直近）の TokenMonitor 実行時刻（2026-05-14T15:21:04Z 以降）に近い「○ 分前」表記
5. 修正前は「最終更新」が `2026-05-14T01:58:23Z`（古い completed）になっていたはずなので、差分が出る

## 7. ロールバック計画

万一表示崩れや型エラーで動かない場合:
- `types.ts` と `DashboardHome.tsx` を revert → `npm run build` → 再 sync → invalidation
- バックエンド API レスポンスは新フィールドを含み続けるが、frontend が古い型なら未使用フィールドとして無視される（後方互換）

## 8. 関連メモリ・ドキュメント

- memory: `feedback_frontend_deploy_separate_from_sam.md` (デプロイ手順)
- memory: `project_frontend_deploy_state.md` (Frontend SPA 全体の状態)
- `.steering/20260514-token-monitor-still-valid-emit/` (バックエンド先行作業)
- `docs/functional-design.md` (ダッシュボード仕様)
