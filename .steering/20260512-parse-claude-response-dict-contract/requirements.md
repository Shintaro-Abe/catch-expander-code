# 要求内容: text generator の workspace モード化 + 検証層 + 自動リトライ

## 起票背景

### 観測されたインシデント (2026-05-12) — 実エビデンス取得済み

#### 同トピック 3 回試行のサマリ

トピック「IT開発における原則:KISS,DRY,YAGNI,アジャイル開発,リーン思考」を 5/12 当日に 3 回投入:

| 時刻 (JST) | execution_id | 結果 | duration_ms | result_text 長 |
|---|---|---|---:|---:|
| 15:59 | `exec-20260512065943-b40a6186` | **completed** | (text gen 応答は code gen に PromptRecorder で上書きされ取得不能) | — |
| 17:16 | `exec-20260512081658-4ccedd19` | **failed** | 661,086 ms (11m1s) | 30,061 |
| 19:04 | `exec-20260512100453-f253b974` | **failed** | 678,299 ms (11m18s) | 30,067 |

成功率 1/3。失敗 2 件は **完全に同じ Part 分割パターン**で、確率的揺らぎではなく **特定条件 (大規模トピック + 長応答) で再現性のある LLM 挙動**。

#### 失敗事象の本質

19:04 失敗のスタックトレース:

```
File "/app/orchestrator.py", line 1069, in run
    deliverables.pop("code_files", None)
TypeError: pop expected at most 1 argument, got 2
```

`list.pop()` は引数 1 個 (index) しか取らない → `deliverables` が list 型だった。

#### 実 LLM 応答の構造 (19:04 / 17:16 共通パターン)

S3 PromptRecorder を Lambda 経由で取得した text generator 生応答:

**19:04 (30,067 文字) の冒頭**:

````
以下は続きのブロック配列です（Part 2）。前の出力の `content_blocks` 配列末尾に結合してください。

```json
[
    {"type": "bulleted_list_item", "bulleted_list_item": {...}},
    ...
]
```
````

**17:16 (30,061 文字) の冒頭**:

````
以下は残りの `content_blocks` です。前の出力の配列末尾に結合してください。

```json
[
    {"type": "bulleted_list_item", "bulleted_list_item": {...}},
    ...
]
```
````

#### LLM 挙動の本質

Claude Sonnet 4.6 (Maxプラン) は **大規模トピック + 長応答が必要なケース**で、独自に「Part 1 / Part 2 に分割応答」戦略を取る。`generator.md` プロンプトに Part 分割禁止指示がないため、LLM が「context window / 出力制約への配慮」として自発的に判断している。今回返ってきたのは Part 2 (続きの array) のみで、Part 1 (本体 dict) は別リクエストでないと得られない、というモデル側の認知。

