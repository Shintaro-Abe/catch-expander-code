# Codex レビュー依頼 (3 回目): text generator workspace モード化 + Dashboard 追従

## 役割

あなたは Catch-Expander プロジェクトのシニアレビュアーです。text generator workspace モード化パッチに対する **3 回目のレビュー**を実施してください。2 回目で「3 回目要」と判定された残課題への対応と、Dashboard 追従の完成度を最終評価してください。

## 過去 2 回のレビューの経緯

### 1 回目 (P1 ゼロ、P2 5 件、マージ可)

- P2-1: subagent_completed 非対称 → ✅ 解消 (1 回目で対応)
- P2-2: feature flag テスト実効性 → 部分解消 (helper 抽出 + 3 ケーステスト追加で対応)
- P2-3: 空ファイル PromptRecorder 保存 → ✅ 解消
- P2-4: 巨大ファイル上限 → 部分解消 (validation 層は OK だが read/record 前段が未対応 — 2 回目で指摘)
- P2-5: 型検証 → ✅ 解消
- P3 docstring → ✅ 解消

### 2 回目 (P1 ゼロ、P2 2 件、3 回目要)

#### P2 (2 回目で新規指摘)
1. **P2-4 部分解消**: validation 層では止めているが read/record の前段が止まっていない。default 引数評価問題も
2. **Dashboard 未追従**: T-12 (frontend) / T-13 (Lambda) が未実施

#### P3 (2 回目で新規指摘)
1. P2-1 解消だがテストは間接検証のみ。`run()` 統合テスト推奨
2. P2-2 も同様に helper 判定式の直接検証のみ。`WORKSPACE_TEXT_GEN=false` 統合テスト推奨
3. `_MAX_DELIVERABLE_BYTES` コメント「backoff せず」と実装 (backoff retry する) の不整合

## 2 回目指摘への対応 (本レビュー対象)

### P2-1 (read/record 上限) の補強

**対応**: `call_claude_with_text_workspace` で `stat()` を先行実行し、上限超過時は read せず preview (300 bytes) のみ返す。

```python
file_bytes = expected_path.stat().st_size
if file_bytes > _MAX_DELIVERABLE_BYTES:
    oversize = True
    # 検証層が file_too_large を検出できるよう、短い preview のみ読む
    with expected_path.open("r", encoding="utf-8", errors="replace") as fh:
        deliverable_content = fh.read(300)
    logger.warning("deliverable.json exceeds size limit; reading preview only", ...)
else:
    deliverable_content = expected_path.read_text(encoding="utf-8")
```

`outcome` に `oversize: bool` フィールドを追加。検証層は `outcome["oversize"]` を優先的に使い、`outcome.get("file_bytes", ...)` の default 引数評価問題を `if "file_bytes" in outcome` で回避。

**テスト**: `test_generator_retries_when_oversize_flag_set_by_workspace` + `test_validate_deliverable_payload_avoids_redundant_encoding` 追加。

### P2-2 (Dashboard 未追従) の対応

**T-12 (frontend) 完了**:
- `frontend/src/api/types.ts`: `SubagentIORecord.subagent` literal に `"generator_text"` / `"generator_code"` 追加 + `output_files?: Record<string, string>` 追加
- `frontend/src/routes/ExecutionDetail.tsx`: generators filter に 3 種類 (`generator` / `generator_text` / `generator_code`) を一括包含、種別表示 (テキスト / コード / 旧形式)、output_files があれば各ファイルを `IOExpandable` で展開
- `npm run build` 成功確認済み (型エラーなし)

**T-13 (Lambda) 完了**:
- `src/dashboard_api/get_subagent_io/app.py`: `_SUBAGENT_ORDER` に `generator_text=1` / `generator_code=2` / `generator=1` (後方互換) 追加

### P3-3 (コメント整合)

**対応**: `_MAX_DELIVERABLE_BYTES` のコメントから「retry も無意味なため backoff せずに失敗扱い」を削除、実装通り「他の validation failure と同じく exp backoff 付きで最大 MAX_GENERATOR_RETRIES 回まで再生成される」に修正。

### P3-1 / P3-2 (run() 統合テスト)

**対応**: 本 steering スコープでは `run()` 統合テストは追加せず、helper 単体テストで十分とする判断。理由:
- `run()` は依存が多く (`call_codex` / `call_claude` / `call_claude_with_text_workspace` / `call_claude_with_workspace` / DDB / Slack / Notion) mock コストが大きい
- T-17 (実機検証) で 5/12 と同じトピックを Slack 投入することで、`run()` 全体の実機統合検証は実施予定
- 本テストレベルでは現状 (helper 単体テスト) で十分な情報を得られる

→ 本 steering スコープ外として明示。

## 変更ファイル統計 (累計、1〜2 回目 + T-12/T-13)

```
src/agent/orchestrator.py                        | +538 行
src/agent/prompts/generator.md                   |  +69 行
src/observability/prompt_recorder.py             |  +17 行
src/dashboard_api/get_subagent_io/app.py         |   +9 行
template.yaml                                    |  +17 行
tests/unit/agent/test_orchestrator.py            | +432 行
tests/unit/observability/test_prompt_recorder.py |  +68 行
frontend/src/api/types.ts                        |  +16 行
frontend/src/routes/ExecutionDetail.tsx          |  +35 行
合計: +1,201 行 / -34 行
```

## テスト結果 (本対応後)

- `TestTextGeneratorWorkspace`: 15 → **18 ケース** 全件 pass
  - 新規追加: `test_generator_retries_when_oversize_flag_set_by_workspace`, `test_validate_deliverable_payload_avoids_redundant_encoding`
