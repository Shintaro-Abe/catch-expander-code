# 設計書: コード生成のファイルシステム書き込み方式への再設計

作成日: 2026-04-25
ステータス: ドラフト（承認待ち）
対応 requirements: `.steering/20260425-code-gen-redesign-filesystem/requirements.md`

---

## 1. 設計方針

### 1.1 基本ルール

- **影響範囲をコード生成パスに限定**: テキスト成果物・トピック解析・WF 設計・リサーチャー・レビュアーの呼び出しは無変更
- **Claude CLI ヘルパー二系統化**: 既存 `call_claude` は維持し、コード生成専用の新ヘルパー `call_claude_with_workspace` を追加
- **Sandbox は呼び出しごとに使い捨て**: `tempfile.mkdtemp` で `/tmp/agent-output-<random>/` を作成、`finally` で削除
- **ホワイトリスト主義 + symlink 全拒否**: 拡張子・ファイル名は既存の `_FILE_EXTENSIONS` / `_FILENAME_EXACT` を流用。symlink は早期に拒否（コード成果物に symlink を含む正当な理由がない）
- **失敗時は空のまま完走 + Slack 部分失敗通知**: 自動再試行はしない（既存レビューループに委ねる）。失敗時は構造化ログ + Slack スレッドへ「コード生成失敗」を可視化する（ユーザーが GitHub プッシュ漏れに気づける）
- **既存の `_PRESERVED_DELIVERABLE_FIELDS`（`code_files`）は維持**: 4/23 修正のレビューループ保護を壊さない
- **旧 JSON パース系ヘルパーは本タスクで削除**: `_normalize_code_files_payload` / `_build_code_failure_diagnostics` は新方式で不要。デッドコード化を避けるため即削除（git history で復元可能）

### 1.2 新方式のコール構造

```
call_claude_with_workspace(prompt, code_type)
  │
  ├── 1. tempfile.mkdtemp("/tmp/agent-output-<code_type>-")
  │     → /tmp/agent-output-iac_code-xK3p9q/
  │
  ├── 2. subprocess.run(
  │       ["claude", "-p", "-", "--model", "sonnet",
  │        "--allowedTools", "Write,Edit",
  │        "--output-format", "json"],
  │       input=prompt,
  │       cwd=<sandbox_dir>,        ← cwd を sandbox に固定
  │       capture_output=True,
  │       check=True,
  │     )
  │
  ├── 3. _collect_workspace_files(sandbox_dir)
  │     → {"main.tf": "...", "variables.tf": "..."}
  │
  ├── 4. _classify_workspace_outcome(collected, raw_stdout)
  │     → {"files_kind": "valid|all_empty|...", "files": {...}}
  │
  └── 5. shutil.rmtree(sandbox_dir, ignore_errors=True)  ← finally
```

---

## 2. 全体アーキテクチャ

### 2.1 移行前後の比較

