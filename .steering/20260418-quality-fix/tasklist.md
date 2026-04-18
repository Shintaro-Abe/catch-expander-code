# タスクリスト: 成果物品質問題の修正

## 方針

- Must (M1-M3) を最優先で実装し、独立した commit に分ける
- Should (S1-S2) は Must 完了後に追加
- N1 は保留（Must/Should の再実行検証後に要否判断）
- 各 Fix 完了ごとにユニットテスト green を確認してから次へ

## フェーズ 1: Must 修正（破綻防止）

### M1: source_id をシステム全体で一意化

- [x] **M1-1** `src/agent/orchestrator.py` に `_namespace_source_ids(result, step_id)` ユーティリティ関数を追加（`result.step_id` 補正 + `sources[].source_id` を `{step_id}:{source_id}` に書換）
- [x] **M1-2** `_execute_research` 内の researcher プロンプト組立に `"あなたのステップID: {step_id}"` 行を追加
- [x] **M1-3** `_execute_research` で `_parse_claude_response` 直後に `_namespace_source_ids(result, step_id)` を呼ぶ
- [x] **M1-4** `src/agent/state/dynamodb_client.py:put_sources` から `item["source_id"] = str(uuid.uuid4())` を削除し、`seen_ids` による source_id dedup に差し替える
- [x] **M1-5** `src/agent/prompts/researcher.md` の出力形式セクションを、`step_id` フィールドに与えられた ID を使う旨・source_id は src-001 から付番で可（prefix はシステム側で付ける）旨に更新
- [x] **M1-6** `tests/test_orchestrator.py` に以下のテストを追加:
  - `test_namespace_source_ids_applies_step_id_prefix`
  - `test_namespace_source_ids_corrects_wrong_step_id`
  - `test_namespace_source_ids_handles_missing_sources`
- [x] **M1-7** `tests/test_dynamodb_client.py` の既存 `test_put_sources_*` を「source_id そのまま保存・重複時スキップ」仕様に更新
- [x] **M1-8** `pytest tests/` 全件パス確認

### M2: `_run_review_loop` の修正結果を永続化

- [x] **M2-1** `_run_review_loop` の戻り値型を `tuple[dict, dict]` に変更（全 `return` 文を `return review_result, current_deliverables` 形式に）
- [x] **M2-2** `current_deliverables = _parse_claude_response(fix_raw)` を `parse_error` チェック付きのロジックに置換（parse_error 時は rebind しない + warning ログ）
- [x] **M2-3** `orchestrator.py:352-355` 呼び出し元を `review_result, deliverables = self._run_review_loop(...)` に更新
- [x] **M2-4** "Deliverables updated by review fix" ログを追加（loop, issues_count）
- [x] **M2-5** `tests/test_orchestrator.py` 既存の `_run_review_loop` 関連テストを tuple 戻り値に対応
- [x] **M2-6** 以下のテストを追加:
  - `test_run_review_loop_returns_fixed_deliverables_on_passed`
  - `test_run_review_loop_returns_fixed_deliverables_on_max_loop`
  - `test_run_review_loop_keeps_previous_on_parse_error`
  - `test_run_integrates_fixed_deliverables_into_notion_content`（統合テスト）
- [x] **M2-7** `pytest tests/` 全件パス確認

### M3: コード成果物生成を成果物タイプ別に完全分離

- [x] **M3-1** `src/agent/prompts/generator.md` から `code_files` に関する出力指示・構造化ルールを削除、出力形式を `content_blocks + summary` のみに改訂
- [x] **M3-2** `_build_code_generation_prompt` シグネチャを `code_type: str`(単一) に変更、`code_type_labels` を内部で保持
- [x] **M3-3** `orchestrator.py` の 5〜5b セクションを改訂:
  - 初回 `call_claude(gen_prompt)` の結果 `deliverables` に code_files 期待を削除
  - `code_types` がある場合は常にタイプ別ループで `_build_code_generation_prompt` を呼び、結果を `code_files_merged` と `readme_parts` に蓄積
  - 最後に `deliverables["code_files"] = {"files": code_files_merged, "readme_content": ...}` をセット（空なら設定しない）
