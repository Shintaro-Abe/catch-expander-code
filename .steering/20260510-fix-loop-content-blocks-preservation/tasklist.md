# タスクリスト: fix loop での content_blocks 構造的保護

## ステータス凡例

- ⏳ 未着手
- 🚧 進行中
- ✅ 完了
- ❌ ブロック / 不具合発生
- ⏭️ スキップ (理由を明記)

## タスク一覧

### Phase A: 実装

#### T-1 ⏳ ヘルパー関数 `_classify_content_blocks_fallback_reason` を追加

**ファイル**: `src/agent/orchestrator.py`

**配置**: モジュールトップ (`_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)` の直後、line 125 付近)

**実装内容**:
```python
def _classify_content_blocks_fallback_reason(parsed: dict) -> str | None:
    """fixer 応答の content_blocks が fallback すべき無効値か判定する。

    Returns:
        - "missing_key": parsed に content_blocks キーが存在しない
        - "none_value": parsed["content_blocks"] が None
        - "non_list": parsed["content_blocks"] が list 型ではない
        - "empty_list": parsed["content_blocks"] が空 list
        - None: parsed["content_blocks"] が valid な non-empty list (fallback 不要)
    """
    if "content_blocks" not in parsed:
        return "missing_key"
    value = parsed["content_blocks"]
    if value is None:
        return "none_value"
    if not isinstance(value, list):
        return "non_list"
    if len(value) == 0:
        return "empty_list"
    return None
```

**完了条件**:
- 関数が定義されている
- docstring が完備されている
- 既存の linter / type checker (`ruff` / `mypy`) でエラーが出ない

---

#### T-2 ⏳ fix loop 内置換ブロックを改修

**ファイル**: `src/agent/orchestrator.py`

**変更箇所**: `_run_review_loop` 内の fix attempt 後 deliverables 置換ブロック (現行 `:1492-1513`)

**変更内容**: design.md 「Step 2: fix loop 内置換ブロックの改修」のコード片に従い、以下を実装:
1. `parsed = _parse_claude_response(fix_raw)` の直後に処理は無変更
2. `parse_error` 経路は `logger.warning` 呼び出しを追加 (旧コードでは warning ログがなかったため、observability 改善も兼ねる) — **要確認**: 既存実装に warning が既にあるか再チェック
3. else 経路で:
   - `prev_content_blocks = current_deliverables.get("content_blocks")` でスナップショット
   - `preserved` / `_accumulate_fixer_notes` / `current_deliverables = parsed` / `update(preserved)` は既存通り
   - `_classify_content_blocks_fallback_reason(parsed)` で判定
   - 無効値かつ旧版が valid non-empty list の場合のみ `current_deliverables["content_blocks"] = prev_content_blocks` で復元
   - `logger.warning` で `loop` / `reason` / `previous_blocks_count` を記録
   - `logger.info` の `extra` に `content_blocks_fallback: fallback_reason` を追加

**完了条件**:
- fix loop の挙動が design.md 通りに変更されている
- `_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)` は無変更
- 関数シグネチャ・戻り値は無変更
- linter / type checker エラーなし

---

### Phase B: テスト

#### T-3 ⏳ ユニットテスト 6 ケースを追加

**ファイル**: `tests/unit/agent/test_orchestrator.py`

**追加先クラス**: 既存 `TestReviewLoop` クラス

**追加ケース** (design.md 「ユニットテスト設計」表通り):

1. `test_fix_loop_preserves_content_blocks_when_fixer_omits_key`
   - fixer 応答 JSON に `content_blocks` キーなし → 旧版維持
2. `test_fix_loop_preserves_content_blocks_when_fixer_returns_none`
   - `content_blocks: null` → 旧版維持
3. `test_fix_loop_preserves_content_blocks_when_fixer_returns_empty_list`
   - `content_blocks: []` → 旧版維持
4. `test_fix_loop_preserves_content_blocks_when_fixer_returns_non_list`
   - `content_blocks: "string"` → 旧版維持
5. `test_fix_loop_uses_fixer_content_blocks_when_valid`
   - fixer が valid non-empty list を返す → fixer 版採用 (regression 防止)
6. `test_fix_loop_logs_warning_on_fallback`
   - case 2 と同条件で `caplog` で warning ログ検証 (`loop`/`reason`/`previous_blocks_count`)

**完了条件**:
- 6 ケース全て pass
- 各ケースが AAA 構造に従う
- 既存テスト (`pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop`) regression なし

---

#### T-4 ⏳ 既存テスト regression 確認

**コマンド**:
```bash
pytest tests/unit/agent/test_orchestrator.py -v
```

**完了条件**:
- 新規 6 ケース全件 pass
- 既存 `TestReviewLoop` 系全件 pass
- pre-existing `call_codex` モック不整合は既知事象としてスキップ判定 (本 steering スコープ外)

---

### Phase C: コードレビュー

#### T-5 ⏳ Codex 連続レビュー (1 回目)

**前提**: ユーザー承認 (`memory/feedback_codex_review_requires_approval.md`)

