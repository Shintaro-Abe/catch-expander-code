# タスクリスト: レビュー修正ループでの code_files 欠落バグの修正

> **進捗マーク同期 2026-04-30**: 実装は当時完了済みだったが tasklist のチェックボックスが追従していなかったため、commit `73458e3` で実装済みの T1〜T6 を `[x]` 化（T7.1〜T7.2 はスコープ外明記のため `[ ]` のまま）

> **ステータス: 完了 (2026-04-23 push 済み・2026-04-26 状態更新)**
>
> - 実装 push 完了: `73658e3 fix: preserve code_files across review-fix loop`
> - `src/agent/orchestrator.py:24` に `_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)` 追加済み
> - `src/agent/orchestrator.py:810-822` 付近で `_run_review_loop` の修正適用パスにて preserved 抽出 → update 反映を実施
> - 個別タスクのチェックボックスは履歴保持のため未更新。**実装側のソースで完了状態を確認可能**

## 完了条件

- 全タスクが `[x]` になる
- `pytest tests/` で全件パス（既存 + 新規）
- main へのマージ可能な状態（commit 済み・動作確認済み）
- デプロイと実機再投入はユーザー判断で別途実施（本 tasklist では push まで）

---

## T1. 実装前調査 [x]

### T1.1 既存テスト構造の確認 [x]
- `tests/unit/agent/test_orchestrator.py` の以下の既存テストを Read し、テストスタイル（フィクスチャ、`mock_claude` のシグネチャ、`Orchestrator` インスタンス化方法）を把握する
  - `test_review_loop_fix_then_pass`（:322）
  - `test_run_review_loop_returns_fixed_deliverables_on_passed`（:369）
  - `test_run_review_loop_returns_fixed_deliverables_on_max_loop`（:413）
  - `test_run_review_loop_keeps_previous_on_parse_error`（:441）
- 新規テストを既存スタイルに整合させる

### T1.2 モジュール冒頭の既存定数スタイル確認 [x]
- `src/agent/orchestrator.py` 1–30 行目あたりの定数命名規則（例: `MAX_REVIEW_LOOPS = 2`）を確認
- `_PRESERVED_DELIVERABLE_FIELDS` の配置位置と命名が整合するか判断

---

## T2. 実装 [x]

### T2.1 定数 `_PRESERVED_DELIVERABLE_FIELDS` を追加 [x]
- `src/agent/orchestrator.py` のモジュール冒頭、既存の `MAX_REVIEW_LOOPS` 近辺に追加
- コメントで「なぜ保護が必要か」「generator 契約との関係」を明示

### T2.2 `_run_review_loop` の修正適用パスを書き換え [x]
- `src/agent/orchestrator.py:737-742` の `else` ブロックを設計書（design.md）通りに書き換え
  - `preserved` dict 抽出 → `current_deliverables = parsed` → `current_deliverables.update(preserved)`
  - logger.info の extra に `preserved_fields` を追加

### T2.3 自己チェック [x]
- 文法エラーがないことを確認
- 保護対象キーが元 dict に存在しないケース（`preserved = {}`）でも安全に動くか目視確認

---

## T3. 回帰テスト追加 [x]

### T3.1 `test_review_loop_preserves_code_files_on_fix_success` [x]
- 入力 `current_deliverables` に `code_files = {"files": {...}, "readme_content": "..."}` と text フィールドを両方含める
- `mock_claude` を設定: review 1 回目 `passed=False, issues=[{severity:"error"}]` → fix 1 回目 text-only 成功 → review 2 回目 `passed=True`
- 戻り値 `final_deliverables["code_files"]` が入力の code_files と等しいことを assert

### T3.2 `test_review_loop_no_code_files_when_absent` [x]
- 入力に `code_files` を含めない
- review → fix → review 同上フロー
- 戻り値 `final_deliverables` に `code_files` キーが存在しないことを assert

### T3.3 `test_review_loop_preserves_code_files_across_multiple_fixes` [x]
- review → fix 成功 → review → fix 成功 → 上限到達（loop=2 で Review loop limit reached）のシナリオ
- 各 fix 成功後も `code_files` が保持されていることを、最終戻り値で assert

### T3.4 既存テスト非回帰確認 [x]
- 上記 3 件以外のテストが影響を受けていないことを確認
- 特に `test_run_review_loop_returns_fixed_deliverables_on_passed` / `on_max_loop` の挙動変更がないこと

---

## T4. テスト実行 [x]

### T4.1 関連テストの単体実行 [x]
```bash
pytest tests/unit/agent/test_orchestrator.py -v
```
全件パスすることを確認

### T4.2 全テスト実行 [x]
```bash
pytest tests/
```
全件パスすることを確認（既存のテスト数と一致すること）

---

## T5. 永続ドキュメント確認 [x]

### T5.1 `docs/functional-design.md` のレビューループ記述確認 [x]
- レビュー修正ループに関する記述があれば、`code_files` が保護対象である旨と矛盾しないか確認
- 矛盾がなければ更新不要。矛盾があれば最小限の追記

### T5.2 `docs/architecture.md` の該当箇所確認 [x]
- 同上

---

## T6. コミット & push [x]

### T6.1 差分確認 [x]
```bash
git status
git diff src/agent/orchestrator.py tests/unit/agent/test_orchestrator.py
```

### T6.2 ステージング & コミット [x]
- 対象ファイルを明示的に `git add`（`-A` / `.` は使わない）
- コミットメッセージ例:
  ```
  fix: preserve code_files across review-fix loop

  generator-based review fix returns text deliverables only, so the
  previous overwrite silently dropped code_files produced by the separate
  iac_code/program_code pipeline. Protect via explicit field list so
  iac_code/program_code deliverables survive successful fix attempts.
  ```

### T6.3 main への push [x]
```bash
git push origin main
```

---

## T7. 本スコープ外（ユーザー判断で実施） [ ]

### T7.1 `sam deploy` でデプロイ
- ECS タスク定義を更新
- `sam deploy` コマンド実行

### T7.2 Slack 再投入と動作確認
- Slack から "AWSのCloud Front" トピックを再投入
- `catch-expander-code` リポジトリに新規ディレクトリが push されることを確認
- Notion 側にも `github_url` が埋め込まれることを確認

---

## メモ

- `_run_review_loop` を直接テストするスタイルは既存（:314〜:464）に揃っており、同じモック戦略を踏襲できる
- `MAX_REVIEW_LOOPS = 2` の実装仕様上、`for loop in range(MAX_REVIEW_LOOPS + 1)` = 0..2 の 3 回反復。T3.3 のシナリオ設計時に注意
