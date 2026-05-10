# 要求内容: fix loop での content_blocks 構造的保護

## 起票背景

### 観測されたインシデント (2026-05-09)

5/9 22:49 と 23:47 JST に実行された 2 件の Slack トピック投入で、**Notion 成果物が品質情報ブロックのみ表示され、記事本体（content_blocks）が完全に欠落**するインシデントが発生。ユーザーが Notion 上で確認した内容:

```
■ 品質情報

検証ステータス: ✅ 出典検証済み: 15/53 件
情報の鮮度: 最新 2026-04-10 / 最古 2025-01-01
セルフレビュー結果: チェック項目 4/4 合格
注意事項:
  - priority 1の公式ドキュメントと、表に直接関係するキーバインド資料を中心に curl で...
  - 公式 VS Code ドキュメント内で Cmd+Esc...
```

記事の本文セクション（heading / paragraph / table / code 等の content_blocks）が一切無く、`_build_quality_metadata_block` で生成された 1 ブロックだけが Notion ページに残った。

### 直前の対応 (2026-05-10)

T1 (`10f16cf`) と Mermaid プロンプト (`29192ff`) を `git revert` (commit `99b619c3`) してロールバック完了。これは 5/9 のプロンプト変更が `8c5b220` (`fix_prompt` スコープ制約) のプロンプト中心性を希釈したことが**仮説的な引き金**だったため、実装を 5/9 直前 (`2d9ab71`) と同等に戻した。

ロールバック後、本番では再現性のあるテスト実行で content_blocks 喪失が発生しないことを確認すべき（Slack 投入で検証中、本 steering は検証成功前提で起票）。

### 真因の構造的本質 (5/9 ロールバックでは未解決)

ロールバックで T1 と Mermaid プロンプト由来の不安定要因は除去されたが、**根本的な構造バグは残っている**。

`src/agent/orchestrator.py` の `_run_review_loop` 内 fix loop ロジック:

```python
# orchestrator.py:1499-1517 (commit 99b619c 時点)
parsed = _parse_claude_response(fix_raw)
if parsed.get("parse_error"):
    # keep previous deliverables
else:
    preserved = {k: current_deliverables[k] for k in _PRESERVED_DELIVERABLE_FIELDS if k in current_deliverables}
    _accumulate_fixer_notes(accumulated_fixer_notes, parsed)
    current_deliverables = parsed             # ← deliverables 全置換
    current_deliverables.update(preserved)    # ← code_files のみ復元
```

定数 `_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)` (line 125, commit `73458e3` で導入) は **`content_blocks` を含まない**。fixer LLM (Claude Sonnet 4.6) が応答 JSON で content_blocks を omit すると:
1. `current_deliverables = parsed` で deliverables 全置換 → content_blocks キーも上書き対象
2. `current_deliverables.update(preserved)` は code_files だけを戻す
3. 結果: content_blocks が deliverables から**消失**
4. Storage 段階で `deliverables.get("content_blocks", [])` が空 list を返す
5. Notion ページに quality_block のみ残る

**この構造バグは commit `73458e3` (2026-04-23) から潜在し続けている**:
- `8c5b220` (2026-04-29) で fix_prompt にスコープ制約を追加 →「content_blocks/summary を主役」と明示することで fixer が content_blocks を omit する確率を経験的に下げていた
- しかし**プロンプトレベルの保護は LLM の確率的挙動に依存**しており、構造的保証ではない
- 5/9 のプロンプト変更（Mermaid + issue_category）でその経験則が崩れた可能性が高い

### 本 steering の目的

5/9 ロールバックで暫定復旧した状態を踏まえ、**今後どのようなプロンプト変更があっても content_blocks 喪失が再発しないよう、Python コード層で構造的保護を追加**する。

具体的には fix loop の deliverables 置換ロジックを改修し、fixer 応答が content_blocks を omit / 空 list で返した場合に **直前の content_blocks を自動で引き継ぐ** 条件付き fallback を実装する。

