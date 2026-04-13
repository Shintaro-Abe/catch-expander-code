# 設計書：F9 成果物履歴管理

## 1. 実装アプローチ

### 基本方針

- **Lambda 内完結**。ECS タスクを起動せず、DynamoDB クエリと Slack 投稿をすべて `lambda_handler` の同期処理内で行う
- **`src/trigger/app.py` のみ変更**。`src/agent/` はコンテナ（ECS）側のパッケージであり Lambda からは参照できない。`app.py` は既存の `_find_completed_execution` と同じパターンで boto3 を直接使用する
- **既存 GSI の流用**。`workflow-executions` の `user-id-index` GSI（PK: `user_id`、SK: `created_at`）は F8 フィードバック検索でも使用中。`ScanIndexForward=False` を追加するだけで降順取得が可能

### requirements.md セクション 5 との差異

requirements.md のコンポーネント変更（5-2, 5-3）では `src/agent/state/dynamodb_client.py` と `src/agent/notify/slack_client.py` への変更を記載したが、**これらは ECS コンテナのパッケージであり Lambda から呼び出せない**。正しい変更範囲は下記のとおり。

---

## 2. 変更するコンポーネント

### 変更概要

| ファイル | 種別 | 変更内容 |
|---------|------|---------|
| `src/trigger/app.py` | 変更 | 履歴コマンド検出・DynamoDB クエリ・Slack 投稿の 6 関数追加、`lambda_handler` に分岐追加 |
| `tests/unit/trigger/test_app.py` | 変更 | F9 テスト追加 |

**変更なし:**
- `src/agent/` 配下の全ファイル（ECS コンテナ側、F9 では無変更）
- `template.yaml`（新規 AWS リソース・IAM ロール追加なし）
- DynamoDB スキーマ（既存テーブル・GSI をそのまま使用）

---

## 3. 詳細設計

### 3.1 `lambda_handler` の変更（処理順序）

```
メッセージ受信
  └─ Slack 署名検証（既存）
  └─ リトライ無視（既存）
  └─ イベントタイプ判定（既存）
  └─ bot_id チェック（既存）
  └─ メンション除去 → topic 取得（既存）
  └─ is_thread_reply 判定（既存）
       ↓
  [新規] 履歴コマンド判定（is_thread_reply が False のときのみ）
  └─ _is_history_command(topic) が True
       └─ _extract_history_keyword(topic) → keyword
       └─ _handle_history_command(…) → Slack 投稿
       └─ return 200（ECS 起動なし）
       ↓
  [既存] F8 フィードバック判定（is_thread_reply が True のとき）
       ↓
  [既存] 新規トピックフロー（ECS 起動）
```

**優先順位の設計意図:**  
`is_thread_reply = True` のスレッド返信に `履歴` が含まれる場合は F8 フィードバック判定を通す（F9 に分岐させない）。これにより「成果物スレッドに `履歴について詳しく教えて` と返信した場合」も F8 フィードバックとして拾われる。

### 3.2 追加する 6 関数の仕様

#### `_is_history_command(text: str) -> bool`

```python
def _is_history_command(text: str) -> bool:
    """テキストが履歴コマンドかどうかを判定する。
    「履歴」または「history」（大文字小文字不問）で始まる場合に True を返す。
    メンション除去済みの topic を受け取ることを前提とする。
    """
    lower = text.lower()
    return lower.startswith("履歴") or lower.startswith("history")
```

#### `_extract_history_keyword(text: str) -> str | None`

```python
def _extract_history_keyword(text: str) -> str | None:
    """履歴コマンドのキーワード部分を抽出する。
    「履歴 Terraform」→ "Terraform"
    「history k8s」    → "k8s"
    「履歴」           → None
    """
    if text.lower().startswith("history"):
        rest = text[7:].strip()   # len("history") == 7
    else:
        rest = text[2:].strip()   # len("履歴") == 2（Unicode 文字数）
    return rest if rest else None
```

#### `_query_completed_executions(user_id: str, table_prefix: str) -> list[dict]`

```python
def _query_completed_executions(user_id: str, table_prefix: str) -> list[dict]:
    """user-id-index GSI でユーザーの実行履歴を created_at 降順で取得し、
    status == "completed" のもののみ返す。

    実装ポイント:
    - ScanIndexForward=False → created_at 降順（GSI の SK が created_at）
    - Limit=20 → 最新 20 件を DynamoDB から取得（クライアント側 status フィルタのバッファ）
    - status フィルタはクライアント側で実施（個人利用スケールのため許容）
    """
    table = dynamodb.Table(f"{table_prefix}-workflow-executions")
    response = table.query(
        IndexName="user-id-index",
        KeyConditionExpression=Key("user_id").eq(user_id),
        ScanIndexForward=False,
        Limit=20,
    )
    items = response.get("Items", [])
    return [item for item in items if item.get("status") == "completed"]
```

