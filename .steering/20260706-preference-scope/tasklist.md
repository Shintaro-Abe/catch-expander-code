# タスクリスト：F8 学習済み好みの適用スコープ導入

> **進捗ステータス (2026-07-11 更新)**: Phase 1-6 実装済み・commit **e8b5469** に格納。
> Phase 7 Codex レビューゲート実行済み（Pass 1: 7 件 → Pass 2: 3 件 → Pass 3: 指摘ゼロで収束、
> `.audit/2026-07-06_preference-scope.md`）。Pass 1-2 の是正コードは**作業ツリーに未コミットで存在**
> （scope.py / feedback_processor.py / get_my_profile / migrate_preference_scopes.py + テスト）。
> **Phase 8: T8-1〜T8-3 完了（2026-07-11）。残りは T8-4/T8-5 実機検証のみ**（Slack トピック投入が必要）。
> 是正コードは 4fa1184 / b62a438 でコミット済み（intertwined 解消）。

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
- [x] T5-2 dry-run 実行 → 対照表をユーザーが目視確認（2026-07-11、4 件: 技術+code / 汎用×2 / research_report+comparison_table）

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

- [x] T8-1 `sam build` / `sam deploy` 完了（2026-07-11、ユーザー実行）。agent image は CI (build-agent.yml) が push 済みの b62a438 タグを AgentImageUri で指定。**検証**: stack UPDATE_COMPLETE + task definition `catch-expander-agent:15` が b62a438 image を参照
- [x] T8-2 frontend deploy 完了（2026-07-11、s3 sync + invalidation はユーザー実行）。**検証**: invalidation Completed + CloudFront が新バンドル index-crXKFnSx.js を配信
- [x] T8-3 移行スクリプト dry-run → 目視確認 → --apply 完了（2026-07-11、--apply はユーザー実行）。**検証**: DynamoDB get-item で 4 件全てに提案どおりの scope 付与・created_at 温存を確認。ロールバック用の旧値はセッションログに出力済み
- [x] T8-4 実機検証 **主要項目完了（2026-07-13, exec-20260713132408-fc99067c。prompt recorder の全 12 記録を get-subagent-io Lambda 経由で精査）**:
  - [x] **受け入れ条件 1（コード好みの調査レポート汚染防止）**: ③ text 生成プロンプト（research_report、61,053 字）に code スコープ好み #0（AWS CDK…）が**含まれない**ことを確認。reviewer_fix×2 でも同様に除外。※トピックが技術（spring boot）だったため deliverables 次元の除外で検証（カテゴリ次元の除外は時事トピック未実行のため実機未観測、test_scope でユニット担保）
  - [x] **受け入れ条件 2（汎用好みの注入）**: 同プロンプトに汎用 #1（初学者）/ #2（Markdown）が**含まれる**ことを確認。#3（research_report/comparison_table スコープ）も正しく含まれる
  - [x] **陽性ケース**: ③ code 生成プロンプト（program_code）に #0 が**含まれ**、#3 が**除外**されることを確認（deliverables フィルタの両方向動作）。researcher プロンプト 5 本は好み注入なし（設計どおり）
  - [ ] フィードバック送信 → Slack 応答にスコープラベル表示（受け入れ条件 5、フィードバック送信が必要）
  - [ ] /profile 画面でスコープバッジ表示（受け入れ条件 5、ユーザー目視待ち）
- [x] T8-5 検証結果の報告（2026-07-13、受け入れ基準ごとに根拠明示。未検証 3 項目を明記: カテゴリ次元の実機除外 / Slack スコープラベル / /profile バッジ目視）

## 承認チェックポイント一覧

1. requirements.md / design.md / tasklist.md の承認（今回一括作成のため 3 点まとめて）
2. T5-2 / T8-3: 移行分類結果の目視確認
3. T7-3: Codex レビュー実行の承認（pass ごと）
4. T8-1: build / deploy の承認