`_parse_claude_response` の戦略 1 (```json コードブロック抽出) が JSON array を return → `deliverables` が list → line 1069 の `.pop("code_files", None)` で TypeError。

### 真因の構造的本質

#### 連続パッチ履歴 (時系列)

| # | 修正対象 | コミット | 何を補強したか |
|---|---|---|---|
| 1 | `_PRESERVED_DELIVERABLE_FIELDS` 導入 | `73458e3` | パース後の dict から `code_files` を保護 |
| 2 | `fix_prompt` スコープ制約 | `8c5b220` | プロンプトで成果物構造を強要 |
| 3 | fix loop 内 isinstance ガード | `81db3dd` (直前 steering) | パース結果が非 dict だった場合の防御 |

これらすべて **「LLM 出力 → 戦略 4 種で JSON パース → アプリ内部 dict」という壊れやすい境界**を維持したまま、その下流に防御を積み増している。memory `obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` の「3 commit ルール」アンチパターンの **延長線上**にいる状態。

#### 真の根本問題

> LLM (Claude Sonnet 4.6) が `30,000 文字の単一 dict 応答` という設計上の前提を **自発的に破って Part 分割応答を返す**。これは Catch-Expander が `Claude Code CLI の text 出力をパースしてアプリ内部 dict を得る` という境界設計を採用している限り、確率的に再発する。

→ 真の根本問題は **「LLM とアプリの境界が text/JSON ベースで脆い」**こと。

### 採用方針の決定経緯

ゼロベース見直しで 5 アプローチを比較検討:

| # | アプローチ | 内容 | 採否 |
|---|---|---|---|
| Z-1 | Anthropic API + Tool Use / response_format | API レベルで JSON Schema 強制、唯一プロンプト依存しない真の構造保証 | ❌ **コスト不採用** (Max プラン契約見直し含む大改修) |
| **Z-2** | **workspace モード統一 + 努力目標 + 検証層** | **LLM に Write tool で `deliverable.json` に書かせ、ファイルから dict を読む** | ✅ **採用** |
| Z-3 | generator を section 単位に分割 | workflow_plan の generate_steps 細分化 | ❌ orchestration 再設計が広範 |
| Z-4 | 案 B (型契約強制) + 自動リトライ | 連続パッチの延長 | ❌ 対症療法集大成 |
| Z-5 | 最小応急処置のみ | line 1054 だけにガード追加 | ❌ 根本解決にならない |

### Z-2 採用の整合性

**Z-2 もプロンプト制御に依存する**事実を明示する。前回 steering で案 C (プロンプト強化) を「LLM 確率挙動依存」で却下した論理を一貫させると、Z-2 にも同じ脆さが内包される。

ただし Z-2 は以下の点で案 C より優れる:

1. **LLM の得意動作様式に乗る**: 「ファイルに書く」は LLM 訓練分布の中心。「30,000 文字を 1 stdout JSON にまとめる」より確率的に安定
2. **code generation の実績**: orchestrator.py:1091 で稼働中。`Wrote: ...` パターンが相当数の execution で安定動作
3. **Part 分割の動機が物理的に消える**: stdout 出力制約への配慮が不要 → LLM が分割を選ぶ理由がない
4. **検証層で確定的に失敗検出**: 失敗時は post-hoc に確実に検知可能 (案 C は LLM 確率挙動を信じるしかない)

→ **「努力目標 (プロンプト依存) + 検証層 (確定的検出) + 自動リトライ (確率的成功率向上)」の三身一体**で実効的安定性を確保する設計。

## ユーザーストーリー

### US-1: ユーザーがどんなトピックでも安定して成果物を受け取れる

**As a** Catch-Expander の利用者として、
**I want** トピックの構造 (「箇条書き」「単一概念」「比較」等) に関わらず実行が成功するようにしたい、
**So that** 5/12 19:04 のような「特定のトピックでだけタスクが落ちる」現象が消える。

### US-2: 開発者が generator 出力の structural integrity を信頼できる

**As a** Catch-Expander の開発者として、
**I want** generator が返す deliverables が必ず dict であることを **構造的に保証**したい、
**So that** call site で `.pop()` / `.get()` 等の dict メソッドを呼ぶ際にガードを書く必要がなく、Part 分割応答等の LLM 自発判断による失敗パターンに振り回されない。

### US-3: 運用者が異常を Dashboard で観測できる

**As a** Catch-Expander の運用者として、
**I want** generator の生成内容 (deliverable.json) を Dashboard 上で確認できるようにしたい、
**So that** 「失敗した execution で LLM が何を書いたか」を事後分析でき、Z-2 化前の観測導線を失わない。

### US-4: 失敗時の自動リトライで成功率を底上げできる

**As a** Catch-Expander の利用者として、
**I want** generator が 1 回目で失敗 (ファイル不在 / 不正 JSON / 必須キー欠落) しても、自動でリトライして最終的に成果物を受け取りたい、
**So that** 努力目標の達成率が確率的でも、実効的な成功率を高めることができる。

## 受け入れ条件

### AC-1: text generator の workspace モード化 + プロンプト改修

- [ ] `src/agent/orchestrator.py` の text generator 呼び出し (line 1051 `call_claude(gen_prompt)`) を `call_claude_with_workspace` ベースに変更
- [ ] `prompts/generator.md` を以下のように改修:
  - 出力方式を「Write ツールで `deliverable.json` に書く」に変更
  - 必須キー (`content_blocks`, `summary`, `quality_metadata`) を明示
  - 禁止行為を明記: stdout への書き込み、Part 分割、複数ファイル、ファイル名変更
  - スキーマ例を本文に含める
- [ ] sandbox の作成・cleanup を既存 code generation と同じパターンで実装

### AC-2: 検証層 (確定的失敗検出)

generator 完了後、deliverable.json を読む前に以下を検証:

- [ ] **検出 A**: sandbox に `deliverable.json` が **存在しない** → `reason="file_missing"`
- [ ] **検出 B**: ファイルが存在するが `json.load()` が **JSONDecodeError** → `reason="invalid_json"`
- [ ] **検出 C**: パース結果が **dict でない** (list / scalar / None) → `reason="not_dict"`, `actual_type` 記録
- [ ] **検出 D**: 必須キー (`content_blocks`, `summary`, `quality_metadata`) のいずれかが **欠落** → `reason="missing_keys"`, `missing` 記録
- [ ] **検出 E**: `content_blocks` が **空 list / 非 list** → `reason="invalid_content_blocks"`

各検出ケースで `logger.warning` + 例外 `NonDictGeneratorResponse(reason, **extras)` を raise

### AC-3: 自動リトライ機構

- [ ] `MAX_GENERATOR_RETRIES = 2` (合計 3 回試行、code generation の `MAX_CLAUDE_RETRIES = 3` と整合)
- [ ] AC-2 の検出 A〜E のいずれかが発生したら、**generator を最大 2 回まで自動リトライ**
- [ ] リトライ間は exponential backoff (2^n 秒)
- [ ] 全試行失敗で `subagent_failed` event emit + 上位に raise (既存の Slack 失敗通知経路に流す)
- [ ] 各試行の失敗理由 (reason / actual_type / missing 等) を CloudWatch Logs に warning で記録

### AC-4: PromptRecorder 拡張

- [ ] `PromptRecorder.record()` API に `output_files: dict[str, str] | None = None` 引数を追加
- [ ] workspace モード呼び出し時は `deliverable.json` の中身を `output_files["deliverable.json"]` として保存
- [ ] S3 JSON スキーマに `output_files` フィールドを追加 (既存レコードとの後方互換維持: `output_files` 欠落時は省略可)
- [ ] **派生 2 統合**: text generator と code generator が同じキー `"generator/0"` で互いに上書きするバグを修正。それぞれ `"generator_text/0"` / `"generator_code/{code_type}"` に分離
- [ ] 既存 5 call site (`researcher` / `reviewer_eval` / `reviewer_fix`) の record 呼出は無変更 (output_files=None で互換維持)

### AC-5: Dashboard frontend 改修

- [ ] `frontend/src/api/types.ts` の `SubagentIORecord` に `output_files?: Record<string, string>` 追加
- [ ] `frontend/src/routes/ExecutionDetail.tsx` の generator 表示部分に「ファイル一覧 + 内容表示」UI 追加:
  - workspace モードの record は `output` (stdout の "Wrote: ..." 履歴) + `output_files["deliverable.json"]` の両方を `IOExpandable` で表示
  - 旧 record (output_files 無し) は従来通り `output` のみ表示 (後方互換)
- [ ] フロントエンドビルド + s3 sync + CloudFront invalidation (`memory/feedback_frontend_deploy_separate_from_sam.md` 準拠)
- [ ] 「派生 2 統合」で subagent 名が分離 (`generator_text` / `generator_code`) されるため、frontend `_SUBAGENT_ORDER` 相当の表示順設定を更新

### AC-6: ユニットテスト

- [ ] `_parse_claude_response` 系のテストは text generator では呼ばれなくなるため **regression のみ** (本 steering で関数本体は触らない)
- [ ] 新規テスト: `tests/unit/agent/test_orchestrator.py::TestTextGeneratorWorkspace` クラスに以下追加
  - `test_generator_succeeds_when_deliverable_json_is_valid_dict`: 正常路
  - `test_generator_retries_when_file_missing`: 検出 A → リトライ → 成功
  - `test_generator_retries_when_json_invalid`: 検出 B → リトライ → 成功
  - `test_generator_retries_when_not_dict`: 検出 C → リトライ → 成功
  - `test_generator_retries_when_missing_keys`: 検出 D → リトライ → 成功
  - `test_generator_fails_after_max_retries`: 全試行失敗 → 例外 raise + subagent_failed emit
  - `test_prompt_recorder_stores_output_files`: PromptRecorder が output_files を正しく保存
  - `test_prompt_recorder_separates_text_and_code_subagent_keys`: 派生 2 のキー分離が動作
- [ ] 既存 `TestReviewLoop::fix_loop_*` 9 ケース全件 pass (regression)
- [ ] Dashboard frontend 単体テスト (Vitest) を追加: output_files 含む / 含まない record 両対応の表示確認

### AC-7: Codex 連続レビュー

- [ ] スコープ広い (orchestrator + frontend + PromptRecorder) ため Codex 連続レビュー最低 2 回、新規 P1 ゼロまで継続
- [ ] レビュー対象: AC-1 ~ AC-5 のすべての変更、特に検証層の網羅性、リトライポリシーの妥当性、frontend 後方互換性
- [ ] `memory/feedback_codex_review_requires_approval.md` 準拠

### AC-8: 実機検証

- [ ] dev デプロイ後、5/12 19:04 と同じトピック「IT開発における原則:KISS,DRY,YAGNI,アジャイル開発,リーン思考」を Slack に投入
- [ ] 期待挙動:
  - LLM が `deliverable.json` を書く → 検証層 pass → 成果物 Notion に投稿成功
  - LLM が依然として Part 分割を試みた場合 (ファイル書かず stdout に出力) → 検出 A 発火 → 自動リトライで成功 or 全失敗で明示的に Slack 失敗通知
- [ ] Dashboard 画面で execution の subagent IO セクションを開き、generator の `output_files["deliverable.json"]` 表示を確認
- [ ] CloudWatch Logs で検証層 warning / リトライログを観測

### AC-9: ドキュメント整合

- [ ] `docs/functional-design.md` に「text generator の workspace モード方式」を新節として追加
- [ ] `docs/architecture.md` の関連箇所 (LLM 呼出パターン) を更新
- [ ] obsidian への学び記録: `2026-05-12_llm-part-split-and-workspace-migration.md` (任意)

## 制約事項

### スコープ制約

- 対象は `src/agent/orchestrator.py` の **text generator 呼び出し経路 (line 1045-1069 周辺) のみ**
- 以下 4 つの `_parse_claude_response` call site は **本 steering スコープ外** (応答サイズが小さく Part 分割発火条件に達しない):
  - line 907 (analysis)
  - line 923 (workflow_plan)
  - line 1378 (researcher)
  - line 1460 (reviewer_eval)
  - line 1523 (fixer)
- 上記 5 箇所の Z-2 化は将来の派生 steering で扱う

### 実装制約

- `call_claude_with_workspace` の既存実装は無変更で流用 (sandbox 作成、stdout 解析、files 収集ロジック)
- LLM 呼び出しモデルは `Claude Sonnet 4.6` (Maxプラン経由) 維持。Anthropic SDK 直接利用 (Z-1) は採用しない
- 努力目標である事実を docstring / docs に明示

### 非機能制約

- ECS タスク実行時間: 既存 code generation と同程度 (5〜10 分/呼び出し) を想定。リトライ発火時はさらに +5〜15 分の可能性
- 月額コスト: Max プラン定額のため変動なし。ただしレート制限到達リスクは要監視
- ロールバック容易性: feature flag (`WORKSPACE_TEXT_GEN=true|false`) で即時切戻し可能

### コスト制約

- 新規 AWS リソース追加なし
- 新規外部依存なし

## 非対応 / スコープ外 (代替案の却下根拠)

### 案 A: 各 call site に isinstance ガード追加 (パイプライン層)

- **却下**: 対症療法サイクル継続、関数の型契約嘘との非対称が残る

### 案 B: `_parse_claude_response` の型契約強制 (型層)

- **却下**: 「LLM text を後追いでパース」という壊れやすい境界を維持したまま下流防御を厚くするだけ。連続パッチアンチパターン (4 件目) になる

### 案 C 単独: プロンプト層強化のみ

- **却下** (単独では): LLM 確率挙動依存
- **但し統合**: Z-2 のプロンプト改修内に Part 分割禁止指示を含める形で本 steering 内に取り込み済み (AC-1)

### Z-1: Anthropic API + Tool Use / response_format

- **却下**: 真の構造保証だが、Max プラン CLI 前提の放棄が必要で **コスト面で不採用**

### Z-3: generator を section 単位に分割

- **却下**: workflow_plan / generator orchestration の再設計が広範。本 steering の最小コスト原則と乖離

### 他 4 call sites (analysis / workflow / researcher / reviewer) の Z-2 化

- **本 steering スコープ外**: 応答サイズが小さく Part 分割発火条件に達しない (実害低い)
- 将来同型インシデント観測時に派生 steering で検討

## 派生する別 steering 候補 (本 steering 外)

- **派生 4**: 残り 5 call sites の workspace モード化 (analysis / workflow / researcher / reviewer / fixer)
- **派生 5**: Anthropic API + Tool Use への完全移行 (Z-1、長期 ROADMAP として)

## 関連ナレッジ

### 必読メモリ

- `memory/project_parse_claude_response_type_contract_violation.md` — 同型バグの全カウント、本 steering の真因
- `memory/project_review_loop_recurring_patch_site.md` — 連続パッチ密集地点
- `memory/feedback_anti_pattern_discipline.md` — 3 層代替案規律
- `memory/feedback_test_patches_call_codex_and_claude.md` — テスト patch パターン (本 steering でも継承)
- `memory/feedback_codex_review_requires_approval.md` — Codex レビュー承認ルール
- `memory/feedback_codex_wsl2_sandbox.md` — WSL2 Codex 実行
- `memory/feedback_frontend_deploy_separate_from_sam.md` — frontend デプロイ手順
- `memory/feedback_deploy_after_ci_completion.md` — CI 完了後の sam deploy

### 必読 obsidian

- `obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` — 「3 commit ルール」アンチパターン
- `obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` — Codex 多層検出

### 関連 steering

- `.steering/20260423-review-loop-code-files-loss/` — `_PRESERVED_DELIVERABLE_FIELDS` 導入 (連続パッチ 1 件目)
- `.steering/20260429-deliverables-summary-divergence/` — fix_prompt 制約 (連続パッチ 2 件目)
- `.steering/20260425-code-gen-redesign-filesystem/` — workspace モードを code generation で導入した先行事例
- `.steering/20260510-fix-loop-content-blocks-preservation/` — fix loop 内ガード (連続パッチ 3 件目、直前完了)

### 実エビデンス

- `.audit/20260512-parse-response-evidence/llm-response-evidence.json` — 5/12 の 3 実行 (15:59 / 17:16 / 19:04) の generator 応答実物

## アンチパターン回避の自己点検

### 「3 commit ルール」(`obsidian/2026-04-26_symptomatic-fix-anti-pattern.md`) チェック

直前 3 件のパッチ (`73458e3` / `8c5b220` / `81db3dd`) はすべて **「LLM text → アプリパース」境界を維持した下流補強**。本 steering の案 B (型契約強制) は 4 件目の同類パッチに該当 → アンチパターン発動条件。

本 steering の Z-2 は:
- **境界設計そのものを変更** (text パース → ファイル経由)
- **3 層代替案規律** (`memory/feedback_anti_pattern_discipline.md`) 遵守:
  - プロンプト層 (案 C 単独): 却下、ただし Z-2 内に統合
  - パイプライン層 (案 A): 却下
  - 型層 (案 B): 却下、本 steering で「対症療法だった」と再評価
  - **境界設計の変更 (Z-2)**: 採用

「対症療法サイクルを能動的に終わらせる」位置づけ。

### 「努力目標」前提の明示責任

Z-2 は **LLM 確率挙動依存を完全には排除できない** (Z-1 だけが真の独立)。本 steering は以下を明示することで「楽観バイアス」を回避:

- requirements.md (本ファイル) で「努力目標 + 検証層 + リトライ」の三身一体を明文化
- 検証層 5 種 (AC-2) で post-hoc 確定的検出
- 自動リトライ (AC-3) で確率的成功率を底上げ
- 全試行失敗時の明示的 fail-fast で「黙って劣化版を届ける」を防ぐ

## 起票の合理性

| 観点 | 判定 |
|---|---|
| 単独 PR 化に適した粒度か | △ (orchestrator + frontend 両方に変更が及ぶが、Dashboard 観測性維持の責任が一体的) |
| 既存 steering を再開すべきか | ❌ 新規が清潔 (案 B → Z-2 へ方針転換した経緯を記録) |
| 必須か | ✅ production で実エラー観測済み (5/12 19:04)、再発リスクあり |
| アンチパターン非該当か | ✅ 境界設計変更で連続パッチサイクルを能動的に終わらせる |
| Dashboard 観測性は維持されるか | ✅ AC-5 で改修込み、改修なしの劣化版採用は能動的に却下 |
