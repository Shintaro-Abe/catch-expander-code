本セッションの 4 commit (a986422 / 20392bf / 27f236b / 83bd80b) を独立 second-opinion でレビューしてほしい。観点はセキュリティ/IAM/入力バリデーション、設計保守性・コード品質、dashboard/API/frontend 三層整合性の 3 つすべて。

## 主要な変更ファイル

- `src/token_monitor/handler.py` — still_valid 経路に `oauth_refresh_skipped` emit を追加
- `src/dashboard_api/get_token_monitor_health/app.py` — `skip_count` / `last_skip_at` / `last_check_at` 3 フィールド追加、`success_rate` は skipped 除外で後方互換
- `frontend/src/api/types.ts`、`frontend/src/routes/DashboardHome.tsx` — TokenMonitorHealth に 3 フィールド追加、UI で「最終更新 (last_refresh_at)」→「最終チェック (last_check_at)」差し替え
- `src/trigger/app.py` +254 行 — Slack interactive payload を Content-Type で分岐、`block_actions` で `views.open`、`view_submission` で UpdateItem の SET+REMOVE 同時実行で B 方式の個別フィールド削除、`learned_preferences` は UpdateExpression に含めず保持
- `template.yaml` — `TriggerFunction` Policies に `UserProfilesTable` の `GetItem` / `UpdateItem` を最小権限で追加
- `tests/unit/trigger/test_app.py` — `TestProfileModal` 7 ケース追加 (profile keyword 検出 / block_actions Modal 展開 / SET+REMOVE 検証 / learned_preferences 不可侵 / 500 字超バリデーション / payload 欠落 400 等)
- `tests/unit/token_monitor/test_app.py`、`tests/unit/dashboard_api/test_extended_metrics.py` — 前 2 commit のテスト
- `docs/{architecture,functional-design,credential-setup,product-requirements}.md` — 実装ステータス・API スキーマ・IAM 例の整合

## レビュー観点

### A. セキュリティ / IAM / 入力バリデーション

- Slack 署名検証が interactive payload にも適用されているか
- `PutItem` を入れず `GetItem` + `UpdateItem` だけにした判断の妥当性
- `UpdateExpression` の `ExpressionAttributeNames` で AttributeName injection リスク
- `learned_preferences` の不可侵が UpdateExpression レベルで本当に守られているか
- 500 字超バリデーションの `response_action:errors` 形式の正しさ
- `im:*` スコープを残したリスク

### B. 設計・保守性・コード品質

- `PROFILE_FIELDS` をモジュールトップに定数化したことの妥当性、将来追加時の影響
- `_handle_interactive` / `_handle_block_actions` / `_handle_view_submission` の責務分割
- `_update_user_profile_fields` の UpdateExpression 構築ロジックの読みやすさ・バグ余地
- dashboard_api 側で `last_check_at = max(ISO strings)` の妥当性 (タイムゾーン違い、ms 精度同点処理)
- `PROFILE_FIELDS` の構造 (4-tuple) を NamedTuple か dataclass にすべきか

### C. dashboard / API / frontend 三層整合性

- `oauth_refresh_skipped` emit → dashboard_api query → frontend `last_check_at` 表示、の型と意味の一貫性
- 既存 `last_refresh_at` を残したまま `last_check_at` を追加した後方互換判断
- `success_rate` が skipped 除外であることの prompt/コード/docs の一貫性

## 出力フォーマット

Markdown 1 本で:

```markdown
# Codex Review — 2026-05-16 session commits

## サマリ
- Critical: N / High: N / Medium: N / Low: N / Info: N
- 総合所感 (5 行以内)

## 指摘事項

### [severity] [path:line] 短いタイトル
- 問題: ...
- 影響: ...
- 推奨修正: ...

(以下、severity 順に列挙)
```

severity は **Critical / High / Medium / Low / Info** の 5 段階。
