# Codex レビュー依頼 (Pass 2): preference-scope 是正確認

## 対象

**working tree の未コミット差分のうち preference-scope 関連のみ**:
`src/agent/feedback/scope.py` / `src/agent/feedback/feedback_processor.py` /
`scripts/migrate_preference_scopes.py` / `src/agent/orchestrator.py` の
`_render_prefs_for_generation` 文言変更 / `tests/unit/agent/test_scope.py` /
`tests/unit/agent/test_feedback_processor.py`

**レビュー対象外**: orchestrator.py の Agent SDK 移行差分（claude_agent_sdk 化、
`_parse_claude_response` 厳密契約化）は別ワークストリーム（ADR 0001）のため今回は見ない。
ただし feedback_processor.py が新契約 `ClaudeResponseParseError` を正しく扱えているかは対象。

## Pass 1 指摘（.audit/2026-07-06_preference-scope.md）と是正内容

1. P1 `_parse_claude_response` 非 dict → SDK 厳密契約（dict or raise）へ移行し、
   `ClaudeResponseParseError` を catch して「好みなし」扱い
2. P1 型破損 scope が汎用（過剰注入）に倒れる → `_scope_of` が None を返し、
   is_general / category_matches / preference_applies / has_deliverable_constraint 全てで非適用。
   ラベルは「不明」。scope キー欠損（移行前レコード）のみ汎用扱いを維持
3. P2 replaces_index の str/bool → int 限定ガード（bool 除外）
4. P2 既存好み一覧の malformed 要素 → text を持つ dict のみに正規化してから
   プロンプト・merge 両方に使用
5. P2 text generator の union フィルタ文言 → 「[ ] の適用範囲に該当する成果物にのみ反映」
6. P2 移行スクリプトの text キー衝突 → index 単位の proposal + 突合検証 + 件数不一致で中断
7. P3 apply 側型検証 → isinstance(list) 必須 + 要素 str & enum 検証

## 確認してほしいこと

- 上記 7 件が実際に解消されているか（見かけの修正で穴が残っていないか）
- 是正によって新たに入った bug / 過剰に厳しくなった後方互換の破壊がないか
  （特に: scope キー欠損 = 汎用の維持、`{"scope": None}` の扱い、②ワークフロー設計での
  型破損 pref の挙動）
- テストが是正内容を実際に固定できているか

## 出力形式

P1 / P2 / P3 の順に `ファイル:行` と修正案。指摘ゼロならその旨を明記。
