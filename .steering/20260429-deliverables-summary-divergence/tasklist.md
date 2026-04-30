# タスクリスト: fix_prompt スコープ制約追記による サマリ ↔ code_files 乖離の最小コスト緩和

> 採用案: **案 E (プロンプト層スコープ制約追記)**
> 関連: `requirements.md` / `design.md`
> 過去 4 件の steering で連続パッチされた高リスク領域 (`_run_review_loop`) のため、変更は文字列追記のみに限定する。

## 進捗サマリ

- [x] T1: ベースライン確認 (現状のテスト pass 状況を記録)
- [x] T2: `src/agent/orchestrator.py` の `fix_prompt` にスコープ制約セクションを追記
- [x] T3: `tests/unit/agent/test_orchestrator.py` に新規テスト 2 ケースを追加 (Codex 連続レビューで 7 ケースに拡張)
- [x] T4: TestReviewLoop の単体テストを実行 (新規ケース pass + 既存回帰なし)
- [x] T5: 全体テストを実行 (orchestrator / trigger / token_monitor 全件 pass、計 241 件)
- [x] T6: ruff lint / format チェック
- [x] T7: `docs/functional-design.md` のレビューループ節を更新
- [x] T8: ステアリング 3 ファイル間の最終整合確認

**完了** 2026-04-29 (commit `8c5b220`) → 2026-04-30 02:28 JST 実機検証で 4 観測点全発火確認。Codex 独立レビュー 3 回で当初案より厚い実装に進化:
- T3: 2 ケース予定 → **7 ケース** (Codex 1〜3 回目対応で `_merge` → `_accumulate` 再設計、`isinstance` ガード、accumulator multi-fix 検証ケース追加)
- 補助ヘルパー新設: `_accumulate_fixer_notes` / `_apply_accumulated_fixer_notes` (`src/agent/orchestrator.py:32-75`)
- 詳細: `obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md`、`memory/project_review_loop_recurring_patch_site.md` (5 件目)

---

## T1: ベースライン確認

### 内容

実装に着手する前に現状の状態を記録し、回帰判定の基準点を明確にする。

### 手順

```bash
cd /workspaces/Catch-Expander
pytest tests/unit/agent/test_orchestrator.py -v --tb=short 2>&1 | tail -20
pytest tests/unit/ -v --tb=short 2>&1 | tail -10
```

### 完了条件

- [ ] `tests/unit/agent/test_orchestrator.py` の現状件数 (66 件想定) と pass 数を記録
- [ ] `tests/unit/` 全体の現状件数 (237 件想定) と pass 数を記録
- [ ] 既に failing しているテストがある場合はその件数を記録 (本タスクで増えていないことを確認するため)

### メモ

ベースラインで failing テストが存在する場合、その分は本タスクのスコープ外。回帰判定では「件数増加なし」で判定する。

---

## T2: `src/agent/orchestrator.py` の `fix_prompt` にスコープ制約セクションを追記

### 内容

`src/agent/orchestrator.py:820-826` の `fix_prompt` 文字列に、本ループの修正可能範囲を LLM に伝える制約セクションを追記する。`design.md:2.2` の変更後コードを参照。

### 変更内容

旧:

```python
fix_prompt = (
    f"{gen_prompt}\n\n"
    f"## 修正指示\n\n"
    f"以下のレビュー指摘に基づき、成果物を修正してください。\n\n"
    f"指摘事項:\n```json\n{fix_instructions}\n```\n\n"
    f"現在の成果物:\n```json\n{json.dumps(current_deliverables, ensure_ascii=False)}\n```"
)
```

新 (`### 本ループでの修正可能範囲` セクションを 3 行追記):

```python
fix_prompt = (
    f"{gen_prompt}\n\n"
    f"## 修正指示\n\n"
    f"以下のレビュー指摘に基づき、成果物を修正してください。\n\n"
    f"### 本ループでの修正可能範囲\n"
    f"- 修正できるのは text 成果物 (`content_blocks` / `summary`) のみ。"
    f"`code_files` (`*.tf`, `*.py`, README 等) は別パイプラインで独立生成されており本ループでは修正できません。\n"
    f"- コード関連指摘 (構文・API バージョン・リソース定義・README 整合性等) を受け取った場合、"
    f"`summary` および `content_blocks` に「コードを修正した」「filter ブロックを削除した」等の修正主張を**書かないでください**。"
    f"代わりに `quality_metadata.notes` (list[str]) に「コード関連指摘 N 件は本ループ未修正」として正直に記録してください。\n"
    f"- テキスト関連指摘 (本文表現・出典記述・構成変更) は従来通り反映してください。\n\n"
    f"指摘事項:\n```json\n{fix_instructions}\n```\n\n"
    f"現在の成果物:\n```json\n{json.dumps(current_deliverables, ensure_ascii=False)}\n```"
)
```

