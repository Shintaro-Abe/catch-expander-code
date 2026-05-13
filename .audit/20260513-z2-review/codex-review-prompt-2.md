# Codex レビュー依頼 (2 回目): text generator の workspace モード化 + 検証層 + 自動リトライ

## 役割

あなたは Catch-Expander プロジェクトのシニアレビュアーです。text generator workspace モード化パッチに対する **2 回目のレビュー**を実施してください。`memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` の経験則に従い、1 回目の修正で剥がれた **次層のミス**がないかを確認してください。

## 1 回目レビュー結果 (前提)

- **P1**: なし (主目的の TypeError 再発防止は達成)
- **P2**: 5 件
- **P3**: 5 件 (うち 1 件 = 古い docstring を本対応に含めた)
- **マージ可否**: 可 (P1 ゼロ)、ただし P2-1 / P2-2 は推奨修正

## 1 回目指摘への対応 (本レビュー対象)

### P2-1: subagent_completed の名前非対称
- **指摘**: subagent_started / subagent_failed は `"generator_text"` だが、subagent_completed のみ `"generator"` 固定 (orchestrator.py:1452)
- **対応**: `generator_subagent_name` 変数を使うよう変更。workspace 有効時は `"generator_text"`、旧経路では `"generator"` で emit。コメントで Codex 1 回目 P2-1 対応と明記
- **テスト**: `test_generator_emits_subagent_completed_with_generator_text_subagent_name` 追加 (PromptRecorder 経由で subagent 名整合を間接検証)

### P2-2: feature flag テストが実効性なし
- **指摘**: 環境変数判定式だけを確認、`run()` も分岐関数も呼んでいない、実装側で flag が無視されても通る (test_orchestrator.py:2243)
- **対応**: `_should_use_workspace_text_gen() -> bool` ヘルパー関数を抽出し、`run()` 内で使う。判定式を一元化
- **テスト 3 ケース**:
  - `test_should_use_workspace_text_gen_default_true`: env 未設定で True
  - `test_should_use_workspace_text_gen_false_when_env_false`: env=false で False
  - `test_should_use_workspace_text_gen_handles_uppercase`: FALSE/False/TRUE などケース変換

### P2-3: 空 deliverable.json が PromptRecorder に保存されない
- **指摘**: `if deliverable_content` の truthiness で空文字が落ちる (orchestrator.py:1752)
- **対応**: `if deliverable_content is not None` に変更。Codex 1 回目 P2-3 対応コメント明記
- **テスト**: `test_generator_records_empty_deliverable_as_failure_trace` 追加 (1 回目で空文字、2 回目で valid → 1 回目の record が `{"deliverable.json": ""}` を含む)

### P2-4: 巨大ファイルの上限がない
- **指摘**: text workspace は無制限に read/record/parse する、メモリ/S3/解析コスト膨張リスク (orchestrator.py:932)
- **対応**:
  - 新規定数 `_MAX_DELIVERABLE_BYTES = 1024 * 1024` (1MB) 追加 (Notion 100 ブロック上限 × 1〜2KB/ブロック を勘案)
  - 検証層に新ステップ F (file_too_large) 追加 (B JSON load より前で size 検査)
  - `NonDictGeneratorResponse` の reason に `"file_too_large"` 追加
- **テスト**: `test_generator_retries_when_file_too_large` 追加 (outcome.file_bytes=2MB で発火、リトライ後成功)

### P2-5: 検証層が型を見ていない
- **指摘**: `summary: []` や `quality_metadata: "..."` でも検証 pass する (orchestrator.py:207)
- **対応**:
  - 検証層に新ステップ G (invalid_summary: non-empty str チェック) + H (invalid_quality_metadata: dict チェック) 追加
  - reason に `"invalid_summary"` + `"invalid_quality_metadata"` 追加
- **テスト 2 ケース**:
  - `test_generator_retries_when_invalid_summary` (summary=[] でリトライ)
  - `test_generator_retries_when_invalid_quality_metadata` (quality_metadata=str でリトライ)

### P3 (古い docstring) 対応
- `call_claude_with_workspace` の docstring から「テキスト成果物のパスは call_claude のまま」を削除、`call_claude_with_text_workspace` への参照に置き換え

## 変更ファイル統計 (1 回目 + 2 回目累積)

```
src/agent/orchestrator.py                        | +503 行
src/agent/prompts/generator.md                   |  +69 行
src/observability/prompt_recorder.py             |  +17 行
template.yaml                                    |  +17 行
tests/unit/agent/test_orchestrator.py            | +357 行
tests/unit/observability/test_prompt_recorder.py |  +68 行
合計: +1,031 行 / -34 行
```

## テスト結果 (本対応後)

- `TestTextGeneratorWorkspace`: 8 → **15 ケース** 全件 pass
- `TestPromptRecorderWithOutputFiles`: 3 ケース全件 pass
- 直前 steering の `fix_loop_*` 9 ケース regression なし全件 pass
- 全 observability テスト 11 件 pass

## 2 回目レビュー観点

### 1. 1 回目指摘の解消確認

各 P2 指摘について以下を判定:

- **P2-1 (subagent_completed 非対称)**: 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- **P2-2 (feature flag テスト実効性)**: 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
  - `_should_use_workspace_text_gen` ヘルパー抽出と 3 ケーステストで「flag が無視されたら検出できる」状態か
