# タスクリスト: コード生成のファイルシステム書き込み方式への再設計

作成日: 2026-04-25
対応 requirements: `.steering/20260425-code-gen-redesign-filesystem/requirements.md`
対応 design: `.steering/20260425-code-gen-redesign-filesystem/design.md`

## 完了条件

- 全タスクが `[x]` になる
- `pytest tests/` で全件パス（既存 + 新規）
- main へのマージ可能な状態（commit 済み）
- 実機デプロイと実機検証はユーザー判断（T10 で扱う、本 tasklist では push まで）

---

## T1. 実装前調査 [ ]

### T1.1 既存 `call_claude` 周辺の確認 [ ]
- `src/agent/orchestrator.py` 1-160 行目あたりの定数・ヘルパー配置を Read し、新規ヘルパー `call_claude_with_workspace` を挿入する位置を決める
- `MAX_CLAUDE_RETRIES` 等の既存定数を再利用するか確認

### T1.2 削除対象関数の参照箇所確認 [ ]
- `_normalize_code_files_payload` の呼び出し元を grep で全て発見
- `_build_code_failure_diagnostics` の呼び出し元を grep で全て発見
- `_RESERVED_META_KEYS` / `_FILE_KEY_RATIO_THRESHOLD` / `_FILE_KEY_MIN_COUNT` / `_CODE_FAILURE_PREVIEW_LIMIT` / `_CODE_FAILURE_TOP_KEYS_LIMIT` の参照箇所も確認
- 削除可能か（コード生成パスのみで使用か）を判定

### T1.3 既存テストへの影響確認 [ ]
- `tests/unit/agent/test_orchestrator.py` で `_normalize_code_files_payload` / `_build_code_failure_diagnostics` を直接テストしているケースを発見
- 削除と同時にテストも削除する必要があるか確認

### T1.4 Slack 通知メソッドの確認 [ ]
- `src/agent/notify/slack_client.py` の `post_progress` のシグネチャを Read
- 既存メソッドで部分失敗通知に使えるか、新規メソッドが必要か判断

---

## T2. 新規実装 [ ]

### T2.1 定数追加 [ ]
- `src/agent/orchestrator.py` モジュール冒頭に追加:
  - `_MAX_FILE_BYTES = 100 * 1024`（NF2.4 ファイルサイズ上限）
  - `_WORKSPACE_STDOUT_PREVIEW_LIMIT = 500`（失敗時 stdout プレビュー長）

### T2.2 `_collect_workspace_files` の実装 [ ]
- design.md `3.3` の通り
- `is_symlink()` チェックを最初に実施（symlink 全拒否）
- `resolve(strict=True)` + `relative_to(sandbox_resolved)` で sandbox 外を二重チェック
- ホワイトリストは既存 `_looks_like_file_path` を流用
- サイズ上限 `_MAX_FILE_BYTES`、UTF-8 デコード失敗の破棄
- 戻り値: `(files: dict[str, str], rejected: list[dict])`

### T2.3 `_classify_workspace_outcome` の実装 [ ]
- design.md `3.4` の通り
- 戻り値: `{"files_kind": "valid|all_empty|no_recognized|none", "files_count", "files_total_bytes", "rejected"}`
- 判定優先順位: `files_count == 0 && rejected` → `no_recognized` → `files_count == 0` → `none` → `total_bytes == 0` → `all_empty` → それ以外 → `valid`

### T2.4 `call_claude_with_workspace` の実装 [ ]
- design.md `3.2` の通り
- `tempfile.mkdtemp(prefix=f"agent-output-{code_type}-")` で sandbox 作成
- `subprocess.run(["claude", "-p", "-", "--model", model, "--allowedTools", "Write,Edit", "--output-format", "json"], cwd=sandbox, ...)`
- `MAX_CLAUDE_RETRIES` 回までリトライ（既存 `call_claude` と同等のリトライロジック）
- `finally` で `shutil.rmtree(sandbox, ignore_errors=True)`
- 戻り値: `(raw_stdout: str, files: dict[str, str], outcome: dict)`

### T2.5 `_build_code_generation_prompt` の書き換え [ ]
- design.md `3.1` のテンプレートに置き換え
- 「Write ツールでファイルを書き出すこと」「相対パスのみ」「最大 5 ファイル」「README.md 含めてよい」「PoC 品質コメント」等を明示
- 既存の `_CODE_TYPE_LABELS` を流用

### T2.6 オーケストレーターのコード生成ループ書き換え [ ]
- `src/agent/orchestrator.py:469-517` 付近を design.md `3.5` の実装に置き換え
- `failed_code_types` リストを導入し、ループ後に Slack 部分失敗通知を投稿
- `code_files_merged` / `readme_parts` の集約パターンは維持
- README.md は `files.pop("README.md", None)` で分離して `readme_parts` に追加

