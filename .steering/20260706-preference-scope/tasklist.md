# タスクリスト：F8 学習済み好みの適用スコープ導入

> **進捗ステータス (2026-07-11 更新)**: Phase 1-6 実装済み・commit **e8b5469** に格納。
> Phase 7 Codex レビューゲート実行済み（Pass 1: 7 件 → Pass 2: 3 件 → Pass 3: 指摘ゼロで収束、
> `.audit/2026-07-06_preference-scope.md`）。Pass 1-2 の是正コードは**作業ツリーに未コミットで存在**
> （scope.py / feedback_processor.py / get_my_profile / migrate_preference_scopes.py + テスト）。
> **Phase 8（deploy + 移行スクリプト --apply + 実機検証）は未着手・ユーザー承認待ち。**
> 作業ツリーは Agent SDK 移行 steering と intertwined。

design.md 承認後に着手。各フェーズ末尾の品質チェックを通過してから次へ進む。

## Phase 1: 型層（スキーマ + 判定関数）

- [x] T1-1 `src/agent/feedback/scope.py` 新規作成（enum 定数 / `preference_applies` / `validate_scope` / `format_scope_label`）
- [x] T1-2 `tests/unit/agent/test_scope.py` 新規作成（30 テスト green）

**完了条件**: `pytest tests/unit/agent/test_scope.py` 全パス ✔

## Phase 2: 抽出側（feedback_processor）

- [x] T2-1 `_build_extraction_prompt` に scope 出力・enum 定義・逆マップ・既存スコープラベル併記・訂正対応を追加
- [x] T2-2 `process()` に `validate_scope()` 適用、`MAX_TOTAL_PREFERENCES = 20`、置換時 scope 上書き
- [x] T2-3 `feedback_received` payload に `new_general_count` / `new_scoped_count` 追加
- [x] T2-4 `slack_client.post_feedback_result` にスコープラベル + 訂正案内行
- [x] T2-5 単体テスト追加・更新（test_feedback_processor 38 / test_slack_client green）

**完了条件**: 対象テスト全パス ✔

## Phase 3: 適用側（orchestrator 段階的絞り込み）

- [x] T3-1 `profile_text` 一括構築を廃止し、① 汎用のみ / ② カテゴリ一致 + 成果物スコープ小節 / ③ 完全フィルタの段階別構築に変更
- [x] T3-2 ② の文言分岐、category 不明時の汎用縮退
- [x] T3-3 単体テスト追加（`TestRenderPrefsSections` 等、受け入れ条件 1 の再現テスト含む）

**完了条件**: `pytest tests/unit/agent/` パス（※ SDK 移行由来の test_orchestrator ハングは別 steering で対応中）

## Phase 4: 可視化（API + フロントエンド）

- [x] T4-1 `get_my_profile/app.py` `_serialize_learned_preferences` を `{text, scope}[]` に変更 + テスト
- [x] T4-2 `frontend/src/api/types.ts` に `LearnedPreference` 追加、`MyProfile` 変更
- [x] T4-3 `MyProfile.tsx` スコープバッジ表示（`ScopeBadges` / `SCOPE_DELIVERABLE_LABELS`）
- [x] T4-4 `npm run build`（tsc -b + vite build、2383 modules 変換成功、2026-07-11 検証）

## Phase 5: 移行スクリプト

- [x] T5-1 `scripts/migrate_preference_scopes.py` 作成（dry-run / --apply の 2 段）
- [ ] T5-2 dry-run 実行 → 対照表を**ユーザーが目視確認** — **未実施（Phase 8 の T8-3 で実施予定）**。※2026-07-11 時点のブロッカー: devcontainer リビルドで AWS 認証情報が消失（boto3 STS → NoCredentialsError、`aws` CLI 自体も未インストール）。dry-run 実行前にユーザーの再認証が必要

## Phase 6: ドキュメント

- [x] T6-1 `docs/glossary.md` に用語追加（適用スコープ / 汎用好み / 段階的絞り込み / 6 値 vs 7 値対応表）
- [x] T6-2 `docs/adr/0002-preference-scope-extraction-time-classification.md` 作成（status: accepted）
- [x] T6-3 `docs/functional-design.md` F8 節のデータモデル・適用フロー更新

## Phase 7: 品質ゲート（CLAUDE.md 遵守）

- [x] T7-1 ruff / `pytest tests/unit/agent/{test_scope,test_feedback_processor,test_slack_client}` 新規 failure ゼロ
- [x] T7-2 pre-commit-secret-scan → commit（e8b5469 に格納）
- [x] T7-3 git push → **Codex レビューゲート** 実行済み（`.audit/2026-07-06_preference-scope.prompt.md` ほか）
- [x] T7-4 Codex 指摘是正（Pass 1: 7 件 / Pass 2: 3 件 → Pass 3 指摘ゼロで収束）。**是正コードは作業ツリーに未コミット**（要 commit）

## Phase 8: デプロイ・実機検証（すべてユーザー明示承認後）

- [ ] T8-1 承認後 `sam build` / `sam deploy`（agent image は scripts/deploy-agent.sh 運用に従う。CI push との IMMUTABLE タグ衝突回避: CI 完了後に deploy）
- [ ] T8-2 frontend deploy（npm run build → s3 sync → CloudFront invalidation）※ backend より後
- [ ] T8-3 移行スクリプト dry-run → 目視確認 → --apply
- [ ] T8-4 実機検証（deploy-verify Skill + 以下）:
  - コードスコープ好みがある状態で時事トピックを実行 → prompt recorder / CloudWatch Logs で ③ 生成プロンプトに当該好みが**ない**ことを確認（受け入れ条件 1）
  - 汎用好みが同プロンプトに**ある**ことを確認（受け入れ条件 2）
  - フィードバック送信 → Slack 応答にスコープラベル表示（受け入れ条件 5）
  - /profile 画面でスコープバッジ表示（受け入れ条件 5）
- [ ] T8-5 検証結果の報告（受け入れ基準ごとに根拠を明示、未検証項目は「verified」と呼ばない）

## 承認チェックポイント一覧

1. requirements.md / design.md / tasklist.md の承認（今回一括作成のため 3 点まとめて）
2. T5-2 / T8-3: 移行分類結果の目視確認
3. T7-3: Codex レビュー実行の承認（pass ごと）
4. T8-1: build / deploy の承認
