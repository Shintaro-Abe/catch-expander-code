# 設計: text generator の workspace モード化 + 検証層 + 自動リトライ

## 設計方針

### 基本コンセプト

**「LLM の text 応答をアプリでパースする」境界設計をやめる**。code generation で既に稼働している workspace モード (`call_claude_with_workspace`) を text generator にも適用し、LLM に Write ツール経由で `deliverable.json` というファイルに直接 dict を書かせる。アプリは `json.load()` でファイルを読むことで、確定的に dict を取得する。

これに **検証層** (5 種の失敗検出) と **自動リトライ** (最大 2 回) を組み合わせ、「努力目標 + 確定的検出 + 確率的成功率底上げ」の三身一体で実効的安定性を確保する。

### 設計レイヤの選定 (requirements.md の決定経緯)

| 層 | 案 | 採否 |
|---|---|---|
| プロンプト層 | 案 C (Part 分割禁止指示) | Z-2 内に統合 (単独却下) |
| パイプライン層 | 案 A (各 call site ガード) | ❌ 対症療法 |
| 型層 | 案 B (`_parse_claude_response` 型契約強制) | ❌ 対症療法 (連続パッチ 4 件目) |
| **境界設計変更層** | **Z-2 (workspace モード統一)** | ✅ **採用** |
| API 層 | Z-1 (Anthropic API + Tool Use) | ❌ コスト不採用 |

### 局所化の原則

- 変更は `src/agent/orchestrator.py` の **text generator 呼出経路 (line 1045-1069 周辺)** + 関連ヘルパー + `prompts/generator.md` + `src/observability/prompt_recorder.py` + `frontend/` の 5 領域に限定
- 他 5 つの `_parse_claude_response` call site (analysis / workflow / researcher / reviewer / fixer) は **本 steering で触らない**
- `_parse_claude_response` 関数本体は **無変更** (text generator では呼ばれなくなるため、放置で問題なし)
- 関数シグネチャ・戻り値・呼出グラフは generator 経路以外無変更

### 三身一体の責務分担

| 責務 | 担当 | 性質 |
|---|---|---|
| LLM に dict を `deliverable.json` に書かせる | プロンプト + Write tool | **努力目標** (確率的) |
| ファイル不在 / 不正 JSON / 必須キー欠落 を検出 | アプリ層の検証関数 | **確定的** (post-hoc) |
| 失敗時に再生成試行 | アプリ層の retry loop | **確率的成功率底上げ** |
| 全試行失敗で明示的に停止 | 上位 try / catch + Slack 失敗通知 | **確定的 fail-fast** |

## 実装アプローチ

### Step 1: `prompts/generator.md` の改修

text 成果物の出力方式を「stdout JSON」から「Write ツール経由のファイル書き込み」に変更。Part 分割禁止指示も含める。

```markdown
# ジェネレーター

## 役割

あなたは成果物生成専門のジェネレーターエージェントです。
リサーチャーの調査結果をもとに、ワークフロー計画で指定されたテキスト成果物を生成・推敲してください。

## 出力方式 (重要)

成果物は **Write ツール**を使い、現在の作業ディレクトリに **`deliverable.json` という単一ファイル**として書いてください。

### スキーマ

```json
{
  "content_blocks": [
    {"type": "heading_2", "heading_2": {"rich_text": [...]}},
    {"type": "paragraph", "paragraph": {"rich_text": [...]}},
    ...
  ],
  "summary": "成果物の要約 (200-300 文字)",
  "quality_metadata": {
    "sources_verified": <int>,
    "sources_unverified": <int>,
    "sources_total": <int>,
    "checklist_passed": <int>,
    "checklist_total": <int>,
    "newest_source_date": "<YYYY-MM-DD or null>",
    "oldest_source_date": "<YYYY-MM-DD or null>",
    "notes": [...],
    "unverified_details": [...]
  }
}
```

### 禁止事項

- **stdout には何も書かない**でください。応答は Write ツール経由のファイル書き込みのみで行ってください
- **「Part 1 / Part 2」「以下は続き」「以下は残り」「配列末尾に結合」等の分割応答**を行わないでください。`deliverable.json` という単一ファイルで成果物全体を完結させてください
- **`deliverable.json` 以外のファイル名で書かない**でください
- **複数の deliverable*.json ファイルを書かない**でください
- code_files (IaC / プログラムコード) は別パイプラインで独立生成されるため、ここでは出力しません

### 処理手順

1. リサーチャーの調査結果を整理
2. ワークフロー計画の `deliverable_types` に応じた構成を設計
3. content_blocks を Notion ブロック仕様に従って構築
4. summary と quality_metadata を組み立てる
5. **Write ツールで `deliverable.json` に書き出す** (1 回のツール呼び出しで完結することを推奨。長い場合は append でも可)

(既存のレビュー指針 / 検証手順は維持)
```

