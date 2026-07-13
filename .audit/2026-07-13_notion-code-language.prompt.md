# Codex レビュー依頼: Notion code block language 正規化 (T28)

## 背景

E2E (exec-20260713114159) で、GitHub push・Notion ページ作成成功後のブロック追記
PATCH が 400 validation_error（`code.language` が許容 enum 外）で失敗し、ワークフロー
全体が failed になった。原因は 2 点:
1. `prompts/generator.md` の例文自体が Notion 非対応の `"language": "terraform"` を教示
2. `notion_client` に language の検証層がなく、LLM 生成の content_blocks がそのまま送信される

## 今回の変更 (HEAD = 97c1c45, ベース 891cc86)

- `notion_client.py`: `_NOTION_CODE_LANGUAGES`（許容 enum、400 応答 + API リファレンス由来）
  + `_CODE_LANGUAGE_ALIASES`（terraform→hcl, yml→yaml, properties→plain text 等）
  + `_normalize_code_language(s)` を新設。未知値・非文字列は "plain text" に縮退。
  `create_page` / `append_blocks` で `_split_long_rich_text` と合成適用。非 mutate
- `prompts/generator.md`: 例を hcl に訂正 + 許容値の指示行を追加
- 回帰テスト 10 件（本番障害の terraform 再現、payload 検証、非 mutate 検証等）

## レビュー観点

- P1: `_NOTION_CODE_LANGUAGES` の enum 妥当性（許容値の過不足。特に 400 応答は
  1000 文字で切れており "prolog" 以降は API リファレンス知識で補完している —
  存在しない値を許してしまうと 400 が再発する）
- P1: 正規化の適用漏れ経路（create_page / append_blocks 以外に children を送る箇所、
  toggle 等のネスト children 内の code block）
- P2: alias マップの妥当性（誤マッピングで内容と表示言語がズレる害 vs plain text 縮退）
- P2: 大文字・空白入り language の扱い（"Plain Text", " JAVA " 等）
- P3: 非 mutate 実装の shallow copy 深度は十分か

## 対象差分 (git diff 891cc86..97c1c45)

