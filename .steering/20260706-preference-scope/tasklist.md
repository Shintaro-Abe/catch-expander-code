# タスクリスト：F8 学習済み好みの適用スコープ導入

design.md 承認後に着手。各フェーズ末尾の品質チェックを通過してから次へ進む。

## Phase 1: 型層（スキーマ + 判定関数）

- [ ] T1-1 `src/agent/feedback/scope.py` 新規作成
  - enum 定数（`SCOPE_CATEGORIES` / `SCOPE_DELIVERABLES` / 展開マップ / 日本語ラベル）
  - `preference_applies(pref, category, deliverable_type)`（design §3.1）
  - `validate_scope(raw_scope, source_category, source_deliverable_types)`（design §3.4 非対称フォールバック）
  - `format_scope_label(scope)`（design §3.5）
- [ ] T1-2 `tests/unit/agent/test_scope.py` 新規作成（design §5 の該当ケース）

**完了条件**: `pytest tests/unit/agent/test_scope.py` 全パス

## Phase 2: 抽出側（feedback_processor）

- [ ] T2-1 `_build_extraction_prompt` に scope 出力・enum 定義・元実行 deliverable_types 逆マップ・既存好みスコープラベル併記・訂正フィードバック対応を追加（design §3.3）
- [ ] T2-2 `process()` に `validate_scope()` 適用、`MAX_TOTAL_PREFERENCES = 20`、置換時 scope 上書き
- [ ] T2-3 `feedback_received` payload に `new_general_count` / `new_scoped_count` 追加（design §3.6）
- [ ] T2-4 `slack_client.post_feedback_result` にスコープラベル + 訂正案内行（design §3.5）
- [ ] T2-5 単体テスト追加・更新（test_feedback_processor / test_slack_client）

**完了条件**: 対象テスト全パス（pre-existing failure 26 件は対象外）

## Phase 3: 適用側（orchestrator 段階的絞り込み）

- [ ] T3-1 `profile_text` 一括構築を廃止し、① 汎用のみ / ② カテゴリ一致 + 成果物スコープ小節 / ③ 完全フィルタの段階別構築に変更（design §3.2）
- [ ] T3-2 ② の文言分岐（「反映してください」/「該当する成果物を選ぶ場合は考慮」）、category 不明時の汎用縮退
- [ ] T3-3 単体テスト追加（受け入れ条件 1 の再現テスト含む。call_codex / call_claude 両 patch）

**完了条件**: `pytest tests/unit/agent/` パス

## Phase 4: 可視化（API + フロントエンド）

- [ ] T4-1 `get_my_profile/app.py` `_serialize_learned_preferences` を `{text, scope}[]` に変更 + テスト
- [ ] T4-2 `frontend/src/api/types.ts` に `LearnedPreference` 追加、`MyProfile` 変更
- [ ] T4-3 `MyProfile.tsx` スコープバッジ表示
- [ ] T4-4 `npm run build` + フロント lint パス

## Phase 5: 移行スクリプト

- [ ] T5-1 `scripts/migrate_preference_scopes.py` 作成（dry-run / --apply の 2 段、design §3.8）
- [ ] T5-2 dry-run 実行 → 対照表を**ユーザーが目視確認**（承認チェックポイント。実行は deploy 後の Phase 8 でも可だが、分類ロジックは本 Phase で確定）

## Phase 6: ドキュメント

- [ ] T6-1 `docs/glossary.md` に用語追加: 適用スコープ (Preference Scope) / 汎用好み (General Preference) / 段階的絞り込み (Progressive Narrowing) / 成果物区分（6 値）と `deliverable_type`（7 値）の対応表
- [ ] T6-2 `docs/adr/0002-preference-scope-extraction-time-classification.md` 作成（採用: 抽出時 1 回分類 + 実行時決定的フィルタ / 不採用: 実行時 LLM 判定・自由記述条件・埋め込み検索、の理由）
- [ ] T6-3 `docs/functional-design.md` F8 節のデータモデル・適用フロー更新

## Phase 7: 品質ゲート（CLAUDE.md 遵守）

- [ ] T7-1 ruff / 型チェック / `pytest tests/unit` 全体（pre-existing 26 件を除き新規 failure ゼロ）
- [ ] T7-2 pre-commit-secret-scan Skill 実行 → commit（論理単位で分割）
- [ ] T7-3 git push → **Codex レビューゲート**: `.audit/2026-XX-XX_preference-scope.prompt.md` + 結果ファイル準備 → **ユーザー承認待ちで停止**
- [ ] T7-4 Codex 指摘の是正（外部仕様の実態を確認してから解釈する）。指摘ゼロ収束まで pass 継続は**都度ユーザー確認**

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