**Limit の選択根拠:**  
`Limit=20` は DynamoDB API レベルで適用される（FilterExpression より前）。クライアント側でさらに `status == "completed"` でフィルタし、最終的に最大 5 件を返す。個人利用スケール（実行数が数百件以内）であれば 20 件の取得で十分なバッファとなる。

#### `_get_deliverable_url(execution_id: str, table_prefix: str) -> str | None`

```python
def _get_deliverable_url(execution_id: str, table_prefix: str) -> str | None:
    """deliverables テーブルから対象 execution_id の external_url を取得する。
    レコードが存在しない場合は None を返す。

    実装ポイント:
    - deliverables テーブルは PK=execution_id, SK=deliverable_id
    - Limit=1 で最初の 1 件のみ取得（1 実行に複数 deliverable がある場合でも最初の 1 件）
    """
    table = dynamodb.Table(f"{table_prefix}-deliverables")
    response = table.query(
        KeyConditionExpression=Key("execution_id").eq(execution_id),
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0].get("external_url") if items else None
```

#### `_handle_history_command(user_id, channel, msg_ts, keyword, table_prefix, slack_token)`

```python
def _handle_history_command(
    user_id: str,
    channel: str,
    msg_ts: str,
    keyword: str | None,
    table_prefix: str,
    slack_token: str,
) -> None:
    """履歴コマンドを処理する。ECS タスクは起動しない。

    処理フロー:
    1. completed executions を取得（降順）
    2. keyword が指定された場合、topic で部分一致フィルタ（大文字小文字不問）
    3. 先頭 5 件に絞る
    4. 各 execution_id の Notion URL を解決
    5. Slack スレッドに一覧を投稿
    """
    executions = _query_completed_executions(user_id, table_prefix)

    if keyword:
        executions = [
            e for e in executions
            if keyword.lower() in e.get("topic", "").lower()
        ]

    executions = executions[:5]

    items = []
    for e in executions:
        try:
            url = _get_deliverable_url(e["execution_id"], table_prefix)
        except Exception:
            logger.exception(
                "Failed to get deliverable URL",
                extra={"execution_id": e["execution_id"]},
            )
            url = None
        items.append({
            "topic": e.get("topic", ""),
            "category": e.get("category", ""),
            "date": e.get("created_at", "")[:10],  # "YYYY-MM-DD" の先頭 10 文字
            "url": url,
        })

    _post_history_result(channel, msg_ts, items, keyword, slack_token)
```

#### `_post_history_result(channel, thread_ts, items, keyword, slack_token)`

```python
def _post_history_result(
    channel: str,
    thread_ts: str,
    items: list[dict],
    keyword: str | None,
    slack_token: str,
) -> None:
    """成果物一覧を Slack スレッドに投稿する。
    thread_ts には元メッセージの ts を渡す（スレッド返信として投稿）。
    """
    slack_client = WebClient(token=slack_token)

    if not items:
        if keyword:
            text = f"📭 「{keyword}」に一致する成果物は見つかりません。"
        else:
            text = "📭 まだ成果物がありません。トピックを送信すると調査を開始します。"
    else:
        n = len(items)
        header = (
            f"📚 成果物履歴「{keyword}」（最新 {n} 件）"
            if keyword
            else f"📚 成果物履歴（最新 {n} 件）"
        )
        lines = [header, ""]
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. {item['topic']} — {item['category']} — {item['date']}")
            lines.append(f"   {item['url']}" if item["url"] else "   （URL なし）")
        text = "\n".join(lines)

    slack_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
```

### 3.3 `lambda_handler` への追加コード

既存の `is_thread_reply` 算出直後に下記を追加する。

```python
# [F9] 履歴コマンド検出（トップレベル投稿のみ）
if not is_thread_reply and _is_history_command(topic):
    table_prefix_hist = os.environ["DYNAMODB_TABLE_PREFIX"]
    slack_bot_token_hist = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
    keyword = _extract_history_keyword(topic)
    try:
        _handle_history_command(
            user_id, channel, event_msg_ts, keyword, table_prefix_hist, slack_bot_token_hist
        )
    except Exception:
        logger.exception("History command failed", extra={"user_id": user_id, "keyword": keyword})
        slack_client_err = WebClient(token=slack_bot_token_hist)
        slack_client_err.chat_postMessage(
            channel=channel,
            thread_ts=event_msg_ts,
            text="❌ 履歴の取得中にエラーが発生しました。しばらく経ってから再試行してください。",
        )
    return {"statusCode": 200, "body": ""}

# [既存] F8 フィードバック判定
if is_thread_reply:
    ...
```

---

## 4. データ構造の変更

### 変更なし

本機能は既存テーブルを読み取るのみ。スキーマ変更・新規テーブル・GSI 追加は一切行わない。

