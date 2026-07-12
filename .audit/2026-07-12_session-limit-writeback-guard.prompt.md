# Codex レビュー依頼: session limit 分類漏れ + 空 credentials writeback 破壊の修正 (T26/T27)

## 背景

2026-07-12 の E2E 再実行 2 回で発見した障害の修正 (HEAD = 8ebf97e, ベース ec5cb6d):

1. exec-20260712003302: ローカル Claude Code セッションが OAuth refresh token を
   ローテーションし、Secrets Manager 側の refresh token が失効 → ECS タスクで 401 →
   CLI がログアウトして ~/.claude/.credentials.json を空にした
   (accessToken="", refreshToken="", expiresAt=0) → タスク終了時の
   `_writeback_claude_credentials` がその空状態をシークレットに書き戻して破壊
2. exec-20260712005527: ワークフロー 56 分進行後に CLI の使用上限
   「You've hit your session limit · resets 2am (UTC)」で失敗。
   `_USAGE_LIMIT_RESULT_PATTERNS` = ("usage limit", "rate limit", "rate_limit", "429")
   に "session limit" が無く rate_limited=False に誤分類 → 本来スキップすべき
   advisor エスカレーションが実行され、Slack 通知もレート制限専用文言にならなかった

## 今回の変更

- orchestrator.py: `_USAGE_LIMIT_RESULT_PATTERNS` に "session limit" を追加
- main.py: `_claude_credentials_look_valid(content)` を新設し、
  `_writeback_claude_credentials` で「変更あり」判定の後・put の前に検査。
  claudeAiOauth.accessToken / refreshToken のどちらかが空、または JSON 不正なら
  writeback をスキップ (fail-closed)。トークン値の検証・ログ出力はしない
- テスト: 回帰 3 件追加 (session limit 文言 / logged-out スキップ / 不正 JSON スキップ)、
  既存 writeback テストのフィクスチャを claudeAiOauth 実形状に更新

## レビュー観点

- P1: `_claude_credentials_look_valid` の判定が過剰防衛になっていないか
  （正当な refresh 結果を誤って skip する形はないか。Claude CLI の credentials.json
  スキーマは {"claudeAiOauth": {accessToken, refreshToken, expiresAt, ...}}）
- P1: "session limit" パターン追加による過剰マッチ（正常応答テキストに
  "session limit" が含まれるケースで誤って rate limit 扱いになるリスク）の許容性
- P2: writeback ガードの位置（unchanged 判定・optimistic concurrency 判定との順序）
- P2: Codex 側 writeback (`_writeback_codex_credentials`) に同種ガードが無いことの妥当性
  （Codex CLI のログアウト時挙動は未観測のため今回スコープ外とした）
- P3: テストfixture 変更が既存の検証意図を弱めていないか

## 対象差分 (git diff ec5cb6d..8ebf97e)

