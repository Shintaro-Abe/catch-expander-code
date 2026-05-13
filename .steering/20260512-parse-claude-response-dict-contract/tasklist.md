# タスクリスト: text generator の workspace モード化 + 検証層 + 自動リトライ

## ステータス凡例

- ⏳ 未着手
- 🚧 進行中
- ✅ 完了
- ❌ ブロック / 不具合発生
- ⏭️ スキップ (理由を明記)

## タスク一覧

### Phase A: プロンプト + ヘルパー実装

#### T-1 ⏳ `prompts/generator.md` を Z-2 版に改修

**ファイル**: `src/agent/prompts/generator.md`

**変更内容** (design.md Step 1 通り):

- 「出力方式」節を新設し、`deliverable.json` への Write ツール経由ファイル書き込みを指示
- スキーマ (`content_blocks` / `summary` / `quality_metadata`) を明示、サンプル含める
- 禁止事項を明記:
  - stdout への書き込み禁止
  - 「Part 1 / Part 2」「以下は続き」「以下は残り」「配列末尾に結合」等の分割応答禁止
  - `deliverable.json` 以外のファイル名禁止
  - 複数の `deliverable*.json` ファイル禁止
- 既存のレビュー指針 / 検証手順は維持

**完了条件**:
- generator.md が Z-2 版になっている
- スキーマ・禁止事項・処理手順が明示されている
- 既存テスト (`test_generator_no_longer_returns_code_files` 等) が pass する内容になっている

---

#### T-2 ⏳ `call_claude_with_text_workspace` ラッパー実装

**ファイル**: `src/agent/orchestrator.py`

**実装内容**:

- `call_claude_with_workspace` を参考にした text 成果物用ラッパー関数
- シグネチャ: `(prompt, *, expected_filename="deliverable.json", emitter, cost_acc) -> tuple[str, str | None, dict]`
- 戻り値:
  - `raw_stdout`: Claude CLI stdout 全文
  - `deliverable_content`: `deliverable.json` の中身 (str)。書かれなかった場合は None
  - `outcome`: `{"file_exists": bool, "file_bytes": int, "extra_files": list[str]}`
- sandbox の作成・cleanup
- `--allowed-tools=Write` を含む subprocess 実行
- 既存 `call_claude_with_workspace` とは関心を分離 (拡張子 whitelist 不要、単一ファイル想定)

**完了条件**:
- 関数が定義され、ローカルで sandbox 経由のファイル書き込みが動作
- docstring に「努力目標」前提が明記されている
- linter / type checker エラーなし

---

#### T-3 ⏳ `_validate_deliverable_payload` + `NonDictGeneratorResponse` 実装

**ファイル**: `src/agent/orchestrator.py`

**実装内容**:

1. **`NonDictGeneratorResponse(RuntimeError)`** 例外クラス
   - `reason: str` + `extras: dict` を保持
   - reason の値: `"file_missing"` / `"invalid_json"` / `"not_dict"` / `"missing_keys"` / `"invalid_content_blocks"` / `"exception"`

2. **`_REQUIRED_DELIVERABLE_KEYS`** 定数: `("content_blocks", "summary", "quality_metadata")`

3. **`_validate_deliverable_payload(deliverable_content, outcome)`** 関数
   - 検証順序: A (file_missing) → B (invalid_json) → C (not_dict) → D (missing_keys) → E (invalid_content_blocks)
   - 戻り値: `(deliverables: dict | None, reason: str | None, extras: dict)`
   - reason=None なら検証成功 (deliverables は valid dict)

**完了条件**:
- 例外型と関数が定義されている
- 検証順序が design.md 通り
- 直前 steering の `_classify_content_blocks_fallback_reason` との重複は **将来共通化候補** としてコメント or タスクで記録 (T-18 で記録)

---

#### T-4 ⏳ `_run_text_generator_with_retries` 実装 + line 1054 経路書換

**ファイル**: `src/agent/orchestrator.py`

**実装内容**:

1. **`_run_text_generator_with_retries`** メソッド
   - 最大 `MAX_GENERATOR_RETRIES + 1 = 3` 試行
   - 各試行で `call_claude_with_text_workspace` 呼び出し → PromptRecorder.record → `_validate_deliverable_payload`
   - reason=None で即時 return
   - reason 非 None で warning ログ + `time.sleep(2 ** (attempt + 1))` (最終試行直前以外)
   - 全試行失敗で `subagent_failed` emit + `NonDictGeneratorResponse` raise

2. **line 1045-1069 周辺**の text generator 経路を新メソッド呼出に置換
   - 既存の `gen_raw = call_claude(...)` → `deliverables = _parse_claude_response(gen_raw)` を削除
   - 代わりに `deliverables = self._run_text_generator_with_retries(gen_prompt, execution_id, generator_start_ns)`
   - `deliverables.pop("code_files", None)` は維持 (旧経路との互換)

3. **`subagent_started` emit の subagent 名**を `"generator_text"` に変更

**完了条件**:
- メソッドが定義され、orchestrator から呼び出される
- リトライ + backoff が動作する
- 全試行失敗時に subagent_failed + 例外 raise が同時に動作
- 既存テストとの互換性 (regression なし)

---

#### T-5 ⏳ `MAX_GENERATOR_RETRIES` 定数 + feature flag 分岐 + `template.yaml` 環境変数

**ファイル**:
- `src/agent/orchestrator.py`
- `template.yaml`

**実装内容**:

1. **`MAX_GENERATOR_RETRIES = 2`** 定数を orchestrator.py に追加
   - `MAX_CLAUDE_RETRIES = 3` (CLI 単体リトライ) とは別レイヤ

2. **feature flag 分岐**を text generator 経路に追加
   ```python
   if os.environ.get("WORKSPACE_TEXT_GEN", "true").lower() == "true":
       deliverables = self._run_text_generator_with_retries(...)
   else:
       # 旧経路 (即時切り戻し用)
       gen_raw = call_claude(gen_prompt, ...)
       if self._prompt_recorder is not None:
           self._prompt_recorder.record("generator", "0", gen_prompt, gen_raw)
       deliverables = _parse_claude_response(gen_raw)
   ```

3. **`template.yaml`** の ECS Task Definition Environment に `WORKSPACE_TEXT_GEN` を追加 (デフォルト `"true"`)

**完了条件**:
- 定数 + flag 分岐 + template 環境変数すべて配置
- env 切替で旧/新経路が動作 (テストで確認)

---

### Phase B: PromptRecorder 拡張 (派生 2 統合)

#### T-6 ⏳ PromptRecorder API 拡張 (`output_files` 引数 + キー分離)

**ファイル**: `src/observability/prompt_recorder.py`

**実装内容**:

1. **`record()` メソッド**に `output_files: dict[str, str] | None = None` Optional 引数を追加
2. **S3 JSON body**に `output_files` フィールドを条件付き追加 (None なら省略、後方互換)
3. **docstring** に新 subagent 値 (`"generator_text"` / `"generator_code"`) を記載
4. orchestrator.py:1095 の code generator record 呼出を `record("generator_code", code_type, ...)` に変更 (派生 2 統合)

**完了条件**:
- API 拡張済み、既存呼出は無変更で互換
- S3 キーが `prompts/{execution_id}/generator_text_0.json` / `generator_code_{type}.json` に分離
- 既存 `test_prompt_recorder.py` 全件 pass

---

### Phase C: テスト

#### T-7 ⏳ ユニットテスト 8 ケース + PromptRecorder 拡張テスト

**ファイル**:
- `tests/unit/agent/test_orchestrator.py` (新規 `TestTextGeneratorWorkspace` クラス)
- `tests/unit/observability/test_prompt_recorder.py` (新規ケース 2 件)

**追加テスト** (design.md「ユニットテスト設計」通り):

`TestTextGeneratorWorkspace`:

1. `test_generator_succeeds_when_deliverable_json_is_valid_dict`
2. `test_generator_retries_when_file_missing`
3. `test_generator_retries_when_json_invalid`
4. `test_generator_retries_when_not_dict`
5. `test_generator_retries_when_missing_keys`
6. `test_generator_retries_when_invalid_content_blocks`
7. `test_generator_fails_after_max_retries`
8. `test_generator_feature_flag_disables_workspace_mode`