### Step 2: text 成果物用 workspace ラッパー (新規 or 既存関数拡張)

既存 `call_claude_with_workspace` は code 成果物用に「拡張子 whitelist」「size 上限」等の制約があるため、text 成果物用にはラッパーを新設する:

```python
def call_claude_with_text_workspace(
    prompt: str,
    *,
    expected_filename: str = "deliverable.json",
    emitter: Any = None,
    cost_acc: dict | None = None,
) -> tuple[str, str | None, dict]:
    """Claude Code CLI を workspace モードで呼び、text 成果物用 deliverable.json を読む。

    code generation 用の call_claude_with_workspace と並列の関数。
    違い:
    - 期待ファイル名が単一 (`deliverable.json`)
    - 拡張子 whitelist 不要 (.json 固定)
    - sandbox から expected_filename のみを読み返す

    Returns:
        (raw_stdout, deliverable_content, outcome) のタプル
        - raw_stdout: Claude CLI の stdout 全文 ("Wrote: deliverable.json" 等)
        - deliverable_content: deliverable.json の中身 (str)。書かれなかった場合は None
        - outcome: {"file_exists": bool, "file_bytes": int, "extra_files": list[str]}
    """
```

実装は `call_claude_with_workspace` (orchestrator.py:540 周辺) を参考に、cwd=tempdir、`--allowed-tools=Write` を渡し、subprocess 実行後 `(tempdir / expected_filename)` の存在と中身を返す。

### Step 3: orchestrator.py text generator 経路の書換

**現状 (line 1045-1069)**:

```python
generator_start_ns = time.monotonic_ns()
self._emitter.emit("subagent_started", {"subagent": "generator", ...})
try:
    gen_raw = call_claude(gen_prompt, emitter=self._emitter, cost_acc=self._cost_acc)
    if self._prompt_recorder is not None:
        self._prompt_recorder.record("generator", "0", gen_prompt, gen_raw)
    deliverables = _parse_claude_response(gen_raw)
except Exception as e:
    self._emitter.emit("subagent_failed", {...})
    raise
# ジェネレーターは text 成果物のみを返す。code_files は常に独立生成する
deliverables.pop("code_files", None)
```

**Z-2 版**:

```python
generator_start_ns = time.monotonic_ns()
self._emitter.emit("subagent_started", {"subagent": "generator_text", ...})

# 2026-05-12 観測の Part 分割応答対策: workspace モードに移行し、検証層 + 自動リトライで
# 「努力目標 + 確定的検出 + 確率的成功率底上げ」の三身一体で実効的安定性を確保する。
# 詳細: .steering/20260512-parse-claude-response-dict-contract/
deliverables = self._run_text_generator_with_retries(
    gen_prompt=gen_prompt,
    execution_id=execution_id,
    generator_start_ns=generator_start_ns,
)
# 注: Z-2 後の deliverables は必ず valid dict (検証 pass 済み)。
#     `code_files` は Z-2 のスキーマで存在しないが、後段互換のため pop は残す (no-op)。
deliverables.pop("code_files", None)
```

`_run_text_generator_with_retries` を新メソッドとして抽出:

```python
def _run_text_generator_with_retries(
    self,
    gen_prompt: str,
    execution_id: str,
    generator_start_ns: int,
) -> dict:
    """text generator を workspace モードで実行し、検証 + リトライを行う。

    Returns:
        valid な dict (必須キー存在を検証層で保証済み)

    Raises:
        NonDictGeneratorResponse: 全試行失敗時。subagent_failed event も emit 済み
    """
    last_reason: str | None = None
    last_extras: dict = {}
    for attempt in range(MAX_GENERATOR_RETRIES + 1):  # 0, 1, 2 で計 3 試行
        try:
            raw_stdout, deliverable_content, outcome = call_claude_with_text_workspace(
                gen_prompt, emitter=self._emitter, cost_acc=self._cost_acc
            )
            if self._prompt_recorder is not None:
                # 派生 2 統合: text generator は generator_text/0、code generator は generator_code/{type}
                self._prompt_recorder.record(
                    "generator_text", "0", gen_prompt, raw_stdout,
                    output_files={"deliverable.json": deliverable_content} if deliverable_content else None,
                )
            # 検証層
            deliverables, reason, extras = _validate_deliverable_payload(
                deliverable_content, outcome
            )
            if reason is None:
                logger.info(
                    "Text generator succeeded",
                    extra={
                        "execution_id": execution_id,
                        "attempt": attempt + 1,
                        "file_bytes": outcome.get("file_bytes", 0),
                    },
                )
                return deliverables
            last_reason, last_extras = reason, extras
            logger.warning(
                "Text generator validation failed",
                extra={
                    "execution_id": execution_id,
                    "attempt": attempt + 1,
                    "reason": reason,
                    **extras,
                },
            )
        except Exception as e:  # noqa: BLE001
            last_reason, last_extras = "exception", {"error_type": type(e).__name__, "error_message": str(e)[:500]}
            logger.warning(
                "Text generator raised during workspace call",
                extra={"execution_id": execution_id, "attempt": attempt + 1, **last_extras},
            )

        # exponential backoff (最後の試行ではスリープしない)
        if attempt < MAX_GENERATOR_RETRIES:
            wait_seconds = 2 ** (attempt + 1)
            time.sleep(wait_seconds)

    # 全試行失敗
    self._emitter.emit(
        "subagent_failed",
        {
            "subagent": "generator_text",
            "stage": "text_generation",
            "duration_ms": (time.monotonic_ns() - generator_start_ns) // 1_000_000,
            "error_type": "NonDictGeneratorResponse",
            "error_message": f"All {MAX_GENERATOR_RETRIES + 1} attempts failed: reason={last_reason}, extras={last_extras}",
        },
        status_at_emit="failed",
    )
    raise NonDictGeneratorResponse(reason=last_reason or "unknown", **last_extras)
```