```diff
diff --git a/.steering/20260706-agent-sdk-migration/tasklist.md b/.steering/20260706-agent-sdk-migration/tasklist.md
index 79dd51a..db86db1 100644
--- a/.steering/20260706-agent-sdk-migration/tasklist.md
+++ b/.steering/20260706-agent-sdk-migration/tasklist.md
@@ -57,7 +57,18 @@
 - [x] T22: `_query_claude_sync` を「最初の ResultMessage で return」に変更（SDK `receive_response` と同セマンティクス）。ResultMessage 到達前にストリームが素の `Exception` を raise した場合は `ClaudeInvocationError` に正規化してリトライ経路に乗せる（`ClaudeSDKError` 系は従来どおり素通し — CLINotFoundError/CLIConnectionError の即時 fail と ProcessError の stderr 429 判定を温存）。P2-2 正規化分岐にも stderr 429 判定を追加（SDK reader が途中 ProcessError をエラーフレーム化するため）
 - [x] T23: 回帰テスト追加（`TestQueryClaudeSync` 6 件）— (1) is_error ResultMessage 後にエラーフレーム Exception が来ても ResultMessage が返る、(2) 本番障害の E2E 再現: usage limit が `rate_limited=True` に分類され advisor スキップ、(3) ResultMessage なしの素の Exception → 正規化、(4) CLINotFoundError 素通し、(5) 空ストリーム → ClaudeInvocationError、(6) エラーフレーム内 429 の rate limit 分類。**全 533 passed**、変更ファイルの新規 lint ゼロ（11 件は HEAD 比較で pre-existing と同一集合を確認）
 - [x] T24: Dockerfile から未使用の `@anthropic-ai/claude-code@2.1.207` を除去（本番ログ全呼び出しで SDK 同梱 CLI 2.1.191 使用を確認、PATH claude への依存コードなし）。Dockerfile コメント / architecture.md（基盤表・ランタイム表・Dockerfile 抜粋）を同梱 CLI 前提に訂正
-- [ ] T25: 品質ゲート — ruff / 全テスト green ✔ → secret scan → commit → push → Codex レビューゲート（承認制）→ 再デプロイ（ユーザー実行）→ E2E 再実行
+> **E2E 再実行 2 回の結果 (2026-07-12)**:
+> exec-20260712003302: 401 認証失敗（ローカル Claude Code セッションが refresh token を
+> ローテーションしシークレット側が失効。さらに CLI がログアウトした空 credentials を
+> writeback がシークレットに書き戻し破壊 → T27）。
+> exec-20260712005527: 再同期後 56 分ほぼ完走（解析/設計/リサーチ 5 本/text generator/
+> コード生成 2 種/fix loop まで実機成功 = T20 の大半を実証）、最終盤に
+> `You've hit your session limit · resets 2am (UTC)` で失敗。新 CLI の上限文言
+> "session limit" が `_USAGE_LIMIT_RESULT_PATTERNS` に無くレート制限扱いにならなかった → T26。
+
+- [x] T26: `_USAGE_LIMIT_RESULT_PATTERNS` に "session limit" を追加（advisor スキップ / Slack レート制限文言の分類を回復）+ 本番観測文言での回帰テスト（`test_call_claude_session_limit_in_error_result`）
+- [x] T27: `_writeback_claude_credentials` に空 credentials ガード（`_claude_credentials_look_valid`: accessToken / refreshToken が空 or JSON 不正なら writeback をスキップ）+ 回帰テスト 2 件、既存フィクスチャを claudeAiOauth 実形状に更新。**全 537 passed**、新規 lint ゼロ
+- [ ] T25: 品質ゲート — ruff / 全テスト green ✔ → secret scan（初回 102 件は全件精査で FP、`.gitleaks.toml` 整備 202efa0）✔ → commit（46b9784）→ push ✔ → **Codex レビューゲート収束**（Pass 1: P2×2 → aclosing 化 + 回帰テストで是正（ec5cb6d、534 passed）、Pass 2: 指摘ゼロ。`.audit/2026-07-11_sdk-stream-error-result.md`）✔ → 再デプロイ完了（2026-07-12、ユーザー実行。**検証**: stack UPDATE_COMPLETE + AgentImageUri=ec5cb6d + task definition `catch-expander-agent:16` が ec5cb6d image を参照）✔ → 残り: E2E 再実行（T20 / preference-scope T8-4）
 
 ## 完了条件
 
diff --git a/src/agent/main.py b/src/agent/main.py
index 5bf0971..85d6d11 100644
--- a/src/agent/main.py
+++ b/src/agent/main.py
@@ -1,4 +1,5 @@
 import hashlib
+import json
 import logging
 import os
 import subprocess
@@ -59,6 +60,21 @@ def _setup_claude_credentials(secret_value: str) -> str:
     return _hash_text(secret_value)
 
 
