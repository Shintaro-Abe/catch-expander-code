# Codex レビュー結果: SDK ストリームの is_error result 破棄修正 (T22-T24)

- 対象: `git diff 3115f5e..46b9784`（+ .gitleaks.toml の 202efa0）
- 方式: `codex exec --sandbox read-only -`（差分埋め込み、bwrap 制限のためローカルコマンド実行不可 → 差分ベースレビュー）
- モデル: gpt-5.5（`~/.codex/config.toml`）

## Pass 1（2026-07-12、15,251 tokens）

### Findings

- **P2-1** `orchestrator.py` `_query_claude_sync`: ResultMessage 即 return は方向性として妥当だが、
  早期 return 時の SDK stream close が暗黙（`asyncio.run()` shutdown の async generator finalizer 頼み）。
  `contextlib.aclosing(_sdk_query(...))` で包んで明示 close する方が堅い。
  → **是正**: `aclosing` 化を実施。
- **P2-2** `test_orchestrator.py` フェイク stream: `result → tail Exception` の順序再現は良いが、
  「result で止めたことで SDK 側 cleanup が走るか」は未カバー。
  → **是正**: 早期 return 後に generator の `finally` が実行されることを検証する回帰テストを追加。
- **P3** `.gitleaks.toml`: `.claude/worktrees` 全除外は「定期 scan 対象から外れる」ことを運用で許容するかの確認。
  → **判断**: 許容。worktree は各ブランチ作業ツリーのコピーで、当該ブランチのコミット時に本体側でスキャンされる。

### Assessment（指摘なしと明言された観点）

- P1 分類漏れ: 問題なし。`ClaudeSDKError` 素通しで CLINotFoundError / CLIConnectionError / ProcessError の
  既存上位分岐は維持され、plain Exception 化したケースのみ正規化される
- `rate_limit_hit` 二重 emit: なし（正規化経路と `result_msg.is_error` 経路は排他的）
- Dockerfile の claude 除去: PATH 直接呼び出しコードが無い前提で整合（前提はプロジェクト側 grep で確認済み）
- gitleaks allowlist: やや広いがブロッカーではない

## Pass 2（2026-07-12、5,586 tokens）

**指摘ゼロ、収束。**

- P2-1 / P2-2 是正は意図どおり: `aclosing` の `__aexit__` で早期 return 時も `aclose()` が走り、
  追加テストは「2 件目を消費せず返るが finally は実行される」ことを直接検証できている
- 例外面も整合: `aclose()` 中の `ClaudeSDKError` は素通し、それ以外は `ClaudeInvocationError` に正規化
- 注意点（指摘にはしない）: ResultMessage 取得後に `aclose()` が例外を投げると正常結果を上書きして
  呼び出し失敗になるが、これは `aclosing` の標準挙動で「cleanup の失敗を無視しない設計」として妥当

方式ノート: Pass 1/2 とも bwrap 制限でローカルコマンド実行不可 → 差分埋め込みベースのレビュー。