### Step 4: 検証層 (新規ヘルパー関数 + 例外型)

```python
class NonDictGeneratorResponse(RuntimeError):
    """text generator が workspace モードで valid な deliverable.json を生成しなかった場合の例外。

    reason の値:
    - "file_missing": deliverable.json が sandbox に存在しない
    - "invalid_json": ファイル存在するが json.load() で JSONDecodeError
    - "not_dict": JSON load 結果が dict でない (list / scalar / None)
    - "missing_keys": 必須キー (content_blocks / summary / quality_metadata) のいずれかが欠落
    - "invalid_content_blocks": content_blocks が空 list / 非 list
    - "exception": call_claude_with_text_workspace が予期せぬ例外を発生
    """
    def __init__(self, reason: str, **extras: Any) -> None:
        self.reason = reason
        self.extras = extras
        msg = f"Text generator validation failed: reason={reason}, extras={extras}"
        super().__init__(msg)


_REQUIRED_DELIVERABLE_KEYS = ("content_blocks", "summary", "quality_metadata")


def _validate_deliverable_payload(
    deliverable_content: str | None,
    outcome: dict,
) -> tuple[dict | None, str | None, dict]:
    """deliverable.json の中身を検証し、(deliverables, reason, extras) を返す。

    reason=None なら検証成功 (deliverables は valid dict)。
    reason が非 None なら検証失敗 (deliverables は None、extras に追加情報)。

    検証順序:
    A. ファイル不在 → "file_missing"
    B. JSON 不正 → "invalid_json"
    C. dict でない → "not_dict" + actual_type
    D. 必須キー欠落 → "missing_keys" + missing list
    E. content_blocks が無効 → "invalid_content_blocks"
    """
    # A: ファイル不在
    if not outcome.get("file_exists") or deliverable_content is None:
        return None, "file_missing", {
            "extra_files": outcome.get("extra_files", []),
        }
    # B: JSON load
    try:
        parsed = json.loads(deliverable_content)
    except json.JSONDecodeError as e:
        return None, "invalid_json", {
            "json_error": str(e)[:200],
            "content_preview": deliverable_content[:300],
        }
    # C: dict でない
    if not isinstance(parsed, dict):
        return None, "not_dict", {
            "actual_type": type(parsed).__name__,
            "content_preview": str(parsed)[:300],
        }
    # D: 必須キー欠落
    missing = [k for k in _REQUIRED_DELIVERABLE_KEYS if k not in parsed]
    if missing:
        return None, "missing_keys", {
            "missing": missing,
            "present_keys": list(parsed.keys())[:20],
        }
    # E: content_blocks が無効
    cb = parsed["content_blocks"]
    if not isinstance(cb, list):
        return None, "invalid_content_blocks", {
            "actual_type": type(cb).__name__,
        }
    if len(cb) == 0:
        return None, "invalid_content_blocks", {
            "actual_type": "empty_list",
        }
    # 全て pass
    return parsed, None, {}
```

### Step 5: 自動リトライ機構 (Step 3 と統合)

リトライは `_run_text_generator_with_retries` 内の `for attempt in range(MAX_GENERATOR_RETRIES + 1)` ループで実装。

```python
MAX_GENERATOR_RETRIES = 2  # 合計 3 試行 (initial + 2 retries)
```

`MAX_CLAUDE_RETRIES = 3` (CLI 単体の reflective retry) と独立。Z-2 のリトライは「LLM 応答が valid dict にならない時の再生成」であり、CLI のネットワークリトライとは別レイヤ。

backoff: `2 ** (attempt + 1)` 秒 (= 2, 4 秒)。最後の試行直前にスリープしない。

### Step 6: PromptRecorder API 拡張

