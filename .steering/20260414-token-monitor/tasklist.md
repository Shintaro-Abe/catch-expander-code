# タスクリスト: Claude OAuth トークン失効監視

## タスク

- [x] ステアリングドキュメント作成
- [x] `src/token_monitor/app.py` 実装
- [x] `src/token_monitor/requirements.txt` 作成
- [x] `tests/unit/token_monitor/test_app.py` ユニットテスト実装
- [x] `template.yaml` 更新（パラメータ + Lambda + EventBridge Schedule）
- [x] ユニットテスト実行・パスを確認（15テスト全パス）
- [x] cfn-lint で template.yaml を検証（エラーなし）
- [x] `sam deploy` でデプロイ（UPDATE_COMPLETE, SlackNotificationChannelId=C0ARFKTELS0）

## 完了条件

- ユニットテストが全てパスすること
- cfn-lint のエラーがないこと
- デプロイ後、EventBridge Schedule が有効化されていること
