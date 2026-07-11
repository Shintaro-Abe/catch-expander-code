# Codex レビュー結果: Claude 呼び出しの Agent SDK 移行

prompt: `.audit/2026-07-06_agent-sdk-migration.prompt.md`

## 実行方式に関する注記（2026-07-11）

コミット前レビュー方式（ユーザー承認済み）。対象は未コミット作業ツリーの `git diff HEAD`。

- codex-plugin-cc（`codex@openai-codex` v1.0.6）導入に伴い、従来の手動 `danger-full-access` ではなく
  **read-only sandbox + 差分埋め込み**で実行。devcontainer 内では codex の bwrap sandbox が
  namespace を作れずシェル実行不可のため、コード+テスト全差分と ADR 0001 をプロンプトに同梱した
  （入力: `git diff HEAD -- <code+tests>` 3,244 行）
- 実行バイナリ: OpenAI Codex v0.144.1 / model **gpt-5.4** (reasoning medium)。
  過去パス（preference-scope）の gpt-5.5 とはモデルが異なる点に留意

## Pass 1

**結果 (2026-07-11, gpt-5.4, tokens 46,228): P1×0 / P2×2 / P3×0**

「上記 2 点以外は call_claude の戻り値契約変更と _parse_claude_response の厳密化に追随できており、
明確な envelope JSON 前提の残骸は見当たらない」との総評。

### P2（推奨）

1. `orchestrator.py:677` — `_run_claude_with_retries()` の advisor 試行失敗が
   `except _ClaudeAttemptFailure: pass` で握り潰され、最終 `ClaudeInvocationError` が常に
   「最後の Sonnet 失敗」の `stderr` / `exit_code` / `rate_limited` を持つ。
   「Sonnet 通常失敗 + advisor usage limit」で `rate_limited=False` になり main.py の
   Slack 通知が誤分類。→ advisor 失敗で `last_failure` を更新

2. `orchestrator.py:551-553` — `_query_claude_sync()` の「ResultMessage なし終端」が
   `ClaudeInvocationError` 直接送出のため `_ClaudeAttemptFailure` 正規化を通らず、
   この経路だけ `MAX_CLAUDE_RETRIES` / advisor fallback / rate_limit 判定を迂回して即時 abort。
   → `_attempt_claude_query()` で捕捉しリトライ共通経路へ正規化

### Pass 1 是正（2026-07-11 実施済み）

- P2-1: `except _ClaudeAttemptFailure as e: last_failure = e` に変更。
  **旧実装パリティとの整合**: 旧 CLI 実装も advisor 失敗を握り潰して sonnet の `last_error` を
  raise していたため、これは移行由来ではなく旧来からの品質問題。リトライ回数・エスカレーション
  条件は不変で、最終例外のメタデータのみ正確化されるためパリティ違反ではないと判断
- P2-2: `_attempt_claude_query()` に `except ClaudeInvocationError` を追加し
  `_ClaudeAttemptFailure(rate_limited=False)` へ正規化（旧実装では CLI 異常応答はリトライ対象だった）
- 回帰テスト 2 件追加: `test_call_claude_advisor_failure_updates_final_exception` /
  `test_call_claude_no_result_message_is_retried`
- 検証: 全スイート **527 passed**（`AWS_DEFAULT_REGION=ap-northeast-1 --timeout=30`、36.69s）、
  ruff は既知 pre-existing 11 件のみ（新規ゼロ）

## Pass 2

**結果 (2026-07-11, gpt-5.5, tokens 46,495): 指摘ゼロ → 収束**

（初回実行は `~/.codex/config.toml` 1 行目の構文エラー `]model` で失敗 → 修正して再実行。
Pass 2 からは model = gpt-5.5 で過去レビューと揃った）

Pass 1 是正 2 件は「意図どおり」と確認:
- advisor 失敗が最終例外の `rate_limited/stderr/exit_code` に反映される
- ResultMessage なし終端が `_ClaudeAttemptFailure` 正規化でリトライ経路に乗る

加えて: 戻り値契約統一 / `_parse_claude_response` 厳密契約 / emit 等価性（旧 endpoint key 維持含む）/
main.py 分岐順序、いずれも違反なしとの総評。

※ 本レビューは埋め込み差分のみで実施（テスト実行なし）。テスト green はこちら側で
527 passed を別途確認済み。
