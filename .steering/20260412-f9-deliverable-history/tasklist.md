# タスクリスト：F9 成果物履歴管理

## 概要

`design.md` の設計に基づくタスクリスト。依存関係順に並べている。

---

## Phase 1: ヘルパー関数の実装（`src/trigger/app.py`）

### 1-1. `_is_history_command` の実装

- [x] `_is_history_command(text: str) -> bool` を `app.py` に追加する
  - [x] `text.lower().startswith("履歴")` または `text.lower().startswith("history")` のとき `True` を返す
  - [x] それ以外は `False` を返す

### 1-2. `_extract_history_keyword` の実装

- [x] `_extract_history_keyword(text: str) -> str | None` を `app.py` に追加する
  - [x] `text.lower().startswith("history")` の場合、先頭 7 文字を除いた残りを `.strip()` してキーワードとする
  - [x] それ以外（`履歴`）の場合、先頭 2 文字を除いた残りを `.strip()` してキーワードとする
  - [x] キーワードが空文字の場合 `None` を返す

### 1-3. `_query_completed_executions` の実装

- [x] `_query_completed_executions(user_id: str, table_prefix: str) -> list[dict]` を `app.py` に追加する
  - [x] `dynamodb.Table(f"{table_prefix}-workflow-executions")` を参照する
  - [x] `IndexName="user-id-index"`, `KeyConditionExpression=Key("user_id").eq(user_id)` でクエリする
  - [x] `ScanIndexForward=False` を指定して `created_at` 降順で取得する
  - [x] `Limit=20` を指定する
  - [x] レスポンスの `Items` のうち `status == "completed"` のものだけをリストで返す

### 1-4. `_get_deliverable_url` の実装

- [x] `_get_deliverable_url(execution_id: str, table_prefix: str) -> str | None` を `app.py` に追加する
  - [x] `dynamodb.Table(f"{table_prefix}-deliverables")` を参照する
  - [x] `KeyConditionExpression=Key("execution_id").eq(execution_id)` と `Limit=1` でクエリする
  - [x] `Items` が空の場合 `None` を返す
  - [x] 取得できた場合は `items[0].get("external_url")` を返す

### 1-5. `_post_history_result` の実装

- [x] `_post_history_result(channel, thread_ts, items, keyword, slack_token)` を `app.py` に追加する
  - [x] `WebClient(token=slack_token)` でクライアントを生成する
  - [x] `items` が空 かつ `keyword` が `None` の場合：`📭 まだ成果物がありません。トピックを送信すると調査を開始します。` を投稿する
  - [x] `items` が空 かつ `keyword` が指定されている場合：`📭 「{keyword}」に一致する成果物は見つかりません。` を投稿する
  - [x] `items` が 1 件以上 かつ `keyword` が `None` の場合：ヘッダーを `📚 成果物履歴（最新 N 件）` とする
  - [x] `items` が 1 件以上 かつ `keyword` が指定されている場合：ヘッダーを `📚 成果物履歴「{keyword}」（最新 N 件）` とする
  - [x] 各 item を `{i}. {topic} — {category} — {date}` の形式で列挙する
  - [x] `url` がある場合は次の行に `   {url}` を記載する
  - [x] `url` がない場合は次の行に `   （URL なし）` を記載する
  - [x] `chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)` で投稿する

### 1-6. `_handle_history_command` の実装

- [x] `_handle_history_command(user_id, channel, msg_ts, keyword, table_prefix, slack_token)` を `app.py` に追加する
  - [x] `_query_completed_executions(user_id, table_prefix)` で完了済み実行リストを取得する
  - [x] `keyword` が指定されている場合、`keyword.lower() in e.get("topic", "").lower()` でフィルタする
  - [x] フィルタ後のリストを `[:5]` で最大 5 件に絞る
  - [x] 各実行について `_get_deliverable_url(e["execution_id"], table_prefix)` で URL を解決する（例外時は `url=None` として継続）
  - [x] `items` リストを `{"topic", "category", "date", "url"}` の辞書形式で組み立てる（`date` は `created_at` の先頭 10 文字）
  - [x] `_post_history_result(channel, msg_ts, items, keyword, slack_token)` を呼び出す

---

## Phase 2: `lambda_handler` への分岐追加（`src/trigger/app.py`）

### 2-1. 履歴コマンド判定ブロックの追加

- [x] 既存の `is_thread_reply` 算出直後に履歴コマンド判定ブロックを追加する
  - [x] `not is_thread_reply and _is_history_command(topic)` のとき履歴フローに入る
  - [x] `table_prefix_hist = os.environ["DYNAMODB_TABLE_PREFIX"]` を取得する
  - [x] `slack_bot_token_hist = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])` を取得する
  - [x] `keyword = _extract_history_keyword(topic)` でキーワードを抽出する
  - [x] `try` ブロックで `_handle_history_command(user_id, channel, event_msg_ts, keyword, table_prefix_hist, slack_bot_token_hist)` を呼び出す
  - [x] `except Exception` 時は `logger.exception("History command failed", ...)` を記録し、エラーメッセージ（`❌ 履歴の取得中にエラーが発生しました。しばらく経ってから再試行してください。`）を `chat_postMessage` で投稿する
  - [x] 履歴フロー処理後は `return {"statusCode": 200, "body": ""}` で即時終了する（ECS RunTask を呼び出さない）

