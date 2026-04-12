# タスクリスト：F8 フィードバック学習

## 概要

`design.md` の設計に基づくタスクリスト。依存関係順に並べている。

---

## Phase 1: Lambda フィードバック検出

### 1-1. `_find_completed_execution` 関数の実装

- [x] `src/trigger/app.py` に `boto3.dynamodb.conditions.Key`, `Attr` のインポートを追加
- [x] `_find_completed_execution(user_id, thread_ts, table_prefix)` 関数を実装
  - [x] `user-id-index` GSI に対して `Key("user_id").eq(user_id)` でクエリ
  - [x] `FilterExpression` で `slack_thread_ts == thread_ts` のみ絞り込み（status フィルタは含めない）
  - [x] items が空の場合 `None` を返す、あれば `items[0]` を返す（status は呼び出し側で判定）

### 1-2. `lambda_handler` への分岐追加

- [x] `event_data.get("thread_ts")` と `event_data.get("ts")` を取得
- [x] `is_thread_reply = bool(thread_ts and thread_ts != msg_ts)` の判定
- [x] スレッド返信の場合 `_find_completed_execution` を呼び出す
- [x] 実行レコードあり + `status == "completed"` の場合：
  - [x] ACK メッセージをスレッドに投稿（`📝 フィードバックを受け取りました。プロファイルに反映中...`）
  - [x] `TASK_TYPE=feedback` で ECS RunTask を起動（環境変数6項目: `TASK_TYPE`, `USER_ID`, `FEEDBACK_TEXT`, `EXECUTION_ID`, `SLACK_CHANNEL`, `SLACK_THREAD_TS`）
  - [x] `return {"statusCode": 200, "body": ""}` で即時終了
- [x] 実行レコードあり + `status != "completed"` の場合：
  - [x] 何もせず `return {"statusCode": 200, "body": ""}` で終了（無視。処理中の実行への返信は受け付けない）
- [x] 実行レコードなし（`None`）の場合は既存の新規トピックフローへ fall through

---

## Phase 2: SlackClient 新規メソッド追加

### 2-1. `post_feedback_result` の実装

- [x] `src/agent/notify/slack_client.py` に `post_feedback_result(channel, thread_ts, preferences, total_count)` を追加
  - [x] `✅ フィードバックを記録しました。次回から以下の好みを反映します：` ヘッダー行
  - [x] 各 preference を `• {text}` 形式で列挙
  - [x] `（現在 N 件の好みが登録されています）` フッター行
  - [x] `_post_with_retry` で投稿

### 2-2. `post_feedback_unextracted` の実装

- [x] `src/agent/notify/slack_client.py` に `post_feedback_unextracted(channel, thread_ts)` を追加
  - [x] 所定のテキスト（`📝 フィードバックを受け取りました。具体的な好みを抽出できませんでしたが...`）
  - [x] `_post_with_retry` で投稿

---

## Phase 3: FeedbackProcessor 実装

### 3-1. パッケージ作成

- [x] `src/agent/feedback/__init__.py` を作成（空ファイル）

### 3-2. `FeedbackProcessor` クラスの実装

- [x] `src/agent/feedback/feedback_processor.py` を作成
- [x] `__init__(self, slack_client, db_client)` の実装

### 3-3. `process` メソッドの実装

- [x] 環境変数から引数を受け取る（`user_id`, `feedback_text`, `execution_id`, `slack_channel`, `slack_thread_ts`）
- [x] `db_client.get_execution(execution_id)` で実行レコード取得
  - [x] 見つからない場合は `topic="不明"`, `category="不明"` で続行
- [x] `db_client.get_user_profile(user_id)` でプロファイル取得
  - [x] プロファイルなし / `learned_preferences` 未設定の場合は空リストとして扱う
- [x] `_build_extraction_prompt` でプロンプト生成
- [x] `call_claude(prompt, allowed_tools=None)` で好み抽出（`orchestrator.py` の関数を import）
- [x] `_parse_claude_response(raw)` で JSON パース（`orchestrator.py` の関数を import）
  - [x] パース失敗時は `preferences = []` として扱う
- [x] `preferences` を最大3件に切り捨て
- [x] `_merge_preferences(existing, new_preferences)` でマージ
- [x] `profile["learned_preferences"] = merged` を設定し `db_client.put_user_profile(profile)` で更新
  - [x] プロファイルが存在しない場合は `{"user_id": user_id, "learned_preferences": merged}` で新規作成
- [x] プロファイル更新後 `updated_at` を現在時刻で設定
- [x] Slack 応答
  - [x] `preferences` が1件以上: `post_feedback_result` 呼び出し
  - [x] `preferences` が0件: `post_feedback_unextracted` 呼び出し
- [x] 例外発生時: `❌ フィードバックの反映中にエラーが発生しました。ご不便をおかけします。` を Slack に投稿してから raise

### 3-4. `_build_extraction_prompt` の実装