`test_prompt_recorder.py`:

- `test_record_stores_output_files`
- `test_record_separates_generator_text_and_code_keys`

mock パターン: `@patch("orchestrator.time.sleep")` で backoff skip、`@patch("orchestrator.call_claude_with_text_workspace")` で workspace モック、`@patch("orchestrator.call_codex")` で reviewer モック (`memory/feedback_test_patches_call_codex_and_claude.md` 準拠)

**完了条件**:
- 8 + 2 ケース全件 pass
- AAA 構造 (Arrange-Act-Assert) で記述

---

#### T-8 ⏳ 直前 steering 9 ケース regression 確認

**コマンド**:
```bash
.venv/bin/pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop -k "fix_loop_" -v
```

**完了条件**:
- 直前 steering (`20260510-fix-loop-content-blocks-preservation`) で追加した 9 ケース全件 pass
- `_parse_claude_response` 系の既存テストも regression なし
- pre-existing failure (call_codex モック未対応) は本 steering スコープ外として残存許容

---

### Phase D: コードレビュー

#### T-9 ⏳ Codex 連続レビュー (1 回目)

**前提**: ユーザー承認 (`memory/feedback_codex_review_requires_approval.md`)

**実施方法**: VS Code ターミナルで直接実行 (`memory/feedback_codex_wsl2_sandbox.md`)

```bash
cat .audit/20260512-parse-response-evidence/codex-review-prompt.md | codex exec --model gpt-5.5 --skip-git-repo-check -c sandbox_mode="danger-full-access" -o .audit/20260512-parse-response-evidence/codex-review-1.txt -
```

**レビュー対象** (design.md「Codex 連続レビュー計画」通り):

- orchestrator.py の text generator 経路 + 新メソッド + 検証層 + 例外型 + feature flag
- generator.md の改修内容 (Part 分割禁止、スキーマ明示の十分性)
- PromptRecorder の API 拡張 + キー分離
- 直前 steering との非対称性 (fix loop は触らない理由) の妥当性

**完了条件**:
- レビュー結果サマリを本 tasklist 末尾の「レビュー履歴」セクションに記録
- P1 指摘の有無を判定

---

#### T-10 ⏳ Codex 1 回目指摘対応 (条件付き)

**完了条件**:
- 全 P1 指摘に対応
- 対応内容を「レビュー履歴」に記録

**スキップ条件**: T-9 で P1 指摘ゼロの場合は ⏭️ スキップ

---

#### T-11 ⏳ Codex 連続レビュー (2 回目)

**前提**: ユーザー承認 (1 回目完了後に再度承認)

**完了条件**:
- 新規 P1/P2 指摘ゼロで収束判定
- 新規指摘ありの場合は T-10 形式で対応 → 必要に応じて 3 回目を実施

---

### Phase E: Dashboard frontend 改修

#### T-12 ⏳ Dashboard frontend 改修

**ファイル**:
- `frontend/src/api/types.ts`
- `frontend/src/routes/ExecutionDetail.tsx`
- `frontend/src/routes/__tests__/ExecutionDetail.test.tsx` (or 同等の spec)

**実装内容** (design.md Step 8 通り):

1. **types.ts**: `SubagentIORecord` に `output_files?: Record<string, string>` 追加、subagent literal に `"generator_text"` / `"generator_code"` 追加 (旧 `"generator"` は後方互換維持)

2. **ExecutionDetail.tsx**: SubagentIOSection に generator_text record 用の表示分岐追加
   - stdout (Write 履歴) + output_files の各ファイル内容を IOExpandable で並べる
   - 旧 record (subagent="generator", output_files なし) は従来通り表示

3. **単体テスト**: output_files 含む / 含まない record 両対応の表示確認

**完了条件**:
- types.ts 拡張済み
- ExecutionDetail.tsx 分岐動作 (Vite 開発サーバで目視確認可能)
- 単体テスト 2 ケース以上追加 + 既存 frontend テスト regression なし

