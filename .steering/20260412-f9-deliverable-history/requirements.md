# 要求定義書：F9 成果物履歴管理

## 1. 概要

### 背景と解決する課題

現在のシステムは成果物を Notion に保存しているが、ユーザーが「以前調べたあのトピックを見たい」と思ったとき、Notion を開いて自分で探すしかない。Slack から離れることなく「どんなトピックを調べたか」「どこに成果物があるか」を一覧確認できる手段がない。

この機能は Slack で `履歴` と送るだけで過去の成果物一覧（トピック名・カテゴリ・日付・Notion URL）を Slack に返す。ECS タスクは起動せず Lambda 内で DynamoDB をクエリして即時返答することで、数秒以内に一覧が手に入る。

### 処理フロー概要

```
[ユーザー] Slackに「履歴」（オプションでキーワード付き）を投稿
      ↓
[Lambda] 履歴コマンド検出 → ECS起動なし
      ↓
[Lambda] DynamoDB: user-id-index GSI でユーザーの完了済み実行を取得
      ↓
[Lambda] DynamoDB: deliverables テーブルで各 execution_id の外部URL解決
      ↓
[Lambda] フォーマット済み一覧を Slack スレッドに投稿
      ↓
[Lambda] HTTP 200 を返す
```

---

## 2. ユーザーストーリー

### US-F9-1: 最新履歴の確認
**As a** ユーザー  
**I want to** Slack で `履歴` と送るだけで過去の成果物一覧を見たい  
**So that** Notion を開かずに「最近どんなトピックを調べたか」を思い出せる

**例：**
- `履歴` → 最新5件が一覧で返る
- `history` → 同上（英語コマンドも受け付ける）

### US-F9-2: キーワードで絞り込み
**As a** ユーザー  
**I want to** `履歴 Terraform` のようにキーワードを付けて過去の成果物を絞り込みたい  
**So that** 調べたいトピックのページに素早くアクセスできる

**例：**
- `履歴 Terraform` → トピック名に "Terraform" を含む成果物だけ返る
- `history k8s` → "k8s" を含む成果物だけ返る

### US-F9-3: Notion ページへの直接アクセス
**As a** ユーザー  
**I want to** 一覧の各エントリに Notion ページのリンクが付いていてほしい  
**So that** クリック一発で成果物の詳細にアクセスできる

### US-F9-4: 成果物がない場合の明確なフィードバック
**As a** ユーザー  
**I want to** 一致する成果物がないときにその旨をはっきり知りたい  
**So that** 「処理に失敗したのか、そもそも存在しないのか」を混同せずに済む

---

## 3. 受け入れ条件

### AC-F9-1: 履歴コマンドの検出（Lambda）

| # | 条件 | 期待動作 |
|---|------|---------|
| 1-1 | `text` が `履歴` または `history` で始まるトップレベル投稿（`thread_ts` なし または `thread_ts == ts`） | 履歴コマンドとして処理 |
| 1-2 | `text` が `履歴` または `history` で始まるが**スレッド返信**（`thread_ts != ts`） | 履歴コマンドとして扱わない。F8 フィードバック判定フローへ |
| 1-3 | `bot_id` が付いたメッセージ | 無視（既存挙動と同じ） |
| 1-4 | `x-slack-retry-num` ヘッダーがある | 無視（既存挙動と同じ） |
| 1-5 | `text` が `履歴` または `history` で始まらない | 履歴コマンドとして扱わない。既存の新規トピックフローへ |

**コマンド判定の優先順位（Lambda 内）：**
```
1. bot_id / retry-num チェック → 無視
2. 履歴コマンド判定 (text が「履歴」または「history」で始まる + トップレベル)
3. F8 フィードバック判定 (スレッド返信 + 完了済み実行あり)
4. 既存の新規トピックフロー
```

**キーワード抽出ルール：**
- `履歴` のみ → キーワードなし
- `履歴 Terraform` → キーワード `"Terraform"`（先頭コマンド部分を除いた残り文字列、前後スペース除去）
- `history k8s overview` → キーワード `"k8s overview"`（スペース含む複合キーワードとして扱う）
- メンション (`<@UXXXXXXX>`) は除去してからコマンド判定する

### AC-F9-2: DynamoDB クエリ（Lambda）

#### 実行履歴の取得

- [ ] `workflow-executions` テーブルの `user-id-index` GSI に対して `Key("user_id").eq(user_id)` でクエリする
- [ ] `ScanIndexForward=False` で `created_at` の降順に取得する
- [ ] クライアント側で `status == "completed"` のレコードのみ残す
- [ ] キーワードが指定された場合、`topic` フィールドに対して大文字小文字を区別しない部分一致フィルタをクライアント側で適用する（`keyword.lower() in topic.lower()`）
- [ ] フィルタ後の先頭 5 件を返却対象とする

#### 成果物 URL の解決

- [ ] 取得した各 `execution_id` に対して `deliverables` テーブルを `execution_id` でクエリし、`Limit=1` で最初の 1 件を取得する
- [ ] 取得した deliverable の `external_url` フィールドを Notion URL として使用する
- [ ] `deliverables` テーブルにレコードが存在しない場合、その実行の URL は `None` とする
- [ ] URL の取得は各 execution_id について独立して行う（1 件でも URL 取得に失敗しても処理全体を止めない）

### AC-F9-3: Slack 返信フォーマット（Lambda）

返信はユーザーの投稿メッセージへの**スレッド返信**として投稿する（`thread_ts` に元メッセージの `ts` を指定）。

#### 成果物が 1 件以上ある場合

```
📚 成果物履歴（最新 N 件）

1. {topic} — {category} — {YYYY-MM-DD}
   {notion_url}

2. {topic} — {category} — {YYYY-MM-DD}
   （URL なし）
```