## ユーザーストーリー

### US-1: ユーザーが Notion 成果物の本文を必ず受け取れる

**As a** Catch-Expander の利用者として、
**I want** Slack で投入したトピックの成果物が、レビュー修正ループを何回経由しても Notion ページに必ず本文付きで届くようにしたい、
**So that** プロンプト変更や LLM の応答揺らぎに関係なく、品質情報だけのページに遭遇することがなくなる。

### US-2: 開発者が将来のプロンプト変更を安心して入れられる

**As a** Catch-Expander の開発者として、
**I want** reviewer.md / generator.md のプロンプト変更が原因で fixer の応答構造が崩れても、Python コードレベルで成果物が保護される構造にしたい、
**So that** プロンプト変更のたびに「fix loop での content_blocks 喪失」のリスクを再評価しなくてよい。

### US-3: 既存の fix loop 修正能力を保つ

**As a** Catch-Expander の利用者として、
**I want** reviewer がテキスト関連の指摘（「セクション 3 の表現を修正して」「出典 [3] と矛盾する数値を直して」等）を出した場合は、従来通り fixer の修正版 content_blocks が反映されるようにしたい、
**So that** 構造的保護を入れたことで「fix loop が機能しなくなる」regression が起きない。

### US-4: 運用者が異常を検知できる

**As a** Catch-Expander の運用者として、
**I want** fixer が content_blocks を omit / 空で返した場合、その事実を観測ログ / イベントに記録したい、
**So that** プロンプト不整合 or LLM の応答品質低下を早期に把握できる。

## 受け入れ条件

### AC-1: 構造的保護の実装

- [ ] `src/agent/orchestrator.py` の fix loop で、fixer 応答 (`parsed`) の content_blocks が以下のいずれかを満たす場合、**直前の `current_deliverables["content_blocks"]` を引き継ぐ**:
  - `parsed` に `content_blocks` キーが存在しない
  - `parsed["content_blocks"]` が `None`
  - `parsed["content_blocks"]` が `list` 型でない
  - `parsed["content_blocks"]` が空 list `[]`
- [ ] fixer が valid な non-empty list を返した場合は **従来通り fixer の修正版を採用**（fix loop 本来の機能を保持）
- [ ] code_files の preservation ロジック (`_PRESERVED_DELIVERABLE_FIELDS`) は変更しない（既存契約維持）
- [ ] 構造的保護は **plain Python の決定論的処理**で実装（LLM プロンプト指示への依存ゼロ）

### AC-2: 観測ログの追加

- [ ] content_blocks の fallback が発動したとき、`logger.warning` で以下を記録:
  - `loop` (現在の fix loop iteration 番号)
  - `reason` (`"missing_key"` / `"none_value"` / `"non_list"` / `"empty_list"` のいずれか)
  - `previous_blocks_count` (引き継いだ旧版 content_blocks の要素数)
- [ ] イベントテーブル (events) への emit は **本タスクでは追加しない** (将来別タスクで `content_blocks_fallback` イベントを定義する余地を残す)

### AC-3: ユニットテスト

- [ ] `tests/unit/agent/test_orchestrator.py` に以下のケースを追加:
  - `test_fix_loop_preserves_content_blocks_when_fixer_omits_key`: fixer 応答に content_blocks キーが無い場合、旧版が維持される
  - `test_fix_loop_preserves_content_blocks_when_fixer_returns_none`: content_blocks=None で旧版維持
  - `test_fix_loop_preserves_content_blocks_when_fixer_returns_empty_list`: content_blocks=[] で旧版維持
  - `test_fix_loop_preserves_content_blocks_when_fixer_returns_non_list`: content_blocks="string" / 42 等の非 list で旧版維持
  - `test_fix_loop_uses_fixer_content_blocks_when_valid`: fixer が valid non-empty list を返したら fixer 版を採用（既存挙動維持）
  - `test_fix_loop_logs_warning_on_fallback`: fallback 発動時に warning ログが出る