- [x] **M3-4** 各タイプ成功時に `"Code files generated"` ログ（code_type, files_count）
- [x] **M3-5** タイプ失敗時に `"Code generation failed for type"` warning
- [x] **M3-6** `tests/test_orchestrator.py` に以下のテスト追加:
  - `test_code_generation_per_type_merges_files`
  - `test_code_generation_partial_failure_keeps_successful_types`
  - `test_generator_no_longer_returns_code_files`（generator.md 改訂検証）
- [x] **M3-7** 既存の code_files fallback テストを「常に独立生成」パスに更新（該当する既存テストはなく、新規パスを上記で網羅）
- [x] **M3-8** `pytest tests/` 全件パス確認（164 passed）

### フェーズ 1 完了条件

- [ ] **P1-1** ユニットテスト全件パス
- [ ] **P1-2** M1/M2/M3 を個別の commit に分割
- [ ] **P1-3** `git push origin main` でデプロイパイプライン起動（※ push の最終承認はユーザー判断）

## フェーズ 2: Should 修正（品質向上）

### S1: レビュアーの検証カバレッジ戦略

- [x] **S1-1** `src/agent/prompts/reviewer.md` に「検証カバレッジ方針」セクションを追加（priority 1 優先 / 最低 30% or 10件 / 上限 15件）
- [x] **S1-2** `reviewer.md` の quality_metadata 出力例に `sources_total` フィールドを追加
- [x] **S1-3** `orchestrator.py:_build_quality_metadata_block` に `sources_total` 表示ロジック追加（`{verified}/{total}` 形式・互換フォールバック付き）
- [x] **S1-4** `tests/test_orchestrator.py::TestBuildQualityMetadataBlock` に sources_total 分岐テスト4件を新規追加

### S2: `published_at` 欠損時のフォールバック

- [x] **S2-1** `src/agent/prompts/researcher.md` に「published_at の扱い」セクションを追加（ISO 8601 / `"unknown"` / `"continuously-updated"`、null 使用禁止）
- [x] **S2-2** `src/agent/prompts/reviewer.md` の `newest_source_date` / `oldest_source_date` 集計ルールを追加（日付以外の値は除外）
- [x] **S2-3** `orchestrator.py` に `_format_freshness_line` を追加し、null / unknown / continuously-updated を「取得日不明のソースが含まれます」表示に切替
- [x] **S2-4** `tests/test_orchestrator.py::TestBuildQualityMetadataBlock` に鮮度表示テスト5件（両側null/キー欠損/unknownマーカー/片側null/正常）を追加

### フェーズ 2 完了条件

- [x] **P2-1** ユニットテスト全件パス（173 passed）
- [x] **P2-2** S1/S2 を個別の commit に分割（S1: `e520885` / S2: `632f448`）

## フェーズ 3: 実機検証

検証対象 execution: `exec-20260418044855-00a18b82`（投入 13:48 / 所要 15:34 / Slack 完了通知あり）

- [x] **V-1** デプロイ後、同一トピック「API GatewayとLambdaの組み合わせについて」を Slack から投入し新 execution を実行
- [x] **V-2** ❌ `storage = "notion"` のみ（`"notion+github"` 不可）。code 生成 parse error で GitHub push が skip された（V-4 と同根）
- [x] **V-3** ✓ 48 件すべて `{step_id}:src-NNN` 形式で重複なし
- [x] **V-4** ⚠ M3 のタイプ別ループは正しく実行されたが、`iac_code` / `program_code` 共に Claude 応答が parse error → `code_files_merged` 空のまま完了。`"Code file generation returned empty result"` 系の旧症状は出ていない
- [x] **V-5** ✓ S1/S2 改修内容は反映:
  - 検証ステータス `sources_total=49 / sources_verified=10`（10/49 形式で表示）
  - 鮮度 `2024-06-04 〜 2024-07-18`（ISO 日付で表示、`N/A` 解消）
  - src-001 重複なし