`src/observability/prompt_recorder.py` を以下に変更:

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
    """サブエージェントのプロンプトと出力を S3 に書き込む (best-effort)。

    Args:
        subagent: サブエージェント種別。
            "researcher" / "generator_text" / "generator_code" / "reviewer_eval" / "reviewer_fix"
            (注: 旧 "generator" は廃止、派生 2 統合で text/code 分離)
        index: レコードの識別子。
        prompt: サブエージェントに渡したプロンプト全文。
        output: stdout 全文 (workspace モードでは "Wrote: ..." 履歴のみの場合あり)。
        output_files: workspace モード時の生成ファイル群 ({filename: content} の dict)。
            Z-2 で追加。旧記録形式との後方互換のため Optional。
    """
    body: dict[str, Any] = {
        "subagent": subagent,
        "index": index,
        "prompt": prompt,
        "output": output,
        "recorded_at": datetime.now(UTC).isoformat(...),
    }
    if output_files:
        body["output_files"] = output_files
    # ... (S3 put_object は既存通り)
```

後方互換: `output_files` 欠落時は省略。旧 record (output_files なし) は frontend で従来通り表示。

### Step 7: PromptRecorder キー分離 (派生 2 統合)

旧 (バグ):
- line 1053: `record("generator", "0", gen_prompt, gen_raw)`  ← text generator
- line 1095: `record("generator", "0", prompt, raw_stdout)`  ← code generator
- → **同じキー `generator/0` で上書き**

Z-2 後 (修正):
- text generator: `record("generator_text", "0", ...)`
- code generator: `record("generator_code", code_type, ...)`

S3 キーも分離:
- 旧: `prompts/{execution_id}/generator_0.json`
- 新: `prompts/{execution_id}/generator_text_0.json` / `prompts/{execution_id}/generator_code_iac_code.json` 等

### Step 8: Dashboard frontend 改修

`frontend/src/api/types.ts`:

```typescript
export interface SubagentIORecord {
  subagent: "researcher" | "generator_text" | "generator_code" | "reviewer_eval" | "reviewer_fix" | "generator"  // "generator" は後方互換 (旧 record)
  index: string
  prompt: string
  output: string
  output_files?: Record<string, string>   // Z-2 で追加
  recorded_at: string
}
```

`frontend/src/routes/ExecutionDetail.tsx` の SubagentIOSection (line 397-) に generator_text の表示分岐を追加:

```typescript
// Z-2 後の generator_text record
{r.subagent === "generator_text" && (
  <>
    <IOExpandable label="プロンプトを表示" content={r.prompt} />
    <IOExpandable label="stdout を表示 (Write 履歴)" content={r.output} />
    {r.output_files && Object.entries(r.output_files).map(([filename, content]) => (
      <IOExpandable
        key={filename}
        label={`${filename} の中身を表示`}
        content={content}
      />
    ))}
  </>
)}

// 旧 record (subagent="generator") は従来通り
{r.subagent === "generator" && (
  <>
    <IOExpandable label="プロンプトを表示" content={r.prompt} />
    <IOExpandable label="出力を表示" content={r.output} />
  </>
)}
```

`_SUBAGENT_ORDER` (Dashboard Lambda 側) も更新:

```python
_SUBAGENT_ORDER = {
    "researcher": 0,
    "generator_text": 1,
    "generator": 1,  # 後方互換 (旧 record)
    "generator_code": 2,
    "reviewer_eval": 3,
    "reviewer_fix": 4,
}
```

### Step 9: ロギング / イベント emit

- `subagent_started` の subagent = `"generator_text"` に変更
- `subagent_failed` の subagent = `"generator_text"`, error_type = `"NonDictGeneratorResponse"`, error_message に reason / extras 含める
- 試行ごとの warning log は `attempt` / `reason` / `extras` を含める
- 成功時の info log は `attempt`, `file_bytes` を含める

### Step 10: feature flag

ロールバック容易性のため環境変数で切替可能にする:

```python
# orchestrator.py の text generator 経路
if os.environ.get("WORKSPACE_TEXT_GEN", "true").lower() == "true":
    deliverables = self._run_text_generator_with_retries(...)
else:
    # 旧経路 (Z-2 失敗時の即時切り戻し用)
    gen_raw = call_claude(gen_prompt, ...)
    if self._prompt_recorder is not None:
        self._prompt_recorder.record("generator", "0", gen_prompt, gen_raw)
    deliverables = _parse_claude_response(gen_raw)
