# 設計: deliverables への GitHub URL 永続化

## 実装アプローチ

### 全体方針

1. `deliverables` テーブルに **オプショナルな `github_url` フィールド** を追加する。スキーマレスのため DynamoDB 側のマイグレーションは不要。
2. 書き込み側 (`Orchestrator`) は `github_url` が非 None のとき put_item ペイロードに乗せる。None のときはフィールド自体を含めない (条件付き付与) ことで `storage="notion"` レコードに余計な空フィールドを残さない。
3. 読み取り側 (F9 履歴コマンド) は `external_url` (Notion URL) と `github_url` (任意) の両方を取得し、Slack 出力に併記する。`github_url` 未存在なら従来どおり Notion URL のみ表示。
4. 関連ドキュメント (`docs/functional-design.md` 第 5 節 / 第 7 節) を本変更に追従させる。
5. テストは既存のフィクスチャ・モック方式 (`moto` または直接 mock) に従い、新フィールドの保存と表示・後方互換挙動をカバーする。

### 設計上のトレードオフと判断

| 論点 | 案 A (採用) | 案 B (不採用) | 判断 |
|---|---|---|---|
| フィールド命名 | `github_url` | `code_url` / `urls.github` (ネスト) | 既存 `external_url` と直交した平らな単数フィールドが最も読みやすく、テストモックが書きやすい。将来他ターゲットが増えたら別途再設計 |
| 未保存時の表現 | フィールド自体を省略 | `null` を明示的に書く | DynamoDB のスキーマレス性を活かし「フィールドなし = 該当なし」と一意に読み取れる方が `.get()` パターンと整合する。`null` は項目数のみ増やして情報量同じ |
| F9 表示形式 | Notion を主リンク、GitHub を 2 行目にインデントで併記 | 同一行に「Notion / GitHub」と並列 | 既存レイアウト (`{i}. topic — category — date` + 2 行目 URL) を最小限に拡張。改行追加だけで Slack の見た目が壊れない |
| 既存レコードの扱い | 遡及補完しない (require: 制約事項参照) | スクレイピングで補完 | コスト過大かつ精度低。新規分のみ保存し、既存は Notion URL のみで運用 |

## 変更するコンポーネント

### 1. `src/agent/orchestrator.py` (書き込み側)

**現在 (640-658 行)**:

```python
self.db.put_deliverable(
    {
        "execution_id": execution_id,
        "deliverable_id": f"dlv-{execution_id}",
        "type": "all",
        "storage": "notion" if not github_url else "notion+github",
        "external_url": notion_url,
        "quality_metadata": quality_metadata,
    }
)
```

**変更後**:

```python
deliverable_record = {
    "execution_id": execution_id,
    "deliverable_id": f"dlv-{execution_id}",
    "type": "all",
    "storage": "notion" if not github_url else "notion+github",
    "external_url": notion_url,
    "quality_metadata": quality_metadata,
}
if github_url:
    deliverable_record["github_url"] = github_url
self.db.put_deliverable(deliverable_record)
```

差分は条件付き 2 行のみ。`put_deliverable` ヘルパー側 (`dynamodb_client.py:107-110`) はスキーマレスのため改修不要。

### 2. `src/trigger/app.py` (読み取り側 / F9 履歴コマンド)

#### 2.1 `_get_deliverable_url` の戻り値拡張 (83-93 行)

**現在**: `str | None` を返す。

**変更後**: `dict | None` を返す。`{"notion_url": str, "github_url": str | None}` 形式。

```python
def _get_deliverable_urls(execution_id: str, table_prefix: str) -> dict | None:
    """deliverables テーブルから対象 execution_id の URL 群を取得する。
    レコードが存在しない場合は None を返す。
    """
    table = dynamodb.Table(f"{table_prefix}-deliverables")
    response = table.query(
        KeyConditionExpression=Key("execution_id").eq(execution_id),
        Limit=1,
    )
    items = response.get("Items", [])
    if not items:
        return None
    item = items[0]
    return {
        "notion_url": item.get("external_url"),
        "github_url": item.get("github_url"),
    }
```

関数名は意味の変化を反映して `_get_deliverable_url` → `_get_deliverable_urls` (複数形) に改名する。

#### 2.2 `_handle_history_command` の呼び出し更新 (104-131 行)

