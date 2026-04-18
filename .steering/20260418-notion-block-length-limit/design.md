# 設計: Notion ブロック長制限対応

## 設計方針

`notion_client.py` の境界（API 投入の直前）で `content_blocks` を走査し、`rich_text[N].text.content` が 2000 文字を超える要素を **同一ブロック内の rich_text 配列を 2000 文字単位で分割** する。

- 呼び出し元（orchestrator / generator）は無変更
- `_split_long_rich_text(blocks: list[dict]) -> list[dict]` をモジュールレベル関数として追加
- `create_page` と `append_blocks` の両方の入口で適用（Notion ページ作成と追記の両経路で同じ制限が効くため）
- ブロック総数は変えない（同一ブロック内の rich_text 配列のみ膨らむ）→ 既存の 100-children chunk ロジックとは干渉しない

## 現状調査の結果

| 項目 | 現状 |
|------|------|
| content_blocks の生成元 | `generator.md` プロンプト経由で Claude が JSON 生成 |
| content_blocks の経路 | orchestrator → `notion.create_page(content_blocks=...)` → `_request_with_retry` で POST |
| 100-children chunk 処理 | `notion_client.create_page` 内で `content_blocks[:100]` を最初の payload、残りを `append_blocks` で 100 ずつ追記 |
| rich_text 配列 | generator は通常 1 要素のみで生成。複数要素の生成は観測されていない |
| 観測された違反 | `code` ブロックの `rich_text[0].text.content` = 3921 char |
| 影響範囲ブロック | rich_text を持つ全タイプ（code / paragraph / heading_* / bulleted_list_item / numbered_list_item / quote / callout / toggle）|

## 影響範囲

| ファイル | 変更内容 | 必須/任意 |
|----------|----------|-----------|
| `src/agent/storage/notion_client.py` | `_NOTION_RICH_TEXT_MAX_CHARS=2000` 定数 / `_split_long_rich_text(blocks)` 関数を追加。`create_page` と `append_blocks` 入口で適用 | 必須 |
| `tests/unit/agent/storage/test_notion_client.py`（無ければ新規）| `_split_long_rich_text` のユニットテスト | 必須 |
| なし | generator / orchestrator のロジックは無変更 | - |

## 分割アルゴリズム

### 入力 / 出力

```python
def _split_long_rich_text(blocks: list[dict]) -> list[dict]:
    """
    各ブロックの rich_text 配列内で content が 2000 文字を超える要素を
    2000 文字チャンクに分割した新しい list を返す。元の list は変更しない。
    """
```

### 処理フロー

1. 各 block について type を見て rich_text を含む可能性のあるキー（block["type"] と同名キー）の `rich_text` 配列を取り出す
2. rich_text が無い / 配列でない場合はそのまま
3. rich_text 内の各 element について:
   - `element["text"]["content"]` が 2000 文字以下: そのまま保持
   - 超える場合: 2000 文字ごとに分割し、同一の他フィールド（`type`, `annotations`, `link` 等）を保持した複数 element に展開
4. 分割後の rich_text 配列を新しい block にセット

### 分割対象キー判定

block["type"] は `code` / `paragraph` / `heading_1` / `heading_2` / `heading_3` / `bulleted_list_item` / `numbered_list_item` / `quote` / `callout` / `toggle` のいずれか。
これらは `block[block["type"]]["rich_text"]` でアクセス可能（Notion API スキーマ規約）。

→ 個別タイプ列挙ではなく **block["type"] の値をキーに使う動的アクセス** で実装する（generator が将来別タイプを返しても自動的に追従）。
type に対応するキーが存在しない / dict でない / `rich_text` が無い場合はスキップ（堅牢に）。

### 安全マージン

`_NOTION_RICH_TEXT_MAX_CHARS = 2000` を厳密に使う。Notion API のエラーメッセージが「`should be ≤ 2000`」なので、ちょうど 2000 までは許容。安全マージンは入れない（過剰な分割を避ける）。

### 分割例

**Before:**
```python
{
  "type": "code",
  "code": {
    "rich_text": [{"type": "text", "text": {"content": "x" * 3921}}],
    "language": "terraform"
  }
}
```

**After:**
```python
{
  "type": "code",
  "code": {
    "rich_text": [
      {"type": "text", "text": {"content": "x" * 2000}},
      {"type": "text", "text": {"content": "x" * 1921}}
    ],
    "language": "terraform"
  }
}
```

## 適用箇所

`notion_client.py`:

