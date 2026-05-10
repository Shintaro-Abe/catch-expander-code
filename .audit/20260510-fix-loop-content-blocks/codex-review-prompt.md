# Codex レビュー依頼: fix loop content_blocks 構造的保護

## 役割

あなたは Catch-Expander プロジェクトのシニアレビュアーです。本プロジェクトの fix loop で 2026-05-09 に発生した「Notion 本文 (content_blocks) 完全消失」インシデントを構造的に防ぐためのパッチをレビューしてください。

## 背景

### インシデント概要

- 2026-05-09 22:49 / 23:47 JST に Slack 投入された 2 件のトピックで、Notion 成果物が品質情報ブロックのみ表示され、記事本体 (content_blocks) が完全に欠落
- T1 (`10f16cf`) と Mermaid プロンプト (`29192ff`) を `git revert` (commit `99b619c`) で暫定復旧済み
- 本タスクは「**プロンプト変更が原因で fixer 応答が崩れても、Python コード層で content_blocks を保護する**」構造的保護の実装

### 真因 (パッチ前のコード)

`src/agent/orchestrator.py` の `_run_review_loop` 内 fix attempt 後の deliverables 置換ロジック:

```python
preserved = {k: current_deliverables[k] for k in _PRESERVED_DELIVERABLE_FIELDS if k in current_deliverables}
_accumulate_fixer_notes(accumulated_fixer_notes, parsed)
current_deliverables = parsed             # ← deliverables 全置換
current_deliverables.update(preserved)    # ← code_files のみ復元
```

`_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)` は **content_blocks を含まない**。fixer LLM が応答 JSON で content_blocks を omit すると消失する構造バグ。

### 過去の steering 履歴 (連続パッチ密集地点)

`_run_review_loop` 周辺は過去 5 件の steering で連続的にパッチされた高リスク領域:

1. `49d11a2` (M2): fix loop が deliverables を返すよう変更
2. `73458e3`: `_PRESERVED_DELIVERABLE_FIELDS` 導入 (code_files 保護)
3. `8c5b220`: fix_prompt スコープ制約 + accumulator
4. T1 関連 (revert 済み)

本パッチは **6 件目**。対症療法アンチパターンを避けるため、3 層代替案規律 (プロンプト層/パイプライン層/型層) を遵守:
- プロンプト層 (案 C): 却下 (LLM 確率挙動依存、`8c5b220` で経験則として導入済みでも 5/9 に崩れた)
- パイプライン層 (本案): 採用
- 型層 (案 D): 却下 (影響範囲過大)

層が異なるため対症療法アンチパターン非該当。

## レビュー対象

### 変更 1: ヘルパー関数追加 (src/agent/orchestrator.py)

`_PRESERVED_DELIVERABLE_FIELDS` 直後に以下を追加:

```python
def _classify_content_blocks_fallback_reason(parsed: dict) -> str | None:
    """fixer 応答の content_blocks が fallback すべき無効値か判定する。

    fixer LLM が content_blocks を omit / null / 空 list / 非 list で返した場合、
    Notion 成果物の本文が消失するインシデント (2026-05-09 観測) を構造的に防ぐため、
    fix loop の deliverables 置換ブロックで本判定を使う。プロンプト指示への依存ゼロ
    の決定論的処理として実装する。

    Returns:
        - "missing_key": parsed に content_blocks キーが存在しない
        - "none_value": parsed["content_blocks"] が None
        - "non_list": parsed["content_blocks"] が list 型ではない
        - "empty_list": parsed["content_blocks"] が空 list
        - None: parsed["content_blocks"] が valid な non-empty list (fallback 不要)
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

### 変更 2: fix loop 内置換ブロック改修 (src/agent/orchestrator.py)

`_run_review_loop` 内、fix attempt 後の else 経路:

```python
else:
    # content_blocks 構造的保護: deliverables 置換前に旧版をスナップショット。
    # fixer が content_blocks を omit / null / 空 list / 非 list で返した場合、
    # 旧版 (valid non-empty list) を引き継ぐことで Notion 本文消失を防ぐ。
    prev_content_blocks = current_deliverables.get("content_blocks")
    preserved = {
        k: current_deliverables[k] for k in _PRESERVED_DELIVERABLE_FIELDS if k in current_deliverables
    }
    # fixer notes は parsed の上書きで失われるため、置換前に accumulator へ退避する。
    _accumulate_fixer_notes(accumulated_fixer_notes, parsed)
    current_deliverables = parsed
    current_deliverables.update(preserved)

    fallback_reason = _classify_content_blocks_fallback_reason(parsed)
    if (
        fallback_reason is not None
        and isinstance(prev_content_blocks, list)
        and prev_content_blocks
    ):
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
            "content_blocks_fallback": fallback_reason,
        },
    )
