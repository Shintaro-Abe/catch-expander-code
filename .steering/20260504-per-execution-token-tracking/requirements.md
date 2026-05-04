# 要求定義 — per-execution トークン追跡

## 目的

各実行のトークン使用量とコストをダッシュボードに表示する。

## ユーザーストーリー

- ダッシュボードの実行一覧画面で、各実行の入力トークン数・出力トークン数・合計トークン数・コスト（USD）を確認できる。
- 古い実行（token フィールドが存在しないもの）は、`execution_completed` イベントの payload からフォールバック補完して表示される。

## 完了条件

- `list_executions` API のレスポンスに `total_tokens_used` / `total_input_tokens` / `total_output_tokens` / `total_cost_usd` が含まれる。
- フロントエンドの実行一覧画面で各実行のトークン内訳とコストが表示される。
- `workflow-executions` テーブルに token フィールドが書き込まれている。
- 旧実行（フィールド欠損）でも `execution_completed` イベントから補完されてフロントエンドに表示される。

## 制約事項

- orchestrator の `_run_review_loop` シグネチャ・ロジック・戻り値は変更しない（`memory/project_review_loop_recurring_patch_site.md` 規律遵守）。
- token 集計は `run()` の finally ブロックで行い、メインフローのエラーハンドリングに影響しない。
- `total_cost_usd` は DynamoDB の `Decimal` 型で保存する。