`url` 単一フィールドを `notion_url` / `github_url` に分離して `items` に格納:

```python
items.append(
    {
        "topic": e.get("topic", ""),
        "category": e.get("category", ""),
        "date": e.get("created_at", "")[:10],
        "notion_url": urls["notion_url"] if urls else None,
        "github_url": urls["github_url"] if urls else None,
    }
)
```

#### 2.3 `_post_history_result` の表示拡張 (134-158 行)

**現在 (152-156 行)**:

```python
for i, item in enumerate(items, 1):
    lines.append(f"{i}. {item['topic']} — {item['category']} — {item['date']}")
    lines.append(f"   {item['url']}" if item["url"] else "   （URL なし）")
```

**変更後**:

```python
for i, item in enumerate(items, 1):
    lines.append(f"{i}. {item['topic']} — {item['category']} — {item['date']}")
    if item.get("notion_url"):
        lines.append(f"   📝 {item['notion_url']}")
    if item.get("github_url"):
        lines.append(f"   💻 {item['github_url']}")
    if not item.get("notion_url") and not item.get("github_url"):
        lines.append("   （URL なし）")
```

絵文字 (📝 / 💻) で視覚的にリンク種別を区別する。`code_files` がない成果物 (`storage="notion"`) は従来どおり Notion 行のみ表示される。

### 3. `tests/unit/agent/test_orchestrator.py` (書き込み側テスト)

追加するケース:

| ケース | 検証内容 |
|---|---|
| `test_put_deliverable_with_github_url` | `github_url` が非 None のとき put_item ペイロードに `github_url` キーが含まれ、値が正しい |
| `test_put_deliverable_without_github_url` | `code_files` が空のとき put_item ペイロードに `github_url` キーが含まれない (KeyError で確認) |

既存テストの `mock_db.put_deliverable.assert_called_once_with(...)` の期待値を、条件付きペイロードに合わせて更新する箇所が出る可能性あり。

### 4. `tests/unit/trigger/test_app.py` (読み取り側テスト)

追加するケース:

| ケース | 検証内容 |
|---|---|
| `test_history_command_displays_github_url` | レコードに `github_url` が含まれるとき、Slack 投稿本文に `💻 https://github.com/...` 行が現れる |
| `test_history_command_omits_github_url_when_absent` | `github_url` がないとき、Slack 投稿本文に GitHub 行が **現れない** (Notion 行のみ) |
| `test_history_command_handles_legacy_record` | 既存形式 (`github_url` フィールド未存在) のレコードでも例外を出さず、Notion 行のみ表示する |
| `test_history_command_no_url` | `external_url` も `github_url` もないとき「（URL なし）」表示が維持される |

### 5. `docs/functional-design.md` (ドキュメント更新)

#### 5.1 第 5 節 `deliverables` テーブル定義

`github_url` フィールドを追加。

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `github_url` | String | 任意 | コード成果物の GitHub ディレクトリ URL。`storage` が `"notion+github"` の場合のみ存在 |

#### 5.2 第 7 節 F9 履歴コマンド出力例

Slack 投稿サンプルを以下のように更新:

```
📚 成果物履歴（最新 5 件）

1. WSLとDocker Desktop — 技術 — 2026-04-26
   📝 https://www.notion.so/WSL-Docker-Desktop-...
   💻 https://github.com/Shintaro-Abe/catch-expander-deliverables/tree/main/wslとdocker-desktop-20260426
2. Open Telemetry — 技術 — 2026-04-26
   📝 https://www.notion.so/Open-Telemetry-...
   💻 https://github.com/.../open-telemetry-20260426
3. ...
```

## データ構造の変更

### `deliverables` テーブル (DynamoDB)

| フィールド | 型 | 状態 | 備考 |
|---|---|---|---|
| `execution_id` | String (PK) | 既存 | 変更なし |
| `deliverable_id` | String (SK) | 既存 | 変更なし |
| `type` | String | 既存 | 変更なし |
| `storage` | String | 既存 | 値の意味変わらず (`notion` / `notion+github`) |
| `external_url` | String | 既存 | Notion URL のみ (意味維持) |
| **`github_url`** | **String** | **追加 (任意)** | `storage="notion+github"` のとき非 None |
| `quality_metadata` | Map | 既存 | 変更なし |
| `created_at` | String | 既存 | 変更なし |