**実施方法**: VS Code ターミナルで直接実行 (`memory/feedback_codex_wsl2_sandbox.md`)

```
codex -c sandbox_mode="danger-full-access"
```

**レビュー対象**:
- `src/agent/orchestrator.py` の変更箇所 (T-1, T-2)
- `tests/unit/agent/test_orchestrator.py` の追加テスト (T-3)

**観点**:
- 構造的保護ロジックの正当性
- 条件分岐の網羅性
- エッジケース見落とし
- テストカバレッジ

**完了条件**:
- レビュー結果サマリを本タスクリスト末尾の「レビュー履歴」セクションに記録
- P1 指摘の有無を判定

---

#### T-6 ⏳ Codex 1 回目指摘対応 (P1 指摘ありの場合のみ)

**完了条件**:
- 全 P1 指摘に対応
- 対応内容を「レビュー履歴」に記録

**スキップ条件**: T-5 で P1 指摘ゼロの場合は ⏭️ スキップ

---

#### T-7 ⏳ Codex 連続レビュー (2 回目)

**前提**: ユーザー承認 (1 回目完了後に再度承認を取る)

**完了条件**:
- 新規 P1/P2 指摘ゼロで収束判定
- 新規指摘ありの場合は T-6 形式で対応 → 必要に応じて 3 回目を実施

---

### Phase D: ドキュメント

#### T-8 ⏳ docs/functional-design.md にレビュー機能の段落追加

**ファイル**: `docs/functional-design.md`

**追加箇所**: レビュー修正ループの節 (既存セクション内)

**追加内容**: design.md 「ドキュメント整合計画」の段落 (4 行程度)

**完了条件**:
- 段落が追加されている
- 既存セクションとの整合性確保

---

### Phase E: 実機検証

#### T-9 ⏳ dev デプロイ (CI 経由)

**手順**:
1. `git add` / `git commit` (commit message は本 steering 内容を反映)
2. `git push` で main ブランチへ push
3. GitHub Actions の CI 完了を確認
4. `sam deploy` 実行 (または CI から自動デプロイ)
5. ECS Task Definition revision 更新を CloudWatch Logs で確認

**注意事項** (`memory/feedback_deploy_after_ci_completion.md`):
- ローカル `deploy-agent.sh` は使わない
- CI 完了後に `sam deploy` のみ

**完了条件**:
- ECS Task が新 revision で稼働
- CloudWatch Logs で起動ログが確認できる

---

#### T-10 ⏳ Slack 投入による実機検証

**手順**:
1. fix loop が走るトピックを Slack に投入 (例: 「AWS Lambda の SnapStart と Provisioned Concurrency の使い分け」)
2. DynamoDB events テーブルで `Deliverables updated by review fix` ログを確認
3. Notion ページに **品質情報ブロック + 記事本体 (content_blocks) 両方表示**を目視
4. CloudWatch Logs で `Fix loop fixer omitted/invalid content_blocks` warning の有無を観測

**fix loop が発火しなかった場合**:
- 別トピック再投入 (最大 3 回)
- 3 回連続未発火なら mock fixer 統合テスト追加 → tasklist に追加タスクとして起票

**完了条件**:
- 1 件以上のトピックで Notion ページに content_blocks が表示
- 観測ログ (warning / info) が design.md 通りに記録

---

### Phase F: クロージング

#### T-11 ⏳ メモリ更新

**対象メモリ**:
- `memory/project_review_loop_content_blocks_loss.md` → 解決済みに更新 (commit hash 記載)
- `memory/project_t1_rollback_pending_2026-05-09.md` → 構造修正完了の記録追記
- `memory/project_review_loop_recurring_patch_site.md` → 6 件目 patch site としての記録 (層が異なるため対症療法非該当の論証も含む)
- `memory/project_c_steering_next_session_handoff.md` → 完了状態に更新 or 削除判定

**完了条件**:
- メモリが本 steering 完了状態を反映
- MEMORY.md インデックスも整合

---

## レビュー履歴

### Codex 1 回目 (T-5)

- 実施日: TBD
- 指摘件数: TBD (P1: TBD / P2: TBD / P3: TBD)
- 主な指摘: TBD
- 対応コミット: TBD

### Codex 2 回目 (T-7)

- 実施日: TBD
- 指摘件数: TBD
- 収束判定: TBD

---

## 完了条件 (全タスク共通)

- [ ] 全タスクが ✅ または ⏭️ (スキップ理由明記)
- [ ] requirements.md AC-1 ~ AC-6 全て満たす
- [ ] git log に本 steering の commit が記録されている
- [ ] dev 環境で 1 件以上のトピックで content_blocks が Notion に表示されることを確認

## ロールバック手順 (緊急時)

万一本 steering の変更で regression が発生した場合:

```bash
git revert <commit-hash>
git push
```

T-1 〜 T-3 はテスト追加と局所改修なので revert で完全ロールバック可能。`_PRESERVED_DELIVERABLE_FIELDS` に手を加えていないため、既存契約への影響なし。
