# タスクリスト — per-execution トークン追跡

> 関連: [`requirements.md`](./requirements.md)

## タスク

- [x] orchestrator `_cost_acc` に `total_input_tokens` / `total_output_tokens` を追加
- [x] `_accumulate_cost` が per-call token breakdown を返すよう変更
- [x] `dynamodb_client.update_execution_tokens()` を追加
- [x] `run()` finally ブロックで `update_execution_tokens` を呼び出す
- [x] `list_executions/_backfill_token_data` の実装と `Limit=1` バグ修正
- [x] frontend の TokenCell・format.ts を共通コンポーネント化
- [x] sam deploy + s3 sync でデプロイ完了
- [x] ダッシュボードで動作確認
