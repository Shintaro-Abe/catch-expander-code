# Design: Claude 呼び出しの Agent SDK 移行

## 実装アプローチ: 同期ファサード

`call_claude` 系の同期シグネチャと呼び出し側コードを不変に保ち、内部実装だけを `claude-agent-sdk` に差し替える。

```
orchestrator.run() / feedback_processor          ← 無変更（戻り値契約の追随のみ）
  └─ call_claude(...) -> str                     ← シグネチャ不変・内部差し替え
       └─ _query_claude_sync(prompt, options)    ← 新設共通ヘルパー
            └─ asyncio.run( query(...) )         ← claude-agent-sdk
                 └─ 内部 CLI subprocess           ← SDK が管理（自前 subprocess.run 廃止）
                      └─ ~/.claude/.credentials.json  ← 認証は無変更
```

- ThreadPoolExecutor の各ワーカースレッドにはイベントループがないため、`asyncio.run()` は呼び出しごとに独立ループを生成する。並列 researcher と自然に共存する
- `subprocess` import は `call_codex`（無変更）が使い続けるため残る

## 依存関係の変更

| ファイル | 変更 |
|---|---|
| `src/agent/requirements.txt` | `claude-agent-sdk==<実装時最新>` を追加（pin 必須） |
| `src/agent/Dockerfile` | `@anthropic-ai/claude-code` を**残し**、バージョン pin を追加（例: `@anthropic-ai/claude-code@X.Y.Z`）。SDK が wheel に CLI を同梱する場合でも、同梱版とグローバル版の解決順を実装時に確認し、どちらを使うかを明示する（`ClaudeAgentOptions` の CLI パス指定 or PATH 依存） |

## wrapper ごとのマッピング

### 共通ヘルパー `_query_claude_sync(prompt, options) -> ResultMessage`（新設）

```python
def _query_claude_sync(prompt: str, options: ClaudeAgentOptions) -> ResultMessage:
    async def _run() -> ResultMessage:
        result: ResultMessage | None = None
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                result = msg
        if result is None:
            raise ClaudeInvocationError("no ResultMessage received")
        return result
    return asyncio.run(_run())
```

### 旧 CLI フラグ → `ClaudeAgentOptions` 対応表

| 旧 CLI | SDK オプション | 備考 |
|---|---|---|
| `--model sonnet` / `claude-opus-4-7` | `model=` | エイリアスそのまま |
| `--allowedTools A,B` | `allowed_tools=["A","B"]` | 指定ツールは自動許可（headless で対話プロンプトなし） |
| `--output-format json` | 不要 | `ResultMessage` が構造化済み（envelope パース消滅） |
| `-p -`（stdin プロンプト） | `prompt=` 引数 | |
| `cwd=sandbox`（subprocess 引数） | `cwd=str(sandbox)` | workspace 系 2 wrapper |
| （CLI 暗黙: Claude Code システムプロンプト） | `system_prompt={"type": "preset", "preset": "claude_code"}` | **パリティ注意**: SDK デフォルトは空システムプロンプト。CLI `-p` の挙動に合わせるため preset 指定を明示する |
| （CLI 暗黙: settings / CLAUDE.md 読込） | `setting_sources` はデフォルト（読み込まない）のまま | コンテナ内 `/app` に CLAUDE.md はなく実質差分なし。挙動差が出た場合のみ再検討 |

### 各 wrapper の変更

| wrapper | options | 戻り値の変更 |
|---|---|---|
| `call_claude` (L432) | `model`, `allowed_tools`（researcher: `["WebSearch","WebFetch"]` / feedback: なし） | 旧: stdout の envelope JSON 文字列 → **新: `ResultMessage.result` のテキスト `str`** |
| `call_claude_with_workspace` (L737) | 上記 + `cwd=sandbox`, `allowed_tools=["Write","Edit"]`（advisor は `+Read`） | タプル構造 `(text, files_dict, outcome)` は維持、第 1 要素のみテキスト化 |
| `call_claude_with_text_workspace` (L868) | 同上 | 同上 `(text, file_content, outcome)` |
| advisor エスカレーション (L506/827/975) | 同一 options で `model=CLAUDE_ADVISOR_MODEL` | ロジック不変 |

## エラーマッピングとリトライ

セマンティクス（`MAX_CLAUDE_RETRIES=3`、指数バックオフ、枯渇時 Sonnet→Opus、rate limit 時はエスカレーションしない）は不変。検出のみ型化する。

