# Codex レビュー依頼: F8 学習済み好みの適用スコープ導入 (commit e8b5469)

## 対象

HEAD commit e8b5469（parent cc8cafb との diff）。
`git show e8b5469` で全変更を確認できる。設計意図は `.steering/20260706-preference-scope/`
（requirements.md / design.md）と `docs/adr/0002-preference-scope-extraction-time-classification.md` を参照。

## 変更概要

learned_preferences が全プロンプトに無条件注入されていた障害を、適用スコープ
（カテゴリ × 成果物区分）の抽出時付与 + 実行時決定的フィルタで修正:

- `src/agent/feedback/scope.py`（新規）: enum / `preference_applies()` / `validate_scope()` / `format_scope_label()`
- `src/agent/feedback/feedback_processor.py`: 抽出プロンプト scope 出力、非対称フォールバック、上限 10→20
- `src/agent/orchestrator.py`: 段階的絞り込み（①汎用のみ / ②汎用+カテゴリ一致+成果物スコープ緩和提示 / ③完全フィルタ）、profile JSON から learned_preferences 除外
- `src/agent/notify/slack_client.py`: スコープラベル + 訂正案内
- `src/dashboard_api/get_my_profile/app.py` + frontend: `{text, scope}[]` 形状変更 + バッジ表示
- `scripts/migrate_preference_scopes.py`: 既存データ一括再分類（dry-run → --apply）

## レビュー観点（優先順）

1. **フィルタの正しさ**: `preference_applies()` / 段階別レンダラーに、好みが「漏れて注入される」
   経路が残っていないか。特に profile_text_base 経由の素通り、②の緩和提示の範囲、
   text generator（複数 text 成果物を 1 呼び出し）への union フィルタの妥当性
2. **非対称フォールバックの穴**: `validate_scope()` で LLM 出力の型・値の組み合わせ
   （scope 非 dict / キー欠損 / 非 list / 混在 / 重複）が仕様（意図的空 = 汎用、検証失敗 = 縮退）
   どおり処理されるか。source_category="不明" 等の縮退先も含む
3. **後方互換**: scope 欠損レコード（移行前）が全経路（orchestrator / feedback_processor の
   既存好み一覧 / get_my_profile / Slack ラベル）でエラーにならず汎用扱いになるか
4. **型契約**: `_parse_claude_response` 由来の非 dict / 非 list 汚染への防御
   （このリポジトリは過去に同型バグ 5 件の実績あり）
5. **周辺整合**: DynamoDB put_item に渡る scope の型（list of str）、イベント payload 追加
   フィールドの集計 Lambda への影響、frontend 型変更の取りこぼし（learned_preferences を
   string[] として扱う残存コード）
6. **移行スクリプト**: 件数不一致・部分適用・text 重複キー衝突時の挙動

## 出力形式

`P1`（修正必須）/ `P2`(推奨) / `P3`(任意) の順に、`ファイル:行` と具体的な修正案を提示。
指摘ゼロならその旨を明記。
