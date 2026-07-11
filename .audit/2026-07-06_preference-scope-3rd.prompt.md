# Codex レビュー依頼 (Pass 3): preference-scope 収束確認

対象・対象外は Pass 2（.audit/2026-07-06_preference-scope-2nd.prompt.md）と同じ。

## Pass 2 指摘への是正内容

1. P1 `{"scope": None}` が汎用扱い → `_scope_of` を `"scope" not in pref` のみ汎用とし、
   明示 None は型破損（非適用）に変更
2. P2 list 要素の型・enum 未検証 → `_scope_of` で全要素 `str` かつ enum 内を検証、
   違反は None（非適用 + ラベル「不明」）。非 str / enum 外 / workflow 語彙混入のテスト追加
3. P3 apply 側の rows / prefs コンテナ型未検証 → `isinstance(list)` / `list[dict]` ガードを
   put_item 前に追加

## 確認してほしいこと

- Pass 2 の 3 件が解消されているか
- 是正で新たな問題が入っていないか（特に `_scope_of` の enum 検証が
  validate_scope 済みの正常データ・キー欠損の移行前レコードの挙動を変えていないか）

## 出力形式

P1 / P2 / P3 の順に `ファイル:行` と修正案。指摘ゼロならその旨を明記。
