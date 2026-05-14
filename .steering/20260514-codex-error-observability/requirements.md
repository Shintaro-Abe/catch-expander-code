# requirements.md — Codex 呼び出しエラーの observability 改善

## 背景

2026-05-14 03:28 JST、Slack 実機検証でトピック「IT開発における原則:KISS,DRY,YAGNI,アジャイル開発,リーン思考」を投入したところ、reviewer の Codex 呼び出しが失敗した。

Slack/Dashboard に届いたエラー情報は以下のみだった：

```json
{
  "duration_ms": 24429,
  "error_message": "Command '['codex', 'exec', '--model', 'gpt-5.5', '--ephemeral', '--skip-git-repo-check', '-c', 'sandbox_mode=\"danger-full-access\"', '-o', '/tmp/tmpbsgc33bw.txt', '-']' returned non-zero exit status 1.",
  "subagent": "reviewer",
  "error_type": "CalledProcessError"
}
```

`error_message` には Codex 側の stderr が一切含まれず、真因不明のまま CloudWatch Logs に潜って初めて以下が判明した：

```
WARNING Codex CLI error, retrying | rc=1 | stderr=
2026-05-13T19:00:32.759294Z ERROR codex_login::auth::manager:
Failed to refresh token: 401 Unauthorized: {
```

つまり Codex CLI の OAuth トークン refresh が 401 で失敗していた（真因の対処は別タスクで実施。本 steering のスコープではない）。

## 課題

`src/agent/orchestrator.py:1059-1124` の `call_codex` は `subprocess.run(capture_output=True)` で stderr をキャプチャしながら、リトライ時の `logger.warning` でのみ stderr を吐き、最終的に `raise last_error`（生の `subprocess.CalledProcessError`）で投げている。

`str(CalledProcessError)` は `"Command '[...]' returned non-zero exit status 1."` 固定で stderr を含まないため、emitter 経由で DynamoDB events / Slack 通知 / Dashboard に渡る `error_message` には真因が一切残らない。

加えて、`logger.warning` 側の stderr スライスも `[:500]` で切られており、401 レスポンス本文の JSON が CloudWatch Logs でも途中で消える。

## ユーザーストーリー

開発者として、Codex CLI 呼び出しが失敗したとき、

- Dashboard の Execution Detail を開けば、CloudWatch Logs に潜らずに **真因（stderr 末尾）が読める** ようにしたい。
- CloudWatch Logs に流れる `Codex CLI error, retrying` ログでも、401 レスポンス全体や stacktrace の **2000 文字まで** が見えるようにしたい。
- 既存の Slack エラー通知（「Codex CLI の実行に失敗しました。」）はそのまま機能する状態を維持したい。

## 受け入れ条件

### 機能要件

1. `call_codex` がリトライ全失敗で最終的に投げる例外について、`str(exc)[:500]` で stderr 末尾が読める。
2. `isinstance(exc, subprocess.CalledProcessError)` 判定は **真** を返し続ける（`main.py:192-212` の Slack 通知分岐が壊れない）。
3. `exc.cmd` に元の codex コマンドリストが保持され、`main.py:201` の `"codex" in cmd_str` 分岐がそのまま動く。
4. CloudWatch Logs の `"Codex CLI error, retrying"` ログで stderr が 2000 文字まで保持される。
5. `exc.__cause__` に元の `CalledProcessError` がチェーンされ、stack_trace が失われない。

### 非機能要件

1. 既存のユニットテスト（`tests/agent/`）が全件 pass する。新規例外型を assert していたテストがあれば、`isinstance(..., CalledProcessError)` の意味論を保つ形で更新する。
2. emitter / DynamoDB events / Slack 通知 / Dashboard 表示のいずれも、コード変更ゼロで自動的に新しい `error_message` を受け取れる（後方互換）。
3. 他の `subprocess` 系コール（`call_claude` 等）への影響はない。

## スコープ外（本 steering で扱わないこと）

| 項目 | 理由 |
|---|---|
| Codex 認証 401 自体の解消（refresh token 再発行） | 運用作業 (`codex login` + Secrets Manager 更新) で対処。本 steering はコード改修のみ |
| 他の subprocess 呼び出し全般の `error_message` 改善 | `call_claude` 等は別経路。同様の課題があれば別 steering で対応 |
| Codex リトライ回数や wait バックオフの変更 | 観測性とは独立、本件で議論しない |
| Dashboard 側の UI 変更 | `error_message` 文字列を表示しているだけで、変更不要 |

## 制約事項

- CLAUDE.md ルールに従い、1 ファイルずつ承認を得て次へ進む。
- `call_codex` は再発パッチ密集地点ではない（過去メモリ `project_review_loop_recurring_patch_site.md` の対象は `_run_review_loop`）が、`_run_review_loop` から呼び出される。回帰リスクを抑える単一焦点パッチに留める。
- `git push` 後は CI 完了を待ってから `sam deploy` する（メモリ `feedback_deploy_after_ci_completion.md`）。
- ECR は IMMUTABLE タグ運用、`latest` タグは push しない（メモリ `feedback_ecr_immutable_no_latest_tag.md`）。

## 完了の定義

1. requirements.md / design.md / tasklist.md の 3 ファイルがそれぞれ承認済み。
2. 実装 + テスト pass + commit + push + CI green + `sam deploy` 成功。
3. Codex 認証 401 解消後（別タスク #7）、同一トピックを再投入して reviewer Codex 呼び出しが成功する。
4. もし 2 回目以降のテスト投入で別の Codex 失敗が起きた場合、Dashboard だけで stderr 末尾が読めることを確認する。