```python
def create_page(self, ..., content_blocks: list[dict], ...) -> tuple[str, str]:
    content_blocks = _split_long_rich_text(content_blocks)
    ...
    if content_blocks:
        payload["children"] = content_blocks[:max_children]
    ...
    remaining = content_blocks[max_children:]
    for i in range(0, len(remaining), max_children):
        chunk = remaining[i : i + max_children]
        self.append_blocks(page_id, chunk)
    return page_url, page_id

def append_blocks(self, page_id: str, blocks: list[dict]) -> None:
    blocks = _split_long_rich_text(blocks)
    payload = {"children": blocks}
    self._request_with_retry(...)
```

`create_page` 経由の追記でも `append_blocks` を呼ぶため二重適用になるが、2000 char 以下のものは何もしないので冪等。

## ユニットテスト計画

`tests/unit/agent/storage/test_notion_client.py`:

| テスト名 | ケース |
|---------|--------|
| `test_split_short_rich_text_unchanged` | 2000 char 以下はそのまま |
| `test_split_exactly_2000_chars_unchanged` | ちょうど 2000 はそのまま |
| `test_split_over_2000_code_block` | code ブロックの 3921 char が 2000+1921 に分割される |
| `test_split_preserves_language_and_annotations` | 分割後も `language` 等のメタが保持される |
| `test_split_preserves_other_rich_text_fields` | element の `type` / `annotations` / `link` 等が分割後も保持 |
| `test_split_handles_multiple_blocks` | 複数ブロック中の長尺 1 件のみ分割 |
| `test_split_handles_block_without_rich_text` | divider など rich_text が無い block は無変更 |
| `test_split_handles_unknown_type_safely` | 未知 type / 不正構造でも例外を出さずスキップ |
| `test_split_does_not_mutate_input` | 入力 list / dict が変更されない |

`requests` をモックする既存パターン（あれば）に倣う。`_request_with_retry` の呼び出し回数が変わらないことも軽く確認（オプション）。

## 検証フロー

### Step 1: ユニットテスト全件パス

```bash
pytest tests/ -q
```

### Step 2: Phase A 観測 execution の再現確認

CloudWatch 09:01:37 UTC で失敗した execution と同じトピック（API Gateway と Lambda）を Slack 投入し、長尺コードが含まれる成果物を生成 → ページ作成成功を確認。

| 確認項目 | 確認方法 |
|---------|---------|
| Notion 400 が出ない | CloudWatch `Notion API client error` フィルタ |
| ページ作成成功 | `Notion page created` ログ |
| 分割 code ブロックが Notion 上で正しく表示 | 該当 Notion ページを目視 |
| storage="notion+github" | DynamoDB `catch-expander-deliverables` |
| status=completed | DynamoDB `catch-expander-workflow-executions` |

### Step 3: 関連 steering AC 同時検証

同 execution で:

- **Followup-A AC-3**: storage="notion+github" 2 回連続 → 1 回成功すれば本 steering の Step 2 と兼ねる、もう 1 回投入で達成
- **Followup-B AC-1**: fix loop 発火（受動）→ 出れば AC-2 の 3 系統反映確認に進む

## 非機能要件

- **後方互換**: `notion_client.create_page` / `append_blocks` のシグネチャ無変更
- **パフォーマンス**: 各 block を 1 回走査するだけ。総 char 数に対して O(N)。実用上無視可能
- **メモリ**: 入力 list を mutate せず新 list を返すため一時的に 2 倍程度。block 数は最大数百なので影響なし
- **冪等性**: 既に 2000 以下のものに適用しても無変化

## リスクと対処

| リスク | 対処 |
|--------|------|
| 分割境界が UTF-16 サロゲートペアの中に入る（絵文字等） | 現状 generator が出力するコードは ASCII / 日本語混在程度で深刻な絵文字使用は無し。観測されたら別途 codepoint 境界保護を追加 |
| Notion 側の文字数カウントが Python の len() と異なる | 観測されたら `_NOTION_RICH_TEXT_MAX_CHARS` を 1900 等に下げる（定数 1 行変更で対応可能）|
| 分割により元のコード可読性が下がる | code ブロック内の rich_text 配列要素は連続表示されるため可読性は維持される（Notion 側仕様）|
| 100-children chunk ロジックとの干渉 | 同一ブロック内の rich_text 配列のみ膨らみ、block 総数は不変のため干渉なし |

## スコープ外（再掲）

- generator プロンプトでのコード長抑制
- Notion 側の他制限（タイトル長 / URL 長 / リクエストサイズ）
- Slack 汎用エラー文言（OAuth 切れ案内）の改善
- code_files の GitHub 側ストレージ（既に成功）