- `TestPromptRecorderWithOutputFiles`: 3 ケース全件 pass
- 直前 steering の `fix_loop_*` 9 ケース regression なし全件 pass
- 全 observability テスト 11 件 pass
- frontend `npm run build` 成功 (型エラーなし)

## 3 回目レビュー観点

### 1. 2 回目指摘の解消確認

- **P2-1 補強 (read/record 上限)**: 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
  - `stat()` 先行検出で read を防げているか
  - `outcome["oversize"]` フラグでの検証層分岐が clean か
  - `if "file_bytes" in outcome` で default 引数評価を回避できているか
  - preview 300 bytes の選択 (errors="replace" で UTF-8 不正対応) が妥当か
- **P2-2 Dashboard 追従**: 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
  - types.ts の literal 拡張は完全か (旧 record `generator` も含む)
  - ExecutionDetail.tsx の generators filter / 種別表示 / output_files 展開が clean か
  - `_SUBAGENT_ORDER` の順序設定 (generator_text=1, generator_code=2) が妥当か
  - 後方互換 (subagent="generator" の旧 record) が壊れていないか
- **P3-1 / P3-2 (run() 統合テスト)**: 本 steering スコープ外として明示する判断は妥当か
- **P3-3 (コメント整合)**: 解消 ✅

### 2. 多層ミスの最終検出 (`memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` 経験則)

3 回連続レビューで、まだ剥がれていない次層のミスがないかを集中検査:

- **frontend の Vitest テスト不足**: `ExecutionDetail.tsx` の表示分岐に対する単体テストがない。本 steering で追加すべきか、別 steering で扱うか
- **Lambda の S3 キー range 変更による既存読み出し影響**: 旧 `prompts/{eid}/generator_0.json` の record が引き続き読めるか (S3 オブジェクトは旧キーで存在し続けるため、`_SUBAGENT_ORDER` の `generator: 1` 後方互換で OK のはず)
- **subagent_completed event の dashboard 集計クエリ**: `subagent_completed` の subagent が `generator_text` になることで、dashboard の DDB クエリ (events テーブル) が `subagent=generator` 前提なら不整合になる可能性
- **deliverable.json 中身が JSON 部分も含めて preview のみの場合の挙動**: oversize 時に content="x"*300 が記録されると、後段で json.loads が失敗 (invalid_json) になり「file_too_large」と「invalid_json」のどちらが reported されるか — 検証層の順序で file_too_large 先勝ちなので OK
- **steering からの逸脱**: P3-1 / P3-2 を「スコープ外」とする判断の論証強度

### 3. ステアリング遵守 (最終確認)

- AC-1 (workspace 移行): ✅
- AC-2 (検証層 5 種 → 8 種に拡張): ✅ (file_too_large / invalid_summary / invalid_quality_metadata 追加)
- AC-3 (自動リトライ): ✅
- AC-4 (PromptRecorder 拡張 + キー分離): ✅
- **AC-5 (Dashboard frontend 改修)**: ✅ T-12/T-13 完了
- AC-6 (ユニットテスト): ✅ 18 + 3 = 21 ケース
- AC-7 (Codex 連続レビュー): 本 3 回目で完結予定
- 確定事項 9〜12: すべて反映
- T-1 〜 T-13 のうち完了タスク: T-1 〜 T-13 すべて完了 (本コードレビュー時点)
- 残: T-14 (docs) / T-15 (backend deploy) / T-16 (frontend deploy) / T-17 (実機検証) / T-18 (メモリ)

### 4. 収束判定の根拠

- 1 回目: コード品質指摘 (P2 5 件) → 全対応
- 2 回目: 補強指摘 (P2 2 件 read/record + Dashboard) → 全対応
- 3 回目で見つかる可能性のある層:
  - frontend テスト不足 (本 steering スコープ外候補)
  - 実機検証時のエッジケース (T-17 で補完)
  - メタ判断 (feature flag 削除時期、共通化候補の優先度)
- 新規 P1/P2 ゼロなら **収束判定** → T-14 以降の docs/deploy/実機検証フェーズへ

### 5. 「終結策」としての論証

本 steering は連続パッチサイクル (`73458e3` → `8c5b220` → `81db3dd` → 本件) の 4 件目だが、**境界設計変更層 (workspace モード統一)** で能動的にサイクル終結させる位置づけ。Codex 3 回目レビューでこの論証が **コードレベルで支えられている**かを最終評価してください:

- LLM の text 応答を後追いパースする経路は text generator から削除されている (feature flag false 時のみ旧経路)
- Part 分割応答が発生しても、ファイル経由のため struct 的に dict 取得が保証される (検証層 + リトライで品質確保)
- 他 5 call sites (analysis / workflow / researcher / reviewer / fixer) は本 steering スコープ外として残るが、それらは応答サイズが小さく Part 分割発火条件に達しない

## 出力形式

```
## P1 (必修正)
- ...

## P2 (推奨修正)
- ...

## P3 (情報提供)
- ...

## 2 回目指摘の解消状況
- P2-1 補強 (read/record 上限): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2-2 (Dashboard 追従): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P3-1 / P3-2 (run() 統合テスト判断): 妥当 / 不十分
- P3-3 (コメント整合): 解消 ✅

## 多層ミス検出
- 新規層の指摘あり / なし
- 内訳: ...

## ステアリング遵守 (AC + 確定事項)
- すべて反映 ✅ / 一部未反映 ⚠️ + 内訳

## 終結策としての論証
- コードレベルで支えられている ✅ / 不十分 ⚠️ + 内訳

## 結論
- 収束判定: 収束 (新規 P1/P2 ゼロ) / 4 回目要 (新規指摘あり) / 不可
- マージ可否: 可 / 条件付き可 / 不可
```