- **P2-3 (空ファイル PromptRecorder)**: 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- **P2-4 (巨大ファイル上限)**: 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
  - `_MAX_DELIVERABLE_BYTES = 1MB` の数値根拠は妥当か
  - `outcome.get("file_bytes", len(deliverable_content.encode("utf-8")))` の fallback は堅牢か
- **P2-5 (型検証)**: 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
  - `invalid_summary` で「空文字も検出する」`not summary.strip()` の判定は適切か
  - `extras` の payload (`actual_type`, `is_empty`) が運用デバッグに十分か
- **P3 (docstring)**: 解消 ✅

### 2. 多層ミスの検出 (`memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` 経験則)

1 回目の修正で剥がれた次層のミスがないかを集中検査:

- **新規ヘルパー `_should_use_workspace_text_gen` の副作用**: 他箇所での同 env 参照と矛盾しないか、import パス問題、import 時点での評価タイミング
- **検証層への step F/G/H 追加によるエッジケース**:
  - file_bytes が 0 (空ファイル) のとき、F (file_too_large) は通過するが G (invalid_summary) で json.loads が空文字 → JSONDecodeError → B 経路に流れる挙動の妥当性
  - `_MAX_DELIVERABLE_BYTES` 直上の境界値 (1024*1024 ちょうど) で `>` 比較なので OK だが、`>=` の方が安全かどうか
  - `outcome.get("file_bytes", ...)` の fallback で `len(deliverable_content.encode("utf-8"))` を計算するが、deliverable_content が `None` ならこの式がそもそも実行されない (A 経路で先に弾かれる) - 整合確認
  - G (invalid_summary) の `not summary.strip()` で summary が `None` だった場合の挙動: `not isinstance(None, str)` で先に拒否されるので problem なし - これは確認価値あり
- **subagent_completed 名前変更の波及**:
  - dashboard の events 集計クエリで `subagent="generator"` で集計しているクエリは存在しないか
  - `_SUBAGENT_ORDER` (Lambda) で `"generator_text"` のソート順が正しいか (T-13 で対応予定)
  - 後方互換: feature flag false 時は引き続き `"generator"` で emit されることをコードと test で保証しているか
- **テスト粒度**:
  - 15 ケースの命名が一貫しているか (`test_generator_retries_when_X` vs `test_generator_records_X` vs `test_should_use_workspace_text_gen_X`)
  - mock pattern の重複 (各テストで `_valid_workspace_result()` を呼ぶ等) の整理余地
  - 直前 steering との `_classify_content_blocks_fallback_reason` 共通化候補について、本 steering tasklist に記録されているか

### 3. 直前 steering との関係 (再評価)

- 直前 steering (`81db3dd`) の `_classify_content_blocks_fallback_reason` を依然として残す判断が妥当か
- 本 steering の `_validate_deliverable_payload` の 8 種検出 (A-H) と直前 steering の 4 種 (missing_key/none_value/non_list/empty_list) は **責務が明確に分離**されているか
- 共通化候補としての記録 (将来 steering での扱い) で良いか

### 4. ステアリング遵守 (再確認)

requirements.md AC + design.md 確定事項のすべてが実装に反映されているか:

- AC-1 (workspace 移行 + プロンプト): 反映 ✅
- AC-2 (検証層 5 種 → 8 種に拡張): A〜H 全て検証可能
- AC-3 (自動リトライ): MAX_GENERATOR_RETRIES = 2 + exp backoff
- AC-4 (PromptRecorder 拡張 + キー分離): output_files Optional + generator_text/generator_code
- AC-6 (ユニットテスト): TestTextGeneratorWorkspace 15 ケース + TestPromptRecorderWithOutputFiles 3 ケース
- 確定事項 9 (feature flag デフォルト true): 反映 ✅
- 確定事項 10 (新規ラッパー分離): 反映 ✅
- 確定事項 11 (検出 E 本 steering 実装): 反映 ✅ → さらに F/G/H で拡張

### 5. 収束判定の根拠

- 1 回目 → 2 回目で見つかった層: コードレベル (P2-1〜P2-5) + 一部設計 (検証層厳密性)
- 2 回目で見つかる可能性のある層: テスト独立性、ドキュメント整合、メタ判断 (feature flag 削除条件 等)
- 新規 P1/P2 ゼロで収束判定するか、3 回目を要求するか

## 出力形式

```
## P1 (必修正)
- ...

## P2 (推奨修正)
- ...

## P3 (情報提供)
- ...

## 1 回目指摘の解消状況
- P2-1 (subagent_completed 非対称): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2-2 (feature flag テスト実効性): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2-3 (空ファイル PromptRecorder): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2-4 (巨大ファイル上限): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P2-5 (型検証): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌
- P3 (docstring): 解消 ✅

## 多層ミス検出
- 新規層の指摘あり / なし
- 内訳: ...

## ステアリング遵守 (AC + 確定事項)
- すべて反映: ✅ / 一部未反映: ⚠️ + 内訳

## 結論
- 収束判定: 収束 (新規 P1/P2 ゼロ) / 3 回目要 (新規指摘あり) / 不可
- マージ可否: 可 / 条件付き可 / 不可
```