### T2.7 自己チェック [ ]
- `python3 -c "import ast; ast.parse(open('src/agent/orchestrator.py').read())"` で構文確認
- import に `shutil`, `tempfile`, `from pathlib import Path` が含まれているか確認

---

## T3. 旧コード削除 [ ]

### T3.1 `_normalize_code_files_payload` 削除 [ ]
- T1.2 で確認した呼び出し元が全て新方式に置き換わっていることを再確認
- 関数本体を削除

### T3.2 `_build_code_failure_diagnostics` 削除 [ ]
- 同上

### T3.3 関連定数の削除 [ ]
- `_RESERVED_META_KEYS`, `_FILE_KEY_RATIO_THRESHOLD`, `_FILE_KEY_MIN_COUNT`, `_CODE_FAILURE_PREVIEW_LIMIT`, `_CODE_FAILURE_TOP_KEYS_LIMIT` のうち T1.2 で「コード生成パスのみで使用」と判定したものを削除
- `_looks_like_file_path` は新方式 `_collect_workspace_files` で使い続けるので残す
- `_FILE_EXTENSIONS` / `_FILENAME_EXACT` も同上

### T3.4 既存テストの整理 [ ]
- T1.3 で発見した「削除対象関数を直接テストしているケース」を削除
- 同様に削除されたケースが他のテストに影響しないか確認

---

## T4. テスト追加 [ ]

### T4.1 `Test_collect_workspace_files` 追加 [ ]
`tests/unit/agent/test_orchestrator.py` に追加:
- `test_collects_whitelisted_files`
- `test_rejects_unknown_extension`
- `test_rejects_oversized_file`
- `test_rejects_non_utf8`
- `test_rejects_symlink_to_outside`
- `test_rejects_symlink_to_inside`
- `test_returns_empty_when_sandbox_empty`
- `test_handles_subdirectory`

すべて `tmp_path` fixture を使用。bytes 書き込みでバイナリケースも作成。

### T4.2 `Test_classify_workspace_outcome` 追加 [ ]
- `test_valid_when_files_have_content`
- `test_all_empty_when_files_have_zero_bytes`
- `test_no_recognized_when_only_rejected`
- `test_none_when_nothing_written`

### T4.3 `Test_call_claude_with_workspace` 追加 [ ]
- `test_creates_and_cleans_sandbox` — sandbox が作成され、終了後に削除される
- `test_passes_cwd_to_subprocess` — `subprocess.run` の `cwd` が sandbox を指す
- `test_includes_write_in_allowed_tools` — `--allowedTools Write,Edit` が cmd に含まれる
- `test_returns_collected_files` — fake subprocess + 事前に書き込んだファイルで files が返る
- `test_retries_on_subprocess_error` — CalledProcessError 後にリトライする
- `test_cleans_sandbox_on_exception` — 例外時も sandbox が削除される

mock 戦略: `subprocess.run` を `side_effect` で「実際にファイルを書く fake 関数」に差し替え、cwd を見て書き込む

### T4.4 オーケストレーター統合テスト追加 [ ]
- `test_orchestrator_uses_workspace_mode_for_code_generation`
- `test_orchestrator_skips_code_files_on_workspace_failure`
- `test_orchestrator_posts_slack_warning_on_partial_failure`
- `test_orchestrator_posts_slack_warning_on_full_failure`
- `test_orchestrator_no_slack_warning_on_success`

`call_claude_with_workspace` を `unittest.mock.patch` で差し替え、戻り値を制御

### T4.5 既存テスト非回帰確認 [ ]
- `test_run_review_loop_*` 系のテストが影響を受けていないこと（4/23 修正の `_PRESERVED_DELIVERABLE_FIELDS` が壊れていない）
- `test_setup_claude_credentials` 系（4/25 認証改修）の非回帰

---

## T5. テスト実行 [ ]

### T5.1 orchestrator テストの単体実行 [ ]
```bash
pytest tests/unit/agent/test_orchestrator.py -v
```
全件パスすることを確認

### T5.2 全テスト実行 [ ]
```bash
pytest tests/
```
229 件 + 新規追加分が全件パスすることを確認

---

## T6. 永続ドキュメント更新 [ ]

### T6.1 `docs/architecture.md` 更新 [ ]
- 「成果物の性質的差異と JSON 適性」セクションを追加（requirements.md `1.3` の比較表をベース）
- コード成果物のみ例外設計を採る根拠を明記
- 過去の繰り返し修正（commit 5 件）への参照を追加

