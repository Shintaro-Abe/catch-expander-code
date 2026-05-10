# Codex レビュー依頼 (3 回目): fix loop content_blocks 構造的保護

## 役割

あなたは Catch-Expander プロジェクトのシニアレビュアーです。fix loop の content_blocks 構造的保護パッチに対する **3 回目のレビュー**を実施してください。`memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` の経験則に従い、2 回目の修正で剥がれた次層のミスがないか、新規 P1/P2 ゼロで収束判定可能かを確認してください。

## 過去レビューの経緯

### 1 回目レビュー → T-6 で対応済み
- P1: 非 dict ガード → `if not isinstance(parsed, dict)` 3 分岐展開
- P2-1: docstring 整合 → 「non-empty list」に下げ + スコープ明記
- P2-2: 観測フィールド分割 → `content_blocks_fallback_reason` / `content_blocks_fallback_applied`
- P2-3: non-dict テスト追加 → `test_fix_loop_keeps_previous_when_fixer_returns_non_dict`

### 2 回目レビュー → T-6b で対応済み

**P1 (必修正)**: parse_error 分岐 B の網羅根拠が pre-existing failure (`test_run_review_loop_keeps_previous_on_parse_error` の `call_codex` patch 忘れ) により崩れていた。

**対応**: 新規 `test_fix_loop_keeps_previous_on_parse_error_branch` を追加。`call_codex` + `call_claude` 両 patch で分岐 B を独立検証。既存テストには触らず (pre-existing failure のまま)。

**P2 (推奨修正)**: 新規 info ログ contract (`content_blocks_fallback_reason` / `content_blocks_fallback_applied`) が assert されていなかった。3 系統 (fallback 発動 / valid fixer / 旧版無効で諦め) の検証が必要。

**対応**:
1. `test_fix_loop_logs_warning_on_fallback`: info ログ assert 追加 (reason="none_value" / applied=True)
2. `test_fix_loop_uses_fixer_content_blocks_when_valid`: info ログ assert 追加 (reason=None / applied=False)
3. `test_fix_loop_does_not_apply_fallback_when_previous_also_invalid` 新規追加 (reason="none_value" / applied=False)

## レビュー対象 (3 回目)

### 変更 1: 新規テスト 1 (parse_error 分岐 B 網羅)

`tests/unit/agent/test_orchestrator.py` に以下を追加:

```python
@patch("orchestrator.call_claude")
@patch("orchestrator.call_codex")
def test_fix_loop_keeps_previous_on_parse_error_branch(self, mock_codex, mock_claude, caplog):
    """fixer 応答が parse_error の場合、旧版 deliverables が保持される (3 分岐の B 分岐網羅)。

    既存 test_run_review_loop_keeps_previous_on_parse_error は call_codex の patch 忘れで
    pre-existing failure になっているため、本 steering で分岐 B 専用の網羅テストを新規追加する。
    Codex レビュー (2 回目, P1) の指摘に対する構造保護。
    """
    import logging
    from orchestrator import Orchestrator

    issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
    mock_codex.side_effect = [
        json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
        json.dumps(
            {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
        ),
    ]
    mock_claude.return_value = json.dumps({"result": "this is not valid json at all"})

    orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
    original = {"content_blocks": [{"t": "original"}], "summary": "初版"}

    with caplog.at_level(logging.WARNING, logger="catch-expander-agent"):
        _, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

    assert final["content_blocks"] == [{"t": "original"}]
    assert final["summary"] == "初版"
    parse_err_records = [
        r for r in caplog.records
        if "Fix attempt produced unparseable response" in r.getMessage()
    ]
    assert len(parse_err_records) == 1
```

### 変更 2: 新規テスト 2 (旧版無効ケース)

```python
@patch("orchestrator.call_claude")
@patch("orchestrator.call_codex")
def test_fix_loop_does_not_apply_fallback_when_previous_also_invalid(
    self, mock_codex, mock_claude, caplog
):
    """旧版 content_blocks 自体が無効な場合、fallback は実施されない (info ログで applied=False)。

    fix loop の最初の iteration で generator 応答時点で既に content_blocks が空だった場合、
    fixer も content_blocks を omit すると、fallback 発動条件
    `isinstance(prev, list) and bool(prev)` が False で fallback されない。
    Codex レビュー (2 回目, P2) の info ログ contract 検証。
    """
    import logging
    from orchestrator import Orchestrator

    issue = {"item": "test", "severity": "error", "description": "x", "fix_instruction": "y"}
    mock_codex.side_effect = [
        json.dumps({"passed": False, "issues": [issue], "quality_metadata": {}}),
        json.dumps(
            {"passed": True, "issues": [], "quality_metadata": {"sources_verified": 1, "sources_unverified": 0}}
        ),
    ]
    mock_claude.return_value = json.dumps(
        {"result": json.dumps({"content_blocks": None, "summary": "fixed"})}
    )

    orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
    original = {"content_blocks": [], "summary": "初版"}

    with caplog.at_level(logging.INFO, logger="catch-expander-agent"):
        orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

    fallback_records = [
        r for r in caplog.records
        if "Fix loop fixer omitted/invalid content_blocks" in r.getMessage()
    ]
    assert len(fallback_records) == 0

    info_records = [
        r for r in caplog.records if "Deliverables updated by review fix" in r.getMessage()
    ]
    assert len(info_records) == 1
    assert info_records[0].content_blocks_fallback_reason == "none_value"
    assert info_records[0].content_blocks_fallback_applied is False
```

