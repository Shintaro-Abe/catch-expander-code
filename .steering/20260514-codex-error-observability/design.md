# design.md — Codex 呼び出しエラーの observability 改善

requirements.md を充足する実装設計。

## 実装アプローチの選択

設計検討で 3 案を比較し、**案 A': CodexInvocationError サブクラス + `__str__` オーバーライド** を採用した。

| 案 | 概要 | 採否 | 理由 |
|---|---|:-:|---|
| A 純粋版 | `call_codex` で `RuntimeError` に詰め替え | ✗ | `isinstance(exc, CalledProcessError)` が False になり、`main.py:192-212` の Slack 通知分岐（Codex/Claude 振り分け、rate limit 判定）が破壊される |
| **A'（採用）** | `CodexInvocationError(subprocess.CalledProcessError)` を新設し、`__str__` をオーバーライド | ✓ | 継承により isinstance チェックは生存。`str(exc)[:500]` のスライスでも stderr が読めるよう、stderr を文字列**先頭**に配置 |
| B emitter 側修正 | `call_codex` は無修正、orchestrator/main の 7+ コールサイトで `isinstance(e, CalledProcessError)` 分岐を増やす | ✗ | 修正面積広く漏れリスク高。`subprocess` 知識が emitter/Slack 通知層へ漏れ出るレイヤー違反 |

## コンポーネント設計

### 1. 例外クラス `CodexInvocationError`

`src/agent/orchestrator.py` に新規追加。配置は `call_codex` の直前。

```python
class CodexInvocationError(subprocess.CalledProcessError):
    """call_codex 専用例外。

    subprocess.CalledProcessError を継承するため、
    `isinstance(exc, CalledProcessError)` 判定は引き続き真。
    `main.py:_notify_task_failure` の codex/Claude 振り分けや rate limit 判定もそのまま動く。

    __str__ をオーバーライドして stderr 末尾を文字列**先頭**に出すため、
    呼び出し側の `error_message = str(e)[:500]` スライスでも stderr が読める。
    """

    def __str__(self) -> str:
        tail = (self.stderr or "")[-1500:]
        return f"codex exec failed rc={self.returncode}: {tail}"
```

**設計ポイント:**

- `subprocess.CalledProcessError` の `__init__(returncode, cmd, output=None, stderr=None)` をそのまま継承（オーバーライド不要）。
- `__str__` 出力先頭に `codex exec failed rc=<n>: ` を置き、続けて stderr 末尾 1500 文字。これにより `str(exc)[:500]` でも `rc` と stderr 冒頭の数百文字が見える。
- `self.cmd` には codex コマンドリスト（`["codex", "exec", "--model", ...]`）が保持されるため、`main.py:201` の `"codex" in cmd_str` 判定は変わらず動く。
- `self.stderr`, `self.returncode`, `self.output` も全て継承プロパティとして利用可能。

### 2. `call_codex` の修正 (`orchestrator.py:1059-1124`)

最終 raise を `CodexInvocationError` に変更し、リトライ時 logger の stderr スライスを 500→2000 に拡張。

```python
# 修正箇所 1: logger.warning の stderr スライス拡張
logger.warning(
    "Codex CLI error, retrying | rc=%s | stderr=%s",
    e.returncode,
    (e.stderr or "")[:2000],   # 500 → 2000
    extra={"attempt": attempt + 1, "wait_seconds": wait},
)

# 修正箇所 2: 全リトライ失敗時の raise
if last_error:
    raise CodexInvocationError(
        last_error.returncode,
        last_error.cmd,
        last_error.output,
        last_error.stderr,
    ) from last_error
```

`from last_error` により `__cause__` チェーンで元 `CalledProcessError` を保持し、CloudWatch の stack_trace 表示も維持。

## データ構造の変更

なし。例外型のみ。

## 影響範囲の分析

### `call_codex` の呼び出し元

`grep -n "call_codex" src/agent/orchestrator.py` 結果より、全て orchestrator 内部。主に `_run_review_loop` (reviewer フェーズ) と関連箇所。例外型は `CalledProcessError` のサブクラスなので、`except subprocess.CalledProcessError` / `isinstance` チェックがある場合も透過的に動作。

### `main.py:192-212` `_notify_task_failure` の Slack 通知分岐

| 条件 | 動作 | 影響 |
|---|---|---|
| `isinstance(exc, subprocess.CalledProcessError)` | `True`（継承） | ✓ 変更なし |
| `"429" in stderr or "rate limit" in stderr` | `exc.stderr` 参照 | ✓ stderr 属性は継承 |
| `"codex" in cmd_str` (cmd_str = " ".join(exc.cmd)) | `True` | ✓ `cmd` 属性は継承 |