---

## Phase 3: テスト

### 3-1. `_is_history_command` のテスト

- [x] `"履歴"` → `True`
- [x] `"履歴 Terraform"` → `True`
- [x] `"履歴Terraform"` → `True`
- [x] `"history"` → `True`
- [x] `"History k8s"` → `True`（大文字始まり）
- [x] `"history k8s overview"` → `True`（複合キーワード）
- [x] `"Terraform入門"` → `False`
- [x] `"フィードバック"` → `False`

### 3-2. `_extract_history_keyword` のテスト

- [x] `"履歴"` → `None`
- [x] `"履歴 Terraform"` → `"Terraform"`
- [x] `"履歴Terraform"` → `"Terraform"`
- [x] `"history"` → `None`
- [x] `"history k8s overview"` → `"k8s overview"`
- [x] `"History Terraform"` → `"Terraform"`

### 3-3. `_query_completed_executions` のテスト

- [x] DynamoDB モック: `completed` × 3 件 + `failed` × 1 件 → `completed` 3 件のみ返すこと
- [x] DynamoDB モック: 結果が空 → 空リストを返すこと
- [x] `ScanIndexForward=False` と `Limit=20` が DynamoDB クエリに渡されていること

### 3-4. `_get_deliverable_url` のテスト

- [x] `deliverables` にレコードあり → `external_url` の値を返すこと
- [x] `deliverables` にレコードなし（`Items` が空）→ `None` を返すこと

### 3-5. `_handle_history_command` の統合テスト（外部依存はモック）

- [x] キーワードなし: 取得した全完了実行（最大 5 件）が `_post_history_result` の `items` に渡されること
- [x] キーワード `"terraform"`: `topic` に `"terraform"`（大文字小文字不問）を含む実行のみ絞り込まれること
- [x] 完了済み実行が 6 件以上のとき: `items` が 5 件に切り捨てられること
- [x] `_get_deliverable_url` が `None` を返す実行: `url: None` の item が `_post_history_result` に渡されること
- [x] 完了済み実行が 0 件: `items` が空リストで `_post_history_result` に渡されること
- [x] `_get_deliverable_url` が例外を raise する実行: その実行の `url` が `None` になり、他の実行は影響を受けないこと（処理が継続すること）

### 3-5b. `_post_history_result` のテスト

- [x] `items` が空 かつ `keyword` が `None` → `"📭 まだ成果物がありません。トピックを送信すると調査を開始します。"` が `chat_postMessage` に渡されること
- [x] `items` が空 かつ `keyword` が `"Terraform"` → `"📭 「Terraform」に一致する成果物は見つかりません。"` が `chat_postMessage` に渡されること
- [x] `items` が 1 件以上 かつ `keyword` が `None` → ヘッダーが `"📚 成果物履歴（最新 N 件）"` の形式であること
- [x] `items` が 1 件以上 かつ `keyword` が `"k8s"` → ヘッダーが `"📚 成果物履歴「k8s」（最新 N 件）"` の形式であること
- [x] `url` がある item → `"   {url}"` の行が含まれること
- [x] `url` が `None` の item → `"   （URL なし）"` の行が含まれること
- [x] 複数 item のとき → `"1. ..."`, `"2. ..."` と番号付きで列挙されること
- [x] `chat_postMessage` が `thread_ts=thread_ts` で呼び出されること

### 3-6. `lambda_handler` ルーティングのテスト

- [x] `text="履歴"` + トップレベル投稿 → `_handle_history_command` が呼び出され, ECS RunTask が呼び出されず, HTTP 200 が返ること
- [x] `text="history Terraform"` + トップレベル投稿 → `_handle_history_command` が `keyword="Terraform"` で呼び出されること
- [x] `text="History"` + トップレベル投稿 → 大文字でも `_handle_history_command` が呼び出されること
- [x] `text="履歴"` + スレッド返信（`thread_ts != ts`）→ `_handle_history_command` が呼び出されず, F8 判定フローへ入ること
- [x] `text="Terraform入門"` + トップレベル投稿 → `_handle_history_command` が呼び出されず, 既存の ECS 起動フローへ入ること
- [x] `_handle_history_command` が例外を raise したとき → エラーメッセージが `chat_postMessage` で投稿され, HTTP 200 が返ること

---

## Phase 4: 品質チェック

- [x] `pytest` ですべてのテストがパスすること（既存 86 件 + 新規テスト）
- [x] `ruff check src/ tests/` でリントエラーがないこと
- [x] `ruff format --check src/ tests/` でフォーマットが整っていること
- [x] `mypy src/trigger/app.py --disallow-untyped-defs` でエラーがないこと

---

## Phase 5: ドキュメント更新

- [x] `docs/functional-design.md` を更新する
  - [x] F9 成果物履歴管理のフロー図・コンポーネント変更内容を追記する
- [x] `tasklist.md` の全タスクを `[x]` に更新する

---

## 完了条件

- [x] 全テストパス（`pytest`）
- [x] リント・フォーマット・型チェッククリーン
- [x] `履歴` / `履歴 {keyword}` コマンドが Slack から動作すること
- [x] ECS タスクが一切起動しないこと（履歴コマンド処理時）