```

デフォルト `true` でデプロイ。`template.yaml` で環境変数を ECS Task Definition に渡す。

### 設計上の判断ポイント

#### ポイント 1: 旧 `_parse_claude_response` 関数は触らない

text generator では workspace 経由になるため `_parse_claude_response` を呼ばなくなる。残り 5 call sites (analysis / workflow / researcher / reviewer / fixer) では依然使われるが、これらは Part 分割発火条件に達しにくい応答サイズのため本 steering スコープ外。関数本体は無変更で放置する。

#### ポイント 2: 検証層の検出 E と直前 steering `_classify_content_blocks_fallback_reason` の関係

両者は **判定対象が異なる**ため重複ではない:

- 直前 steering: **fix loop 内**で fixer 応答の content_blocks を判定 (fallback 用)
- 本 steering: **text generator 直後**で deliverable.json の content_blocks を判定 (validation 用)

ただし「無効な content_blocks の 4 種パターン (missing_key / none_value / non_list / empty_list)」という判定ロジック自体は重複しているため、将来共通ヘルパーに抽出する余地はある。本 steering では別 path で実装し、tasklist に「将来共通化候補」として記録する。

#### ポイント 3: feature flag のデフォルト

デフォルト `true` (Z-2 経路) でデプロイ。理由:
- Z-2 はユニットテスト + 検証層 + リトライで多重防御
- 本番障害時に `WORKSPACE_TEXT_GEN=false` で即時切り戻し可能
- code generation で同等の workspace パターンが既に安定動作

デフォルト `false` は本 steering で意図的に却下: Z-2 を有効化する mental overhead を増やし、Part 分割再発リスクが残り続けるため。

#### ポイント 4: code_files pop の互換維持

Z-2 後の deliverable.json スキーマには `code_files` が含まれない (LLM への指示で禁止)。それでも `deliverables.pop("code_files", None)` を残す理由は:

- 旧経路 (feature flag false) との互換性
- 万一 LLM が指示に反して code_files を含めた場合の防御 (no-op で除去)
- 既存テスト (`test_review_loop_preserves_code_files_on_fix_success` 等) との整合性

#### ポイント 5: PromptRecorder API の後方互換

`output_files: dict[str, str] | None = None` で Optional 引数追加。

- 既存 5 call site (researcher / reviewer_eval / reviewer_fix) は無変更で互換
- S3 JSON に `output_files` フィールドは「あれば書く、無ければ書かない」
- Dashboard frontend で「旧 record (output_files 欠落) は従来表示、新 record (output_files あり) はファイル一覧表示」と分岐

#### ポイント 6: 「努力目標」の docstring 明示

`_run_text_generator_with_retries` の docstring に以下を明記:

```
Note (努力目標):
    本関数は LLM が `deliverable.json` に valid な dict を書くことを **努力目標** として
    プロンプトで指示する。LLM 確率挙動依存のため、達成は保証されない。失敗ケース
    (ファイル不在 / 不正 JSON / 必須キー欠落) は本関数内の検証層で確定的に検出し、
    最大 MAX_GENERATOR_RETRIES 回まで再生成を試みる。全試行失敗で NonDictGeneratorResponse
    を raise する。

    真の構造保証 (Z-1: Anthropic API + Tool Use) は本 steering では採用しない (コスト不採用)。
    本関数は「努力目標 + 検証層 + リトライ」の三身一体で実効的安定性を提供する。