- [ ] 既存 TestReviewLoop 系テスト全件 pass (regression なし)
- [ ] pre-existing `call_codex` モック不整合は本 steering スコープ外として扱う

### AC-4: Codex 連続レビュー

- [ ] `_run_review_loop` 周辺は再発パッチ密集地点 (`memory/project_review_loop_recurring_patch_site.md`) のため、Codex 連続レビューを最低 2 回実施
- [ ] 1 回目で P1 指摘ゼロまたは全修正後 2 回目実施
- [ ] 2 回目で新規 P1/P2 ゼロなら収束判定（メモリ知見の停止条件）
- [ ] レビュー結果サマリを tasklist 末尾に記録

### AC-5: 実機検証

- [ ] dev デプロイ後、Slack で **fix loop が走るトピック**（reviewer が指摘を返しやすい複雑な技術トピック）を 1 件投入
- [ ] DynamoDB events テーブルから `Deliverables updated by review fix` ログが出ていることを確認
- [ ] Notion ページに **品質情報ブロック + 記事本体（content_blocks）両方が表示される**ことを目視確認
- [ ] 観測ログに content_blocks fallback warning が出ているか / 出ていないかを記録

### AC-6: ドキュメント整合

- [ ] `docs/functional-design.md` のレビュー機能の節に「fix loop での content_blocks 構造的保護」の 1 段落を追加
- [ ] `obsidian/` への学び記録（必要に応じて、5/9 インシデント + 5/10 構造修正の経緯）

## 制約事項

### スコープ制約

- 対象は `src/agent/orchestrator.py` の **fix loop 内 deliverables 置換ロジックのみ**
- `_PRESERVED_DELIVERABLE_FIELDS` 定数自体の値（`("code_files",)`）は変更しない（既存契約維持）
- `prompts/generator.md` / `prompts/reviewer.md` / `fix_prompt` 文字列は変更しない（プロンプト層は本タスクで触らない）

### 実装制約

- `dict.update()` の単純ロジックでは `content_blocks` を必ず旧版に戻すと **fix loop の text 修正能力を破壊する** regression を生むため、**条件付き fallback** で実装する（fixer が valid 値を返したら採用、無効値のときだけ旧版引き継ぎ）
- `_PRESERVED_DELIVERABLE_FIELDS` に `content_blocks` を単純追加する案 (Option A) は **却下**（前回 2026-05-09 セッションで分析、後述）

### 非機能制約

- ECS タスクの実行時間増加は無視できる範囲（型チェック数行のみ）
- 構造変更は `_run_review_loop` の 1 ブロックに局所化、シグネチャ・戻り値・呼出グラフは不変
- 既存テスト (`tests/unit/agent/test_orchestrator.py` 全件) を破壊しない

### コスト制約

- 新規 AWS リソースの追加なし
- 新規 Lambda 追加なし
- 新規外部依存なし

### スケジュール制約

- ロールバック (`99b619c3`) の Slack 検証成功を本 steering 着手の前提とする
- 検証で再発した場合は本 steering の前提が崩れるため、追加の真因調査を先行する

## 非対応 / スコープ外（代替案の却下根拠）

### 案 A: `_PRESERVED_DELIVERABLE_FIELDS = ("code_files", "content_blocks")` (単純追加)

```python
# 単純に preserved に content_blocks を追加すると...
preserved = {k: current_deliverables[k] for k in _PRESERVED_DELIVERABLE_FIELDS if k in current_deliverables}
current_deliverables = parsed
current_deliverables.update(preserved)  # ← 旧 content_blocks で fixer の修正版を上書きしてしまう
```

`dict.update(preserved)` は値の有無に関係なく問答無用で上書きするため、fixer が valid な修正版 content_blocks を返しても旧版に戻される。