```
=== 移行前（現状） ===

[ Claude CLI に JSON で返させる方式 ]

  call_claude(prompt) ──→ stdout (str: 外側 JSON)
                          │
                          └─ "result": "```json\n{\"files\":{\"waf.tf\":\"resource\\\"...\"}}\n```"
                                              ↑ ここで HCL/Python のエスケープが
                                                LLM 任せ。8000+ 文字で確率的に壊れる

  _parse_claude_response(stdout)
    └─ 4 戦略でフォールバックパース → 失敗 → {"raw_text": ..., "parse_error": True}

  → code_files 空のまま GitHub push スキップ

=== 移行後（本設計） ===

[ Claude が Write ツールで直接書く方式 ]

  call_claude_with_workspace(prompt, code_type)
    │
    ├─ sandbox = mkdtemp(...)
    ├─ subprocess.run(["claude", "-p", "--allowedTools", "Write,Edit", ...], cwd=sandbox)
    │    └─ Claude が Write/Edit で sandbox 内にファイルを書く（JSON エスケープ層なし）
    │
    ├─ files = _collect_workspace_files(sandbox)
    │    ├─ os.walk + ホワイトリスト + サイズ上限
    │    └─ 想定外パス検出時は警告ログ + 破棄
    │
    └─ outcome = _classify_workspace_outcome(files, stdout)
         ├─ files_kind: "valid" → そのまま採用
         ├─ files_kind: "all_empty" → 失敗扱い + 構造化ログ
         ├─ files_kind: "no_recognized" → 失敗扱い + 構造化ログ
         └─ files_kind: "none" → 失敗扱い + 構造化ログ

  → code_files に書き込み or 空のまま完走
```

### 2.2 コンポーネント変更マトリクス

| コンポーネント | 変更種別 | 主な変更内容 |
|--------------|--------|--------------|
| `src/agent/orchestrator.py` `call_claude` | 無変更 | 既存ヘルパーは温存 |
| `src/agent/orchestrator.py` `_build_code_generation_prompt` | **書き換え** | Write ツール経由のプロンプトに変更 |
| `src/agent/orchestrator.py` `call_claude_with_workspace` | **新規追加** | sandbox 管理 + Claude 呼び出し |
| `src/agent/orchestrator.py` `_collect_workspace_files` | **新規追加** | os.walk + ホワイトリスト |
| `src/agent/orchestrator.py` `_classify_workspace_outcome` | **新規追加** | 失敗判定 |
| `src/agent/orchestrator.py` コード生成ループ（:469-517） | **書き換え** | `_parse_claude_response` 経由を `call_claude_with_workspace` 経由に |
| `src/agent/orchestrator.py` `_normalize_code_files_payload` | **削除** | 新方式では不要（JSON パース層自体がなくなる） |
| `src/agent/orchestrator.py` `_build_code_failure_diagnostics` | **削除** | 新方式の `_classify_workspace_outcome` が同等の役割を果たす |
| `src/agent/orchestrator.py` 関連定数 (`_RESERVED_META_KEYS`, `_FILE_KEY_RATIO_THRESHOLD`, `_FILE_KEY_MIN_COUNT`, `_CODE_FAILURE_PREVIEW_LIMIT`, `_CODE_FAILURE_TOP_KEYS_LIMIT`) | **削除候補** | 上記 2 関数のみで使われている定数を削除。流用可能なら残す |
| `src/agent/notify/slack_client.py` | **追記** | コード生成部分失敗を通知するメソッド（既存 `post_progress` を流用 or 専用追加） |
| `tests/unit/agent/test_orchestrator.py` | **追加** | 新規ヘルパーの単体テスト |
| `docs/architecture.md` | **追記** | コード成果物の例外設計を明記 |
| `docs/functional-design.md` | **追記** | 同上 |

---

## 3. 詳細設計

### 3.1 新規プロンプトテンプレート（F1）

`_build_code_generation_prompt` を以下の方針で書き換え:

```python
def _build_code_generation_prompt(
    topic: str,
    category: str,
    research_results: list[dict],
    profile_text: str,
    code_type: str,
) -> str:
    requested_type = _CODE_TYPE_LABELS.get(code_type, code_type)

    research_summary = "\n\n".join(
        f"### {r.get('step_id', 'unknown')}\n{r.get('summary', '')}"
        for r in research_results
        if not r.get("error") and not r.get("parse_error") and r.get("summary")
    ) or "（調査結果なし）"

    return (
        "# コード成果物生成（ファイル書き込み方式）\n\n"
        "## 依頼\n\n"
        "以下のトピックに関するコード成果物を **現在の作業ディレクトリに直接ファイルとして書き出してください**。\n"
        f"トピック: {topic}\n"
        f"カテゴリ: {category}\n"
        f"ユーザープロファイル:\n{profile_text}\n\n"
        "## 生成するコード種別\n\n"
        f"- {requested_type}\n\n"
        "このプロンプトでは上記 **1種類のみ** を生成してください。\n\n"
        "## 調査結果サマリー\n\n"
        f"{research_summary}\n\n"
        "## 出力指示（厳守）\n\n"
        "- **Write ツールでファイルを書き出すこと**。テキストレスポンスとしてコードを返さない\n"
        "- ファイルパスは **相対パスのみ**（例: `main.tf`、`modules/cloudfront/main.tf`）\n"
        "  - 絶対パス（`/...`）や `..` を含むパスは禁止。書こうとしても破棄される\n"
        "- ファイル数は最大 5 まで\n"
        "- 1 ファイルあたり 100 KB 以下に抑える\n"
        "- README.md を 1 ファイル含めてよい（リポジトリで紹介するための簡潔な説明）\n"
        "- すべてのファイル冒頭にコメントで「PoC 品質」である旨を明示\n"
        "- ハードコードされたシークレット・認証情報を含めない\n"
        "- プロファイルがない場合は AWS + Python（または Terraform）を標準とする\n"
        "- コードは機能的なスケルトン（実装の骨格）として提供。詳細な業務ロジックは省略可\n\n"
        "## 完了の合図\n\n"
        "全ファイルを書き終えたら、レスポンステキストには **書き出したファイル一覧のみ** を返してください\n"
        "（例: `Wrote: main.tf, variables.tf, README.md`）。コード本文は返さなくて構いません。\n"
    )
```

設計判断:
- 「ファイル書き込み」を最優先で指示（タイトル + 出力指示の両方）
- 絶対パス・`..` を明示的に禁止し、エージェント側でも破棄することを宣言（プロンプトと実装の両方で防御）
- ファイル数・サイズ上限をプロンプトでも明示（実装側でも検証）
- 「Wrote: ...」の合図はあくまで補助。**実装側はファイルシステムを真とする**（レスポンステキストに依存しない）

### 3.2 `call_claude_with_workspace` の実装（F2）

```python
import shutil
import tempfile
from pathlib import Path

# 既存の MAX_CLAUDE_RETRIES 等を流用

def call_claude_with_workspace(
    prompt: str,
    code_type: str,
    *,
    model: str = "sonnet",
) -> tuple[str, dict[str, str], dict]:
    """Claude CLI に Write ツールを許可してファイルを書き出させ、収集結果を返す。

    Returns:
        (raw_stdout, files, outcome) のタプル。
        - raw_stdout: Claude CLI の生 stdout（診断ログ用）
        - files: {相対パス: 内容} の dict（ホワイトリスト通過分のみ）
        - outcome: {"files_kind": "valid|all_empty|no_recognized|none", "rejected": [...]}
    """
    sandbox = Path(tempfile.mkdtemp(prefix=f"agent-output-{code_type}-"))
    try:
        cmd = [
            "claude", "-p", "-",
            "--model", model,
            "--allowedTools", "Write,Edit",
            "--output-format", "json",
        ]
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(MAX_CLAUDE_RETRIES):
            try:
                result = subprocess.run(  # noqa: S603
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=str(sandbox),
                )
                raw_stdout = result.stdout
                break
            except subprocess.CalledProcessError as e:
                last_error = e
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "Claude CLI error (workspace mode), retrying | rc=%s | stderr=%s",
                    e.returncode,
                    (e.stderr or "")[:500],
                    extra={"attempt": attempt + 1, "wait_seconds": wait, "code_type": code_type},
                )
                time.sleep(wait)
        else:
            if last_error:
                raise last_error
            msg = "Unexpected: no error and no response (workspace mode)"
            raise RuntimeError(msg)

        files, rejected = _collect_workspace_files(sandbox)
        outcome = _classify_workspace_outcome(files, rejected)
        return raw_stdout, files, outcome
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
```

設計判断:
- `for/else` パターンで「成功時 break / 全 retry 失敗時 else」を明確化
- `cwd=str(sandbox)` で Claude CLI の作業ディレクトリを sandbox に固定
- `finally` で必ず sandbox を削除（成功・失敗・例外・外部 kill すべて）
- 既存 `call_claude` のリトライロジックと同等の挙動を維持
- model は既存の `call_claude` のデフォルト（`sonnet`）に揃える

### 3.3 `_collect_workspace_files` の実装（F3）

```python
_MAX_FILE_BYTES = 100 * 1024  # 100 KB（NF2.4）