```diff
diff --git a/.steering/20260706-agent-sdk-migration/tasklist.md b/.steering/20260706-agent-sdk-migration/tasklist.md
index db86db1..45fd41c 100644
--- a/.steering/20260706-agent-sdk-migration/tasklist.md
+++ b/.steering/20260706-agent-sdk-migration/tasklist.md
@@ -67,8 +67,15 @@
 > "session limit" が `_USAGE_LIMIT_RESULT_PATTERNS` に無くレート制限扱いにならなかった → T26。
 
 - [x] T26: `_USAGE_LIMIT_RESULT_PATTERNS` に "session limit" を追加（advisor スキップ / Slack レート制限文言の分類を回復）+ 本番観測文言での回帰テスト（`test_call_claude_session_limit_in_error_result`）
-- [x] T27: `_writeback_claude_credentials` に空 credentials ガード（`_claude_credentials_look_valid`: accessToken / refreshToken が空 or JSON 不正なら writeback をスキップ）+ 回帰テスト 2 件、既存フィクスチャを claudeAiOauth 実形状に更新。**全 537 passed**、新規 lint ゼロ
-- [ ] T25: 品質ゲート — ruff / 全テスト green ✔ → secret scan（初回 102 件は全件精査で FP、`.gitleaks.toml` 整備 202efa0）✔ → commit（46b9784）→ push ✔ → **Codex レビューゲート収束**（Pass 1: P2×2 → aclosing 化 + 回帰テストで是正（ec5cb6d、534 passed）、Pass 2: 指摘ゼロ。`.audit/2026-07-11_sdk-stream-error-result.md`）✔ → 再デプロイ完了（2026-07-12、ユーザー実行。**検証**: stack UPDATE_COMPLETE + AgentImageUri=ec5cb6d + task definition `catch-expander-agent:16` が ec5cb6d image を参照）✔ → 残り: E2E 再実行（T20 / preference-scope T8-4）
+- [x] T27: `_writeback_claude_credentials` に空 credentials ガード（`_claude_credentials_look_valid`: accessToken / refreshToken が空 or JSON 不正 or claudeAiOauth 非 dict なら writeback をスキップ）+ 回帰テスト 3 件、既存フィクスチャを claudeAiOauth 実形状に更新。**全 538 passed**、新規 lint ゼロ。コミット 8ebf97e + e0ce396。**Codex ゲート: Pass 1 指摘ゼロで収束**（補足 1 件は isinstance ガードで対応。`.audit/2026-07-12_session-limit-writeback-guard.md`。Pass 2 はユーザー判断でスキップ）
+> **4 回目 E2E (exec-20260713114159, 2026-07-13 11:42-13:04)**: text generator リトライ回復
+> (2 回検証失敗→3 回目成功)・コード生成・レビューループ (unparseable fix 2 回とも前版維持で継続)・
+> **GitHub push 成功 (新 PAT)**・Notion ページ作成成功まで到達。ブロック追記の 400
+> (`code.language` が Notion 許容 enum 外) で失敗 → T28。SDK 移行とは無関係の pre-existing バグ
+> (generator プロンプト例が非対応の "terraform" を教示 + notion_client に language 検証層なし)。
+
+- [x] T28: Notion code block language の正規化層 — `notion_client` に `_normalize_code_languages`（許容 enum + alias マップ、未知は "plain text" に縮退、非 mutate）を追加し `create_page` / `append_blocks` に適用。`prompts/generator.md` の例を `terraform` → `hcl` に訂正 + 許容値の指示行を追加。回帰テスト 10 件（本番障害の terraform 再現・payload 検証含む）→ **全 548 passed**、新規 lint ゼロ
+- [ ] T25: 品質ゲート — ruff / 全テスト green ✔ → secret scan（初回 102 件は全件精査で FP、`.gitleaks.toml` 整備 202efa0）✔ → commit（46b9784）→ push ✔ → **Codex レビューゲート収束**（Pass 1: P2×2 → aclosing 化 + 回帰テストで是正（ec5cb6d、534 passed）、Pass 2: 指摘ゼロ。`.audit/2026-07-11_sdk-stream-error-result.md`）✔ → 再デプロイ完了（2026-07-12、ユーザー実行。**検証**: stack UPDATE_COMPLETE + AgentImageUri=ec5cb6d + task definition `catch-expander-agent:16`）✔ → T26/T27 デプロイ完了（2026-07-12、ユーザー実行。**検証**: stack UPDATE_COMPLETE + AgentImageUri=e0ce396 + task definition `catch-expander-agent:17`）✔ → 残り: E2E 再実行（T20 残分 / preference-scope T8-4。**前提: シークレット再同期 + 使用上限の回復**）
 
 ## 完了条件
 
diff --git a/src/agent/prompts/generator.md b/src/agent/prompts/generator.md
index 9839840..5e47eda 100644
--- a/src/agent/prompts/generator.md
+++ b/src/agent/prompts/generator.md
@@ -28,11 +28,13 @@
 {"type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": "見出し3"}}]}}
 {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "本文テキスト"}}]}}
 {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": "リスト項目"}}]}}
-{"type": "code", "code": {"rich_text": [{"type": "text", "text": {"content": "コード"}}], "language": "terraform"}}
+{"type": "code", "code": {"rich_text": [{"type": "text", "text": {"content": "コード"}}], "language": "hcl"}}
 {"type": "table", "table": {"table_width": 3, "has_column_header": true, "children": [...]}}
 {"type": "divider", "divider": {}}
 ```
 
+code ブロックの `language` は Notion API の許容値のみ使用してください（例: `bash`, `java`, `python`, `yaml`, `json`, `sql`, `docker`, `hcl`(Terraform), `plain text`）。`terraform` / `yml` / `properties` などの非対応値は使わないでください。
+
 ### 共通構成
 
 すべてのテキスト成果物は以下の構成に従ってください。
diff --git a/src/agent/storage/notion_client.py b/src/agent/storage/notion_client.py
index 7fb755a..d3d4871 100644
--- a/src/agent/storage/notion_client.py
+++ b/src/agent/storage/notion_client.py
@@ -46,6 +46,101 @@ class NotionCloudflareBlockError(Exception):
         self.cf_ray = cf_ray
 
 
