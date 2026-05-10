# Codex レビュー依頼 (2 回目): fix loop content_blocks 構造的保護

## 役割

あなたは Catch-Expander プロジェクトのシニアレビュアーです。fix loop の content_blocks 構造的保護パッチに対する **2 回目のレビュー**を実施してください。`memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` の経験則に従い、1 回目の修正で剥がれた次層のミスがないかを確認してください。

## 1 回目レビュー結果と対応

### P1 (必修正) — 1 件
- **非 dict fixer 応答で `AttributeError`**: `_parse_claude_response` が `[]`, `"text"`, `123` を返す経路で `parsed.get("parse_error")` が落ちる
- **対応**: `parsed = _parse_claude_response(fix_raw)` 直後に `isinstance(parsed, dict)` ガードを追加。3 分岐 (非 dict / parse_error / 正常) に展開。新ログメッセージ "Fix attempt produced non-dict response, keeping previous deliverables" を追加

### P2 (推奨修正) — 3 件
1. **docstring vs 実装乖離**: 「valid な non-empty list」→「non-empty list」に下げ、スコープ「完全消失防止」を明記
2. **観測フィールド名混同**: `content_blocks_fallback` を `content_blocks_fallback_reason` (判定結果) と `content_blocks_fallback_applied` (実適用) の 2 フィールドに分割
3. **non-dict parsed のテスト追加**: `test_fix_loop_keeps_previous_when_fixer_returns_non_dict` を追加 (`{"result": "[]"}` で非 dict ケース検証)

### P3 (情報提供) — 5 件 (positive)
- スナップショットタイミング ✅
- code_files preserve 契約 ✅
- regression 防止 ✅
- warning ログの集計十分性 ✅

## レビュー対象 (2 回目)

### 変更 1: ヘルパー関数の docstring (`src/agent/orchestrator.py:128-152`)

```python
def _classify_content_blocks_fallback_reason(parsed: dict) -> str | None:
    """fixer 応答の content_blocks が fallback すべき無効値か判定する。

    fixer LLM が content_blocks を omit / null / 空 list / 非 list で返した場合、
    Notion 成果物の本文が消失するインシデント (2026-05-09 観測) を構造的に防ぐため、
    fix loop の deliverables 置換ブロックで本判定を使う。プロンプト指示への依存ゼロ
    の決定論的処理として実装する。

    本タスクのスコープは「完全消失防止」に限定しており、要素レベルの検証
    (例: 各要素が Notion block dict として valid か) は行わない。要素 malformed
    (`[None, None]` 等) の検証は別タスクで扱う想定。

    Returns:
        - "missing_key": parsed に content_blocks キーが存在しない
        - "none_value": parsed["content_blocks"] が None
        - "non_list": parsed["content_blocks"] が list 型ではない
        - "empty_list": parsed["content_blocks"] が空 list
        - None: parsed["content_blocks"] が non-empty list (要素値は検証しない)
    """
    if "content_blocks" not in parsed:
        return "missing_key"
    value = parsed["content_blocks"]
    if value is None:
        return "none_value"
    if not isinstance(value, list):
        return "non_list"
    if len(value) == 0:
        return "empty_list"
    return None
```

### 変更 2: fix loop 内置換ブロック (`_run_review_loop` 内)

```python
parsed = _parse_claude_response(fix_raw)
# parse_error 経路 / parsed が dict でない経路 (JSON array/scalar) はいずれも
# current_deliverables を据え置き、旧版を保持する。
# `_parse_claude_response` は非 str text や JSON array/scalar を parse_error なしで
# そのまま返す経路があり、parsed.get(...) で AttributeError を起こすため明示ガードする。
if not isinstance(parsed, dict):
    logger.warning(
        "Fix attempt produced non-dict response, keeping previous deliverables",
        extra={"loop": loop, "issues_count": len(errors), "parsed_type": type(parsed).__name__},
    )
elif parsed.get("parse_error"):
    logger.warning(
        "Fix attempt produced unparseable response, keeping previous deliverables",
        extra={"loop": loop, "issues_count": len(errors)},
    )
else:
    # content_blocks 構造的保護: deliverables 置換前に旧版をスナップショット。
    # fixer が content_blocks を omit / null / 空 list / 非 list で返した場合、
    # 旧版 (non-empty list) を引き継ぐことで Notion 本文消失を防ぐ。
    # 注: 旧版 list の要素が malformed (例: [None, None]) かどうかは本タスクでは検証しない
    #     (スコープ「完全消失防止」)。要素レベル検証は別タスク扱い。
    prev_content_blocks = current_deliverables.get("content_blocks")
    preserved = {
        k: current_deliverables[k] for k in _PRESERVED_DELIVERABLE_FIELDS if k in current_deliverables
    }
    _accumulate_fixer_notes(accumulated_fixer_notes, parsed)
    current_deliverables = parsed
    current_deliverables.update(preserved)

    fallback_reason = _classify_content_blocks_fallback_reason(parsed)
    fallback_applied = (
        fallback_reason is not None
        and isinstance(prev_content_blocks, list)
        and bool(prev_content_blocks)
    )
    if fallback_applied:
        logger.warning(
            "Fix loop fixer omitted/invalid content_blocks; falling back to previous version",
            extra={
                "loop": loop,
                "reason": fallback_reason,
                "previous_blocks_count": len(prev_content_blocks),
            },
        )
        current_deliverables["content_blocks"] = prev_content_blocks

    logger.info(
        "Deliverables updated by review fix",
        extra={
            "loop": loop,
            "issues_count": len(errors),
            "preserved_fields": list(preserved.keys()),
            # 判定結果 (fixer 応答が無効値だったか) と実適用の有無を分けて記録する。
            # CloudWatch Logs Insights で集計時、fallback_reason だけ見ると
            # 「旧版自身も無効で fallback されなかったケース」が誤集計されるため。
            "content_blocks_fallback_reason": fallback_reason,
            "content_blocks_fallback_applied": fallback_applied,
        },
    )
```