- `N` は実際に返す件数（最大 5）
- 日付は `created_at` の先頭 10 文字（`YYYY-MM-DD`）
- キーワード指定がある場合はヘッダーを `📚 成果物履歴「{keyword}」（最新 N 件）` とする
- URL がある場合は 2 行目にリンクをそのまま記載する
- URL がない場合は `（URL なし）` と記載する

#### 該当する成果物がない場合（キーワードなし）

```
📭 まだ成果物がありません。トピックを送信すると調査を開始します。
```

#### 該当する成果物がない場合（キーワードあり）

```
📭 「{keyword}」に一致する成果物は見つかりません。
```

#### エラー発生時

```
❌ 履歴の取得中にエラーが発生しました。しばらく経ってから再試行してください。
```

### AC-F9-4: ECS タスク非起動の保証

- [ ] 履歴コマンドを受けた場合、いかなる条件下でも ECS RunTask を呼び出さない
- [ ] Lambda は DynamoDB クエリと Slack 投稿を完了した後、`{"statusCode": 200, "body": ""}` を返す

---

## 4. データモデル変更

### 変更なし

本機能は既存テーブルを読み取るのみで、スキーマ変更・新規テーブル作成・GSI 追加はいずれも行わない。

| テーブル | 使用するキー・フィールド | 操作 |
|---------|----------------------|------|
| `workflow-executions` | `user-id-index` GSI（PK: `user_id`, SK: `created_at`）, `status`, `topic`, `category` | 読み取りのみ |
| `deliverables` | PK: `execution_id`, `external_url` | 読み取りのみ |

---

## 5. コンポーネント変更

### 5-1. `src/trigger/app.py`

- [ ] `_handle_history_command(event_data, table_prefix, slack_token)` 関数を追加する
  - DynamoDB クエリ → URL 解決 → Slack 投稿の一連の処理を担う
- [ ] `lambda_handler` に履歴コマンド判定分岐を追加する（F8 判定の前に評価する）

### 5-2. `src/agent/state/dynamodb_client.py`

- [ ] `get_completed_executions(user_id, limit)` メソッドを追加する
  - `user-id-index` GSI クエリ + `status == "completed"` フィルタ + 降順取得
  - 引数: `user_id: str`, `limit: int = 20`（クライアント側フィルタのバッファ含め多めに取得）
  - 戻り値: `list[dict]`（降順に並んだ完了済み実行レコードのリスト）
- [ ] `get_deliverable_url(execution_id)` メソッドを追加する
  - `deliverables` テーブルを PK=`execution_id` でクエリし `Limit=1` で取得
  - 戻り値: `str | None`（`external_url` フィールドの値、またはレコードなしの場合 `None`）

### 5-3. `src/agent/notify/slack_client.py`

- [ ] `post_history_result(channel, thread_ts, items, keyword)` メソッドを追加する
  - `items`: `list[dict]`（各要素に `topic`, `category`, `created_at`, `url: str | None` を含む）
  - `keyword`: `str | None`（ヘッダー文言の切り替えに使用）
  - `_post_with_retry` で投稿する

---

## 6. エラーケースと対処方針

| エラーケース | 対処 |
|------------|------|
| `user-id-index` GSI クエリで DynamoDB エラー | Slack スレッドに `❌ 履歴の取得中にエラーが発生しました。しばらく経ってから再試行してください。` を投稿し、Lambda は HTTP 200 を返す |
| `deliverables` テーブルのクエリで DynamoDB エラー（個別 execution_id） | その実行の URL を `None` として扱い処理を継続。全件取得できた情報で一覧を返す |
| 完了済み実行が 0 件 | 「まだ成果物がありません」メッセージを Slack に投稿 |
| キーワード指定で該当なし | 「{keyword}に一致する成果物は見つかりません」メッセージを Slack に投稿 |
| Slack 投稿エラー（`post_history_result` 内） | `_post_with_retry` のリトライ機構に委ねる。リトライ上限後は例外を raise し Lambda はエラーログを記録 |

---

## 7. 非機能要件

| 項目 | 要件 |
|------|------|
| 応答時間 | 履歴コマンド受信から Slack 返信まで 3 秒以内（Lambda 同期処理のため ECS 待ちなし） |
| DynamoDB 読み取りコスト | 既存 `user-id-index` GSI を活用。新規 GSI・テーブル追加なし |
| ECS タスク起動 | 一切行わない |
| 既存機能への影響 | 履歴コマンド判定を優先することで F8 フィードバック判定・新規トピックフローには影響を与えない |
| スケール想定 | 個人利用スケール。クライアント側フィルタ（Python）で処理する件数は現実的に数十件以内 |

---

## 8. 制約事項

### 機能制約

- 表示件数は最大 5 件に固定する（ページネーションは実装しない）
- キーワードフィルタはトピック名に対する部分一致のみ（カテゴリ・日付での絞り込みは不可）
- 表示対象は `status == "completed"` の実行のみ（`failed` / `in_progress` は表示しない）
- 1 実行につき表示する Notion URL は 1 件（複数 deliverable がある場合は最初の 1 件）

### アーキテクチャ制約

- 新規 AWS リソース（Lambda, DynamoDB テーブル, GSI, SQS 等）は追加しない
- ECS タスク定義・コンテナイメージの変更は行わない
- Lambda のタイムアウト設定（10 秒）は変更しない

### スコープ外（明示的に対象外）

- Notion Search API による本文全文検索
- カテゴリ・日付・ステータスによる絞り込み
- 過去トピックの再実行トリガー
- `failed` ステータスの実行の表示
- 成果物の詳細サマリーの Slack 内表示（Notion リンクへの誘導のみ）
- 件数が 5 件を超える場合のページネーション
