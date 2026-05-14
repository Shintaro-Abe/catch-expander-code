# tasklist.md — Codex 呼び出しエラーの observability 改善

design.md の実装計画を具体的なタスクに分解した一覧。

## タスク一覧

| ID | 内容 | 完了条件 | 状態 |
|---|---|---|---|
| T1 | `src/agent/orchestrator.py` に `CodexInvocationError(subprocess.CalledProcessError)` クラスを追加 (`call_codex` の直前) | `__str__` オーバーライドで stderr 末尾 1500 文字が先頭に出る | pending |
| T2 | `call_codex` 内 `logger.warning` の stderr スライスを `[:500]` → `[:2000]` に拡張 (`orchestrator.py:1102`) | logger.warning が 2000 文字までの stderr を残す | pending |
| T3 | `call_codex` 末尾の `raise last_error` を `raise CodexInvocationError(...) from last_error` に変更 (`orchestrator.py:1107-1108`) | 例外チェーン (`__cause__`) で元 `CalledProcessError` を保持 | pending |
| T4 | 新規ユニットテスト 6 件を `tests/unit/agent/test_orchestrator.py` に追加 (テストクラス `TestCallCodexErrorObservability`) | 全 6 件 pass | pending |
| T5 | 既存テスト pytest 全件実行 (回帰確認) | 既存 pass 件数を維持 | pending |
| T6 | pre-commit-secret-scan Skill で secret スキャン → commit → push | CI green | pending |
| T7 | CI 完了確認後、`sam deploy` で backend デプロイ | ECS Task Definition 新 revision で起動成功 | pending |
| T8 | (#7 完了後) Slack で同一トピック「IT開発における原則:KISS,DRY,YAGNI,アジャイル開発,リーン思考」を再投入 | reviewer Codex 呼び出しが success または、失敗時に Dashboard で stderr が読める | pending |
| T9 | メモリ更新 (本 steering 完了状態の記録、CodexInvocationError パターン追加) | MEMORY.md にエントリ追加 | pending |

## タスク依存関係

```
T1 ──┐
T2 ──┼──→ T4 ──→ T5 ──→ T6 ──→ T7 ──→ T8 ──→ T9
T3 ──┘
```

- T1/T2/T3 は同じファイル（orchestrator.py）の編集なので、1 つの Edit セッション内でまとめて適用するのが自然。
- T4 は実装後に追加（テストファースト不採用、design.md 既定の単純パッチなので実装→検証順で十分）。
- T7 の sam deploy は CI 完了後実行（メモリ `feedback_deploy_after_ci_completion.md` 準拠）。
- T8 は外部依存（タスク #7: Codex 認証 401 解消）が完了している必要あり。タスク #7 が未完了の場合は、別の Codex 失敗ケース（人為的に rate limit ヒット等）で stderr が見えるかの代替検証を検討。

## 各タスクの実装メモ

### T1: CodexInvocationError クラス追加

挿入位置: `orchestrator.py` 内 `def call_codex(...)` の直前。

```python
class CodexInvocationError(subprocess.CalledProcessError):
    """call_codex 専用例外。

    subprocess.CalledProcessError を継承するため
    isinstance(exc, CalledProcessError) は引き続き真。
    str(exc) の先頭に stderr 末尾を出すことで、
    呼び出し側の error_message = str(e)[:500] スライスでも stderr が見える。
    """

    def __str__(self) -> str:
        tail = (self.stderr or "")[-1500:]
        return f"codex exec failed rc={self.returncode}: {tail}"
```

### T2 + T3: call_codex 内部の修正

```python
# T2: logger.warning の stderr スライス拡張 (orchestrator.py:1099-1104)
logger.warning(
    "Codex CLI error, retrying | rc=%s | stderr=%s",
    e.returncode,
    (e.stderr or "")[:2000],   # ← 500 から拡張
    extra={"attempt": attempt + 1, "wait_seconds": wait},
)

# T3: 全失敗時の raise (orchestrator.py:1107-1108)
if last_error:
    raise CodexInvocationError(
        last_error.returncode,
        last_error.cmd,
        last_error.output,
        last_error.stderr,
    ) from last_error
```

### T4: 新規ユニットテスト

`tests/unit/agent/test_orchestrator.py` に追加。

```python
class TestCallCodexErrorObservability:
    """call_codex のエラー observability テスト (steering 20260514)"""

    def _make_failure_run(self, stderr: str):
        """全 retry を CalledProcessError で失敗させる subprocess.run mock"""
        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, output="", stderr=stderr,
            )
        return fake_run

    def test_raises_codex_invocation_error_on_total_failure(self):
        from orchestrator import CodexInvocationError, call_codex
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run("err")),
            patch("orchestrator.time.sleep"),
            pytest.raises(CodexInvocationError),
        ):
            call_codex("prompt")

    def test_codex_invocation_error_is_a_called_process_error(self):
        from orchestrator import CodexInvocationError, call_codex
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run("err")),
            patch("orchestrator.time.sleep"),
        ):
            try:
                call_codex("prompt")
            except subprocess.CalledProcessError:
                pass
            else:
                pytest.fail("expected CalledProcessError-compatible exception")

    def test_str_contains_stderr_tail_at_head(self):
        from orchestrator import CodexInvocationError, call_codex
        marker = "MARKER_OBSERVABILITY_xyz123"
        stderr = "junk_" * 100 + marker
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run(stderr)),
            patch("orchestrator.time.sleep"),
        ):
            try:
                call_codex("prompt")
            except CodexInvocationError as e:
                head500 = str(e)[:500]
                assert marker in head500, f"marker not in head500: {head500!r}"
            else:
                pytest.fail("expected raise")

    def test_preserves_cause_chain(self):
        from orchestrator import CodexInvocationError, call_codex
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run("err")),
            patch("orchestrator.time.sleep"),
        ):
            try:
                call_codex("prompt")
            except CodexInvocationError as e:
                assert isinstance(e.__cause__, subprocess.CalledProcessError)
            else:
                pytest.fail("expected raise")

    def test_preserves_cmd_for_slack_branch(self):
        from orchestrator import CodexInvocationError, call_codex
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run("err")),
            patch("orchestrator.time.sleep"),
        ):
            try:
                call_codex("prompt")
            except CodexInvocationError as e:
                cmd_str = " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
                assert "codex" in cmd_str
            else:
                pytest.fail("expected raise")

    def test_logger_stderr_slice_2000(self, caplog):
        """リトライ時 logger.warning の stderr スライスが 2000 文字"""
        from orchestrator import call_codex
        long_stderr = "A" * 3000
        with (
            patch("orchestrator.subprocess.run", side_effect=self._make_failure_run(long_stderr)),
            patch("orchestrator.time.sleep"),
            caplog.at_level("WARNING", logger="catch-expander-agent"),
        ):
            try:
                call_codex("prompt")
            except Exception:
                pass
        # logger.warning の format string にバインドされた stderr 部分が 2000 文字
        retry_logs = [r for r in caplog.records if "Codex CLI error" in r.getMessage()]
        assert retry_logs, "no retry warning found"
        # フォーマット済みメッセージから stderr= 部分を取り出し、長さが 2000 以内
        first_msg = retry_logs[0].getMessage()
        assert "AAAA" in first_msg
        # 「stderr=」 以降が 2000 を超えないこと
        stderr_part = first_msg.split("stderr=", 1)[1]
        assert len(stderr_part) <= 2000
        assert len(stderr_part) > 500  # 旧スライスの 500 文字より明確に長い
```

### T5: 既存テスト回帰確認

```bash
cd /workspaces/Catch-Expander
pytest tests/unit/agent/ -x -q
```

期待値: pre-existing test 件数（前回セッションでは 38 件）が変わらず pass。

### T6: コミット

`pre-commit-secret-scan` Skill を `git commit` 前に発火（メモリ `feedback_pre_commit_secret_scan_skill.md`）。コミットメッセージ例：

```
fix(agent): expose Codex stderr in CalledProcessError str for observability

call_codex の全リトライ失敗時、CodexInvocationError(CalledProcessError サブクラス)
に詰め替え、__str__ をオーバーライドして stderr 末尾を文字列先頭に出す。
これにより emitter/DynamoDB/Slack/Dashboard の error_message = str(e)[:500]
で stderr が読めるようになり、CloudWatch Logs に潜らずに真因特定可能。

logger.warning の stderr スライスも 500→2000 に拡張、401 レスポンス本文等の
長めの stderr 全体を保持する。

isinstance(exc, CalledProcessError) は継承で生存するため
main.py:_notify_task_failure の Codex/Claude 振り分けは無変更で動作。

steering: .steering/20260514-codex-error-observability/
```

### T7: sam deploy

CI green 確認後（GitHub Actions の workflow_dispatch / push trigger 経由）：

```bash
sam deploy
```

ECR は IMMUTABLE タグ運用なので `latest` タグなし、SHA タグのみ push（メモリ `feedback_ecr_immutable_no_latest_tag.md`）。

### T8: 再投入検証

Slack で同一トピック投入。期待される観測：

- Codex 認証修復済み（タスク #7 完了）の場合: reviewer Codex が成功し、workflow 完走 → Notion 投稿成功。
- もし別の Codex 失敗ケースに遭遇: Dashboard の Execution Detail でエラーメッセージに stderr が含まれていることを目視確認（観測性 acceptance criteria #1）。

### T9: メモリ更新

| メモリファイル | 更新内容 |
|---|---|
| `MEMORY.md` | 新エントリ追加 (CodexInvocationError パターン) |
| 新規 `feedback_codex_invocation_error_pattern.md` | 「subprocess.CalledProcessError サブクラス + __str__ オーバーライドで `error_message = str(e)[:500]` でも stderr が読める」原則を保存 |
| `project_codex_auth_rotation_complete.md` の更新検討 | 401 が起きた場合のローテーション手順を補足 |

## リスクとロールバック

- **回帰リスク**: 既存テストに影響しないことを `grep` で確認済み（design.md 影響範囲分析を参照）。
- **ロールバック**: `git revert <commit>` 単発。frontend デプロイ不要、ECS Task Definition は image SHA のみ更新。

## 完了の定義

すべての T1〜T9 が完了し、acceptance criteria（requirements.md §受け入れ条件）の 5 項目 + 非機能要件 3 項目を満たす。