### 変更 3: テスト 7 ケース (`tests/unit/agent/test_orchestrator.py` の TestReviewLoop 末尾)

1. `test_fix_loop_preserves_content_blocks_when_fixer_omits_key` — content_blocks キーなし → 旧版維持
2. `test_fix_loop_preserves_content_blocks_when_fixer_returns_none` — None → 旧版維持
3. `test_fix_loop_preserves_content_blocks_when_fixer_returns_empty_list` — `[]` → 旧版維持
4. `test_fix_loop_preserves_content_blocks_when_fixer_returns_non_list` — 文字列 → 旧版維持
5. `test_fix_loop_uses_fixer_content_blocks_when_valid` — valid な list (fixer 版採用)
6. `test_fix_loop_logs_warning_on_fallback` — warning ログ検証 (loop=0/reason="none_value"/previous_blocks_count=2)
7. **`test_fix_loop_keeps_previous_when_fixer_returns_non_dict`** (P2-3 で追加) — `{"result": "[]"}` で AttributeError なく旧版維持。"Fix attempt produced non-dict response" warning 検証 (parsed_type="list")

### テスト結果

```
$ pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop -k "fix_loop_" -v
7 passed
```

### 変更ファイル統計

```
src/agent/orchestrator.py             | +71 行 / -1 行
tests/unit/agent/test_orchestrator.py | +206 行
```

## 2 回目レビュー観点

### 1. 1 回目指摘の解消確認
- P1 (非 dict ガード) は 3 分岐展開で適切に解消されているか
- P2-1 (docstring 整合) はスコープ表記も含めて十分か
- P2-2 (フィールド分割) は CloudWatch Logs Insights での集計を妨げない命名か (`_reason` / `_applied`)
- P2-3 (non-dict テスト) はカバレッジ十分か

### 2. 多層ミスの検出 (`memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md`)
1 回目の修正で剥がれた次層のミスがないか。特に:
- **3 分岐の if/elif/else 構造**で、テストが 3 分岐すべてを網羅しているか
   - 分岐 A: 非 dict (新規 test_fix_loop_keeps_previous_when_fixer_returns_non_dict)
   - 分岐 B: parse_error (既存 test_run_review_loop_keeps_previous_on_parse_error)
   - 分岐 C: 正常 + fallback (test_fix_loop_logs_warning_on_fallback)
   - 分岐 C: 正常 + fallback なし (test_fix_loop_uses_fixer_content_blocks_when_valid)
- **新規 fallback_applied フィールドの assert** がテストに含まれているか (現状は warning ログ側のみ検証)
- **`bool(prev_content_blocks)` と `prev_content_blocks` 単独**の差異 (truthy 判定の意図性)
- **`_classify_content_blocks_fallback_reason` の docstring 例**を更新する余地 (例えば現在は `[{"t": "x"}]` のような例なし)

### 3. アンチパターン回避
- 「3 commit ルール」(`obsidian/2026-04-26_symptomatic-fix-anti-pattern.md`) で本パッチが対症療法の 6 件目に該当するリスクがあるが、層 (パイプライン) が異なるため非該当 — この論証は本パッチで十分にコードレベルで支えられているか

### 4. 観測性の最終確認
- `content_blocks_fallback_reason` と `content_blocks_fallback_applied` の組み合わせで、CloudWatch Logs Insights クエリは以下を表現できるか:
   - 「fallback が発動した回数」(applied=true)
   - 「fixer が content_blocks を出さなかったが旧版も無効で諦めた回数」(reason!=None & applied=false)
   - 「正常完了の回数」(reason=None)

## 出力形式

```
## P1 (必修正)
- ...

## P2 (推奨修正)
- ...

## P3 (情報提供)
- ...

## 1 回目指摘の解消状況
- P1 (非 dict ガード): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2-1 (docstring 整合): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2-2 (フィールド分割): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2-3 (テスト追加): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌

## 多層ミス検出
- 新規層の指摘あり / なし
- 内訳: ...

## 結論
- 収束判定: 収束 (新規 P1/P2 ゼロ) / 3 回目要 (新規指摘あり) / 不可
- マージ可否: 可 / 条件付き可 / 不可
```
