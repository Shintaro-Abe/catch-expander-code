# Codex レビュー依頼: SDK ストリームの is_error result 破棄修正 (T22-T24)

## 背景

実機 E2E (exec-20260711154440-67fed1d5) で text generator が 3 回連続
`Exception('Claude Code returned an error result: success')` で即死し
NonDictGeneratorResponse に至った。根本原因調査の結果:

- claude-agent-sdk 0.2.110 の CLI は is_error=true の result message を emit した後、
  意図的に非ゼロ終了する
- SDK の reader task はその ProcessError を `{"type":"error"}` フレームに変換し、
  consumer 側 (`receive_messages`) は素の `Exception` として raise する (SDK query.py:852)
- 旧 `_query_claude_sync` はストリームを最後まで回すため、捕捉済みの is_error
  ResultMessage が破棄され、`_attempt_claude_query` の is_error 分岐
  (usage-limit 検出 / rate_limit_hit emit / 型付きリトライ / advisor 経路 /
  main.py の Slack 失敗分類) が実運用で全て素通しになっていた
- SDK reader は途中の ProcessError も同様にエラーフレーム化するため、
  `except ProcessError` の stderr 429 判定も実運用ではほぼ発火しない

## 今回の変更 (HEAD = 46b9784, ベース 3115f5e)

1. `_query_claude_sync`: 最初の ResultMessage で即 return (SDK `receive_response`
   と同セマンティクス)。ResultMessage 到達前の素の Exception は
   `ClaudeInvocationError(stderr=str(e))` に正規化。`ClaudeSDKError` 系は素通し
2. `_attempt_claude_query` の ClaudeInvocationError 正規化分岐に stderr 429 判定 +
   rate_limit_hit emit を追加
3. 回帰テスト 6 件 (`TestQueryClaudeSync`) 追加
4. Dockerfile から未使用の `@anthropic-ai/claude-code@2.1.207` を除去
   (SDK は wheel 同梱の CLI 2.1.191 を使用することが本番ログで確認済み。
   PATH 上の claude を参照するランタイムコードは存在しない)
5. `.gitleaks.toml` の誤検出抑制 (全 102 件を精査済み、実シークレットなし)

## レビュー観点

- P1: ResultMessage で即 return する変更の正当性。async generator の早期 return で
  SDK 側のクリーンアップ (transport 終了 / writeback) に悪影響はないか。
  特に workspace モード (Write/Edit ツール実行後の result) でファイル書き込みの
  完了が result message 受信時点で保証されるか
- P1: ClaudeSDKError 素通し + 素の Exception 正規化の分類漏れ。CLINotFoundError /
  CLIConnectionError の即時 fail、ProcessError の stderr 429 判定という既存
  セマンティクスを壊していないか
- P2: 正規化分岐の rate_limit_hit emit が二重 emit にならないか
- P2: 回帰テストが実 SDK の挙動 (result → エラーフレームの順序) を正しく模しているか
- P2: Dockerfile から claude 除去の抜け漏れ (PATH claude 依存の見落とし)
- P3: gitleaks allowlist の緩めすぎ (実トークンを見逃す穴)

## 対象差分 (git diff 3115f5e..46b9784)