---

#### T-13 ⏳ `get_subagent_io` Lambda の `_SUBAGENT_ORDER` 更新

**ファイル**: `src/dashboard_api/get_subagent_io/app.py`

**実装内容**:

```python
_SUBAGENT_ORDER = {
    "researcher": 0,
    "generator_text": 1,
    "generator": 1,        # 後方互換 (旧 record)
    "generator_code": 2,
    "reviewer_eval": 3,
    "reviewer_fix": 4,
}
```

**完了条件**:
- 表示順設定が更新済み
- Lambda デプロイは T-15 で sam deploy 時に反映

---

### Phase F: ドキュメント

#### T-14 ⏳ docs (functional-design.md + architecture.md) 更新

**ファイル**:
- `docs/functional-design.md`
- `docs/architecture.md`

**実装内容**:

1. `docs/functional-design.md` に「ジェネレーターの workspace モード方式 (2026-05-13)」節を追加 (design.md「ドキュメント整合計画」通り)
2. `docs/architecture.md` の LLM 呼出パターン節に「workspace モード (text / code 両用)」を明示

**完了条件**:
- 両ドキュメントが本 steering 内容を反映
- 既存セクションとの整合性確保

---

### Phase G: デプロイ

#### T-15 ⏳ pre-commit-secret-scan Skill → backend commit/push → CI → sam deploy

**前提**: T-1〜T-14 完了

**実施手順**:

1. **pre-commit-secret-scan Skill** 発火 (`memory/feedback_pre_commit_secret_scan_skill.md`)
2. **backend 関連ファイルを stage**:
   - `src/agent/orchestrator.py`
   - `src/agent/prompts/generator.md`
   - `src/observability/prompt_recorder.py`
   - `src/dashboard_api/get_subagent_io/app.py`
   - `tests/`
   - `docs/`
   - `template.yaml`
   - `.steering/20260512-parse-claude-response-dict-contract/`
   - `.audit/20260512-parse-response-evidence/`
3. commit + push
4. CI 完了確認 (build-agent.yml の latest タグ削除済みなので衝突なし、`memory/feedback_ecr_immutable_no_latest_tag.md` 準拠)
5. sam deploy (`WORKSPACE_TEXT_GEN=true` がデフォルトで反映、`memory/feedback_deploy_after_ci_completion.md` 準拠)

**完了条件**:
- ECS Task Definition revision 更新済み
- CloudWatch Logs で起動ログ確認

---

#### T-16 ⏳ frontend build → s3 sync → CloudFront invalidation

**前提**: T-15 完了 (Lambda 側の subagent 値拡張がデプロイ済み)

**実施手順** (`memory/feedback_frontend_deploy_separate_from_sam.md` 準拠):

```bash
cd frontend
npm run build
aws s3 sync ./dist/ s3://catch-expander-frontend-417338593075/ --delete
aws cloudfront create-invalidation --distribution-id E18QJCZN0T3BQG --paths "/*"
```

**完了条件**:
- s3 sync 成功
- CloudFront invalidation 作成
- ブラウザで Dashboard を開いて execution の subagent IO セクションを目視確認

---

### Phase H: 実機検証

#### T-17 ⏳ 実機検証 (5/12 と同じトピックを Slack 再投入)

**手順**:

1. Slack で「IT開発における原則:KISS,DRY,YAGNI,アジャイル開発,リーン思考」を 1 回目投入
2. ECS タスク完了を待ち (5〜15 分)、結果を確認:
   - **ケース A**: 成功時 → Notion ページに content_blocks + 品質情報両方表示、Dashboard で `deliverable.json` の中身が表示される
   - **ケース B**: 検証失敗時 → リトライで成功 or 全失敗で subagent_failed (error_type="NonDictGeneratorResponse") emit + Slack 失敗通知
3. **少なくとも 3 回投入**して再現性を確認
4. 5/12 失敗 2 件と同じトピックでの **成功率を Z-2 化前 (1/3) と比較**