def _collect_workspace_files(sandbox: Path) -> tuple[dict[str, str], list[dict]]:
    """sandbox ディレクトリ内のファイルを収集し、ホワイトリストでフィルタする。

    Returns:
        (files, rejected) のタプル。
        - files: 採用されたファイル {相対パス: 内容}
        - rejected: 破棄されたエントリ [{"path": ..., "reason": ...}]
    """
    files: dict[str, str] = {}
    rejected: list[dict] = []

    if not sandbox.exists():
        return files, rejected

    sandbox_resolved = sandbox.resolve(strict=True)

    for entry in sandbox.rglob("*"):
        rel_str = str(entry.relative_to(sandbox))

        # 1. symlink は早期拒否（コード成果物に symlink を含む正当な理由がない）
        if entry.is_symlink():
            rejected.append({"path": rel_str, "reason": "symlink_not_allowed"})
            continue

        if not entry.is_file():
            continue

        # 2. パス安全性二重チェック: symlink でない実ファイルでも、
        #    予期しない resolve 挙動に備えて sandbox 配下に収まるか確認
        try:
            resolved = entry.resolve(strict=True)
            resolved.relative_to(sandbox_resolved)  # 範囲外なら ValueError
        except (OSError, ValueError):
            rejected.append({"path": rel_str, "reason": "outside_sandbox"})
            continue

        # 3. ホワイトリスト: 既存 _looks_like_file_path を流用
        if not _looks_like_file_path(rel_str):
            rejected.append({"path": rel_str, "reason": "not_in_whitelist"})
            continue

        # 3. サイズ上限
        size = entry.stat().st_size
        if size > _MAX_FILE_BYTES:
            rejected.append({"path": rel_str, "reason": "too_large", "size_bytes": size})
            continue

        # 4. 内容読み取り（バイナリ／不正 UTF-8 は破棄）
        try:
            content = entry.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            rejected.append({"path": rel_str, "reason": "not_utf8"})
            continue

        files[rel_str] = content

    return files, rejected
