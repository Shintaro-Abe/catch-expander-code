# タスクリスト: コード成果物生成の失敗修正

## 方針

- Phase A（観測強化）を独立した commit で先に push → 1 回の Slack 投入で root cause を確定
- 観測結果に応じて Phase B のブランチ（B-1〜B-4）を選択し、別 commit で実装
- AC-3 実機検証は 2 回連続成功を必須（間欠失敗の排除）
- 観測結果が R1〜R5 のいずれにも当てはまらない場合は design.md に root cause 種別を追記してから Phase B に進む

## フェーズ A: 観測強化

### A1: 診断ヘルパー追加

- [x] **A1-1** `src/agent/orchestrator.py` に `_build_code_failure_diagnostics(raw: str, parsed: object) -> dict` を追加
- [x] **A1-2** `files_kind` 判定ロジック実装（`missing` / `dict` / `list` / `<not-dict>` / 型名）
- [x] **A1-3** `top_level_keys` を最大 10 件に制限（`_CODE_FAILURE_TOP_KEYS_LIMIT`）
- [x] **A1-4** `response_preview` を最大 500 文字に切り詰め（`_CODE_FAILURE_PREVIEW_LIMIT`）

### A2: 警告メッセージ書換

- [x] **A2-1** `Code generation failed for type` warning をメッセージ本文埋込形式に書換（`%r` でエスケープ）
- [x] **A2-2** メッセージ本文に 8 項目（execution_id / code_type / parse_error / files_kind / files_count / top_level_keys / response_chars / response_preview）が含まれることを確認

### A3: テスト追加

- [x] **A3-1** `tests/unit/agent/test_orchestrator.py` に `TestBuildCodeFailureDiagnostics` を追加（7 テスト）
- [x] **A3-2** `pytest tests/` 全件パス確認（180 passed = 173 + 7）

### A4: ドキュメント追記

- [x] **A4-1** `docs/development-guidelines.md` の「3. エラーハンドリング規約」配下に「コード生成失敗ログの読み方」を新設し、診断項目と root cause 判定の手がかりを表形式で追記

### フェーズ A 完了条件

- [ ] **PA-1** ユニットテスト全件パス
- [ ] **PA-2** A1〜A4 を Phase A 単一 commit に集約
- [ ] **PA-3** `git push origin main` で GitHub Actions build-agent.yml 経由デプロイ
- [ ] **PA-4** 同一トピック「API GatewayとLambdaの組み合わせについて」を Slack から 1 回投入し新 execution を実行
- [ ] **PA-5** CloudWatch Logs から `Code generation failed for type` 警告メッセージを取得し、root cause を R1〜R5 のいずれかに分類
- [ ] **PA-6** R1〜R5 に該当しない場合は design.md に root cause 種別を追記して Phase B 着手前に再ドラフト

## フェーズ B: root cause 別本対応

PA-5 の分類結果に応じて該当ブランチのみ実装（複数該当時は順次適用）。

### B-1: 戦略追加（root cause = R1）

- [ ] **B1-1** Phase A で観測した応答パターンから `_parse_claude_response` の戦略 1〜4 で拾えなかった具体例を tasklist の `Phase A 観測ログ` に追記
- [ ] **B1-2** 観測例を pass する追加戦略（戦略 5）を `_parse_claude_response` に実装。`files` キーを正規表現で抽出するフォールバックも検討
- [ ] **B1-3** 観測応答を入力とするテストを `test_orchestrator.py` に追加（実応答ベースの fixture）
- [ ] **B1-4** 既存の戦略 1〜4 関連テストが pass し続けることを確認

### B-2: スキーマ正規化（root cause = R2 or R3）

- [x] **B2-1** `src/agent/orchestrator.py` に `_normalize_code_files_payload(parsed)` と `_looks_like_file_path(key)` を追加。トップレベル `*.tf` / `*.py` 直接配置を `{"files": dict, "readme_content": str}` に変換
- [x] **B2-2** コード生成ループ内 `code_result = _parse_claude_response(code_raw)` 直後に正規化を挟む
- [x] **B2-3** 正規化はコード生成パスに限定（テキスト成果物パスへ波及させない）。`_parse_claude_response` 自体は無変更
- [x] **B2-4** ユニットテスト 11 件追加（`TestNormalizeCodeFilesPayload`）。観測例 / readme 保持 / 既存 wrapped 形式 passthrough / 誤検知抑制（少数派ファイルキー / 単一ファイル / 値が dict）/ ヘルパー個別テスト
- [x] **B2-5（追加）** B-2 push 前の検証実行で Notion 400 失敗が新規発覚。同 commit に `notion_client.py` の `Notion API client error` / `Notion API server error` ログを Phase A 同様の本文埋込形式に書換（method / url / status / response_body）。AC-3 検証時に Notion 失敗 root cause も同時観測する

