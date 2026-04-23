# 設計: レビュー修正ループでの code_files 欠落バグの修正

## 実装アプローチ

### 方針: 独立生成フィールドを明示リストで保護する（最小差分）

レビュー修正前後で「保護対象フィールド」を明示し、修正レスポンスで上書きされないように引き継ぐ。現状の `code_files` に加え、将来同種のフィールドが追加される場合にもリスト追加だけで対応可能にする。

### 採用しない代替案と理由

| 代替案 | 不採用理由 |
|---|---|
| `current_deliverables.update(parsed)` で shallow merge | text 側の削除（例: 修正時に一部フィールドを落としたい場合）が表現できず、意図しない残存データを保持してしまうリスク |
| fix_prompt に `code_files` を含めて generator に再出力させる | generator のプロンプトテンプレート（`prompts/generator.md`）がコード生成を扱わない契約。責務分離を崩すことになり、code_files は専用プロンプトで生成する既存設計と矛盾する |
| `_run_review_loop` の戻り値を `(review_result, text_only_deliverables)` にして呼び出し側でマージ | 呼び出し側の責務が肥大化し、ループ内で code_files が参照されないのに持ち回る必要が出る。現場所で守る方が凝集度が高い |

## 変更するコンポーネント

### src/agent/orchestrator.py

#### 定数追加（モジュール冒頭付近、既存定数の近く）

```python
# レビュー修正ループで保護する独立生成フィールド。
# generator は text 成果物のみを返す契約のため、code_files 等は修正レスポンスに含まれない。
# 修正後に明示的に引き継がないと失われる（参照: .steering/20260423-review-loop-code-files-loss/）。
_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)
```

#### `_run_review_loop` 修正差分（`src/agent/orchestrator.py:737-742`）

```python
else:
    preserved = {
        k: current_deliverables[k]
        for k in _PRESERVED_DELIVERABLE_FIELDS
        if k in current_deliverables
    }
    current_deliverables = parsed
    current_deliverables.update(preserved)
    logger.info(
        "Deliverables updated by review fix",
        extra={
            "loop": loop,
            "issues_count": len(errors),
            "preserved_fields": list(preserved.keys()),
        },
    )
```

上書き順序に注意: `current_deliverables = parsed` の後に `.update(preserved)` を呼び、text 側に同名キーが偶然あっても保護対象を優先する。

## データ構造の変更

なし。`deliverables` dict の構造は既存のまま。

## 影響範囲の分析

### 直接影響

- **`_run_review_loop` の修正成功パスのみ挙動変更**
  - 修正解析失敗パス（734 行目の warning）は影響なし（`current_deliverables` 温存済み）
  - レビュー一発 passed / 上限到達で修正なし終了のパスも影響なし

### 間接影響

- `deliverables["code_files"]` の有無で分岐する `run()` 内の格納処理（545 行目）が、レビュー修正が走った場合でも True を維持
- DynamoDB `deliverables` テーブルの `storage` フィールドが `notion+github` で記録される
- Slack 完了通知の `github_url` が含まれる

### 影響を受けないもの

- feedback 処理（`feedback_processor.py` 経由）: レビュー修正ループを通らない
- iac_code / program_code が不在のワークフロー: `code_files` が元々存在しないため `preserved` が空 dict となり挙動不変
- Notion ブロック生成、品質メタデータ、Cloudflare 検知等の既存機能

## テスト設計

### 新規ユニットテスト: `tests/unit/agent/test_orchestrator.py`

1. **test_review_loop_preserves_code_files_on_fix_success**
   - `current_deliverables` に `code_files` と text 成果物を両方持たせる
   - `call_claude` モックでレビューが `passed=False, issues=[error]` を返し、次に fix が成功する text-only レスポンスを返す流れを構築
   - 戻り値の `final_deliverables["code_files"]` が元の値と等しいことを assert

2. **test_review_loop_no_code_files_when_absent**
   - `current_deliverables` に `code_files` を含めない入力
   - fix 成功時に `code_files` キーが結果に現れないことを assert（空 dict 引き継ぎの副作用確認）

3. **test_review_loop_preserves_code_files_across_multiple_fixes**
   - MAX_REVIEW_LOOPS に満たない範囲で、1 回目 fix が成功、2 回目も成功のケース
   - 最終 `code_files` が元と等しいことを assert（ループを跨いだ保護確認）

### 既存テストへの影響

- `test_orchestrator.py` のモック構造によっては、`_run_review_loop` を直接呼び出すテストが既にある可能性がある。Read して既存スタイルに合わせた形で新テストを追加する
- `call_claude` や `_parse_claude_response` のモック方法は既存テストに倣う

## ロールアウト手順

1. ローカルで `pytest tests/` を実行し既存テストがパスすることを確認
2. 修正 + 新規テスト実装後、再度 pytest で全件パスを確認
3. main ブランチに commit + push
4. ユーザー判断で `sam deploy` を実施
5. デプロイ後、Slack から "AWSのCloud Front" を再投入し、`catch-expander-code` リポジトリに新規ディレクトリが push されることを手動確認

## ロールバック方針

修正コミットを revert するのみで旧挙動に戻る（データ移行なし・設定変更なし）。

## 永続ドキュメントへの影響

- `docs/functional-design.md` / `docs/architecture.md`: レビュー修正ループの記述に「code_files は保護対象」である旨を追記するかは tasklist で判断。本修正は内部実装詳細のため、更新しない方向を第一候補とする
- `docs/development-guidelines.md`: 影響なし

## 未解決事項 / 確認事項

- 既存 `test_orchestrator.py` の構造（`_run_review_loop` を直接テストしているか、`run()` 経由か）は tasklist 実装段階で確認し、テスト追加方針を調整する
- `_PRESERVED_DELIVERABLE_FIELDS` の命名は orchestrator 内 private を明示したいため `_` プレフィックス採用。モジュール冒頭の既存定数命名と整合するか確認する