+def _claude_credentials_look_valid(content: str) -> bool:
+    """credentials.json がログイン状態のトークンを保持しているかを判定する。
+
+    T27 (exec-20260712003302): 認証失敗時に CLI がログアウトして credentials を
+    空にすることがあり、それを writeback するとシークレット側の正常値まで破壊する。
+    accessToken / refreshToken が非空文字列であることだけを最小条件として確認する
+    （トークン値そのものは検証もログもしない）。
+    """
+    try:
+        oauth = json.loads(content).get("claudeAiOauth", {})
+    except (json.JSONDecodeError, AttributeError):
+        return False
+    return bool(oauth.get("accessToken")) and bool(oauth.get("refreshToken"))
+
+
 def _writeback_claude_credentials(secret_arn: str, initial_hash: str) -> None:
     """タスク終了時、credentials が refresh されていれば Secrets Manager に書き戻す。
 
@@ -75,6 +91,12 @@ def _writeback_claude_credentials(secret_arn: str, initial_hash: str) -> None:
         if _hash_text(current) == initial_hash:
             logger.info("Credentials unchanged at task exit")
             return
+        if not _claude_credentials_look_valid(current):
+            # T27: ログアウト等で空になった credentials を書き戻すとシークレットを破壊する
+            logger.warning(
+                "Credentials at task exit look logged-out/empty; skipping writeback"
+            )
+            return
         sm = boto3.client("secretsmanager")
         remote_now = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
         if _hash_text(remote_now) != initial_hash:
diff --git a/src/agent/orchestrator.py b/src/agent/orchestrator.py
index 54e2f62..5860d62 100644
--- a/src/agent/orchestrator.py
+++ b/src/agent/orchestrator.py
@@ -435,7 +435,9 @@ def _claude_stderr_indicates_rate_limit(stderr_text: str) -> bool:
     return "429" in lowered or "rate limit" in lowered or "rate_limit" in lowered
 
 
-_USAGE_LIMIT_RESULT_PATTERNS = ("usage limit", "rate limit", "rate_limit", "429")
+# "session limit": exec-20260712005527 で観測した新 CLI 文言
+# "You've hit your session limit · resets 2am (UTC)"
+_USAGE_LIMIT_RESULT_PATTERNS = ("usage limit", "session limit", "rate limit", "rate_limit", "429")
 
 
 def _result_indicates_usage_limit(result_text: str) -> bool:
diff --git a/tests/unit/agent/test_main.py b/tests/unit/agent/test_main.py
index aa1e93c..bd64995 100644
--- a/tests/unit/agent/test_main.py
+++ b/tests/unit/agent/test_main.py
@@ -1,3 +1,4 @@
+import json
 import subprocess
 from unittest.mock import MagicMock, patch
 
@@ -218,6 +219,11 @@ class TestWritebackCodexCredentials:
 # ---------------------------------------------------------------------------
 
 
+def _claude_creds_json(access: str, refresh: str) -> str:
+    """claudeAiOauth 形式のダミー credentials (T27 のログイン判定を通す/落とす用)。"""
+    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh}})
+
+
 class TestWritebackClaudeCredentials:
     def _patch_home(self, tmp_path):
         return patch.object(
@@ -234,7 +240,7 @@ class TestWritebackClaudeCredentials:
     def test_skips_when_credentials_unchanged(self, tmp_path):
         from main import _hash_text, _writeback_claude_credentials
 
-        content = '{"token": "same"}'
+        content = _claude_creds_json("same-at", "same-rt")
         self._setup_creds_file(tmp_path, content)
         initial_hash = _hash_text(content)
 
@@ -246,10 +252,11 @@ class TestWritebackClaudeCredentials:
     def test_calls_put_when_credentials_changed(self, tmp_path):
         from main import _hash_text, _writeback_claude_credentials
 
-        initial_content = '{"token": "old"}'
+        initial_content = _claude_creds_json("old-at", "old-rt")
+        new_content = _claude_creds_json("new-at", "new-rt")
         self._setup_creds_file(tmp_path, initial_content)
         initial_hash = _hash_text(initial_content)
-        (tmp_path / ".claude" / ".credentials.json").write_text('{"token": "new"}')
+        (tmp_path / ".claude" / ".credentials.json").write_text(new_content)
 
         mock_client = MagicMock()
         mock_client.get_secret_value.return_value = {"SecretString": initial_content}
@@ -258,19 +265,23 @@ class TestWritebackClaudeCredentials:
 
         mock_client.put_secret_value.assert_called_once_with(
             SecretId="arn:claude",
-            SecretString='{"token": "new"}',
+            SecretString=new_content,
         )
 
     def test_skips_when_remote_updated_concurrently(self, tmp_path):
         from main import _hash_text, _writeback_claude_credentials
 
-        initial_content = '{"token": "old"}'
+        initial_content = _claude_creds_json("old-at", "old-rt")
         self._setup_creds_file(tmp_path, initial_content)
         initial_hash = _hash_text(initial_content)
-        (tmp_path / ".claude" / ".credentials.json").write_text('{"token": "new"}')
+        (tmp_path / ".claude" / ".credentials.json").write_text(
+            _claude_creds_json("new-at", "new-rt")
+        )
 
         mock_client = MagicMock()
-        mock_client.get_secret_value.return_value = {"SecretString": '{"token": "concurrent"}'}
+        mock_client.get_secret_value.return_value = {
+            "SecretString": _claude_creds_json("concurrent-at", "concurrent-rt")
+        }
         with self._patch_home(tmp_path), patch("main.boto3.client", return_value=mock_client):
             _writeback_claude_credentials("arn:claude", initial_hash)
 
@@ -284,13 +295,48 @@ class TestWritebackClaudeCredentials:
 
         mock_client_factory.assert_not_called()
 
+    def test_skips_writeback_when_credentials_logged_out(self, tmp_path):
+        """T27 (exec-20260712003302): 認証失敗で CLI がログアウトし空になった credentials を
+        書き戻すとシークレット側の正常値まで破壊するため、writeback をスキップする。"""
+        from main import _hash_text, _writeback_claude_credentials
+
+        initial_content = _claude_creds_json("old-at", "old-rt")
+        self._setup_creds_file(tmp_path, initial_content)
+        initial_hash = _hash_text(initial_content)
+        (tmp_path / ".claude" / ".credentials.json").write_text(
+            json.dumps(
+                {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}
+            )
+        )
+
+        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
+            _writeback_claude_credentials("arn:claude", initial_hash)
+
+        mock_client_factory.assert_not_called()
+
+    def test_skips_writeback_when_credentials_unparseable(self, tmp_path):
+        """T27: JSON として読めない credentials も writeback しない (fail-closed)。"""
+        from main import _hash_text, _writeback_claude_credentials
+
+        initial_content = _claude_creds_json("old-at", "old-rt")
+        self._setup_creds_file(tmp_path, initial_content)
+        initial_hash = _hash_text(initial_content)
+        (tmp_path / ".claude" / ".credentials.json").write_text("not-json")
+
+        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
+            _writeback_claude_credentials("arn:claude", initial_hash)
+
+        mock_client_factory.assert_not_called()
+
     def test_swallows_put_exception(self, tmp_path):
         from main import _hash_text, _writeback_claude_credentials
 
-        initial_content = '{"token": "old"}'
+        initial_content = _claude_creds_json("old-at", "old-rt")
         self._setup_creds_file(tmp_path, initial_content)
         initial_hash = _hash_text(initial_content)
-        (tmp_path / ".claude" / ".credentials.json").write_text('{"token": "new"}')
+        (tmp_path / ".claude" / ".credentials.json").write_text(
+            _claude_creds_json("new-at", "new-rt")
+        )
 
         mock_client = MagicMock()
         mock_client.get_secret_value.return_value = {"SecretString": initial_content}
diff --git a/tests/unit/agent/test_orchestrator.py b/tests/unit/agent/test_orchestrator.py
index fd04084..246a7cf 100644
--- a/tests/unit/agent/test_orchestrator.py
+++ b/tests/unit/agent/test_orchestrator.py
@@ -126,6 +126,22 @@ class TestCallClaude:
         assert mock_query.call_count == 3  # rate limit 扱いのため advisor なし
         assert excinfo.value.rate_limited is True
 
+    @patch("orchestrator.time.sleep")
+    @patch("orchestrator._query_claude_sync")
+    def test_call_claude_session_limit_in_error_result(self, mock_query, mock_sleep):
+        """新 CLI の session limit 文言 (exec-20260712005527 で観測) を rate limit として
+        扱う (T26)。旧パターン集合では 'session limit' が漏れており advisor エスカレーション +
+        汎用 Slack 文言に誤分類されていた。"""
+        from orchestrator import ClaudeInvocationError, call_claude
+
+        mock_query.return_value = _fake_result_message(
+            "You've hit your session limit · resets 2am (UTC)", is_error=True
+        )
+        with pytest.raises(ClaudeInvocationError) as excinfo:
+            call_claude("prompt")
+        assert mock_query.call_count == 3  # rate limit 扱いのため advisor なし
+        assert excinfo.value.rate_limited is True
+
     @patch("orchestrator.time.sleep")
     @patch("orchestrator._query_claude_sync")
     def test_call_claude_advisor_failure_updates_final_exception(self, mock_query, mock_sleep):
```