```

## 変更するコンポーネント

| ファイル | 変更内容 | 行数目安 |
|---|---|---|
| `src/agent/orchestrator.py` | (1) `_run_text_generator_with_retries` メソッド追加、(2) text generator 経路 (line 1045-1069) を新メソッド呼出に置換、(3) `_validate_deliverable_payload` ヘルパー + `NonDictGeneratorResponse` 例外型、(4) `call_claude_with_text_workspace` ラッパー、(5) `MAX_GENERATOR_RETRIES` 定数、(6) feature flag 分岐 | +180 行 / -20 行 |
| `src/agent/prompts/generator.md` | 出力方式を workspace に変更、Part 分割禁止、スキーマ明示 | +40 行 / -15 行 |
| `src/observability/prompt_recorder.py` | `record()` API に `output_files` 引数追加 | +15 行 |
| `tests/unit/agent/test_orchestrator.py` | `TestTextGeneratorWorkspace` クラス + 8 ケース | +200 行 |
| `tests/unit/observability/test_prompt_recorder.py` | output_files 保存テスト + キー分離テスト | +50 行 |
| `frontend/src/api/types.ts` | `SubagentIORecord` 拡張 | +5 行 |
| `frontend/src/routes/ExecutionDetail.tsx` | generator_text 表示分岐追加 | +30 行 |
| `frontend/src/routes/__tests__/ExecutionDetail.test.tsx` (or 該当 spec) | output_files 表示テスト | +30 行 |
| `src/dashboard_api/get_subagent_io/app.py` | `_SUBAGENT_ORDER` に generator_text / generator_code 追加 | +3 行 |
| `docs/functional-design.md` | workspace モード方式の新節 | +20 行 |
| `docs/architecture.md` | LLM 呼出パターン更新 | +10 行 |
| `template.yaml` | ECS Task Definition に `WORKSPACE_TEXT_GEN` 環境変数追加 | +3 行 |

## データ構造の変更

### PromptRecorder S3 JSON スキーマ (拡張)

**旧形式**:
```json
{
  "subagent": "generator",
  "index": "0",
  "prompt": "...",
  "output": "<Claude CLI stdout 全文>",
  "recorded_at": "2026-05-12T10:22:24.707Z"
}
```

**新形式** (Z-2 後):
```json
{
  "subagent": "generator_text",
  "index": "0",
  "prompt": "...",
  "output": "Wrote: deliverable.json",
  "output_files": {
    "deliverable.json": "{...}"
  },
  "recorded_at": "2026-05-13T12:34:56.789Z"
}
```

後方互換: `subagent: "generator"` の旧 record は frontend で従来表示 (Step 8 参照)。

### `NonDictGeneratorResponse` 例外

新規例外型。`reason` (str) + `extras` (dict) を保持。`subagent_failed` event の error_type / error_message に流れる。

## 影響範囲の分析

### 直接影響

- text generator 経路: Z-2 化、リトライ機構追加
- PromptRecorder: API 拡張、S3 JSON スキーマ拡張、subagent キー分離
- Dashboard frontend: 表示分岐追加 + 後方互換
- ドキュメント: workspace モード方式の記述

### 間接影響 (リスク評価)

| リスク | 評価 | 緩和策 |
|---|---|---|
| 直前 steering の 9 テスト regression | 低 | fix loop は無変更、`_parse_claude_response` も無変更。テスト構造に影響なし |
| 既存 5 call site (analysis / workflow / researcher / reviewer / fixer) への影響 | なし | 触らない |
| LLM が workspace 指示を無視 (努力目標未達成) | 中 | リトライで底上げ、全失敗時は明示的 fail-fast |
| 実行時間増加 (workspace は複数 turn 化、リトライ発火時はさらに増) | 中 | レート制限到達率を実測してから判断、feature flag で切り戻し可能 |
| Dashboard frontend 後方互換の取り違え | 中 | 旧 record は subagent="generator"、新 record は subagent="generator_text" で確実に分岐 |
| PromptRecorder S3 容量増 | 低 | `output_files` を保存する分の bytes 増 (1 record あたり ~30KB)。S3 lifecycle policy で旧 record は自動削除 |

### 関連メモリ整合チェック

- `memory/project_parse_claude_response_type_contract_violation.md`: 本 steering で解決対象とする構造バグ
- `memory/feedback_anti_pattern_discipline.md`: 3 層代替案規律遵守、Z-2 は境界設計変更層で連続パッチサイクルを能動的に終わらせる
- `memory/feedback_frontend_deploy_separate_from_sam.md`: frontend デプロイ手順 (sam とは別経路)
- `memory/feedback_deploy_after_ci_completion.md`: CI 完了後の sam deploy
- `memory/feedback_test_patches_call_codex_and_claude.md`: テスト patch パターン (本 steering でも text generator のみ Z-2 化なので、reviewer は call_codex モック維持)

## ユニットテスト設計

### `TestTextGeneratorWorkspace` クラス (新規 8 ケース)

`tests/unit/agent/test_orchestrator.py` に新クラスとして追加:

| # | テスト名 | シナリオ | 期待挙動 |
|---|---|---|---|
| 1 | `test_generator_succeeds_when_deliverable_json_is_valid_dict` | mock workspace が valid dict を含む `deliverable.json` を書く | deliverables 取得、subagent_started/completed emit、PromptRecorder に output_files 保存 |
| 2 | `test_generator_retries_when_file_missing` | 1 回目: ファイル不在 / 2 回目: valid | 2 attempts で成功、attempt 1 で warning ログ |
| 3 | `test_generator_retries_when_json_invalid` | 1 回目: 不正 JSON / 2 回目: valid | 2 attempts で成功 |
| 4 | `test_generator_retries_when_not_dict` | 1 回目: JSON array / 2 回目: valid | 2 attempts で成功 |
| 5 | `test_generator_retries_when_missing_keys` | 1 回目: content_blocks 欠落 / 2 回目: valid | 2 attempts で成功 |
| 6 | `test_generator_retries_when_invalid_content_blocks` | 1 回目: content_blocks = [] / 2 回目: valid | 2 attempts で成功 |
| 7 | `test_generator_fails_after_max_retries` | 全 3 試行で検証失敗 | `NonDictGeneratorResponse` raise、subagent_failed emit (error_type="NonDictGeneratorResponse") |
| 8 | `test_generator_feature_flag_disables_workspace_mode` | env `WORKSPACE_TEXT_GEN=false` | 旧 call_claude 経路が呼ばれる (regression 防止) |

mock パターン:

```python
@patch("orchestrator.time.sleep")  # backoff を skip
@patch("orchestrator.call_claude_with_text_workspace")
@patch("orchestrator.call_codex")
def test_xxx(self, mock_codex, mock_workspace, mock_sleep):
    mock_codex.side_effect = [...]  # reviewer mocks
    mock_workspace.side_effect = [
        ("Wrote: deliverable.json", '{"content_blocks": [...], "summary": "...", "quality_metadata": {...}}', {"file_exists": True, "file_bytes": 1024, "extra_files": []}),
    ]
    ...