```

設計判断:
- `Path.rglob("*")` で再帰列挙（sandbox 配下のみ）
- `resolve(strict=True)` + `relative_to` で symlink ベースの脱出を防御（NF2.1）
- ホワイトリストは既存 `_looks_like_file_path` を流用（C4）
- サイズ上限・UTF-8 ガード（NF2.4）
- 破棄されたエントリは `rejected` リストに残し、F5.4 で構造化ログに出力

### 3.4 `_classify_workspace_outcome` の実装（F4）

```python
def _classify_workspace_outcome(
    files: dict[str, str],
    rejected: list[dict],
) -> dict:
    """収集結果から outcome を判定する。

    Returns: {
        "files_kind": "valid"|"all_empty"|"no_recognized"|"none",
        "files_count": int,
        "files_total_bytes": int,
        "rejected": [...],
    }
    """
    files_count = len(files)
    total_bytes = sum(len(c.encode("utf-8")) for c in files.values())

    if files_count == 0:
        if rejected:
            kind = "no_recognized"  # 何か書かれたが全部破棄された
        else:
            kind = "none"           # 何も書かれなかった
    elif total_bytes == 0:
        kind = "all_empty"          # 採用ファイルは存在するが全て空
    else:
        kind = "valid"

    return {
        "files_kind": kind,
        "files_count": files_count,
        "files_total_bytes": total_bytes,
        "rejected": rejected,
    }