### 完了条件

- [ ] `fix_prompt` 文字列に `### 本ループでの修正可能範囲` セクションが含まれている
- [ ] セクション内に次の 3 つの bullet が含まれている: (a) 修正可能なのは text 成果物のみ、(b) コード関連指摘は summary に修正主張を書かず notes に記録、(c) テキスト関連指摘は従来通り反映
- [ ] `_run_review_loop` のシグネチャ・戻り値・呼び出しグラフ・他のロジックには触れていない
- [ ] `_PRESERVED_DELIVERABLE_FIELDS` には触れていない
- [ ] `prompts/generator.md` / `prompts/reviewer.md` には触れていない

### 関連ファイル

- 変更: `src/agent/orchestrator.py` (1 箇所)

---

## T3: `tests/unit/agent/test_orchestrator.py` に新規テスト 2 ケースを追加

### 内容

`TestReviewLoop` クラスの `test_review_loop_preserves_code_files_on_fix_success` (line 466 付近) の直後に、`design.md:2.3` の 2 ケースを追加する。

### ケース 1: `test_review_loop_code_issue_does_not_claim_fix_in_summary`

#### 検証ポイント

- [ ] `mock_claude.call_args_list[1]` (= 2 回目の call_claude 呼び出し = fix call) の prompt に次の文字列が含まれる:
  - `本ループでの修正可能範囲`
  - `code_files`
  - `quality_metadata.notes`
- [ ] LLM がスコープ制約を遵守したシナリオ (mock 応答) で、最終 `final_deliverables["summary"]` に「修正」「削除」が含まれない
- [ ] `final_deliverables["code_files"]` が元の値と等しい (preserve 確認)

### ケース 2: `test_review_loop_text_issue_updates_text_normally`

#### 検証ポイント

- [ ] テキスト関連指摘 (例: 数値修正) のシナリオで、最終 `final_deliverables["content_blocks"]` が修正版に更新される
- [ ] `final_deliverables["summary"]` に修正後の数値 (例: `$85`) が含まれる
- [ ] `final_deliverables["code_files"]` が元の値と等しい (preserve 確認)
- [ ] `result["passed"]` が `True` (3 回目の review が pass を返す mock 応答に基づく)

### 完了条件

- [ ] 2 ケースが `TestReviewLoop` クラス内に追加されている
- [ ] 既存テスト (line 314-560 の TestReviewLoop 既存ケース) のコードは変更していない
- [ ] テストファイルが ruff の `S` (security) ルール違反を起こしていない (テストでは S101/S105 は ignore 設定済み)

### 関連ファイル

- 変更: `tests/unit/agent/test_orchestrator.py`

---

## T4: TestReviewLoop の単体テスト実行

### 内容

新規追加したテストと既存 review loop テストの両方が pass することを確認する。

### 手順

```bash
cd /workspaces/Catch-Expander
pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop -v
```

### 完了条件

- [ ] 既存 TestReviewLoop ケースが全件 pass (T1 ベースラインから件数減なし)
- [ ] 新規 2 ケース (`test_review_loop_code_issue_does_not_claim_fix_in_summary` / `test_review_loop_text_issue_updates_text_normally`) が pass
- [ ] エラー / 例外なし

### 失敗時の対応

- 既存テストが落ちた場合 → T2 の文字列追記が `_run_review_loop` の他のロジックに影響していないか再確認 (本来は影響しない設計だが、誤って他行を変更していないか)
- 新規テストが落ちた場合 → mock 応答 / assertion を design.md に照らし直し、テスト側のバグか実装側のバグかを切り分け

---

## T5: 全体テスト実行 (回帰確認)

### 内容

orchestrator / trigger / token_monitor の全テストを実行し、本変更が他領域に影響を及ぼしていないことを確認する。

### 手順

```bash
cd /workspaces/Catch-Expander
pytest tests/unit/ -v --tb=short
```

### 完了条件

- [ ] `tests/unit/` 全体の pass 数が T1 ベースライン + 2 件 (新規) になっている
- [ ] 新たに failing するテストがない
- [ ] orchestrator: 66 + 2 = 68 件 pass
- [ ] trigger: 60 件 pass (本変更で影響なし想定)
- [ ] token_monitor: 既存件数 pass (本変更で影響なし想定)

---

## T6: ruff lint / format チェック

### 内容

変更したファイルが ruff のルール (`E`, `F`, `I`, `W`, `UP`, `B`, `SIM`, `S`) と format ルールに違反していないことを確認する。

### 手順

```bash
cd /workspaces/Catch-Expander
ruff check src/agent/orchestrator.py tests/unit/agent/test_orchestrator.py
ruff format --check src/agent/orchestrator.py tests/unit/agent/test_orchestrator.py
```