```

### `test_prompt_recorder.py` (拡張)

- `test_record_stores_output_files`: output_files 引数を渡したら S3 body に保存される
- `test_record_separates_generator_text_and_code`: 同じ index でも subagent が違えば S3 キーが分離される

### Dashboard frontend テスト

`frontend/src/routes/__tests__/ExecutionDetail.test.tsx` (or 同等):

- `renders generator_text record with output_files expandables`: subagent="generator_text" + output_files ありの mock record が正しく表示
- `falls back to legacy display when subagent is "generator"`: 旧 record (subagent="generator", output_files なし) で従来通り「出力を表示」のみ

### Regression 確認

- 直前 steering の `TestReviewLoop::fix_loop_*` 9 ケース全件 pass (fix loop は無変更)
- `_parse_claude_response` 単体テストは無変更 (関数本体無変更)
- 既存 `test_prompt_recorder.py` 全件 pass (API は後方互換)

## Codex 連続レビュー計画

### 実施タイミング

タスクリスト T-4 (実装 + テスト完了) 後に Codex レビュー (1 回目)。指摘修正後 2 回目を回す。

### 承認ルール

- `memory/feedback_codex_review_requires_approval.md` 準拠: 1 回目完了後にユーザー承認を得てから 2 回目
- VS Code ターミナルで直接実行 (`memory/feedback_codex_wsl2_sandbox.md`)

### 収束判定

- 1 回目で P1 ゼロ かつ 2 回目で新規 P1/P2 ゼロ → 収束
- それ以外は 3 回目まで実施
- 本 steering はスコープ広い (orchestrator + frontend + PromptRecorder) ため、**最低 2 回**を前提とし、3 回目を視野に入れる

### レビュー範囲

- orchestrator.py の text generator 経路 + 新メソッド + 検証層 + 例外型 + feature flag
- generator.md の改修内容 (Part 分割禁止、スキーマ明示の十分性)
- PromptRecorder の API 拡張 + キー分離
- frontend の types.ts + ExecutionDetail.tsx 改修 + 後方互換
- 直前 steering との非対称性 (fix loop は触らない理由) の妥当性

## 実機検証計画

### Phase 1: ローカルテスト

- `pytest tests/unit/agent/test_orchestrator.py::TestTextGeneratorWorkspace -v` で新規 8 ケース全件 pass
- `pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop -k "fix_loop_" -v` で 9 ケース regression なし
- `pytest tests/unit/observability/test_prompt_recorder.py -v` で全件 pass
- `cd frontend && npm test` で frontend 単体テスト全件 pass

### Phase 2: dev デプロイ

1. pre-commit-secret-scan Skill 発火
2. orchestrator + PromptRecorder + template.yaml の commit + push
3. CI 完了確認 (build-agent.yml)
4. sam deploy (ECS Task Definition 更新、`WORKSPACE_TEXT_GEN=true` で環境変数注入)
5. frontend ビルド + s3 sync + CloudFront invalidation (`memory/feedback_frontend_deploy_separate_from_sam.md` 準拠)

### Phase 3: 5/12 と同じトピックを再投入

Slack で「IT開発における原則:KISS,DRY,YAGNI,アジャイル開発,リーン思考」を投入。

期待挙動 (2 つのケース):

**ケース A: LLM が deliverable.json を valid に書く**
- ECS タスク正常完了
- Notion ページに content_blocks + 品質情報両方表示
- CloudWatch Logs に "Text generator succeeded" info
- Dashboard で generator_text record を確認、output_files["deliverable.json"] が表示される

**ケース B: LLM が依然として Part 分割を試みる (ファイル書かず stdout に出力)**
- 検出 A (file_missing) 発火
- リトライ 2 回 (確率的に成功するか、全て失敗するか)
- 全失敗なら subagent_failed emit + Slack 失敗通知
- Dashboard で attempt ごとの reason / extras を確認

### Phase 4: 観測

- CloudWatch Logs で「Text generator validation failed」「Text generator succeeded」のログ件数
- DynamoDB events テーブルで subagent_failed (error_type="NonDictGeneratorResponse") の発生数
- 5/12 失敗 2 件と同じトピックを 3 回以上投入し、Z-2 化前後で成功率を比較

## ドキュメント整合計画

### `docs/functional-design.md` への追記

「マルチエージェント構成」または「3.3 ジェネレーター」節に以下を追加:

```markdown
#### ジェネレーターの workspace モード方式 (2026-05-13)