- [x] `topic`, `category`, 既存 `learned_preferences` テキスト一覧, `feedback_text` を埋め込む
- [x] 既存 `learned_preferences` の表示形式（インデックス付き：`0: {text}`, `1: {text}` ...）
- [x] JSON-only 出力の指示（`preferences` 配列, 各要素に `text` と `replaces_index`）

### 3-5. `_merge_preferences` の実装

- [x] `replaces_index` が `None` でなく `0 <= replaces_index < len(existing)` の場合：既存リストの該当インデックスを新しい preference で置き換え
- [x] `replaces_index` が `None` または範囲外の場合：末尾に追加
- [x] 各 preference に `created_at`（ISO 8601 UTC）を付与
- [x] マージ後リストが10件を超えた場合、先頭から超過分を削除

---

## Phase 4: Orchestrator の `profile_text` 変更

### 4-1. `learned_preferences` の埋め込み

- [x] `src/agent/orchestrator.py` の `profile_text` 構築部分を変更
  - [x] `learned_prefs = profile.get("learned_preferences", [])` を取得
  - [x] `learned_prefs` が空の場合は既存と同じ `profile_text` を生成（既存動作維持）
  - [x] `learned_prefs` が1件以上の場合は所定のセクションヘッダー + 箇条書きを末尾に追記

---

## Phase 5: テスト

### 5-1. `tests/unit/trigger/test_app.py` への追加

- [x] `_find_completed_execution` 関数のテスト
  - [x] 完了済みレコードが見つかる場合（レコードを返す）
  - [x] レコードが見つからない場合（`None` を返す）
  - [x] `status != "completed"` のレコードしかない場合（`None` を返す）
- [x] フィードバック検出フローのテスト（`lambda_handler` レベル）
  - [x] スレッド返信 + 完了済み実行あり → ACK 投稿 + ECS 起動（`TASK_TYPE=feedback`）
  - [x] スレッド返信 + 完了済み実行なし（レコード自体が存在しない）→ 既存トピックフロー
  - [x] スレッドなし（トップレベル投稿）→ 既存トピックフロー
  - [x] スレッド返信 + 実行レコードはあるが `status == "in_progress"` → 無視（HTTP 200 のみ返す、新規トピックフロー **に入らない**）

### 5-2. `tests/unit/agent/test_feedback_processor.py` の新規作成

- [x] `_build_extraction_prompt` のテスト
  - [x] 既存 `learned_preferences` あり / なしで出力が変わること
- [x] `_merge_preferences` のテスト
  - [x] `replaces_index = null` → 末尾追加
  - [x] `replaces_index = 1`（有効範囲）→ 該当インデックスを置き換え
  - [x] `replaces_index = -1`（範囲外）→ null と同様に末尾追加
  - [x] `replaces_index = 99`（範囲外）→ null と同様に末尾追加
  - [x] マージ後11件 → 先頭1件が削除されて10件
  - [x] 複数件の新規 preference を一度に処理
- [x] `process` メソッドの統合テスト（外部依存はモック）
  - [x] 好みが抽出できた場合: DynamoDB 更新 + `post_feedback_result` 呼び出し
  - [x] 好みが抽出できなかった場合: DynamoDB 更新なし + `post_feedback_unextracted` 呼び出し
  - [x] 実行レコードが見つからない場合: `topic="不明"` で続行
  - [x] Claude JSON パース失敗: `preferences = []` として扱う
  - [x] DynamoDB 更新エラー: Slack にエラーメッセージ投稿
  - [x] フィードバックテキストが空文字または絵文字のみの場合: Claude に渡してそのまま解析（`preferences = []` になることを確認）

### 5-3. `tests/unit/agent/test_slack_client.py` への追加

- [x] `post_feedback_result` のテスト（好み1件 / 複数件、total_count 反映）
- [x] `post_feedback_unextracted` のテスト

### 5-4. `tests/unit/agent/test_orchestrator.py` への追加

- [x] `learned_preferences` が1件以上ある場合: `profile_text` に好みセクションが含まれる
- [x] `learned_preferences` が空の場合: `profile_text` に好みセクションが含まれない（既存動作維持）

---

## Phase 6: 品質チェック

- [x] `pytest` ですべてのテストがパスすること（既存46件 + 新規テスト）
- [x] `ruff check src/ tests/` でリントエラーがないこと
- [x] `ruff format --check src/ tests/` でフォーマットが整っていること
- [x] `mypy src/agent/feedback/ src/trigger/app.py --disallow-untyped-defs` でエラーがないこと

---

## Phase 7: ドキュメント更新

- [x] `docs/functional-design.md` を更新
  - [x] F8 フィードバック学習のフロー図・コンポーネント構成を追記
  - [x] `user-profiles` データモデルに `learned_preferences` フィールドを追記
- [x] `tasklist.md` の全タスクを `[x]` に更新

---

## 完了条件

- [x] 全テストパス（`pytest`）
- [x] リント・フォーマット・型チェッククリーン
- [x] Slack フィードバック → DynamoDB 更新 → 次回生成への反映が E2E で動作すること