### T6.2 `docs/functional-design.md` 更新 [ ]
- ジェネレーターセクションに「コード成果物はファイル書き込み方式（`call_claude_with_workspace`）」を追記
- 失敗時の Slack 通知の挙動を追記

### T6.3 旧 steering への注記（任意） [ ]
- `.steering/20260418-code-generation-parse-error/` の冒頭に「2026-04-25 に根本設計を変更したため、本 steering で対応した内容は新方式（ファイル書き込み）に置き換わった」旨を追記
- 完了済み tasklist は残す（履歴として有用）

---

## T7. コミット & push [ ]

### T7.1 差分確認 [ ]
```bash
git status
git diff --stat
```

### T7.2 ステージング & コミット [ ]
- 対象ファイルを明示的に `git add`（`-A` / `.` は使わない）
- 対象:
  - `src/agent/orchestrator.py`
  - `tests/unit/agent/test_orchestrator.py`
  - `docs/architecture.md`
  - `docs/functional-design.md`
  - `.steering/20260425-code-gen-redesign-filesystem/{requirements,design,tasklist}.md`
  - 任意: `.steering/20260418-code-generation-parse-error/` への注記
- コミットメッセージ例:
  ```
  feat: switch code generation to filesystem write mode

  Replace JSON-string-encoded code returns with Claude Code Write tool
  invocations into a per-call sandbox. Eliminates JSON escape failures
  for HCL/Python code that have plagued code generation through 5
  prior fix attempts (ed148f6, d8589e7, d951ecc, 5e77dd3, f0b121a).

  - New helpers: call_claude_with_workspace, _collect_workspace_files,
    _classify_workspace_outcome
  - Slack notification on code-gen partial/full failure
  - Remove _normalize_code_files_payload, _build_code_failure_diagnostics
  ```

### T7.3 main への push [ ]
```bash
git push origin main
```

---

## T8. 本スコープ外（ユーザー判断で実施） [ ]

### T8.1 GitHub Actions ビルド確認 [ ]
- `gh run list --workflow=build-agent.yml --limit 3` で新コードのビルドが成功していることを確認
- 完了まで数分待つ

### T8.2 実機検証 1: コード成果物を含む投入 [ ]
- Slack に `@Catch Expander AWSのCloud Front` を投入
- CloudWatch Logs `/ecs/catch-expander-agent` で以下を確認:
  - `Generating code files for type (workspace mode)` INFO
  - `Code files generated (workspace mode) | files_count=N` INFO
- `catch-expander-code` リポジトリに新ディレクトリが push されることを確認
- Notion に github_url が埋め込まれることを確認

### T8.3 実機検証 2: コードを含まない投入 [ ]
- Slack に時事問題系のトピック（例: `@Catch Expander 最近のAI業界の動向`）を投入
- CloudWatch Logs で従来通りテキスト成果物パスのみ走ることを確認:
  - `Generating code files for type (workspace mode)` ログが**出ない**
- Notion に投稿されることを確認

### T8.4 失敗ケース確認（任意） [ ]
- もし Claude が Write ツールを使わずに失敗したケースが発生した場合、Slack に部分失敗通知が来ることを確認
- CloudWatch Logs で `Code generation failed (workspace mode) | files_kind=...` が出ることを確認

---

## メモ

### 設計選択の確認

- **README.md の取り扱い**: `files.pop("README.md")` で分離し、既存の `readme_content` フィールドにマージ（design.md `3.5`）
- **symlink 全拒否**: `is_symlink()` で早期拒否 + `resolve` で二重防御（design.md `3.3`）
- **旧コードは本タスクで削除**: `_normalize_code_files_payload` / `_build_code_failure_diagnostics` を削除し、デッドコード化を回避
- **自動再試行なし**: 失敗時は構造化ログ + Slack 通知のみ。再投入はユーザー判断

### 過去の失敗からの学び

- 「parse error にパーサーで対処する」を 5 回繰り返したが解決しなかった
- 観測点（パース失敗）と根本原因（LLM が JSON エスケープを完璧にやり続けるのは確率的に困難）が乖離していた
- 統一インターフェース（全 step を JSON）の美しさを優先しすぎて、コード成果物の性質的差異を軽視していた
- 今後は「同じ問題が 2 回再発したらゼロベース見直し」を運用ルールとする（feedback memory に保存）

### 注意事項

- 本タスクは `.steering/20260425-auth-redesign-aipapers/` の認証改修と独立。両方デプロイ済みの状態で動作確認すること
- ECS task definition は `:latest` タグを参照しているので、GitHub Actions ビルド完了 = 次回 RunTask で新コードが使われる