### 変更 3: 既存テスト 2 件への info ログ assert 追加

#### `test_fix_loop_uses_fixer_content_blocks_when_valid` (info ログ contract: valid fixer)

```python
with caplog.at_level(logging.INFO, logger="catch-expander-agent"):
    _, final = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

assert final["content_blocks"] == [{"t": "fixer-version"}]
info_records = [
    r for r in caplog.records if "Deliverables updated by review fix" in r.getMessage()
]
assert len(info_records) == 1
assert info_records[0].content_blocks_fallback_reason is None
assert info_records[0].content_blocks_fallback_applied is False
```

#### `test_fix_loop_logs_warning_on_fallback` (info ログ contract: fallback 発動)

```python
with caplog.at_level(logging.INFO, logger="catch-expander-agent"):
    orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

# 既存の warning assert ...

info_records = [
    r for r in caplog.records if "Deliverables updated by review fix" in r.getMessage()
]
assert len(info_records) == 1
assert info_records[0].content_blocks_fallback_reason == "none_value"
assert info_records[0].content_blocks_fallback_applied is True
```

### テスト 9 ケース全件 PASS

```
$ pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop -k "fix_loop_" -v
9 passed
```

### 3 分岐の網羅性 (本 steering テスト群で完結)

| 分岐 | テスト |
|---|---|
| A: 非 dict (JSON array/scalar) | `test_fix_loop_keeps_previous_when_fixer_returns_non_dict` |
| B: parse_error (raw_text) | `test_fix_loop_keeps_previous_on_parse_error_branch` (2 回目で追加) |
| C: 正常 + fallback 発動 | `test_fix_loop_logs_warning_on_fallback` + `_when_fixer_omits_key` 等 4 ケース |
| C: 正常 + valid fixer | `test_fix_loop_uses_fixer_content_blocks_when_valid` |
| C: 正常 + fallback 諦め | `test_fix_loop_does_not_apply_fallback_when_previous_also_invalid` (2 回目で追加) |

### 変更ファイル統計 (累計)

```
src/agent/orchestrator.py             | +71 行 / -1 行
tests/unit/agent/test_orchestrator.py | +314 行
```

## 3 回目レビュー観点

### 1. 2 回目指摘の解消確認
- P1 (parse_error 分岐 B 網羅): 新規テストで完全網羅できているか
- P2 (info ログ assert): 3 系統 (valid / 発動 / 諦め) を info ログで確実に lock できているか

### 2. 多層ミスの最終検出
2 回目の修正で剥がれた次層のミスがないか:
- **テスト独立性**: 新規 9 ケースが互いに干渉しないか (mock の取り回し、caplog の状態漏れ)
- **既存 pre-existing failure との切り分け**: `test_run_review_loop_keeps_previous_on_parse_error` を放置している方針が正当か
- **content_blocks_fallback_applied の bool 厳密性**: `is True` / `is False` で assert しているが、`True` 値以外の truthy/falsy 値が漏れる経路はないか
- **info ログの過剰検証**: `Deliverables updated by review fix` ログが他経路 (例: review pass の return 直前) で重複生成され、assert がブレる経路はないか

### 3. 構造保護の最終評価
- 5/9 インシデント (Notion 本文消失) を構造的に防ぐ要件 AC-1〜AC-3 の充足度
- 「条件付き fallback」の論理的網羅性
- pre-existing 既存テストの failure を本 steering スコープ外とする判断の妥当性

### 4. 収束判定の根拠
- 1 回目→2 回目で見つかった層: コードレベル (P1 非 dict ガード) → テストレベル (P1 parse_error 網羅崩れ)
- 3 回目で次層ミスが出るなら何があり得るか (例: ドキュメント / 実機 / observability 連携)
- 新規 P1/P2 ゼロで収束判定するか、4 回目を要求するか

## 出力形式

```
## P1 (必修正)
- ...

## P2 (推奨修正)
- ...

## P3 (情報提供)
- ...

## 2 回目指摘の解消状況
- P1 (parse_error 分岐 B 網羅): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2 (info ログ contract assert): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌

## 多層ミス検出
- 新規層の指摘あり / なし
- 内訳: ...

## 結論
- 収束判定: 収束 (新規 P1/P2 ゼロ) / 4 回目要 (新規指摘あり) / 不可
- マージ可否: 可 / 条件付き可 / 不可
```