### 完了条件

- [ ] `ruff check` が違反 0 件で完了
- [ ] `ruff format --check` が「Would reformat: 0 files」で完了
- [ ] line-length 120 を超える行がない (fix_prompt の f-string 連結は各行 120 字以内に収める)

### 失敗時の対応

- line-length 違反 → f-string を `\n` 区切りで分割して 120 字以内に収める
- format 違反 → `ruff format` で自動修正可能なら適用、ロジック差分が出ないことを確認

---

## T7: `docs/functional-design.md` のレビューループ節を更新

### 内容

`docs/functional-design.md:495-508` の「レビューループ」節、line 508 の「**独立生成フィールドの保護**」段落の直後に、設計意図を残す段落を追加する。

### 追加する段落

```markdown
**修正可能範囲のスコープ宣言**: `_run_review_loop` の修正再生成プロンプト (`fix_prompt`) には「本ループで修正できるのは text 成果物 (`content_blocks` / `summary`) のみ。`code_files` は別パイプラインで独立生成されており本ループでは修正できない」とのスコープ制約を明示する。これにより、reviewer が出すコード関連指摘 (構文・API バージョン・README 整合性等) を受け取った場合に、ジェネレーターが summary に「コードを修正した」と主張する（実体は変わらない）乖離を緩和する。コード関連指摘は `quality_metadata.notes` に「本ループ未修正」として記録され、Notion 出力で利用者が認識できる状態を保つ。
```

### 完了条件

- [ ] 上記段落が `docs/functional-design.md:495-508` のレビューループ節内 (`**独立生成フィールドの保護**` の直後) に追加されている
- [ ] 他のセクション (Mermaid 図 / シーケンス図 / トークン消費見積もり等) には触れていない
- [ ] Markdown の見出しレベル / 太字記法が周辺と整合している

### 関連ファイル

- 変更: `docs/functional-design.md`

---

## T8: ステアリング 3 ファイル間の最終整合確認

### 内容

`requirements.md` / `design.md` / `tasklist.md` の 3 ファイル間で、案 E の方針・スコープ・受け入れ条件・実装手順が齟齬なく整合していることを確認する。

### 確認項目

- [ ] requirements.md AC1 (fix_prompt にスコープ制約を追記) と design.md 2.2 (具体的な追記文字列) と tasklist.md T2 (実装手順) の三者が一致
- [ ] requirements.md AC2 (テスト 2 ケース) と design.md 2.3 (テストコード) と tasklist.md T3 (テスト追加手順) の三者が一致
- [ ] requirements.md AC4 (docs/functional-design.md 更新) と design.md 2.4 (追加段落) と tasklist.md T7 (更新手順) の三者が一致
- [ ] requirements.md スコープ外 (案 A/C/D 却下根拠) と design.md 1.2 (アンチパターン回避) の方針が整合
- [ ] requirements.md 制約事項 (約 5 行) と design.md 7 オープン論点 1 (現実的に 6〜8 行) の差分について、tasklist.md ではいずれの判断で進めたかを反映済み

### 完了条件

- [ ] 上記 5 項目が確認できている
- [ ] 齟齬があれば該当ファイルを修正 (修正後に再度 T1 〜 T7 を実行する必要があるかを判断)

---

## 受け入れ条件サマリ (requirements.md AC との対応)

| requirements.md | 対応する tasklist.md タスク |
|---|---|
| AC1: fix_prompt にスコープ制約 (a)〜(e) 追記 | T2 |
| AC2: ユニットテスト 2 ケース追加 | T3, T4 |
| AC3: 既存 review loop テストの回帰なし | T4, T5 |
| AC4: docs/functional-design.md 更新 | T7 |
| AC5: prompts/generator.md / prompts/reviewer.md 不変 | T2 完了条件 |
| AC6: `_run_review_loop` シグネチャ・`_PRESERVED_DELIVERABLE_FIELDS` 不変 | T2 完了条件 |

すべての T が完了 = すべての AC が満たされる、の整合を保つ。

---

## 実装着手前の最終確認 (design.md オープン論点)

実装に着手する前に、design.md 7 のオープン論点 3 件の方針を確定させる:

- [x] **論点 1**: fix_prompt 追記文言は 6〜8 行で進める方針を確定 (実装は最終的に約 12 行 — Codex レビュー反映で長くなった)
- [x] **論点 2**: ケース 1 の summary 検証は `assert "修正" not in summary` 等のゆるい部分一致で進める方針を確定 (実装も該当)
- [x] **論点 3**: docs/functional-design.md 更新 (T7) を本タスクに含める方針を確定 (T7 で実施)

論点が確定したら T1 から順に着手する。