→ Slack 通知メッセージは従来通り「Codex CLI の実行に失敗しました。」が出る。

### `error_message` を emit する箇所

`grep -n "error_message" src/agent/orchestrator.py` で確認した emit 箇所は基本 `str(e)[:500]` を使う。`CodexInvocationError.__str__` の先頭に `"codex exec failed rc=N: <stderr>"` が来るため、`[:500]` スライスで rc + stderr 数百文字が DynamoDB events / Slack / Dashboard へ伝搬される。

### 既存テストへの影響

`tests/unit/agent/test_orchestrator.py` の確認結果：

- **直接 `call_codex` を呼んで失敗を assertRaises している既存テストはない**。
- `@patch("orchestrator.call_codex")` で関数全体を mock しているテスト（9 箇所）は内部実装変更に透過。
- `subprocess.CalledProcessError` を raise しているのは `call_claude_with_workspace` 系テスト（line 2010, 2032）であり、本変更とは無関係。

→ **既存テスト破壊なし**。新規ユニットテストの追加のみ実施。

## テスト計画

`tests/unit/agent/test_orchestrator.py` に新規テストクラス `TestCallCodexErrorObservability` を追加。

```python
class TestCallCodexErrorObservability:
    def test_raises_codex_invocation_error_on_total_failure(self):
        """全リトライ失敗時、CodexInvocationError が投げられる"""
        # MAX_CODEX_RETRIES 回 CalledProcessError raise → assertRaises(CodexInvocationError)

    def test_codex_invocation_error_is_a_called_process_error(self):
        """isinstance 互換性: 既存の Slack 通知分岐が壊れないことを保証"""
        # raise したものを isinstance(exc, subprocess.CalledProcessError) で True

    def test_str_contains_stderr_tail_at_head(self):
        """str(exc)[:500] で stderr が読める"""
        # stderr="<MARKER_xxx>" を埋め込み、str(exc)[:500] に含まれることを確認

    def test_preserves_cause_chain(self):
        """exc.__cause__ が元の CalledProcessError であること（stack_trace 保持）"""

    def test_preserves_cmd_for_slack_branch(self):
        """exc.cmd に codex コマンドリストが入り main.py:201 の判定が動くこと"""
        # " ".join(exc.cmd) に "codex" が含まれる

    def test_logger_stderr_slice_2000(self):
        """リトライ時 logger.warning の stderr スライスが 2000 文字"""
        # stderr=3000 文字を流す → caplog で 2000 文字まで残ることを確認
```

サブプロセスの mock は既存テスト（line 2007 周辺の `fake_run`）と同様のパターンで実装。`call_codex` 内部は `subprocess.run` と `time.sleep` を patch する。

## ロールアウト計画

1. ✅ requirements.md 承認
2. design.md 承認（本ファイル）
3. tasklist.md 承認
4. 実装 (orchestrator.py + tests)
5. ローカル pytest pass 確認
6. pre-commit-secret-scan Skill 経由で commit (CLAUDE.md ルール、メモリ `feedback_pre_commit_secret_scan_skill.md`)
7. push → CI green 待ち（メモリ `feedback_deploy_after_ci_completion.md`）
8. `sam deploy` (backend のみ、frontend デプロイ不要)
9. (#7) Codex 認証 401 解消後に同一トピック再投入
10. (もし別の Codex 失敗が起きたら) Dashboard だけで stderr 末尾が読めることを確認

## 切り戻し

`git revert <commit>` 単発で対応可能。frontend 影響なし、ECS task definition 自体は変わらない（image SHA のみ更新）。`feedback_ecr_immutable_no_latest_tag.md` に従い ECR は SHA タグのみ push。

## 関連メモリ

- `project_codex_auth_rotation_complete.md` — Codex 認証ローテーションの実装場所（main.py の `_setup_codex_credentials` / `_writeback_codex_credentials`）。本 steering の真因 (401) は token rotation の運用面で対処、コード側は無修正。
- `feedback_anti_pattern_discipline.md` — 3 層代替案規律（採用案 A' / 不採用 A / B を冒頭表で比較）。
- `feedback_test_patches_call_codex_and_claude.md` — テストパッチパターン（call_codex 単体テストでは subprocess.run を patch、reviewer ループテストでは call_codex を patch）。
- `feedback_deploy_after_ci_completion.md` — push → CI 完了 → sam deploy の手順。