**観測項目**:
- CloudWatch Logs で「Text generator succeeded」「Text generator validation failed」のログ
- DynamoDB events テーブルで `subagent_failed (error_type="NonDictGeneratorResponse")` の発生数
- Dashboard で `generator_text` record の表示

**完了条件**:
- 3 回以上の投入で成功率 ≥ 2/3 (Z-2 化前の 1/3 から改善)
- 失敗時に明示的な Slack 通知が届く
- Dashboard で生成内容が観測可能
- 結果サマリを tasklist 末尾に記録

---

### Phase I: クロージング

#### T-18 ⏳ メモリ更新

**対象メモリ**:

- `project_parse_claude_response_type_contract_violation.md` → **本 steering で部分解決** (text generator のみ。他 5 call sites は派生 4 候補) を追記
- `project_review_loop_recurring_patch_site.md` → 「6 件目候補だった 5/10 steering は対症療法と再評価、本 steering で境界設計変更により連続パッチサイクル終結」を追記
- 新規 memory: `project_workspace_mode_text_generator_complete.md` (本 steering 完了状態、派生 4 / 派生 5 への移行可否判断材料)
- `project_c_steering_next_session_handoff.md` → 本 steering 完了で削除 or 履歴保持

**完了条件**:
- メモリ群が本 steering 完了状態を反映
- MEMORY.md インデックスも整合

---

## レビュー履歴

### Codex 1 回目 (T-9)

- 実施日: TBD
- 指摘件数: TBD (P1: TBD / P2: TBD / P3: TBD)
- 主な指摘: TBD
- 対応コミット: TBD

### Codex 2 回目 (T-11)

- 実施日: TBD
- 指摘件数: TBD
- 収束判定: TBD

---

## 完了条件 (全タスク共通)

- [ ] 全タスクが ✅ または ⏭️ (スキップ理由明記)
- [ ] requirements.md AC-1 〜 AC-9 全て満たす
- [ ] git log に本 steering の commit が記録されている
- [ ] dev 環境で 5/12 と同じトピックでの成功率が改善している
- [ ] Dashboard で generator_text record の output_files 表示が動作

---

## 設計上の補足 (確定事項)

### 直前 steering との非対称性

- 直前 steering: fix loop 内 fixer 応答に isinstance ガード追加 → **本 steering で fix loop は触らない** (text generator のみ Z-2 化)
- fix loop も将来 Z-2 化する余地はあるが、本 steering スコープ外
- 直前 steering で追加された `_classify_content_blocks_fallback_reason` と本 steering の `_validate_deliverable_payload` の検出 E はロジックが重複している。将来共通化候補として記録 (T-18 で memory 化)

### feature flag の運用

- デフォルト `WORKSPACE_TEXT_GEN=true` で本番投入
- 本番障害発生時は env 変更で即時 `false` に切り戻し可能
- 安定運用が確認できた後 (例: 1 ヶ月 + 50 execution 以上問題なし) で feature flag 自体を削除する別タスクを記録

### 派生する別 steering 候補 (本 steering 完了後に検討)

- **派生 4**: 残り 5 call sites (analysis / workflow / researcher / reviewer / fixer) の workspace モード化
- **派生 5**: Anthropic API + Tool Use への完全移行 (Z-1、長期 ROADMAP)
- **共通化**: `_classify_content_blocks_fallback_reason` と `_validate_deliverable_payload` の検出 E ロジック統合

---

## ロールバック手順 (緊急時)

万一本 steering の変更で regression が発生した場合:

### 即時ロールバック (feature flag のみ)

```bash
aws ecs update-service \
  --cluster catch-expander-cluster \
  --service catch-expander-agent \
  --task-definition <new-revision-with-WORKSPACE_TEXT_GEN=false>
```

または `template.yaml` で `WORKSPACE_TEXT_GEN: "false"` に変更して sam deploy。

### コード完全ロールバック

```bash
git revert <commit-hash>
git push
# CI → sam deploy
# 同時に frontend も旧版に戻す: git revert + npm run build + s3 sync
```

直前 steering と異なり、frontend デプロイの戻しも必要なため、複合手順になる。feature flag による即時ロールバックが第一選択。