| テーブル | 使用するフィールド | 操作 |
|---------|-----------------|------|
| `{prefix}-workflow-executions` | `user_id`（GSI PK）, `created_at`（GSI SK）, `status`, `topic`, `category`, `execution_id` | `Query`（読み取りのみ） |
| `{prefix}-deliverables` | `execution_id`（PK）, `deliverable_id`（SK）, `external_url` | `Query`（読み取りのみ） |

---

## 5. 影響範囲の分析

### 既存テストへの影響

| テストファイル | 影響 | 対処 |
|-------------|------|------|
| `tests/unit/trigger/test_app.py` | `lambda_handler` に分岐追加 → 既存テストはパスするが F9 ケースが未カバー | F9 テストを追加 |
| `tests/unit/agent/` 配下 | 変更なし | 対処不要 |

### 既存動作への影響

- 履歴コマンドを使わないユーザーには影響なし（`_is_history_command` が False を返し fall through）
- `is_thread_reply = True` の場合、`_is_history_command` は評価されない（F8 フロー優先）
- ECS タスク起動ロジックは一切変更しない

---

## 6. テスト設計

### 新規テスト（`tests/unit/trigger/test_app.py` へ追加）

#### `_is_history_command` のテスト

| 入力 | 期待値 | 観点 |
|------|--------|------|
| `"履歴"` | `True` | 基本コマンド（キーワードなし） |
| `"履歴 Terraform"` | `True` | スペース区切りキーワードあり |
| `"履歴Terraform"` | `True` | スペースなしキーワードあり |
| `"history"` | `True` | 英語コマンド（小文字） |
| `"History k8s"` | `True` | 英語コマンド（大文字始まり） |
| `"history k8s overview"` | `True` | 複合キーワード |
| `"Terraform入門"` | `False` | 通常トピック |
| `"フィードバック"` | `False` | 非コマンド日本語テキスト |

#### `_extract_history_keyword` のテスト

| 入力 | 期待値 |
|------|--------|
| `"履歴"` | `None` |
| `"履歴 Terraform"` | `"Terraform"` |
| `"履歴Terraform"` | `"Terraform"` |
| `"history"` | `None` |
| `"history k8s overview"` | `"k8s overview"` |
| `"History Terraform"` | `"Terraform"` |

#### `_query_completed_executions` のテスト

- DynamoDB モック: `completed` × 3 件 + `failed` × 1 件 + `in_progress` × 1 件 → `completed` 3 件のみ返すこと
- DynamoDB モック: 結果が空 → 空リストを返すこと
- `ScanIndexForward=False` が DynamoDB クエリに渡されていること

#### `_get_deliverable_url` のテスト

- `deliverables` にレコードあり → `external_url` の値を返すこと
- `deliverables` にレコードなし → `None` を返すこと

#### `_handle_history_command` の統合テスト（外部依存はモック）

- キーワードなし: `_query_completed_executions` の返却値をそのまま最大 5 件で `_post_history_result` に渡すこと
- キーワード `"Terraform"`: topic に `"Terraform"` を含む実行のみ絞り込まれること（大文字小文字不問）
- 件数が 6 件以上: 5 件に切り捨てられること
- `_get_deliverable_url` が `None` を返す場合: `url: None` の items が `_post_history_result` に渡されること

#### `lambda_handler` ルーティングのテスト

| 条件 | 期待動作 |
|------|---------|
| `text="履歴"`, トップレベル | `_handle_history_command` 呼び出し, ECS RunTask 非呼び出し, HTTP 200 返却 |
| `text="history Terraform"`, トップレベル | `_handle_history_command` に `keyword="Terraform"` で呼び出し |
| `text="履歴"`, スレッド返信（`thread_ts != ts`） | `_handle_history_command` 非呼び出し, F8 判定フローへ |
| `text="Terraform入門"`, トップレベル | `_handle_history_command` 非呼び出し, 既存フロー（ECS 起動）へ |
| `_handle_history_command` が例外を raise | エラーメッセージを Slack に投稿し, HTTP 200 を返すこと |

---

## 7. 考慮事項・将来対応

### DynamoDB ページネーション

`Limit=20` を超える完了済み実行が存在する場合、古い実行は取得されない。
個人利用スケールでは当面問題ないが、利用が増えた場合は `ScanIndexForward=False` + `Limit=N` を繰り返すページネーションループに切り替える。

### Slack ブロックキット対応

現時点では `text` フィールドのみで投稿する（Slack の Block Kit は使用しない）。
リンクが長い場合の見た目改善が必要になった場合は `blocks` パラメータを追加して `section` + `context` ブロックに切り替える。

### チャンネルでのコマンド利用

現在の実装では DM と `app_mention` の両方を受け付ける。
チャンネルでは `@CatchExpander 履歴` のようにメンション付きで送信する必要がある（メンション除去後に `履歴` が残る）。DM では `履歴` のみで動作する。
