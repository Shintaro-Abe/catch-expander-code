# Codex レビュー Pass 2: SDK ストリーム修正の Pass 1 是正確認

## 前提

Pass 1（.audit/2026-07-11_sdk-stream-error-result.md）で P2×2 を指摘し、以下を是正した:

- P2-1: `_query_claude_sync` の早期 return 時の stream close を
  `contextlib.aclosing(_sdk_query(...))` で明示化
- P2-2: 早期 return 後に generator の finally が実行されることを検証する回帰テスト
  `test_early_return_closes_stream_generator` を追加

P3（.claude/worktrees の scan 除外）は「worktree は各ブランチのコミット時に本体側で
スキャンされる」ため運用許容と判断した。

テスト: 全 534 passed。ruff: 変更ファイルに新規指摘なし（既存 11 件は pre-existing）。

## レビュー観点

- Pass 1 の P2-1 / P2-2 是正が意図どおりか
- aclosing 化による新たな問題（例外マスキング、ClaudeSDKError 素通しとの相互作用、
  aclose 中の例外の扱い）がないか
- 上記以外に新規の問題があるか

## 対象差分 (git diff 46b9784 — Pass 1 是正分のみ)

```diff
diff --git a/src/agent/orchestrator.py b/src/agent/orchestrator.py
index 5173e69..54e2f62 100644
--- a/src/agent/orchestrator.py
+++ b/src/agent/orchestrator.py
@@ -1,5 +1,6 @@
 import asyncio
 import concurrent.futures
+import contextlib
 import json
 import logging
 import os
@@ -558,9 +559,14 @@ def _query_claude_sync(prompt: str, options: ClaudeAgentOptions) -> ResultMessag
 
     async def _run() -> ResultMessage:
         try:
-            async for message in _sdk_query(prompt=prompt, options=options):
-                if isinstance(message, ResultMessage):
-                    return message
+            # Codex Pass 1 P2-1: 早期 return 時の transport 終了をイベントループ shutdown の
+            # finalizer 任せにせず、aclosing で generator の aclose() を明示的に走らせる
+            async with contextlib.aclosing(
+                _sdk_query(prompt=prompt, options=options)
+            ) as stream:
+                async for message in stream:
+                    if isinstance(message, ResultMessage):
+                        return message
         except ClaudeSDKError:
             raise
         except Exception as e:
diff --git a/tests/unit/agent/test_orchestrator.py b/tests/unit/agent/test_orchestrator.py
index 1f363d3..fd04084 100644
--- a/tests/unit/agent/test_orchestrator.py
+++ b/tests/unit/agent/test_orchestrator.py
@@ -262,6 +262,26 @@ class TestQueryClaudeSync:
             result = _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
         assert result is msg
 
+    def test_early_return_closes_stream_generator(self):
+        """ResultMessage での早期 return 後も aclosing により generator の finally
+        (= SDK 側 transport cleanup 相当) が実行される (Codex Pass 1 P2-1/P2-2)。"""
+        from orchestrator import _build_claude_options, _query_claude_sync
+
+        cleanup = {"ran": False}
+        msg = _real_result_message("ok")
+
+        async def _gen(*, prompt, options):
+            try:
+                yield msg
+                yield _real_result_message("should not be consumed")
+            finally:
+                cleanup["ran"] = True
+
+        with patch("orchestrator._sdk_query", _gen):
+            result = _query_claude_sync("prompt", _build_claude_options("sonnet", None, None))
+        assert result is msg
+        assert cleanup["ran"] is True
+
     def test_stream_error_before_result_is_normalized(self):
         """ResultMessage 到達前の素の Exception (エラーフレーム) は ClaudeInvocationError に
         正規化され、リトライ共通経路に乗る。"""
```