### B-3: プロンプト改訂（root cause = R4）

- [ ] **B3-1** `_build_code_generation_prompt` の出力例にダミーファイル（例: `main.tf`）を 1 つ含めて空応答を抑制
- [ ] **B3-2** code_type 別に出力ファイル名サンプルを変える（iac_code: `main.tf` / program_code: `main.py`）
- [ ] **B3-3** 空応答時に 1 回だけ retry する分岐を追加するか判断（過剰なら見送り）
- [ ] **B3-4** 修正版プロンプトでの想定応答に対するテストを追加

### B-4: 分割粒度引き下げ（root cause = R5）

- [ ] **B4-1** `_build_code_generation_prompt` の「ファイル数は最大5ファイルに抑える」を「最大3ファイル」に縮小、または `code_types` の各タイプを「main / variables / outputs / README」等に細分化
- [ ] **B4-2** 細分化を選んだ場合は `code_types` 展開ロジックと `code_files_merged` 集約ロジックを更新
- [ ] **B4-3** 細分化に伴うテスト更新（生成回数 / merge 結果）

### フェーズ B 共通

- [ ] **PB-1** 適用ブランチごとに独立 commit
- [ ] **PB-2** ユニットテスト全件パス
- [ ] **PB-3** `git push origin main` でデプロイ

## フェーズ C: 実機検証

- [ ] **V-1** Phase B push 後、同一トピック「API GatewayとLambdaの組み合わせについて」を Slack から **2 回** 投入（間欠失敗排除）
- [ ] **V-2** 両 execution の DynamoDB `workflow-executions.storage` が `"notion+github"` であることを確認
- [ ] **V-3** GitHub catch-expander-code リポジトリに新 execution 向けディレクトリが 2 件作成され、IaC / プログラムコードファイルが push されていることを確認
- [ ] **V-4** CloudWatch Logs に `Code generation failed for type` warning が出ないこと（または部分失敗のみで他方が成功）を確認
- [ ] **V-5** quality-fix の AC-2 を「達成」に更新するため `.steering/20260418-quality-fix/tasklist.md` の AC-2 行を ✓ に書き換え、本 steering 完了を理由として記録

## 完了条件（全体）

- フェーズ A の PA-1〜PA-5 全達成（PA-6 は条件付き）
- フェーズ B の該当ブランチが PB-1〜PB-3 達成
- フェーズ C の V-1〜V-5 全達成
- requirements.md の AC-1〜AC-3（Must）が全て充足
- AC-4（回帰テスト）/ AC-5（ドキュメント追記）が達成

## Phase A 観測ログ（PA-5 完了）

| 項目 | 値 |
|------|-----|
| 観測日時 | 2026-04-18 07:46-07:50 UTC（16:46-16:50 JST）|
| execution_id | `exec-20260418073748-2c27bb2f` |
| iac_code root cause | **R2（スキーマ違い）** `parse_error=False` / `files_kind=missing` / トップレベルキーがファイル名（`bin/app.ts` 等）|
| program_code root cause | **R2（スキーマ違い）** `parse_error=False` / `files_kind=missing` / トップレベルキーがファイル名（`app.py` 等）|
| 適用予定ブランチ | **B-2（スキーマ正規化）** 単独。B-1/B-3/B-4 不要 |
| 観測した top_level_keys（iac_code） | `['bin/app.ts', 'lib/api-lambda-stack.ts', 'lambda/handler.ts', 'package.json', 'cdk.json']` |
| 観測した top_level_keys（program_code） | `['app.py', 'stacks/api_gateway_lambda_stack.py', 'lambda/handler.py', 'lambda/authorizer.py', 'requirements.txt']` |
| 応答サイズ | iac_code: 17,396 chars / program_code: 25,828 chars（サイズ要因ではない）|

**判定根拠:** 両 code_type とも JSON パースは成功しており、Claude が `{"files": {...}, "readme_content": ...}` 形式ではなく、トップレベルにファイルパスをキーとして直接配置した dict を返した。プロンプトの出力形式例（`{"files": {"ファイルパス": "ファイル内容"}}`）を Claude が省略形に解釈した可能性が高い。

## リスクと対処

| リスク | 対処 |
|--------|------|
| Phase A 観測結果が R1〜R5 に該当しない | PA-6 に従い design.md へ root cause 種別を追記してから Phase B 着手 |
| Phase B 適用後も間欠的に失敗 | V-1 で 2 回連続成功を必須化。失敗が再現する場合は別ブランチを追加適用 |
| Phase A 観測のために投入した execution が別の問題で失敗（OAuth 切れ等） | 失敗時は OAuth トークン同期状態を確認後に再投入。観測対象は warning が出る execution に限る |
| `response_preview` に秘匿情報が混入 | 現状の Slack 投入トピックは公開技術トピック前提。センシティブ用途拡張時は別途 preview 制限を導入 |
