# Codex レビュー結果: F8 学習済み好みの適用スコープ導入 (commit e8b5469)

prompt: `.audit/2026-07-06_preference-scope.prompt.md`

## Pass 1

初回実行 (19:11 UTC) は 401（refresh token already used、ECS ローテーション消費済み）で失敗。
`aws login` 後に Secrets Manager `catch-expander/codex-auth` から auth.json を復旧して再実行
（レビュー後に put-secret-value で書き戻し済み）。

**結果 (2026-07-06 19:2x UTC, gpt-5.5, tokens 86,848): P1×2 / P2×4 / P3×1**

### P1（修正必須）

1. `feedback_processor.py:77` — `_parse_claude_response()` が非 dict（JSON array / scalar）を
   parse_error なしで返す経路があるのに無条件 `parsed.get(...)`。Claude が `[]` を返すと
   feedback 処理全体が例外化。→ `isinstance(parsed, dict)` ガードで空抽出扱いに
2. `scope.py:56` — scope が dict でも `categories` / `deliverables` が非 list の場合に空リスト =
   汎用扱いとなり**過剰注入側**に倒れる（例: `{"deliverables": "code"}` が全成果物に漏れる）。
   → scope **欠損のみ**後方互換の汎用扱いとし、型不正スコープは「どこにも注入しない」に倒す

### P2（推奨）

3. `feedback_processor.py:261` — `replaces_index` が str/bool の場合に TypeError / 意図しない置換。
   → `isinstance(int) and not isinstance(bool)` ガード
4. `feedback_processor.py:164` — 既存好み一覧の `p['text']` が list[dict] 前提。string 要素 /
   malformed dict 混入でプロンプト構築が落ちる。→ orchestrator と同じ正規化を適用
5. `orchestrator.py:1294` — text generator の union フィルタ + 「必ず反映」が強すぎ、scope 付き
   好みが同一プロンプト内の別 text 成果物に効く余地。→ 「[ ] の適用範囲にのみ反映」へ文言強化
6. `migrate_preference_scopes.py:158` — `scope_by_text` の text キーが duplicate で後勝ち衝突。
   → index 単位の更新 + `updated == len(rows)` 検証で put_item 前に中断

### P3（任意）

7. `migrate_preference_scopes.py:164` — apply 側の型検証が弱い（str の文字単位 enum チェック /
   None で TypeError）。→ `isinstance(list)` 必須 + 要素 str & enum 検証

### 対応

全 7 件是正（P3 含む）。P1-1 は並行の Agent SDK 移行（ADR 0001）による
`_parse_claude_response` 厳密契約化（dict or raise `ClaudeResponseParseError`）と
統合する形で解消（catch → 好みなし扱い）。

## Pass 2

prompt: `.audit/2026-07-06_preference-scope-2nd.prompt.md`
**結果 (gpt-5.5, tokens 50,445): P1×1 / P2×1 / P3×1**（Pass 1 の 7 件中 6 件解消を確認、
型破損境界に残穴 2 + スクリプト防御 1）

1. P1 `scope.py:59` — `{"scope": None}`（明示 null）がキー欠損と同一扱いで汎用 = 過剰注入側に
   落ちる。→ `"scope" not in pref` のみ汎用、明示 None は型破損（非適用）へ
2. P2 `scope.py:64/174` — list 要素の型・enum 未検証。`categories: [123]` が
   `format_scope_label()` で TypeError（Codex が実行して実証）。→ `_scope_of` で全要素
   str + enum 内を検証、違反は None（非適用・ラベル「不明」）
3. P3 `migrate_preference_scopes.py:157/163` — rows / prefs のコンテナ型未検証。
   → `isinstance(list)` / `list[dict]` ガードを put_item 前に追加

### 対応

全 3 件是正。テスト 105 passed / ruff clean。

## Pass 3

prompt: `.audit/2026-07-06_preference-scope-3rd.prompt.md`（収束確認）
**結果 (gpt-5.5, tokens 80,441): P1 / P2 / P3 すべて指摘ゼロ。収束。**

Codex 側の検証: scope キー欠損 = 汎用維持・明示 None = 非適用・要素型 / enum 検証・
スクリプトのコンテナ型ガードをコードで確認し、対象テスト 68 + 9 passed を実行確認。
正常系（validate_scope 済みデータ / 移行前レコード）への影響なしと判定。

補足: test_orchestrator.py 全体の既存失敗は Agent SDK 移行（ADR 0001 ワークストリーム）
側の契約変更由来で、本レビュー範囲外。