**却下根拠**: fix loop の本来機能（reviewer 指摘に対する text 修正反映）が機能しなくなる新 regression を生む。前回 2026-05-09 セッションで分析済み。

### 案 C: プロンプト層強化（fix_prompt への追加指示）

`fix_prompt` に「content_blocks を必ず full payload で返してください」を追記する案。

**却下根拠**: プロンプト指示への依存は LLM の確率的挙動に依存し続けるため、`8c5b220` の経験則と同質の脆弱性が残る。本 steering の目的（構造的保護）と整合しない。プロンプト層の最適化は別 steering で並行検討する余地はある。

### 案 D: TypedDict / Pydantic で deliverables のスキーマ強制

deliverables 全体を TypedDict / Pydantic 型で固め、parser 段階で content_blocks 必須を強制する案。

**却下根拠**: 影響範囲が広く、orchestrator 全体の型注釈・parser・fixer 応答ハンドリングの大規模リファクタが必要。本タスクの「最小コストで構造的保護」原則と乖離。将来 deliverables のスキーマ統一が議題に上がったとき再検討する余地あり。

## 関連ナレッジ

### 必読メモリ

- `memory/project_t1_rollback_pending_2026-05-09.md` (2026-05-09 セッション末尾) — 本インシデントとロールバック判断の詳細経緯
- `memory/project_review_loop_content_blocks_loss.md` — 構造バグの 6 件目 patch site 候補としての位置づけ
- `memory/project_review_loop_recurring_patch_site.md` — `_run_review_loop` 周辺の連続パッチ履歴 (5 件)
- `memory/feedback_anti_pattern_discipline.md` — プロンプト / パイプライン / 型 3 層代替案規律
- `memory/feedback_codex_review_requires_approval.md` — Codex レビュー実施時の承認ルール

### 必読 obsidian

- `obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` — 「3 commit ルール」アンチパターン
- `obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` — Codex 連続レビューによる多層検出パターン

### 関連 steering

- `.steering/20260418-quality-fix/` (M2: commit `49d11a2`) — fix loop が deliverables を返すよう変更
- `.steering/20260423-review-loop-code-files-loss/` (commit `73458e3`) — `_PRESERVED_DELIVERABLE_FIELDS` 導入
- `.steering/20260429-deliverables-summary-divergence/` (commit `8c5b220`) — fix_prompt スコープ制約 + accumulator
- `.steering/20260509-dashboard-tier-2-completion/` — T1 (issue_category) 導入だったが本日 revert 済み

## 起票の合理性

| 観点 | 判定 |
|---|---|
| 単独 PR 化に適した粒度か | ✅ 1 ファイル (orchestrator.py) の fix loop ブロック改修 + テスト 6 ケース追加 |
| 既存 steering を再開すべきか | ❌ 過去 5 件の `_run_review_loop` パッチとは別の構造的保護のため新規が清潔 |
| 個人利用規模で価値があるか | ✅ 5/9 のような empty Notion 再発を構造的に防ぐため、運用上の信頼性向上効果は大きい |
| 必須か | ✅ 5/9 ロールバックは暫定対応であり、構造修正なしでは latent risk が残る |
| 6 件目の patch site とみなすか | ⚠️ 該当する。`obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` の「3 commit ルール」と整合確認しつつ、構造変更が「同質の対症療法」ではないことを明示 |

## アンチパターン回避の自己点検

5 件目 (commit `8c5b220`) の steering で適用された 3 層代替案規律 (`memory/feedback_anti_pattern_discipline.md`) を本 steering でも遵守:

- **プロンプト層**: 案 C で却下（既存の fix_prompt 中心性を希釈しない）
- **パイプライン層**: 採用（条件付き fallback で fix loop を強化）
- **型層**: 案 D で却下（影響範囲過大）

層が **異なる** ため、5 件目までと同質の連続パッチではない。「対症療法アンチパターン」のチェックリスト「同じ層に 3 回目を入れる前に止まる」には該当しない。
