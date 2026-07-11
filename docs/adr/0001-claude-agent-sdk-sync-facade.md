---
status: accepted
date: 2026-07-06
---

# Claude 呼び出しを Agent SDK の同期ファサードへ移行する

エージェントコンテナ内の Claude 呼び出しは `claude -p` subprocess + stdout JSON パース + stderr 文字列での 429 判定で構成されており、`_parse_claude_response` の型契約違反（6 call sites 中 5 つ）を生む温床だった。これを Python `claude-agent-sdk` に置き換え、`call_claude` 系の同期シグネチャは維持したまま内部だけ `asyncio.run(query(...))` に差し替える（同期ファサード）。戻り値は `ResultMessage.result` のテキスト `str` に契約変更し、`_parse_claude_response` は「フェンス抽出専用・dict を返すか例外を投げる」厳密契約に構造修正する。

## 前提となる事実

- Agent SDK は純粋な HTTP クライアントではなく、内部で Claude Code CLI（Node ランタイム）を subprocess 起動する。**SDK 化しても Dockerfile の `npm install -g @anthropic-ai/claude-code` は残る**（これは意図的であり削除しないこと）
- サブスクリプション認証は現行の Secrets Manager → `~/.claude/.credentials.json` seeding + writeback を無変更で維持する。SDK 内部 CLI が同じ credentials ファイルを読むため
- researcher / reviewer のプロンプトは ```json フェンス必須指示のため、envelope パース（CLI stdout JSON の `result` キー取り出し）は消滅するが、フェンス抽出は SDK 移行後も必要

## Considered Options

- **`claude setup-token` の長期トークン（`CLAUDE_CODE_OAUTH_TOKEN`）への認証切替** — writeback / TokenMonitor 機構を削除できるが、移行と同時に認証も変えると blast radius が広がるため却下。SDK 安定稼働後の別 steering で再検討可
- **orchestrator 全体の async 化（`asyncio.gather`）** — SDK と自然になじむが、orchestrator 全メソッド + テスト 26 件以上の書き換えと `_run_review_loop`（再発バグ密集地帯）への接触を伴うため却下
- **完全パリティ（偽 envelope JSON を合成して返す）** — テスト無変更で済むが、移行目的（型付き I/O）と矛盾する症状パッチのため却下
- **anthropic SDK（純 HTTP）への置換** — Node を排除できるが WebSearch / Write / Edit のツール実行系を自前実装することになり、サブスクリプション認証も使えないため却下

## Consequences

- リトライ回数・バックオフ・Sonnet→Opus エスカレーションのセマンティクスは維持し、検出だけ `ProcessError` / `ResultMessage.is_error` の型付き判定に置換。サブスク使用上限が「正常応答のテキスト」として返るケースを新規に捕捉する
- テストのモックは `@patch("orchestrator.subprocess.run")` から wrapper レベルに移行し、モック戻り値は envelope JSON からテキスト（フェンス付き JSON）に変わる
- hooks 可観測性・fix loop のセッション継続は本移行に含めない（後続 steering）
- `call_codex`（OpenAI CLI）は対象外・無変更