- [x] **V-6** ❌ GitHub リポジトリに新規ディレクトリ無し（V-2/V-4 の影響）
- [x] **V-7** ✓ ユーザー確認済み（Notion ページ本文 `[src-XXX]` と出典一覧 source_id が一致）
- [x] **V-8** AC 評価結果を下表に記録

### V-8 受け入れ条件評価（requirements.md AC-1〜AC-7）

| AC | 評価 | 根拠 |
|----|------|------|
| AC-1 検証カバレッジ向上 | ✓ | sources_verified=10（修正前 3）。reviewer.md の検証カバレッジ方針が機能 |
| AC-2 storage=notion+github | ❌ | code 生成（iac_code/program_code）が Claude 応答 parse error で失敗、GitHub push skip |
| AC-3 source_id 一意化 | ✓ | 48 件全て一意、`{step_id}:src-NNN` 形式統一（M1 修正反映） |
| AC-4 review fix 反映 | N/A | 今回 review が初回 pass、fix loop 未発火のため M2 修正の動作確認は未実施 |
| AC-5 全テストパス | ✓ | 173 tests pass（push 時点） |
| AC-6 sources_total 表示 | ✓ | Slack 通知に `10/49` 表示（S1 修正反映） |
| AC-7 鮮度フォールバック | ✓ | ISO 日付で newest/oldest 表示（S2 修正反映） |

**総括**: Must AC（AC-1〜AC-5）は AC-2 が未充足、それ以外は達成。AC-2 失敗は M3 の構造変更後に顕在化した「code 生成の parse error」という新症状で、今回の quality-fix スコープ（応答サイズ超過対策）とは別の root cause。AC-4 は fix loop が未発火だったため次回 fix 発生時に再評価する。

## フェーズ 4: 後続（保留判断）

- [ ] **N1** （任意）`fix_prompt` の差分修正化を実装。今回 fix loop 未発火のため判断保留（次回 fix 発生時に効果測定）
- [ ] **Followup-A** AC-2 未達（code 生成 parse error）に対する新 steering 起票。`.steering/[YYYYMMDD]-code-generation-parse-error/` で iac_code / program_code 応答の安定化を扱う
- [ ] **Followup-B** AC-4（review fix 反映）の追検証。次回 fix loop 発火時に deliverables への修正反映を確認

## 完了条件（全体）

- フェーズ 1 の P1-1〜P1-3 全達成 ✓
- フェーズ 2 の P2-1〜P2-2 全達成 ✓
- フェーズ 3 の V-1〜V-8 全実施完了 ✓（V-2/V-6 失敗、V-4 警告、AC-2 未達）
- requirements.md の受け入れ条件 AC-1〜AC-5（Must）: AC-2 未達のため未充足。Followup-A で別 steering へ移管

## リスクと対処

| リスク | 対処 |
|--------|------|
| M3 後、タイプ別 Claude CLI 呼び出しでも応答サイズ超過する | 単一タイプでも失敗する場合は、該当タイプのみ warning 出力しつつ他タイプは生成継続（部分失敗許容）|
| M1 で researcher が step_id prefix 指示を無視し、source_id を `{step_id}:src-001` 形式で返してしまう | `_namespace_source_ids` の実装で「既に prefix 付きなら再付与しない」分岐を追加 |
| M2 で修正後 deliverables が broken JSON の場合 | parse_error 時に current_deliverables を rebind しないロジックを M2-2 で実装済み |
| 実機検証時に別の新症状が発生する | 新 steering を別途起票し、quality-fix と分離して対処 |