| 事象 | 旧検出 | 新検出 |
|---|---|---|
| CLI プロセス異常終了 | `subprocess.CalledProcessError` | `claude_agent_sdk.ProcessError`（`exit_code` / `stderr` 属性） |
| CLI 起動不能 | 同上に混入 | `CLINotFoundError` / `CLIConnectionError` → リトライ対象外の即時失敗（環境異常のため） |
| API レートリミット (429) | stderr に `"429"` / `"rate limit"` 文字列 | `ProcessError.stderr` への同判定を型付き属性に適用 |
| **サブスク使用上限（正常応答で返る）** | **検出漏れ** | `ResultMessage.is_error == True` または result テキストの usage-limit パターン → `rate_limit_hit`（subtype: `subscription_limit` を新設）として emit + リトライ扱い |
| 実行内エラー | 検出漏れ（stdout に混入） | `ResultMessage.is_error` + `subtype`（`error_max_turns` 等）で分岐 |

- 例外の基底として `ClaudeInvocationError`（`CodexInvocationError` と対になる命名）を新設し、SDK 例外をラップして `main.py` へ伝播する。`str(e)` に stderr 断片を含める（既存 `CodexInvocationError` パターン踏襲）
- `main.py` L192-211 の except 分岐: `subprocess.CalledProcessError` 分岐は codex 用に残し、`ClaudeInvocationError` 分岐を追加（rate limit / 一般エラーの Slack 文言は既存を流用）

## `_parse_claude_response` の構造修正

**旧**: 6 段フォールバック、失敗時は生テキストを黙って返す（型契約違反の温床）

**新契約**: `_parse_claude_response(text: str) -> dict` — dict を返せない場合は `ClaudeResponseParseError` を送出

残す段: ```json フェンス抽出 → 汎用フェンス抽出 → 直 JSON parse → `raw_decode` スキャン
消す段: envelope 全体 parse / `result`・`content` キー取り出し（SDK で構造化済みのため不要）

呼び出し側（6 call sites）は try/except で失敗時の既存フォールバック挙動（リトライ・fixer_notes 記録等）を明示する。**黙って非 dict が下流に流れる経路を型で塞ぐことが本修正の核心**（`.steering/20260512` の対症療法を構造修正に昇格）。

## usage / cost 集計

`_accumulate_cost` の入力を stdout JSON から `ResultMessage.usage`（`input_tokens` / `output_tokens`）+ `total_cost_usd` に変更。`api_call_completed` イベントのフィールド契約（subtype=`anthropic`、duration、トークン数）は不変。

## テスト設計

| 対象 | 旧 patch | 新 patch |
|---|---|---|
| wrapper 単体 | `@patch("orchestrator.subprocess.run")` + `MagicMock(stdout='{"result": ...}')` | `@patch("orchestrator._query_claude_sync")` + `ResultMessage` 相当のスタブ（`result` / `usage` / `is_error` 属性） |
| orchestrator 統合 | `@patch("orchestrator.call_claude")` 戻り値 = envelope JSON 文字列 | 同 patch、戻り値 = フェンス付きテキスト `'```json\n{...}\n```'` |
| feedback_processor | 同上 | 同上 |
| review loop | `call_codex` + `call_claude` 両 patch 必須 | **規律継続**（reviewer は生 JSON、fixer はフェンス付きテキスト） |

新規テスト: `_parse_claude_response` の `ClaudeResponseParseError` 送出、subscription-limit 検出、`CLINotFoundError` 即時失敗。

## 影響範囲

- `src/agent/orchestrator.py` — 3 wrapper 内部 + `_parse_claude_response` + 6 call sites + 定数
- `src/agent/main.py` — except 分岐追加（認証 seeding / writeback は**触らない**）
- `src/agent/feedback/feedback_processor.py` — `call_claude` 戻り値契約への追随
- `src/agent/requirements.txt` / `src/agent/Dockerfile` — 依存追加・pin
- `tests/unit/agent/test_orchestrator.py` / `test_feedback_processor.py` / `tests/integration/test_workflow.py`
- `template.yaml` — **変更なし**（環境変数・IAM は現行のまま）
- `docs/architecture.md` / `docs/glossary.md`（用語は反映済み） — 実装完了後に整合更新

## 実機検証で確認する環境前提（コード変更では解決できない項目）

1. SDK 内部 CLI が `~/.claude/.credentials.json`（サブスク OAuth）を読んで認証できること
2. read-only rootfs + `/home-scratch` HOME 環境で SDK 内部 CLI が起動できること（CLI のロック / キャッシュ書き込み先）
3. `cwd=sandbox`（`/tmp-scratch` 配下）で Write ツールがファイルを生成し、`_collect_workspace_files` が回収できること
4. タスク終了時の credentials writeback が従来どおり発火すること