```diff
diff --git a/.gitleaks.toml b/.gitleaks.toml
index b9277e3..6bf4ab2 100644
--- a/.gitleaks.toml
+++ b/.gitleaks.toml
@@ -22,6 +22,14 @@ paths = [
   '''(^|/)\.aws(/|$)''',
   '''(^|/)\.aws-sam(/|$)''',
   '''(^|/)\.audit(/|$)''',
+  # 2026-07-11 追加: gitignored な生成物 / ツールキャッシュ / worktree コピー
+  # (detect --no-git のワーキングツリー全体スキャンで拾われるため明示除外)
+  '''(^|/)\.ruff_cache(/|$)''',
+  '''(^|/)\.pytest_cache(/|$)''',
+  '''(^|/)\.claude/worktrees(/|$)''',
+  '''(^|/)graphify-out(/|$)''',
+  # fingerprint 行 (sha:path:rule:line) が電話番号ルールに誤マッチするため
+  '''\.gitleaksignore$''',
 ]
 
 # Python MagicMock の assert_* メソッド名が openai-oauth-refresh-token rule (`rt_*` 系)
@@ -45,7 +53,8 @@ tags = ["ip", "network"]
   description = "Allowlist test/example IPs and localhost references"
   regexes = [
     '''192\.168\.0\.[01](?:\b|$)''',      # ゲートウェイ例（ドキュメント等でよく使われる）
-    '''10\.0\.0\.[01](?:\b|$)''',
+    '''10\.0\.0\.[0-9](?:\b|$)''',        # テストフィクスチャの例示 IP（10.0.0.2 等）
+    '''10\.0\.[0-9]{1,3}\.0/''',          # IaC (template.yaml / 構成図) の意図的なサブネット CIDR
   ]
 
 # メールアドレス（個人情報）
@@ -83,6 +92,12 @@ tags = ["pii", "phone"]
   regexes = [
     # バージョン番号等の誤検知を減らす
     '''0\.[0-9]+\.[0-9]+''',
+    # hex カラーコード (#000000 等、drawio / CSS)
+    '''#[0-9a-fA-F]{6}''',
+    # 小数の端数 (AWS 単価 $0.0000133334 やコスト値 0.000100 等)
+    '''\.[0-9]{4,}''',
+    # Slack 系ダミー ID (U01234567 等)
+    '''U0[0-9A-Z]{6,}''',
   ]
 
 # AWS アクセスキー ID（デフォルトルールでもカバーされるが明示的に定義）
@@ -117,6 +132,9 @@ tags = ["openai", "oauth", "secret"]
   description = "Allowlist common non-token rt_ patterns"
   regexes = [
     '''(?i)rt_(?:timeout|error|success|limit|max|min|value|type|count|index)''',
+    # snake_case の識別子断片 (insert_rich_text_unchanged 等のテスト名 / メソッド名)。
+    # 実トークンはランダム base64url で英小文字のみの複数語構成にはならない
+    '''rt_[a-z]+(?:_[a-z]+){2,}''',
   ]
 
 # 汎用 API キー（変数名に api と key を含む代入パターン）
diff --git a/.steering/20260706-agent-sdk-migration/tasklist.md b/.steering/20260706-agent-sdk-migration/tasklist.md
index 3b0310d..79dd51a 100644
--- a/.steering/20260706-agent-sdk-migration/tasklist.md
+++ b/.steering/20260706-agent-sdk-migration/tasklist.md
@@ -45,6 +45,20 @@
   - [ ] `api_call_completed` / `rate_limit_hit` イベントが events テーブルに従来契約で記録されることを確認
 - [x] T21: `docs/architecture.md` の CLI 記述を SDK 構成に整合更新（エージェント基盤表 / §4.5 出力フォーマット）、セッションハンドオフ memo 更新。※ Dockerfile の CLI インストール・Node.js ランタイム・認証記述は SDK 移行後も事実として正しいため温存
 
+## Phase 6: T20 E2E 失敗（exec-20260711154440-67fed1d5）の是正
+
+> **経緯**: 2026-07-11 15:44 UTC の実機 E2E で、リサーチ 5 本成功後に text generator が
+> 3 回連続即死 → `NonDictGeneratorResponse`。根本原因は `_query_claude_sync` が SDK ストリームを
+> 最後まで回すため、CLI が「is_error result → 意図的な非ゼロ終了」をした際に SDK のエラーフレームが
+> 素の `Exception` として raise され（SDK `query.py:852`）、捕捉済み ResultMessage を破棄すること。
+> これにより `_attempt_claude_query` の is_error 分岐（usage-limit 検出 / rate_limit_hit emit /
+> 型付きリトライ・advisor 経路）が実運用でデッドコード化していた（構造修正を選択、症状パッチは不採用）。
+
+- [x] T22: `_query_claude_sync` を「最初の ResultMessage で return」に変更（SDK `receive_response` と同セマンティクス）。ResultMessage 到達前にストリームが素の `Exception` を raise した場合は `ClaudeInvocationError` に正規化してリトライ経路に乗せる（`ClaudeSDKError` 系は従来どおり素通し — CLINotFoundError/CLIConnectionError の即時 fail と ProcessError の stderr 429 判定を温存）。P2-2 正規化分岐にも stderr 429 判定を追加（SDK reader が途中 ProcessError をエラーフレーム化するため）
+- [x] T23: 回帰テスト追加（`TestQueryClaudeSync` 6 件）— (1) is_error ResultMessage 後にエラーフレーム Exception が来ても ResultMessage が返る、(2) 本番障害の E2E 再現: usage limit が `rate_limited=True` に分類され advisor スキップ、(3) ResultMessage なしの素の Exception → 正規化、(4) CLINotFoundError 素通し、(5) 空ストリーム → ClaudeInvocationError、(6) エラーフレーム内 429 の rate limit 分類。**全 533 passed**、変更ファイルの新規 lint ゼロ（11 件は HEAD 比較で pre-existing と同一集合を確認）
+- [x] T24: Dockerfile から未使用の `@anthropic-ai/claude-code@2.1.207` を除去（本番ログ全呼び出しで SDK 同梱 CLI 2.1.191 使用を確認、PATH claude への依存コードなし）。Dockerfile コメント / architecture.md（基盤表・ランタイム表・Dockerfile 抜粋）を同梱 CLI 前提に訂正
+- [ ] T25: 品質ゲート — ruff / 全テスト green ✔ → secret scan → commit → push → Codex レビューゲート（承認制）→ 再デプロイ（ユーザー実行）→ E2E 再実行
+
 ## 完了条件
 
 requirements.md の受け入れ条件 1〜7 がすべて観測事実つきで verified になること。
diff --git a/docs/architecture.md b/docs/architecture.md
index 5e37aac..368fc14 100644
--- a/docs/architecture.md
+++ b/docs/architecture.md
@@ -18,7 +18,7 @@
 
 | 項目 | 技術 |
 |------|------|
-| エージェントフレームワーク | Claude Agent SDK（`claude-agent-sdk`、内部で Claude Code CLI を subprocess 起動） |
+| エージェントフレームワーク | Claude Agent SDK（`claude-agent-sdk`、wheel に同梱する Claude Code CLI を subprocess 起動。CLI バージョンは SDK pin に追従） |
 | LLMモデル | Claude Sonnet 4.6（通常ステップ）/ GPT-5.5 via Codex CLI（品質レビューのみ） |
 | LLMプラン | Maxプラン（月額固定） |
 | 認証 | MaxプランOAuth（Claude Code公式アプリケーション経由） |
@@ -40,7 +40,7 @@
 | コンポーネント | 言語 | ランタイム |
 |--------------|------|-----------|
 | Lambda（トリガー） | Python 3.13 | AWS Lambda |
-| ECSタスク（エージェント実行） | Node.js（Claude Code CLI） + Python（ラッパースクリプト） | ECS Fargate |
+| ECSタスク（エージェント実行） | Python（エージェントロジック + Claude Agent SDK 同梱 CLI） + Node.js（Codex CLI） | ECS Fargate |
 
 ### フロントエンド（ダッシュボード SPA）
 
@@ -164,8 +164,8 @@ FROM node:22-slim
 # 非rootユーザー作成
 RUN groupadd -r appuser && useradd -r -g appuser -m appuser
 
-# Claude Code CLI と Codex CLIのインストール
-RUN npm install -g @anthropic-ai/claude-code @openai/codex@0.125.0 \
+# Codex CLI のインストール（Claude Code CLI は claude-agent-sdk が wheel に同梱するものを使用）
+RUN npm install -g @openai/codex@0.125.0 \
     && codex --version
 
 # Python（ラッパースクリプト用）+ curl
diff --git a/src/agent/Dockerfile b/src/agent/Dockerfile
index 2021731..0fafd0c 100644
--- a/src/agent/Dockerfile
+++ b/src/agent/Dockerfile
@@ -3,12 +3,14 @@ FROM node:22-slim
 # 非rootユーザー作成
 RUN groupadd -r appuser && useradd -r -g appuser -m appuser
 
-# Claude Code CLI と Codex CLI のインストール
-# claude-code は claude-agent-sdk (requirements.txt に pin) が内部で subprocess 起動する CLI。
-# devcontainer で claude-code 2.1.207 + claude-agent-sdk 0.2.110 の組み合わせを検証済みのため両者を pin する (ADR 0001 / T1)。
-RUN npm install -g @anthropic-ai/claude-code@2.1.207 @openai/codex@0.125.0 \
-    && codex --version \
-    && claude --version
+# Codex CLI のインストール
+# Claude Code CLI は claude-agent-sdk (requirements.txt に pin) が wheel に同梱するものを
+# subprocess 起動するため、npm での別途インストールは不要 (T24)。
+# 同梱 CLI のバージョンは SDK pin (0.2.110 → CLI 2.1.191) に決定的に追従する。
+# ※ 旧記述「SDK は CLI を同梱せず PATH 上の claude を使う」は誤りで、npm pin の 2.1.207 は
+#   SDK 経由では一度も使われていなかった (exec-20260711154440-67fed1d5 調査で判明)。
+RUN npm install -g @openai/codex@0.125.0 \
+    && codex --version
 
 # Python（ラッパースクリプト用）と curl（レビュアープロンプトの出典URL検証用）
 RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip curl \
diff --git a/src/agent/orchestrator.py b/src/agent/orchestrator.py
index 84f1201..5173e69 100644
--- a/src/agent/orchestrator.py
+++ b/src/agent/orchestrator.py
@@ -16,6 +16,7 @@ from urllib.parse import urlparse
 
 from claude_agent_sdk import (
     ClaudeAgentOptions,
+    ClaudeSDKError,
     CLIConnectionError,
     CLINotFoundError,
     ProcessError,
@@ -537,21 +538,38 @@ def _build_claude_options(
 
 
 def _query_claude_sync(prompt: str, options: ClaudeAgentOptions) -> ResultMessage:
-    """Agent SDK の query() を同期実行し、最終 ResultMessage を返す (同期ファサードの最下層)。
+    """Agent SDK の query() を同期実行し、最初の ResultMessage を返す (同期ファサードの最下層)。
 
     orchestrator は同期構造 + ThreadPoolExecutor 並列のため、呼び出しごとに独立の
     イベントループを asyncio.run で生成する。ユニットテストはこの関数を patch する。
+
+    ResultMessage 受信時点で即 return する (SDK `receive_response` と同セマンティクス)。
+    CLI は is_error=true の result を出した後に意図的に非ゼロ終了し、SDK はその終端を
+    素の ``Exception`` としてストリームに流すため (SDK query.py `receive_messages`)、
+    ストリームを最後まで回すと捕捉済みの ResultMessage が破棄され、is_error 分岐
+    (usage-limit 検出 / rate_limit_hit emit / 型付きリトライ) が全て素通しになる
+    (T22, exec-20260711154440-67fed1d5 の障害対応)。
+
+    ResultMessage 到達前にストリームが素の ``Exception`` を raise した場合 (SDK reader が
+    途中失敗をエラーフレームに変換したケース) は ``ClaudeInvocationError`` に正規化して
+    リトライ経路に乗せる。``ClaudeSDKError`` 系はそのまま伝播させ、上位の
+    CLINotFoundError / CLIConnectionError 即時 fail と ProcessError の stderr 429 判定を温存する。
     """
 
     async def _run() -> ResultMessage:
-        result: ResultMessage | None = None
-        async for message in _sdk_query(prompt=prompt, options=options):
-            if isinstance(message, ResultMessage):
-                result = message
-        if result is None:
-            msg = "no ResultMessage received from Agent SDK"
-            raise ClaudeInvocationError(msg)
-        return result
+        try:
+            async for message in _sdk_query(prompt=prompt, options=options):
+                if isinstance(message, ResultMessage):
+                    return message
+        except ClaudeSDKError:
+            raise
+        except Exception as e:
+            raise ClaudeInvocationError(
+                "agent sdk stream failed before ResultMessage",
+                stderr=str(e),
+            ) from e
+        msg = "no ResultMessage received from Agent SDK"
+        raise ClaudeInvocationError(msg)
 
     return asyncio.run(_run())
 
@@ -595,8 +613,23 @@ def _attempt_claude_query(
     except ClaudeInvocationError as e:
         # Codex Pass 1 P2-2: SDK ストリームが ResultMessage なしで終端するケース。
         # 旧実装では CLI の異常応答としてリトライ対象だったため、即時 abort ではなく
-        # 共通のリトライ経路 (_ClaudeAttemptFailure) に正規化する
-        raise _ClaudeAttemptFailure(str(e), rate_limited=False) from e
+        # 共通のリトライ経路 (_ClaudeAttemptFailure) に正規化する。
+        # T22 補強: SDK reader は途中の ProcessError もエラーフレーム化して素の Exception に
+        # するため、この経路にも stderr 429 判定を掛けて旧実装のレート制限分類を温存する
+        stderr_text = e.stderr or ""
+        rate_limited = _claude_stderr_indicates_rate_limit(stderr_text)
+        if rate_limited:
+            _emit_rate_limit_hit(
+                emitter,
+                subtype="anthropic_429",
+                endpoint_path=endpoint_path,
+                detail=f"stderr_snippet={stderr_text[:200]}",
+            )
+        raise _ClaudeAttemptFailure(
+            str(e),
+            rate_limited=rate_limited,
+            stderr=stderr_text,
+        ) from e
 
     if result_msg.is_error:
         result_text = result_msg.result or ""
diff --git a/tests/unit/agent/test_orchestrator.py b/tests/unit/agent/test_orchestrator.py
index ff0a129..1f363d3 100644
--- a/tests/unit/agent/test_orchestrator.py
+++ b/tests/unit/agent/test_orchestrator.py
@@ -203,6 +203,121 @@ class TestCallClaude:
         assert cost_acc["total_output_tokens"] == 50
 
 
+def _real_result_message(result_text: str, *, is_error: bool = False, subtype: str = "success"):
+    """isinstance(message, ResultMessage) を通す必要がある _sdk_query 層テスト用の本物の ResultMessage。
+
+    _query_claude_sync を patch する上位層テストは _fake_result_message (SimpleNamespace) で足りるが、
+    _query_claude_sync 自体のストリーム消費を検証するにはスタブでは通らない (T22)。
+    """
+    from claude_agent_sdk import ResultMessage
+
+    return ResultMessage(
+        subtype=subtype,
+        duration_ms=1000,
+        duration_api_ms=800,
+        is_error=is_error,
+        num_turns=1,
+        session_id="test-session",
+        result=result_text,
+        usage={"input_tokens": 10, "output_tokens": 5},
+    )
+
+
+def _fake_sdk_stream(messages, tail_exc=None):
+    """_sdk_query 互換のフェイク async generator を返す。
+
+    tail_exc は全 message を yield し切った後に raise する。実 SDK が
+    「is_error result → CLI の意図的な非ゼロ終了 → エラーフレームを素の Exception として
+    raise」する挙動 (SDK query.py receive_messages) を模す。ResultMessage で即 return
+    していれば tail_exc には到達しない。
+    """
+
+    async def _gen(*, prompt, options):
+        for m in messages:
+            yield m
+        if tail_exc is not None:
+            raise tail_exc
+
+    return _gen
+
+
+class TestQueryClaudeSync:
+    """_query_claude_sync のストリーム消費セマンティクスのテスト (T22)。
+
+    exec-20260711154440-67fed1d5 の障害対応: ストリームを最後まで回すと、is_error result 後の
+    意図的な非ゼロ終了 (エラーフレーム = 素の Exception) が捕捉済み ResultMessage を破棄し、
+    is_error 分岐 (usage-limit 検出 / 型付きリトライ) が全て素通しになっていた。
+    """
+
+    def test_returns_result_message_before_trailing_error_frame(self):
+        """is_error ResultMessage の後にエラーフレーム Exception が控えていても、
+        ResultMessage 受信時点で即 return し例外に到達しない。"""
+        from orchestrator import _build_claude_options, _query_claude_sync
+
+        msg = _real_result_message("Claude AI usage limit reached|1783785000", is_error=True)
+        stream = _fake_sdk_stream(
+            [msg], tail_exc=Exception("Claude Code returned an error result: success")
+        )
+        with patch("orchestrator._sdk_query", stream):
+            result = _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
+        assert result is msg
+
+    def test_stream_error_before_result_is_normalized(self):
+        """ResultMessage 到達前の素の Exception (エラーフレーム) は ClaudeInvocationError に
+        正規化され、リトライ共通経路に乗る。"""
+        from orchestrator import ClaudeInvocationError, _build_claude_options, _query_claude_sync
+
+        stream = _fake_sdk_stream([], tail_exc=Exception("Fatal error in message reader"))
+        with patch("orchestrator._sdk_query", stream), pytest.raises(ClaudeInvocationError) as excinfo:
+            _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
+        assert "Fatal error in message reader" in excinfo.value.stderr
+
+    def test_sdk_typed_errors_propagate_unwrapped(self):
+        """ClaudeSDKError 系 (CLINotFoundError 等) はラップせず素通しし、
+        上位の即時 fail / stderr 429 判定を温存する。"""
+        from claude_agent_sdk import CLINotFoundError
+        from orchestrator import _build_claude_options, _query_claude_sync
+
+        stream = _fake_sdk_stream([], tail_exc=CLINotFoundError("claude not found"))
+        with patch("orchestrator._sdk_query", stream), pytest.raises(CLINotFoundError):
+            _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
+
+    def test_stream_end_without_result_raises_invocation_error(self):
+        """正常終端で ResultMessage が 1 件もない場合は従来どおり ClaudeInvocationError。"""
+        from orchestrator import ClaudeInvocationError, _build_claude_options, _query_claude_sync
+
+        stream = _fake_sdk_stream([])
+        with patch("orchestrator._sdk_query", stream), pytest.raises(ClaudeInvocationError) as excinfo:
+            _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
+        assert "no ResultMessage" in str(excinfo.value)
+
+    @patch("orchestrator.time.sleep")
+    def test_call_claude_usage_limit_with_nonzero_exit_is_rate_limited(self, mock_sleep):
+        """本番障害の end-to-end 再現: usage limit の is_error result + 非ゼロ終了が
+        素の Exception ではなく rate_limited=True の ClaudeInvocationError に分類され、
+        advisor エスカレーションもしない。"""
+        from orchestrator import ClaudeInvocationError, call_claude
+
+        msg = _real_result_message("Claude AI usage limit reached|1783785000", is_error=True)
+        stream = _fake_sdk_stream(
+            [msg], tail_exc=Exception("Claude Code returned an error result: success")
+        )
+        with patch("orchestrator._sdk_query", stream), pytest.raises(ClaudeInvocationError) as excinfo:
+            call_claude("prompt")
+        assert excinfo.value.rate_limited is True
+
+    @patch("orchestrator.time.sleep")
+    def test_call_claude_stream_429_error_frame_is_rate_limited(self, mock_sleep):
+        """途中失敗のエラーフレーム (SDK reader が ProcessError を変換したケース) でも
+        stderr 429 判定が効き rate_limited=True で失敗する (T22 補強)。"""
+        from orchestrator import ClaudeInvocationError, call_claude
+
+        stream = _fake_sdk_stream([], tail_exc=Exception("API Error: 429 Too Many Requests"))
+        with patch("orchestrator._sdk_query", stream), pytest.raises(ClaudeInvocationError) as excinfo:
+            call_claude("prompt")
+        assert excinfo.value.rate_limited is True
+
+
 class TestNamespaceSourceIds:
     """_namespace_source_ids関数のテスト"""
 
```
