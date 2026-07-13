# Codex レビュー Pass 2: Notion language 正規化の Pass 1 是正確認

## 前提

Pass 1（.audit/2026-07-13_notion-code-language.md）の P1×2 に以下の是正を実施:

- P1-1（enum 妥当性）: ライブ Notion API へ検証プローブを実行（意図的な invalid language の
  POST /pages → 400 validation_error 応答から enum 全 90 値を取得）。実装の
  `_NOTION_CODE_LANGUAGES` frozenset とライブ enum は**完全一致**（過不足ゼロ）を確認。
  hcl / toml / solidity 等、公式ドキュメントに無い値もすべてライブ側に存在した
  （docs の方が実 API と乖離）。観測値を正とする旨をコードコメントに明記
- P1-2（ネスト漏れ）: `_normalize_block_code_language` に再帰化。type payload 内の
  `children` list を再帰処理し、変更経路のみ shallow copy、無変更時は同一オブジェクトを
  返して identity を保つ。ネスト正規化 + identity 保持の回帰テスト 2 件追加

テスト: 全 550 passed。新規 lint なし。

## レビュー観点

- Pass 1 の P1-1 / P1-2 是正が意図どおりか
- 再帰実装の問題（zip(strict=True) の妥当性、code ブロック自身が children を持つ端ケース、
  深いネストでの性能、非 mutate 契約の破れ）がないか
- 上記以外の新規問題

## 対象差分 (git diff 97c1c45..773cf77 — Pass 1 是正分のみ)

```diff
diff --git a/src/agent/storage/notion_client.py b/src/agent/storage/notion_client.py
index d3d4871..f6a4e56 100644
--- a/src/agent/storage/notion_client.py
+++ b/src/agent/storage/notion_client.py
@@ -47,7 +47,10 @@ class NotionCloudflareBlockError(Exception):
 
 
 # Notion API code block の language 許容値 (API version 2022-06-28)。
-# 2026-07-13 の 400 validation_error 応答 enum + API リファレンスより。
+# 2026-07-13 にライブ API へ検証プローブ (意図的な invalid language の POST /pages →
+# 400 応答の enum 全 90 値) を実行し、本セットと完全一致を確認済み (Codex T28 Pass 1
+# P1-1 の検証。公式ドキュメントは hcl 等を欠いており実 API と乖離しているため、
+# 観測値を正とする)。
 # LLM 生成の content_blocks は許容外の値 (例: "terraform", "yml", "properties") を
 # 出し得るため、送信前に _normalize_code_languages で決定的に正規化する
 # (T28, exec-20260713114159。_split_long_rich_text と同じ検証層パターン)。
@@ -115,30 +118,51 @@ def _normalize_code_language(language) -> str:
     return _CODE_LANGUAGE_ALIASES.get(lowered, "plain text")
 
 
+def _normalize_block_code_language(block):
+    """1 ブロック (+ ネスト children) の code.language を正規化する。
+
+    toggle / bulleted_list_item 等は type payload 内に children を持てるため再帰する
+    (Codex T28 Pass 1 P1-2)。変更が無い場合は入力オブジェクトをそのまま返し、
+    変更がある経路のみ shallow copy で再構築する (非 mutate 規律)。
+    """
+    if not isinstance(block, dict):
+        return block
+    new_block = block
+
+    code_payload = block.get("code")
+    if isinstance(code_payload, dict):
+        original = code_payload.get("language")
+        normalized = _normalize_code_language(original)
+        if normalized != original:
+            logger.info(
+                "Normalized Notion code block language | original=%r normalized=%r",
+                original,
+                normalized,
+            )
+            new_block = {**new_block, "code": {**code_payload, "language": normalized}}
+
+    block_type = block.get("type")
+    if isinstance(block_type, str):
+        type_payload = new_block.get(block_type)
+        if isinstance(type_payload, dict):
+            children = type_payload.get("children")
+            if isinstance(children, list):
+                new_children = [_normalize_block_code_language(c) for c in children]
+                if any(n is not o for n, o in zip(new_children, children, strict=True)):
+                    new_block = {
+                        **new_block,
+                        block_type: {**type_payload, "children": new_children},
+                    }
+    return new_block
+
+
 def _normalize_code_languages(blocks: list[dict]) -> list[dict]:
     """code block の language を許容値に正規化した新しい list を返す。
 
     入力 list / dict は mutate しない (_split_long_rich_text と同じ規律)。
-    code block 以外・不正構造はそのまま通す。
+    code block 以外・不正構造はそのまま通す。ネスト children も再帰的に処理する。
     """
-    result: list[dict] = []
-    for block in blocks:
-        code_payload = block.get("code") if isinstance(block, dict) else None
-        if not isinstance(code_payload, dict):
-            result.append(block)
-            continue
-        original = code_payload.get("language")
-        normalized = _normalize_code_language(original)
-        if normalized == original:
-            result.append(block)
-            continue
-        logger.info(
-            "Normalized Notion code block language | original=%r normalized=%r",
-            original,
-            normalized,
-        )
-        result.append({**block, "code": {**code_payload, "language": normalized}})
-    return result
+    return [_normalize_block_code_language(block) for block in blocks]
 
 
 def _is_cloudflare_block(status_code: int, body: str) -> bool:
diff --git a/tests/unit/agent/test_notion_client.py b/tests/unit/agent/test_notion_client.py
index 96f040e..dfc965c 100644
--- a/tests/unit/agent/test_notion_client.py
+++ b/tests/unit/agent/test_notion_client.py
@@ -167,6 +167,41 @@ class TestNormalizeCodeLanguages:
         self._normalize(original)
         assert original == snapshot
 
+    def test_nested_children_are_normalized_recursively(self):
+        """Codex T28 Pass 1 P1-2: toggle 等の type payload 内 children の code block も
+        正規化する（トップレベルのみだと 400 が再発する）。"""
+        toggle = {
+            "type": "toggle",
+            "toggle": {
+                "rich_text": [{"type": "text", "text": {"content": "詳細"}}],
+                "children": [
+                    self._code_block("terraform"),
+                    {
+                        "type": "bulleted_list_item",
+                        "bulleted_list_item": {
+                            "rich_text": [],
+                            "children": [self._code_block("yml")],
+                        },
+                    },
+                ],
+            },
+        }
+        snapshot = copy.deepcopy(toggle)
+        result = self._normalize([toggle])
+        children = result[0]["toggle"]["children"]
+        assert children[0]["code"]["language"] == "hcl"
+        nested = children[1]["bulleted_list_item"]["children"]
+        assert nested[0]["code"]["language"] == "yaml"
+        assert toggle == snapshot  # 非 mutate
+
+    def test_nested_children_without_changes_keep_identity(self):
+        toggle = {
+            "type": "toggle",
+            "toggle": {"rich_text": [], "children": [self._code_block("java")]},
+        }
+        result = self._normalize([toggle])
+        assert result[0] is toggle
+
     @patch("storage.notion_client.requests.request")
     def test_create_page_normalizes_code_language_in_payload(self, mock_request):
         """create_page 経由で送信 payload の language が正規化されることを確認。"""
```