GSI は変更なし。`storage="notion+github"` で `github_url` が空、または `storage="notion"` で `github_url` が存在する状態は **作らない**。整合性は呼び出し側 (`Orchestrator`) で保証する (アサーションは入れない — 設計時の暗黙ルール)。

## 影響範囲の分析

### 直接影響

| ファイル | 変更内容 | 行数 (概算) |
|---|---|---|
| `src/agent/orchestrator.py` | 条件付き put_item ペイロード | +3 / -1 |
| `src/trigger/app.py` | 関数改名・戻り値拡張・表示分岐 | +15 / -5 |
| `tests/unit/agent/test_orchestrator.py` | テスト追加・既存期待値更新 | +30 / -5 |
| `tests/unit/trigger/test_app.py` | テスト 4 ケース追加 | +60 / -5 |
| `docs/functional-design.md` | スキーマ表 + F9 出力例更新 | +10 / -2 |

### 間接影響

- **Lambda コードパッケージサイズ**: 変化なし (純粋なロジック追加)
- **DynamoDB の RU/WU 消費**: ほぼ変化なし (`github_url` は典型 ~80 文字。1 アイテムサイズ増は 0.1% 未満)
- **F9 コマンドのレスポンスタイム**: 変化なし (同一 query を流用)
- **CloudFormation Stack**: 再デプロイ不要 (テーブルスキーマ変更を伴わない)
- **本番デプロイ手順**: 既存どおり `sam deploy` でアプリケーションコードのみ更新

### 後方互換性

| 観点 | 動作 |
|---|---|
| 既存レコード (本変更前に書かれた deliverable) | `github_url` フィールド未存在。F9 コマンドで Notion URL のみ表示 |
| 新規レコード × `storage="notion"` | `github_url` フィールド未存在。F9 コマンドで Notion URL のみ表示 |
| 新規レコード × `storage="notion+github"` | `github_url` 存在。F9 コマンドで Notion + GitHub 両方表示 |
| `_get_deliverable_url` を直接呼ぶ外部コード | **存在しない** (`grep` で確認済み)。改名して問題なし |

### ロールバック戦略

問題発生時:

1. `src/agent/orchestrator.py` の put 拡張をコメントアウト (新規レコードへの保存停止)
2. `src/trigger/app.py` の F9 表示拡張をコメントアウト (Notion URL のみ表示に戻す)
3. すでに保存された `github_url` フィールドはレコードに残るが、誰も参照しないため無害

完全なリバートは git revert 1 コミットで可能。テーブルスキーマ変更がないため DynamoDB 側のロールバック作業は不要。

## 実装順序 (推奨)

| 順序 | 内容 | 理由 |
|---|---|---|
| 1 | `src/agent/orchestrator.py` の put 拡張 + ユニットテスト | 書き込み先行で新規レコードに `github_url` を貯め始める |
| 2 | `src/trigger/app.py` の読み取り拡張 + ユニットテスト | 書き込み済みのレコードを表示できる状態にする |
| 3 | `docs/functional-design.md` 更新 | 実装と仕様を同期 |
| 4 | デプロイ (`sam deploy`) | 本番反映 |
| 5 | 動作確認: 新規ワークフロー実行 → F9 履歴コマンドで GitHub リンクが表示されることを確認 | E2E |

順序 1 と 2 を分割するメリット: 順序 1 のみ先行マージしても F9 表示は壊れない (新フィールドは無視される) ため、デプロイのリスクが低い。ただし両者を 1 PR にまとめても可 (規模が小さいため)。

## 受け入れ条件との対応

| AC | 検証手段 |
|---|---|
| AC1 (新規保存) | `test_put_deliverable_with_github_url` |
| AC2 (F9 表示) | `test_history_command_displays_github_url` + 本番動作確認 |
| AC3 (Notion only レイアウト維持) | `test_history_command_omits_github_url_when_absent` |
| AC4 (既存レコード後方互換) | `test_history_command_handles_legacy_record` |
| AC5 (ユニットテスト) | 上記 4 ケースの追加 |
| AC6 (ドキュメント整合) | `docs/functional-design.md` 第 5 節 + 第 7 節の更新 |