+# Notion API code block の language 許容値 (API version 2022-06-28)。
+# 2026-07-13 の 400 validation_error 応答 enum + API リファレンスより。
+# LLM 生成の content_blocks は許容外の値 (例: "terraform", "yml", "properties") を
+# 出し得るため、送信前に _normalize_code_languages で決定的に正規化する
+# (T28, exec-20260713114159。_split_long_rich_text と同じ検証層パターン)。
+_NOTION_CODE_LANGUAGES = frozenset({
+    "abap", "abc", "agda", "arduino", "ascii art", "assembly", "bash", "basic", "bnf",
+    "c", "c#", "c++", "clojure", "coffeescript", "coq", "css", "dart", "dhall", "diff",
+    "docker", "ebnf", "elixir", "elm", "erlang", "f#", "flow", "fortran", "gherkin",
+    "glsl", "go", "graphql", "groovy", "haskell", "hcl", "html", "idris", "java",
+    "javascript", "json", "julia", "kotlin", "latex", "less", "lisp", "livescript",
+    "llvm ir", "lua", "makefile", "markdown", "markup", "matlab", "mathematica",
+    "mermaid", "nix", "notion formula", "objective-c", "ocaml", "pascal", "perl", "php",
+    "plain text", "powershell", "prolog", "protobuf", "purescript", "python", "r",
+    "racket", "reason", "ruby", "rust", "sass", "scala", "scheme", "scss", "shell",
+    "smalltalk", "solidity", "sql", "swift", "toml", "typescript", "vb.net", "verilog",
+    "vhdl", "visual basic", "webassembly", "xml", "yaml", "java/c/c++/c#",
+})
+
+# LLM が高頻度で出す非許容値 → 許容値の対応。ここに無い未知値は "plain text" に縮退する
+_CODE_LANGUAGE_ALIASES = {
+    "terraform": "hcl",
+    "tf": "hcl",
+    "yml": "yaml",
+    "sh": "shell",
+    "zsh": "shell",
+    "console": "shell",
+    "shell-session": "shell",
+    "js": "javascript",
+    "jsx": "javascript",
+    "node": "javascript",
+    "ts": "typescript",
+    "tsx": "typescript",
+    "py": "python",
+    "rb": "ruby",
+    "kt": "kotlin",
+    "cs": "c#",
+    "csharp": "c#",
+    "cpp": "c++",
+    "objc": "objective-c",
+    "golang": "go",
+    "dockerfile": "docker",
+    "gradle": "groovy",
+    "make": "makefile",
+    "md": "markdown",
+    "tex": "latex",
+    "proto": "protobuf",
+    "ps1": "powershell",
+    "plaintext": "plain text",
+    "text": "plain text",
+    "txt": "plain text",
+    "plain": "plain text",
+    "properties": "plain text",
+    "ini": "plain text",
+    "conf": "plain text",
+    "env": "plain text",
+}
+
+
+def _normalize_code_language(language) -> str:
+    """language を Notion 許容値に正規化する。未知値・非文字列は "plain text" に縮退。"""
+    if not isinstance(language, str):
+        return "plain text"
+    lowered = language.strip().lower()
+    if lowered in _NOTION_CODE_LANGUAGES:
+        return lowered
+    return _CODE_LANGUAGE_ALIASES.get(lowered, "plain text")
+
+
+def _normalize_code_languages(blocks: list[dict]) -> list[dict]:
+    """code block の language を許容値に正規化した新しい list を返す。
+
+    入力 list / dict は mutate しない (_split_long_rich_text と同じ規律)。
+    code block 以外・不正構造はそのまま通す。
+    """
+    result: list[dict] = []
+    for block in blocks:
+        code_payload = block.get("code") if isinstance(block, dict) else None
+        if not isinstance(code_payload, dict):
+            result.append(block)
+            continue
+        original = code_payload.get("language")
+        normalized = _normalize_code_language(original)
+        if normalized == original:
+            result.append(block)
+            continue
+        logger.info(
+            "Normalized Notion code block language | original=%r normalized=%r",
+            original,
+            normalized,
+        )
+        result.append({**block, "code": {**code_payload, "language": normalized}})
+    return result
+
+
 def _is_cloudflare_block(status_code: int, body: str) -> bool:
     if status_code != 403:
         return False
@@ -236,7 +331,7 @@ class NotionClient:
         slack_user: str,
     ) -> tuple[str, str]:
         """成果物ページを作成し、(ページURL, ページID)を返す"""