```

### 変更 3: ユニットテスト 6 ケース追加 (tests/unit/agent/test_orchestrator.py)

`TestReviewLoop` クラス末尾に追加:

1. `test_fix_loop_preserves_content_blocks_when_fixer_omits_key` — content_blocks キーなし
2. `test_fix_loop_preserves_content_blocks_when_fixer_returns_none` — None
3. `test_fix_loop_preserves_content_blocks_when_fixer_returns_empty_list` — []
4. `test_fix_loop_preserves_content_blocks_when_fixer_returns_non_list` — 文字列
5. `test_fix_loop_uses_fixer_content_blocks_when_valid` — valid な list (fixer 版採用)
6. `test_fix_loop_logs_warning_on_fallback` — warning ログ検証

各テストは `@patch("orchestrator.call_codex")` (reviewer) と `@patch("orchestrator.call_claude")` (fixer) を併用。reviewer 応答は生 JSON、fixer 応答は `{"result": "..."}` 形式。

### 変更ファイル

```
src/agent/orchestrator.py             | +49 行
tests/unit/agent/test_orchestrator.py | +165 行
```

### 6 ケスト全件 PASS

```
$ pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop -k "fix_loop_" -v
6 passed
```

## レビュー観点

以下を **P1 (必修正) / P2 (推奨修正) / P3 (情報提供)** で分類してください:

### 必須観点

1. **構造的保護ロジックの正当性**:
   - `_classify_content_blocks_fallback_reason` の 4 つの判定 (missing_key/none_value/non_list/empty_list) は網羅的か
   - fix loop の置換ブロックで `prev_content_blocks` のスナップショットタイミングは正しいか (`current_deliverables = parsed` の前か)
   - fallback 条件 `isinstance(prev_content_blocks, list) and prev_content_blocks` で、旧版自身が無効値の場合に fallback しない判定は妥当か

2. **fix loop 本来機能の保持**:
   - fixer が valid な non-empty list を返した場合、修正版が確実に採用されるか (regression が起きないか)
   - `_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)` の既存契約が壊れていないか

3. **エッジケース見落とし**:
   - `parsed` 自体が dict でないケースは ?  (parsed_error 経路で除外されるべきだが念のため)
   - content_blocks の内部要素が malformed (例: `[None, None]`) のケースは ?
   - `isinstance(value, list)` は dict list comprehension などをすり抜けるか

4. **テストカバレッジ**:
   - 6 ケースで構造保護の各条件を網羅しているか
   - `caplog` の検証 (loop=0, reason="none_value", previous_blocks_count=2) は実装と整合するか
   - regression 防止テスト (case 5) は十分か

5. **観測性**:
   - `logger.warning` の構造化フィールド (loop / reason / previous_blocks_count) は CloudWatch Logs Insights 集計に十分か
   - `logger.info` の `content_blocks_fallback: None` (fallback なし) で silent success が観測可能か

6. **対症療法アンチパターン回避**:
   - 過去 5 件のパッチと層が異なるか (パイプライン層であるか)
   - `8c5b220` のプロンプト中心性を希釈していないか

### 任意観点

7. **ドキュメンテーション**: docstring / コメントは将来の保守者に十分な情報を提供しているか
8. **命名規約**: `_classify_content_blocks_fallback_reason` の関数名は一貫しているか
9. **追加検討**: events テーブルへの emit (本タスクでは意図的にスコープ外) を将来追加する場合の準備は適切か

## 出力形式

```
## P1 (必修正)
- [Title] 概要 (該当箇所: file:line)
  詳細: ...
  根拠: ...
  推奨修正: ...

## P2 (推奨修正)
- ...

## P3 (情報提供)
- ...

## 総合評価
- 構造保護ロジックの正当性: ✅/⚠️/❌
- fix loop 本来機能保持: ✅/⚠️/❌
- テストカバレッジ: ✅/⚠️/❌
- 対症療法アンチパターン回避: ✅/⚠️/❌

## 結論
- マージ可否: 可 / 条件付き可 (P1 対応後) / 不可
```