text 成果物の生成は **Claude Code CLI の Write ツール経由** で `deliverable.json` を書かせる workspace モード方式を採用する。
これにより:
- LLM が応答を「Part 1 / Part 2」に分割する確率的挙動を構造的に発火しにくくする
- ファイル書き込みは LLM の typical な動作様式で、stdout 巨大 JSON より安定

実装は `_run_text_generator_with_retries` で、最大 3 試行 (initial + 2 retries) で valid な dict を取得する。
検証層 (file_missing / invalid_json / not_dict / missing_keys / invalid_content_blocks) で確定的に失敗検出する。
全試行失敗で `NonDictGeneratorResponse` を raise し、Slack に失敗通知する。

本方式は **「努力目標 + 検証層 + リトライ」の三身一体**で実効的安定性を確保する。
真の構造保証 (Anthropic API + Tool Use) は採用しない (コスト不採用)。

(コード生成 (`code_files`) は別経路の `call_claude_with_workspace` で並列的に独立生成される)
```

### `docs/architecture.md` への追記

LLM 呼出パターンの節に「workspace モード (text / code 両用)」を明示。

### obsidian (任意)

`obsidian/2026-05-12_llm-part-split-and-workspace-migration.md` を新規作成し、Part 分割インシデント + workspace 移行の経緯を記録 (任意、後続セッションで判断)。

## 次フェーズ予告: tasklist.md の構成案

design.md 承認後、tasklist.md を起草する。タスク粒度の予告:

- **T-1**: `prompts/generator.md` を Z-2 版に改修 (workspace 指示 + Part 分割禁止 + スキーマ明示)
- **T-2**: `call_claude_with_text_workspace` ラッパー実装
- **T-3**: `_validate_deliverable_payload` + `NonDictGeneratorResponse` 実装
- **T-4**: `_run_text_generator_with_retries` 実装 + line 1054 経路書換
- **T-5**: `MAX_GENERATOR_RETRIES` 定数 + feature flag 分岐 + `template.yaml` 環境変数
- **T-6**: PromptRecorder API 拡張 (output_files 引数 + キー分離)
- **T-7**: ユニットテスト 8 ケース + PromptRecorder 拡張テスト
- **T-8**: 直前 steering 9 ケース regression 確認
- **T-9**: Codex 連続レビュー (1 回目) — ユーザー承認
- **T-10**: Codex 1 回目指摘対応 (条件付き)
- **T-11**: Codex 連続レビュー (2 回目) — ユーザー承認
- **T-12**: Dashboard frontend 改修 (types.ts + ExecutionDetail.tsx + 単体テスト)
- **T-13**: get_subagent_io Lambda の `_SUBAGENT_ORDER` 更新
- **T-14**: docs (functional-design.md + architecture.md) 更新
- **T-15**: pre-commit-secret-scan Skill → backend commit/push → CI → sam deploy
- **T-16**: frontend build → s3 sync → CloudFront invalidation
- **T-17**: 実機検証 (5/12 と同じトピックを Slack 再投入、3 回以上)
- **T-18**: メモリ更新 (Z-2 採用記録、対症療法サイクル終結の記録)

## 設計上の判断事項 (確定 + オープン)

### 確定事項 (本 design.md で固定)

1. **境界設計変更層 (Z-2)** を採用、案 B (型契約強制) は対症療法として却下
2. text generator のみ Z-2 化、他 5 call sites は本 steering スコープ外
3. `_parse_claude_response` 関数本体は無変更で放置
4. 検証層は 5 種 (file_missing / invalid_json / not_dict / missing_keys / invalid_content_blocks)
5. リトライは最大 2 回 (合計 3 試行)、exponential backoff (2 / 4 秒)
6. PromptRecorder API は `output_files` Optional 引数で後方互換維持
7. PromptRecorder キーは `generator_text` / `generator_code` に分離 (派生 2 統合)
8. Dashboard frontend は旧 `generator` record との後方互換を維持
9. feature flag `WORKSPACE_TEXT_GEN` デフォルト `true`

### 追加確定事項 (design.md 承認時 2026-05-13 にユーザー判断で確定)

10. **`call_claude_with_text_workspace` を新規ラッパーとして分離**: text/code で responsibility が異なるため。関心の分離を明確化、code workspace の制約 (拡張子 whitelist 等) と切り分け
11. **検証層の検出 E (invalid_content_blocks) は本 steering で実装**: deliverable.json の構造完全性を 1 ステップで担保。直前 steering の `_classify_content_blocks_fallback_reason` との重複は将来共通化候補として tasklist に記録
12. **feature flag `WORKSPACE_TEXT_GEN` のデフォルトは `true`**: 本番でも Z-2 が直ぐ有効、検証層で多重防御込みで投入。本番障害時は `false` で即時切り戻し