```

判定優先順位:
1. `files_count == 0`:
   - `rejected` あり → `no_recognized`（書こうとしたが全部弾かれた）
   - `rejected` なし → `none`（Write を呼ばずに終わった）
2. `total_bytes == 0` → `all_empty`（採用ファイルあるが空）
3. それ以外 → `valid`

### 3.5 オーケストレーターのコード生成ループ書き換え（F1/F4/F5）

`src/agent/orchestrator.py:469-517` を以下に置き換え:

```python
if code_types and "github" in storage_targets:
    self.slack.post_progress(slack_channel, slack_thread_ts, "⚙️ コード成果物を生成中...")
    code_files_merged: dict[str, str] = {}
    readme_parts: list[str] = []
    failed_code_types: list[str] = []
    for code_type in code_types:
        logger.info(
            "Generating code files for type (workspace mode)",
            extra={"execution_id": execution_id, "code_type": code_type},
        )
        prompt = _build_code_generation_prompt(
            topic, category, research_results, profile_text, code_type
        )
        raw_stdout, files, outcome = call_claude_with_workspace(prompt, code_type)

        if outcome["files_kind"] == "valid":
            # README.md があれば readme_parts に分離して merge から除外
            readme_text = files.pop("README.md", None)
            if readme_text and readme_text.strip():
                label = _CODE_TYPE_LABELS.get(code_type, code_type)
                readme_parts.append(f"## {label}\n\n{readme_text}")

            code_files_merged.update(files)
            logger.info(
                "Code files generated (workspace mode) | execution_id=%s code_type=%s "
                "files_count=%d files_total_bytes=%d rejected_count=%d",
                execution_id,
                code_type,
                outcome["files_count"],
                outcome["files_total_bytes"],
                len(outcome["rejected"]),
            )
        else:
            stdout_preview = (raw_stdout or "")[:500]
            logger.warning(
                "Code generation failed (workspace mode) | execution_id=%s code_type=%s "
                "files_kind=%s files_count=%d rejected=%s stdout_preview=%r",
                execution_id,
                code_type,
                outcome["files_kind"],
                outcome["files_count"],
                outcome["rejected"][:5],
                stdout_preview,
            )
            failed_code_types.append(code_type)

    if code_files_merged:
        deliverables["code_files"] = {
            "files": code_files_merged,
            "readme_content": "\n\n".join(readme_parts) if readme_parts else "",
        }

    # Slack 部分失敗通知: 全部失敗 or 一部失敗のいずれも通知（ユーザーの気付きが重要）
    if failed_code_types:
        labels = [_CODE_TYPE_LABELS.get(t, t) for t in failed_code_types]
        self.slack.post_progress(
            slack_channel,
            slack_thread_ts,
            "⚠️ 一部のコード成果物の生成に失敗しました（" + " / ".join(labels) + "）。"
            "GitHub への push は省略されました。再投入で改善する場合があります。",
        )
