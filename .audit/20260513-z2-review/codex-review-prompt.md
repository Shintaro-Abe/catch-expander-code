# Codex レビュー依頼 (1 回目): text generator の workspace モード化 + 検証層 + 自動リトライ

## 役割

あなたは Catch-Expander プロジェクトのシニアレビュアーです。2026-05-12 19:04 に観測された production TypeError (`list.pop("code_files", None)` → `pop expected at most 1 argument, got 2`) と、その背後にある LLM「Part 分割応答」パターンを構造的に防ぐためのパッチをレビューしてください。

## 背景

### インシデント (2026-05-12)

トピック「IT開発における原則:KISS,DRY,YAGNI,アジャイル開発,リーン思考」を 5/12 当日 3 回投入し、成功率 1/3。失敗 2 件は同じパターン:

- 17:16 失敗 (30,061 文字応答、duration 661 秒)
- 19:04 失敗 (30,067 文字応答、duration 678 秒、TypeError 観測)

LLM (Claude Sonnet 4.6) が `~30,000 文字超`の応答が必要な大規模トピックで、独自に「Part 1 / Part 2」分割応答戦略を発火し、result_text 冒頭に「以下は続きのブロック配列です（Part 2）」「配列末尾に結合してください」と書き、JSON コードブロック内に **トップレベル array** を返す挙動。