-        content_blocks = _split_long_rich_text(content_blocks)
+        content_blocks = _normalize_code_languages(_split_long_rich_text(content_blocks))
         properties: dict = {
             "タイトル": {"title": [{"text": {"content": title}}]},
             "カテゴリ": {"select": {"name": category}},
@@ -276,6 +371,6 @@ class NotionClient:
 
     def append_blocks(self, page_id: str, blocks: list[dict]) -> None:
         """ページにブロックを追記する"""
-        blocks = _split_long_rich_text(blocks)
+        blocks = _normalize_code_languages(_split_long_rich_text(blocks))
         payload = {"children": blocks}
         self._request_with_retry("PATCH", f"{NOTION_API_BASE}/blocks/{page_id}/children", payload)
diff --git a/tests/unit/agent/test_notion_client.py b/tests/unit/agent/test_notion_client.py
index a9debee..96f040e 100644
--- a/tests/unit/agent/test_notion_client.py
+++ b/tests/unit/agent/test_notion_client.py
@@ -103,6 +103,102 @@ class TestSplitLongRichText:
         assert blocks == snapshot
 
 
+class TestNormalizeCodeLanguages:
+    """_normalize_code_languages のテスト (T28, exec-20260713114159 の 400 再発防止)"""
+
+    def _normalize(self, blocks):
+        from storage.notion_client import _normalize_code_languages
+
+        return _normalize_code_languages(blocks)
+
+    def _code_block(self, language):
+        return {
+            "type": "code",
+            "code": {
+                "rich_text": [{"type": "text", "text": {"content": "x"}}],
+                "language": language,
+            },
+        }
+
+    def test_terraform_is_mapped_to_hcl(self):
+        """本番障害の再現: generator プロンプト例由来の terraform を hcl に正規化する。"""
+        result = self._normalize([self._code_block("terraform")])
+        assert result[0]["code"]["language"] == "hcl"
+
+    def test_common_aliases_are_mapped(self):
+        blocks = [self._code_block(lang) for lang in ("yml", "sh", "py", "properties")]
+        result = self._normalize(blocks)
+        assert [b["code"]["language"] for b in result] == [
+            "yaml",
+            "shell",
+            "python",
+            "plain text",
+        ]
+
+    def test_valid_language_passes_through_unchanged(self):
+        original = self._code_block("java")
+        result = self._normalize([original])
+        assert result[0] is original
+
+    def test_unknown_language_falls_back_to_plain_text(self):
+        result = self._normalize([self._code_block("klingon")])
+        assert result[0]["code"]["language"] == "plain text"
+
+    def test_uppercase_valid_language_is_lowercased(self):
+        result = self._normalize([self._code_block("Java")])
+        assert result[0]["code"]["language"] == "java"
+
+    def test_non_string_language_falls_back_to_plain_text(self):
+        result = self._normalize([self._code_block(None)])
+        assert result[0]["code"]["language"] == "plain text"
+
+    def test_non_code_blocks_and_malformed_entries_pass_through(self):
+        blocks = [
+            {"type": "paragraph", "paragraph": {"rich_text": []}},
+            {"type": "code", "code": "not-a-dict"},
+            "not-a-dict-block",
+        ]
+        result = self._normalize(blocks)
+        assert result == blocks
+
+    def test_input_blocks_are_not_mutated(self):
+        original = [self._code_block("terraform")]
+        snapshot = copy.deepcopy(original)
+        self._normalize(original)
+        assert original == snapshot
+
+    @patch("storage.notion_client.requests.request")
+    def test_create_page_normalizes_code_language_in_payload(self, mock_request):
+        """create_page 経由で送信 payload の language が正規化されることを確認。"""
+        from storage.notion_client import NotionClient
+
+        mock_response = mock_request.return_value
+        mock_response.status_code = 200
+        mock_response.raise_for_status.return_value = None
+        mock_response.json.return_value = {"id": "page-id", "url": "https://notion.so/page"}
+
+        client = NotionClient("ntn_test_token", "db-id-123")
+        client.create_page("Test", "技術", [self._code_block("terraform")], None, "U1")
+
+        payload = mock_request.call_args[1]["json"]
+        assert payload["children"][0]["code"]["language"] == "hcl"
+
+    @patch("storage.notion_client.requests.request")
+    def test_append_blocks_normalizes_code_language_in_payload(self, mock_request):
+        from storage.notion_client import NotionClient
+
+        mock_response = mock_request.return_value
+        mock_response.status_code = 200
+        mock_response.raise_for_status.return_value = None
+        mock_response.json.return_value = {}
+
+        client = NotionClient("ntn_test_token", "db-id-123")
+        client.append_blocks("page-id", [self._code_block("yml")])
+
+        payload = mock_request.call_args[1]["json"]
+        assert payload["children"][0]["code"]["language"] == "yaml"
+
+
 class TestNotionClient:
     """NotionClient のテスト"""
 
```