```

設計判断:
- 既存の `code_files_merged` / `readme_parts` 集約パターンは維持
- README.md は files dict から `pop` で分離（既存の `readme_content` フィールドにマージ）
- 失敗時の構造化ログは F5.2/F5.3/F5.4 を満たすため、`rejected[:5]` までを含める（DoS 抑止）
- 既存の `_build_code_failure_diagnostics` は使わない（新方式では不要）

### 3.6 構造化ログ仕様（F5）

| イベント | level | フィールド |
|---|---|---|
| `Generating code files for type (workspace mode)` | INFO | `execution_id`, `code_type` |
| `Code files generated (workspace mode)` | INFO | `execution_id`, `code_type`, `files_count`, `files_total_bytes`, `rejected_count` |
| `Code generation failed (workspace mode)` | WARNING | `execution_id`, `code_type`, `files_kind`, `files_count`, `rejected[:5]`, `stdout_preview` |
| `Claude CLI error (workspace mode), retrying` | WARNING | `attempt`, `wait_seconds`, `code_type`, `rc`, `stderr` |
| Slack スレッド通知: コード生成部分失敗 | n/a | `failed_code_types` の人間可読ラベル |

設計判断:
- `INFO` は通常運用の観測。`WARNING` は要対応（過去の `parse_error=True` 同等）
- `stdout_preview` は 500 文字（既存 `_CODE_FAILURE_PREVIEW_LIMIT`）

---

## 4. データ構造

### 4.1 `deliverables["code_files"]` の構造

**変更なし**（既存の `_PRESERVED_DELIVERABLE_FIELDS` 互換性を維持）:

```python
{
    "files": {
        "main.tf": "...",
        "variables.tf": "...",
        "modules/cloudfront/main.tf": "...",
    },
    "readme_content": "## IaCコード\n\n...\n\n## プログラムコード\n\n...",
}
```

### 4.2 sandbox ディレクトリ構造

```
/tmp/agent-output-iac_code-xK3p9q/
├── main.tf
├── variables.tf
├── outputs.tf
├── modules/
│   └── cloudfront/
│       └── main.tf
└── README.md
```

`os.walk` 後に `relative_to(sandbox)` で `modules/cloudfront/main.tf` 形式の相対パスを得る。

---

## 5. テスト戦略

### 5.1 新規ユニットテスト（F7.1〜F7.3）

`tests/unit/agent/test_orchestrator.py` に追加:

#### 5.1.1 `Test_collect_workspace_files`
- `test_collects_whitelisted_files` — `.tf`, `.py`, `Dockerfile` 等が採用される
- `test_rejects_unknown_extension` — `.exe`, `.bin` 等が `rejected` に入る
- `test_rejects_oversized_file` — 100 KB 超過のファイルが `too_large` で破棄される
- `test_rejects_non_utf8` — バイナリファイルが `not_utf8` で破棄される
- `test_rejects_symlink_to_outside` — `/etc/passwd` への symlink が `symlink_not_allowed` で破棄される（is_symlink で早期検知）
- `test_rejects_symlink_to_inside` — sandbox 内の他ファイルへの symlink も `symlink_not_allowed` で破棄される（symlink 全拒否方針）
- `test_returns_empty_when_sandbox_empty` — 空ディレクトリで `({}, [])` が返る
- `test_handles_subdirectory` — `modules/cloudfront/main.tf` が採用される

#### 5.1.2 `Test_classify_workspace_outcome`
- `test_valid_when_files_have_content` — files 1+ かつ total_bytes > 0 で `valid`
- `test_all_empty_when_files_have_zero_bytes` — files あるが全て 0 バイトで `all_empty`
- `test_no_recognized_when_only_rejected` — files 0 + rejected 1+ で `no_recognized`
- `test_none_when_nothing_written` — files 0 + rejected 0 で `none`

#### 5.1.3 `Test_call_claude_with_workspace`
- `test_creates_and_cleans_sandbox` — sandbox が作成され、終了後に削除される
- `test_passes_cwd_to_subprocess` — `subprocess.run` の `cwd` が sandbox を指す
- `test_includes_write_in_allowed_tools` — `--allowedTools Write,Edit` が cmd に含まれる
- `test_returns_collected_files` — fake subprocess + 事前に書き込んだファイルで files が返る
- `test_retries_on_subprocess_error` — CalledProcessError 後にリトライする
- `test_cleans_sandbox_on_exception` — 例外時も sandbox が削除される

#### 5.1.4 統合テスト（オーケストレーター）
- `test_orchestrator_uses_workspace_mode_for_code_generation` — `call_claude_with_workspace` がモックされ、`deliverables["code_files"]` に正しい dict が入る
- `test_orchestrator_skips_code_files_on_workspace_failure` — `outcome["files_kind"] == "none"` のとき `deliverables["code_files"]` が設定されない
- `test_orchestrator_posts_slack_warning_on_partial_failure` — 一部 code_type が失敗したとき Slack に部分失敗通知が投稿される
- `test_orchestrator_posts_slack_warning_on_full_failure` — 全 code_type が失敗したとき Slack に通知が投稿される
- `test_orchestrator_no_slack_warning_on_success` — 全 code_type 成功時は失敗通知が投稿されない

### 5.2 既存テストの非回帰

- `tests/unit/agent/test_orchestrator.py` 全体（既存 ~62 件）
- 特に `test_run_review_loop_returns_fixed_deliverables_on_passed` 等の review-fix 系で `code_files` 保護が壊れていないことを確認

### 5.3 mock 戦略

- `subprocess.run` を `unittest.mock.patch` で差し替え
- side_effect で「実際にファイルを書く fake 関数」を提供（cwd を見て書き込む）
- `tempfile.mkdtemp` は実 IO を使う（pytest tmp_path より直接的）

---

## 6. 影響範囲分析

### 6.1 機能への影響

| 機能 | 影響 | 対応 |
|------|-----|------|
| 時事問題・トレンド調査（コード成果物なし）の投入 | **影響なし**（コード生成パスは `if code_types and "github" in storage_targets:` でスキップ） | 確認テストのみ |
| IT トピック（コード成果物あり）の投入 | **改善**（parse_error が構造的に発生しない） | 実機検証で確認 |
| Notion 投稿 | 影響なし | 確認テストのみ |
| Slack 進捗通知 / 完了通知 | 影響なし | 既存の `post_progress` 呼び出しを維持 |
| レビューループの code_files 保護 | 影響なし（4/23 修正の `_PRESERVED_DELIVERABLE_FIELDS` をそのまま流用） | 既存テストの非回帰確認 |
| Token Refresher Lambda（4/25 別 steering） | 影響なし | 別系統 |

### 6.2 デプロイ手順への影響

- ECS task definition: 変更なし（コードは GitHub Actions で latest タグに push されるだけ）
- IAM: 変更なし
- Secrets Manager: 変更なし
- DevContainer: 変更なし

### 6.3 ドキュメント更新範囲

- `docs/architecture.md`: 「成果物の性質的差異と JSON 適性」セクションを追加。コード成果物のみ例外設計を採る根拠を明記
- `docs/functional-design.md`: ジェネレーターセクションに「コード成果物はファイル書き込み方式」を追記
- `CLAUDE.md`: 触らない（一般的な開発ルールのみ記載のため）

---

## 7. リスク・トレードオフ

### 7.1 Claude が Write ツールを使わずにテキストで返す可能性

- リスク: プロンプト指示を無視してコードを stdout に返す
- 緩和: F4 の `outcome["files_kind"] == "none"` で検知 + 構造化ログに `stdout_preview` を残す。レビューループが既に走るため再生成のチャンスはある（ただし review-fix もテキストモードのため、根本的にはユーザーの再投入が必要）
- 受容: 自動再試行は入れない（無限ループ防止、コスト増抑止）

### 7.2 sandbox 内に大量ファイルを書かれる可能性（DoS）

- リスク: Claude が 1000 ファイル書く等の暴走
- 緩和: `_collect_workspace_files` は全ファイルを `os.stat` するが、サイズ上限・拡張子フィルタで採用しない。ECS のディスク容量上限（Fargate デフォルト 20 GB）に達する前にプロンプトの「最大 5 ファイル」指示で抑制を期待
- 残リスク: プロンプトを完全無視されると ephemeral disk 圧迫の可能性。本タスクではスコープ外（NF2.4 で 1 ファイルあたり制限のみ）

### 7.3 symlink 経由の脱出

- リスク: `os.symlink("/etc/passwd", sandbox/x)` のようにシステムファイルへの symlink を Write が作る
- 緩和: `entry.resolve(strict=True).relative_to(sandbox.resolve())` で範囲外を破棄（3.3 のステップ 1）
- 受容: Claude CLI が symlink を作れるか自体が不明（Write ツールの仕様未確認）。作れない場合は無害

### 7.4 旧 JSON パース系コードの削除に伴う一時的な不安定リスク

- リスク: `_normalize_code_files_payload` / `_build_code_failure_diagnostics` を本タスクで削除するため、新方式に未発見のバグがあると旧 fallback がない
- 緩和: 削除前に新方式が単体テストで全件パスすることを確認。さらに git history で復元可能（`git revert` 一発で旧関数を取り戻せる）
- 受容: デッドコード化を避けるメリットの方が大きい

### 7.5 ai-papers-digest との設計差異

- リスク: 同一の `call_claude` 系インターフェースを共有していたが、本タスクで分岐
- 緩和: NF4 の通り `docs/architecture.md` に根拠を明記。ai-papers-digest はコード生成しないため影響なし
- 受容: コード成果物を持たないプロジェクトには JSON 方式で十分

---

## 8. 完了の定義

requirements.md `8. 受け入れ条件（マスタ）` を満たすこと。具体的には:

- [ ] `_build_code_generation_prompt` がファイル書き込み方式に書き換えられている
- [ ] `call_claude_with_workspace` / `_collect_workspace_files` / `_classify_workspace_outcome` が新規実装されている
- [ ] オーケストレーターのコード生成ループが新ヘルパー経由になっている
- [ ] `_collect_workspace_files` が symlink を `is_symlink()` チェックで早期拒否する
- [ ] コード生成失敗時に Slack スレッドへ部分失敗通知が投稿される
- [ ] `_normalize_code_files_payload` / `_build_code_failure_diagnostics` および付随定数が削除されている
- [ ] 単体テスト（5.1.1〜5.1.4、計 18+ 件）が pytest で全件パス
- [ ] 既存テスト 229 件が全件パス（非回帰）
- [ ] `docs/architecture.md` / `docs/functional-design.md` に例外設計を明記
- [ ] 実機検証 1: コード成果物を含む投入 → GitHub に push されることを確認
- [ ] 実機検証 2: コードを含まない投入 → テキスト成果物のみ Notion に投稿されることを確認
- [ ] main へマージ済み