`_parse_claude_response` の戦略 1 (```json コードブロック抽出) がこの array を return → `deliverables` が list 型 → `deliverables.pop("code_files", None)` で破壊的失敗。

エビデンス保存: `.audit/20260512-parse-response-evidence/llm-response-evidence.json`

### 直前 steering との関係

直前 steering (`81db3dd` `.steering/20260510-fix-loop-content-blocks-preservation/`) では fix loop 内に `isinstance(parsed, dict)` ガードを追加した。これは Codex 3 回連続レビューで P1/P2 ゼロに収束したが、**今回のゼロベース見直しで「対症療法だった」と再評価**。

連続パッチ履歴:
1. `73458e3` (`_PRESERVED_DELIVERABLE_FIELDS` 導入)
2. `8c5b220` (`fix_prompt` スコープ制約)
3. `81db3dd` (fix loop 内 isinstance ガード) ← 直前 steering
4. **本 steering**: 「LLM text を後追いでパース」境界をやめて workspace モード化

→ 連続パッチサイクルを **「境界設計変更層」**で能動的に終結させる位置づけ。

### 採用方針 (Z-2 = workspace モード統一)

- **三身一体**: 努力目標 (プロンプト + Write tool) + 検証層 (5 種の確定的検出) + 自動リトライ (最大 2 回、exp backoff)
- **スコープ**: text generator のみ (他 5 call sites は本 steering スコープ外)
- **feature flag**: `WORKSPACE_TEXT_GEN` デフォルト `"true"`、`"false"` で旧経路に即時切り戻し可能
- **却下案**: Z-1 (Anthropic API + Tool Use) はコスト不採用、案 B (型契約強制) は対症療法サイクル継続

詳細: `.steering/20260512-parse-claude-response-dict-contract/requirements.md` / `design.md` / `tasklist.md`

## レビュー対象

### 変更ファイル統計

```
src/agent/orchestrator.py                        | +437 行
src/agent/prompts/generator.md                   |  +69 行
src/observability/prompt_recorder.py             |  +17 行
template.yaml                                    |  +17 行
tests/unit/agent/test_orchestrator.py            | +221 行
tests/unit/observability/test_prompt_recorder.py |  +68 行
合計: +829 行 / -31 行
```

### 1. `src/agent/prompts/generator.md` の Z-2 改修

出力方式を「stdout に JSON を返す」から「Write ツール経由で `deliverable.json` に書く」に変更。
- 必須キー (`content_blocks` / `summary` / `quality_metadata`) を明示
- 禁止事項: stdout 出力 / Part 分割 / ファイル名変更 / 複数ファイル / トップレベル array / code_files 出力
- 長い成果物の扱い: 複数 Write turn での append を許容
- quality_metadata の詳細はレビュアーが上書きする前提を明示

### 2. `orchestrator.py` の主要追加

#### (a) `call_claude_with_text_workspace` 関数 (新規)

```python
def call_claude_with_text_workspace(
    prompt: str,
    *,
    expected_filename: str = "deliverable.json",
    model: str = "sonnet",
    emitter: Any = None,
    cost_acc: dict | None = None,
) -> tuple[str, str | None, dict]:
    """Claude CLI に Write ツールを許可して sandbox に deliverable.json を書かせ、内容を返す。"""
```

- 既存 `call_claude_with_workspace` (code 用) と並列、関心の分離
- sandbox 作成 → claude CLI 実行 (cwd=sandbox, Write tool 許可) → expected_filename を読み返す
- 戻り値: `(raw_stdout, deliverable_content, outcome)`
  - outcome: `{"file_exists": bool, "file_bytes": int, "extra_files": list[str]}`
- Sonnet retry exhaust 時に Opus 4.7 advisor へエスカレーション (既存パターン踏襲)
- finally で sandbox cleanup + api_call_completed emit

#### (b) `NonDictGeneratorResponse` 例外型 (新規)

```python
class NonDictGeneratorResponse(RuntimeError):
    """text generator が workspace モードで valid な deliverable.json を生成しなかった場合の例外。"""
    def __init__(self, reason: str, **extras: Any) -> None:
```

reason の値: `"file_missing"` / `"invalid_json"` / `"not_dict"` / `"missing_keys"` / `"invalid_content_blocks"` / `"exception"`

#### (c) `_REQUIRED_DELIVERABLE_KEYS` 定数 + `_validate_deliverable_payload` 関数 (新規)

```python
_REQUIRED_DELIVERABLE_KEYS = ("content_blocks", "summary", "quality_metadata")

def _validate_deliverable_payload(
    deliverable_content: str | None,
    outcome: dict,
) -> tuple[dict | None, str | None, dict]:
    """検証順序: A (file_missing) → B (invalid_json) → C (not_dict) → D (missing_keys) → E (invalid_content_blocks)"""
```

#### (d) `MAX_GENERATOR_RETRIES = 2` 定数 + `_run_text_generator_with_retries` メソッド (新規)

```python
def _run_text_generator_with_retries(
    self,
    gen_prompt: str,
    execution_id: str,
    generator_start_ns: int,
) -> dict:
    """検証 + リトライ。全試行失敗で NonDictGeneratorResponse raise。"""
    for attempt in range(MAX_GENERATOR_RETRIES + 1):  # 0, 1, 2 で計 3 試行
        try:
            raw_stdout, deliverable_content, outcome = call_claude_with_text_workspace(...)
            if self._prompt_recorder is not None:
                self._prompt_recorder.record(
                    "generator_text", "0", gen_prompt, raw_stdout,
                    output_files={"deliverable.json": deliverable_content} if deliverable_content else None,
                )
            deliverables, reason, extras = _validate_deliverable_payload(deliverable_content, outcome)
            if reason is None:
                return deliverables
            # warning ログ + extras 記録
        except Exception as e:
            # exception 記録
        if attempt < MAX_GENERATOR_RETRIES:
            time.sleep(2 ** (attempt + 1))  # 2, 4 秒
    # 全試行失敗
    self._emitter.emit("subagent_failed", {...})
    raise NonDictGeneratorResponse(reason=last_reason or "unknown", **last_extras)
```

#### (e) text generator 経路の feature flag 分岐 (line 1316 周辺)

```python
workspace_text_gen = os.environ.get("WORKSPACE_TEXT_GEN", "true").lower() == "true"
generator_subagent_name = "generator_text" if workspace_text_gen else "generator"
self._emitter.emit("subagent_started", {"subagent": generator_subagent_name, ...})
if workspace_text_gen:
    deliverables = self._run_text_generator_with_retries(...)
else:
    # 旧経路 (即時切り戻し用)
    gen_raw = call_claude(gen_prompt, ...)
    ...
    deliverables = _parse_claude_response(gen_raw)
deliverables.pop("code_files", None)
```

#### (f) code generator の record キー変更 (派生 2 統合)

```python
self._prompt_recorder.record(
    "generator_code", code_type, prompt, raw_stdout,
    output_files=files if files else None,
)
```

旧 `"generator", "0"` (text generator と同じキーで上書きするバグ) を解消。

### 3. `src/observability/prompt_recorder.py` の改修

```python
def record(
    self,
    subagent: str,
    index: str,
    prompt: str,
    output: str,
    *,
    output_files: dict[str, str] | None = None,
) -> None:
    """workspace モード時の生成ファイル群を S3 JSON body に保存可能。output_files=None なら省略 (後方互換)。"""
```

### 4. `template.yaml` の改修

ECS Task Definition Environment に `WORKSPACE_TEXT_GEN=true` を追加。

### 5. テスト

#### `tests/unit/agent/test_orchestrator.py::TestTextGeneratorWorkspace` (新規 8 ケース、全 pass)

1. `test_generator_succeeds_when_deliverable_json_is_valid_dict`
2. `test_generator_retries_when_file_missing`
3. `test_generator_retries_when_json_invalid`
4. `test_generator_retries_when_not_dict`
5. `test_generator_retries_when_missing_keys`
6. `test_generator_retries_when_invalid_content_blocks`
7. `test_generator_fails_after_max_retries`
8. `test_generator_feature_flag_disables_workspace_mode`

#### `tests/unit/observability/test_prompt_recorder.py::TestPromptRecorderWithOutputFiles` (新規 3 ケース、全 pass)

- `test_record_stores_output_files`
- `test_record_omits_output_files_when_none` (後方互換)
- `test_record_separates_generator_text_and_code_keys`

#### Regression (38 件全件 pass)

- TestReviewLoop の fix_loop_* 9 ケース (直前 steering)
- TestParseClaudeResponse
- TestCallClaudeWithWorkspace (既存 code workspace)
- observability テスト全件

## レビュー観点

以下を **P1 (必修正) / P2 (推奨修正) / P3 (情報提供)** で分類してください。
各観点に **対応する steering 参照** (`AC-X` = requirements.md の受け入れ条件、`確定事項 X` = design.md 末尾の確定事項) を併記しています。Codex は steering との整合性を含めて評価してください。

### 1. 設計の妥当性

**Steering 参照**: requirements.md「採用方針の決定経緯」/「Z-2 採用の整合性」、design.md「設計方針」、AC-1 (workspace 移行)

- 「LLM text を後追いでパース」境界をやめて workspace モード化する判断は適切か
- 三身一体 (努力目標 + 検証層 + 自動リトライ) の責務分担は健全か
- text generator のみ Z-2 化、他 5 call sites を残す判断 (本 steering スコープ) は妥当か

### 2. `call_claude_with_text_workspace` の実装

**Steering 参照**: AC-1 / design.md Step 1 (ラッパー実装) / 確定事項 10 (新規ラッパーとして分離)

- sandbox 作成・cleanup の堅牢性 (finally 内の `shutil.rmtree(..., ignore_errors=True)` で十分か)
- Sonnet → Opus advisor escalation のロジックが既存 `call_claude_with_workspace` と整合しているか
- `extra_files` 検出ロジック (LLM がファイル名違反した場合の検知) の有用性
- `expected_filename` 引数のデフォルト値 / 上書き可能性の設計
- rate_limit_hit / api_call_completed emit の整合性

### 3. 検証層 (`_validate_deliverable_payload`)

**Steering 参照**: AC-2 (検証層 5 種) / design.md Step 2 (検証順序) / 確定事項 4 / 確定事項 11 (検出 E を本 steering で実装)

- A〜E の検出順序 (file_missing → invalid_json → not_dict → missing_keys → invalid_content_blocks) は妥当か
- 各検出の `extras` payload (content_preview / actual_type / missing / json_error 等) が運用デバッグに十分か
- 検出 E (`invalid_content_blocks`) と直前 steering の `_classify_content_blocks_fallback_reason` の重複の扱い (将来共通化候補として記録) は妥当か
- 必須キーリスト `_REQUIRED_DELIVERABLE_KEYS` の選択 (3 つ) は適切か
- 検出すべきだが見落としているケースはあるか (例: deliverable.json が空ファイル / 巨大すぎるファイル / `summary` の型違反)

### 4. 自動リトライ (`_run_text_generator_with_retries`)

**Steering 参照**: AC-3 (自動リトライ機構) / design.md Step 3 / 確定事項 5 (最大 2 回 / exp backoff 2,4 秒)

- `MAX_GENERATOR_RETRIES = 2` (合計 3 試行) は妥当か (code generation の MAX_CLAUDE_RETRIES = 3 との関係)
- exponential backoff (2, 4 秒) のタイミング
- 例外 (`Exception` 全般を catch) の対応 (BLE001 noqa は意図通りか)
- 全失敗時の `subagent_failed` emit の payload (subagent / error_type / error_message) が dashboard / Slack 通知で機能するか
- リトライ間の state (last_reason / last_extras) の保持ロジック

### 5. feature flag

**Steering 参照**: AC-1 制約 / design.md Step 5 / 確定事項 9 (デフォルト true) / 確定事項 12 (デフォルト確定根拠)

- デフォルト `"true"` の判断 (本番でも Z-2 経路を直ぐ有効) は妥当か
- 旧経路 (`call_claude` + `_parse_claude_response`) との分岐構造が明確か
- 切り戻し手順 (env 変更 + sam deploy or ECS update-service) が運用可能か
- feature flag を将来削除する判断条件 (例: 1 ヶ月 + 50 execution 安定後) を明示する必要があるか

### 6. PromptRecorder キー分離 (派生 2 統合)

**Steering 参照**: AC-4 (PromptRecorder 拡張) / design.md Step 4 / 確定事項 6 (output_files Optional) / 確定事項 7 (キー分離)

- `generator_text/0` と `generator_code/{code_type}` のキー設計が clean か
- `output_files` Optional 引数で後方互換を維持する設計 (旧 record は `output_files` キー欠落) が妥当か
- 旧 `subagent="generator"` キー (feature flag false 時) との衝突がないか
- Dashboard frontend (T-12 で実施予定) で `"generator_text"` / `"generator"` / `"generator_code"` 全パターンに対応する責任が明確か

### 7. テストカバレッジ

**Steering 参照**: AC-6 (ユニットテスト) / design.md「ユニットテスト設計」/ tasklist.md T-7 / 確定事項 8 (Dashboard 後方互換)

- 8 + 3 ケース (合計 11 ケース) で検証層 / リトライ / feature flag を網羅できているか
- 特に `test_generator_feature_flag_disables_workspace_mode` の単純化バージョン (環境変数判定式のみテスト) は十分か、もしくは run() 全体の統合テストが必要か
- mock pattern (`@patch("orchestrator.call_claude_with_text_workspace")` + `@patch("orchestrator.time.sleep")`) の堅牢性
- ヘルパー (`_valid_deliverable_json` / `_valid_workspace_result` / `_make_orch`) の static method 化が clean か

### 8. 直前 steering との非対称性

**Steering 参照**: requirements.md「アンチパターン回避の自己点検」/ design.md「設計上の判断ポイント」/ 確定事項 2 (text generator のみ Z-2 化)

- 直前 steering (fix loop の content_blocks 保護) は本 steering で **対症療法だった** と再評価されているが、ロールバック (revert) は行わない判断は妥当か
- 直前 steering の `_classify_content_blocks_fallback_reason` (fix loop 内) と本 steering の `_validate_deliverable_payload` (text generator 直後) の責務分離は明確か

### 9. 「努力目標」前提の明示

**Steering 参照**: requirements.md「採用方針整合性」/「Z-2 採用の整合性」/ design.md「三身一体の責務分担」/ ポイント 6 (docstring 明示)

- `_run_text_generator_with_retries` の docstring に「努力目標」前提が明示されている
- `call_claude_with_text_workspace` の docstring に同様の Note セクションがある
- requirements.md / design.md / tasklist.md にも明文化済み
- これらが将来の保守者にとって十分な情報か

### 10. 連続パッチアンチパターン回避

**Steering 参照**: requirements.md「アンチパターン回避の自己点検」/「起票の合理性」/ 必読 obsidian `2026-04-26_symptomatic-fix-anti-pattern.md`

- `obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` の「3 commit ルール」に対し、本 steering が **境界設計変更層**で能動的にサイクル終結させる立場を取ることの論証強度
- パッチ 4 件目に当たる連続パッチ密集地点で、Z-2 が本当に「終結策」になりうるか

### 11. ステアリング遵守チェック (メタ観点)

**Steering 参照**: requirements.md AC-1〜AC-4, AC-6 / design.md 確定事項 1〜12 / tasklist.md T-1〜T-8

本コードレビューの範囲 (実装 + テスト、未実装の T-9 以降は除く) について:

- **AC-1 (workspace 移行 + プロンプト改修)**: `prompts/generator.md` の Z-2 改修 + `call_claude_with_text_workspace` ラッパー + `_run_text_generator_with_retries` メソッドで実現できているか
- **AC-2 (検証層 5 種)**: `_validate_deliverable_payload` で A〜E すべて検出可能か
- **AC-3 (自動リトライ最大 2 回 + exp backoff)**: `MAX_GENERATOR_RETRIES = 2` + backoff `2 ** (attempt + 1)` で実現できているか
- **AC-4 (PromptRecorder 拡張 + キー分離)**: `output_files` Optional 引数 + `generator_text` / `generator_code` 分離が完了しているか
- **AC-6 (ユニットテスト 8 + 2 ケース)**: TestTextGeneratorWorkspace 8 ケース + TestPromptRecorderWithOutputFiles 3 ケース で達成済みか (規定の 2 ケースより多い 3 ケースになっている点も評価)
- **確定事項 1〜12 すべて**: 採用 / 却下 / スコープ判断が design.md 通りに実装に反映されているか
- 実装が steering から **逸脱している点**があれば明示

steering からの逸脱がある場合、それが意図的か否か (例えば実装中の発見でより良い方法を採用した) も含めて指摘してください。

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
- 設計の妥当性: ✅/⚠️/❌
- 検証層の網羅性: ✅/⚠️/❌
- 自動リトライ実装: ✅/⚠️/❌
- feature flag 運用: ✅/⚠️/❌
- PromptRecorder キー分離: ✅/⚠️/❌
- テストカバレッジ: ✅/⚠️/❌
- 連続パッチアンチパターン回避: ✅/⚠️/❌

## 結論
- マージ可否: 可 / 条件付き可 (P1 対応後) / 不可
```
