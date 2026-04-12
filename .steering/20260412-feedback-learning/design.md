# 設計書：F8 フィードバック学習

## 1. 実装アプローチ

### 基本方針

- **新規 AWS リソースなし**。ECS タスク起動フローをそのまま流用し、`TASK_TYPE=feedback` 環境変数でトピック処理との分岐を行う
- **`profile_text` への埋め込み方式**。`learned_preferences` を `profile_text` 文字列の末尾に追記することで、既存の3プロンプト（トピック解析・ワークフロー設計・生成）すべてに1ヶ所の変更で反映する
- **新規パッケージ `src/agent/feedback/`**。フィードバック処理ロジックを `orchestrator.py` に混在させず独立したモジュールとして配置する

### Slack イベント対応（設計上の重要判断）

**DM（`message.im`）**: スレッド返信がそのまま Bot に届くため、`@mention` 不要。

**チャンネル（`app_mention`）**: スレッド返信が `@mention` なしで届かない（Slack の仕様）。
`message.channels` サブスクリプションを追加すると Bot がいる全チャンネルの全メッセージを受信することになり、誤検知・コスト増大のリスクがある。

**採用方針 → Option A（`@mention` 必須）**  
チャンネルでのフィードバックは `@CatchExpander コードが良かった` のように送信してもらう。
DM ではメンション不要（現在と同じ）。この非対称性は Slack 通知に明記する。

> 将来的にチャンネル限定でのメンションなしフィードバックが必要になった場合は、
> Slack App Manifest に `message.channels` スコープを追加する。

---

## 2. 変更するコンポーネント

### 変更概要

| ファイル | 種別 | 変更内容 |
|---------|------|---------|
| `src/trigger/app.py` | 変更 | フィードバック検出ロジック（`_find_completed_execution`）と ECS 起動分岐を追加 |
| `src/agent/feedback/__init__.py` | 新規 | パッケージ初期化 |
| `src/agent/feedback/feedback_processor.py` | 新規 | `FeedbackProcessor` クラス（フィードバック解析・プロファイル更新） |
| `src/agent/notify/slack_client.py` | 変更 | `post_feedback_result()`, `post_feedback_unextracted()` メソッド追加 |
| `src/agent/orchestrator.py` | 変更 | `profile_text` 構築時に `learned_preferences` を埋め込む |
| `src/agent/main.py`（外部管理） | 変更（契約記述） | `TASK_TYPE` による分岐追加 |
| `tests/unit/trigger/test_app.py` | 変更 | フィードバック検出テスト追加 |
| `tests/unit/agent/test_feedback_processor.py` | 新規 | `FeedbackProcessor` ユニットテスト |
| `tests/unit/agent/test_orchestrator.py` | 変更 | `learned_preferences` 反映テスト追加 |
| `tests/unit/agent/test_slack_client.py` | 変更 | 新規 Slack メソッドテスト追加 |

**変更なし:**
- `template.yaml` — 新規 AWS リソース・IAM ロール追加なし
- `src/agent/state/dynamodb_client.py` — 既存の `get_user_profile` / `put_user_profile` をそのまま使用
- `src/agent/storage/` — 変更なし

---

## 3. 詳細設計

### 3.1 Lambda（`src/trigger/app.py`）

#### フィードバック検出フロー

```
メッセージ受信
  └─ Slack 署名検証（既存）
  └─ bot_id チェック（既存）
  └─ event_type チェック（app_mention / message.im）（既存）
       ↓
  [新規] thread_ts の存在チェック
  └─ thread_ts がない → 新規トピックフロー（既存）
  └─ thread_ts がある（= スレッド返信）
       └─ DynamoDB で完了済み実行を検索
            └─ 見つからない → 新規トピックフロー（既存）
            └─ 見つかった（status == "completed"）
                 └─ フィードバックルート（ECS 起動）
```

#### `_find_completed_execution` 関数

```python
def _find_completed_execution(
    user_id: str,
    thread_ts: str,
    table_prefix: str,
) -> dict | None:
    """
    workflow-executions テーブルを user-id-index GSI でクエリし、
    指定 thread_ts に対応する完了済み実行レコードを返す。
    見つからない場合は None を返す。

    実装ポイント:
    - user-id-index GSI (PK: user_id) を使用（既存インデックス流用）
    - FilterExpression で slack_thread_ts と status を絞り込む
    - 1スレッド = 1実行なので items は 0 か 1 のみ
    """
    table = dynamodb.Table(f"{table_prefix}-workflow-executions")
    response = table.query(
        IndexName="user-id-index",
        KeyConditionExpression=Key("user_id").eq(user_id),
        FilterExpression=(
            Attr("slack_thread_ts").eq(thread_ts) &
            Attr("status").eq("completed")
        ),
    )
    items = response.get("Items", [])
    return items[0] if items else None
```

#### フィードバック処理分岐（`lambda_handler` への追加）

```python
# --- 既存のメッセージ受信処理でユーザーID・テキスト・チャンネルを取得後 ---

thread_ts = event_data.get("thread_ts")
msg_ts = event_data.get("ts", "")
is_thread_reply = bool(thread_ts and thread_ts != msg_ts)

if is_thread_reply:
    execution = _find_completed_execution(user_id, thread_ts, table_prefix)
    if execution:
        # --- フィードバックルート ---
        # ACK をスレッドに投稿
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="📝 フィードバックを受け取りました。プロファイルに反映中...",
        )
        # フィードバック処理用 ECS タスク起動
        ecs_client.run_task(
            ...  # 既存と同じネットワーク設定
            overrides={
                "containerOverrides": [{
                    "name": "agent",
                    "environment": [
                        {"name": "TASK_TYPE",       "value": "feedback"},
                        {"name": "USER_ID",          "value": user_id},
                        {"name": "FEEDBACK_TEXT",    "value": topic},  # メンション除去済みテキスト
                        {"name": "EXECUTION_ID",     "value": execution["execution_id"]},
                        {"name": "SLACK_CHANNEL",    "value": channel},
                        {"name": "SLACK_THREAD_TS",  "value": thread_ts},
                    ],
                }]
            },
        )
        return {"statusCode": 200, "body": ""}
    # 見つからなければ fall through → 新規トピックフロー（既存）

# --- 既存の新規トピックフロー ---
```

### 3.2 外部管理ファイル `src/agent/main.py` のインターフェース定義

`main.py` はリポジトリ外管理のため、本ドキュメントで変更後の期待インターフェースを定義する。

```python
TASK_TYPE = os.environ.get("TASK_TYPE", "topic")  # デフォルトは既存フロー

if TASK_TYPE == "feedback":
    from feedback.feedback_processor import FeedbackProcessor

    user_id         = os.environ["USER_ID"]
    feedback_text   = os.environ["FEEDBACK_TEXT"]
    execution_id    = os.environ["EXECUTION_ID"]
    slack_channel   = os.environ["SLACK_CHANNEL"]
    slack_thread_ts = os.environ["SLACK_THREAD_TS"]

    processor = FeedbackProcessor(
        slack_client=SlackClient(slack_bot_token),
        db_client=DynamoDbClient(table_prefix),
    )
    processor.process(user_id, feedback_text, execution_id, slack_channel, slack_thread_ts)

else:
    # 既存トピックフロー（変更なし）
    execution_id    = os.environ["EXECUTION_ID"]
    user_id         = os.environ["USER_ID"]
    topic           = os.environ["TOPIC"]
    slack_channel   = os.environ["SLACK_CHANNEL"]
    slack_thread_ts = os.environ["SLACK_THREAD_TS"]

    orchestrator = Orchestrator(...)
    orchestrator.run(execution_id, user_id, topic, slack_channel, slack_thread_ts)
```

### 3.3 `FeedbackProcessor`（`src/agent/feedback/feedback_processor.py`）

```python
class FeedbackProcessor:
    def __init__(self, slack_client: SlackClient, db_client: DynamoDbClient) -> None: ...

    def process(
        self,
        user_id: str,
        feedback_text: str,
        execution_id: str,
        slack_channel: str,
        slack_thread_ts: str,
    ) -> None:
        """フィードバックを解析しプロファイルを更新する"""
```

#### 処理フロー

```
1. 実行レコード取得（topic, category）
   └─ 見つからない場合: topic="不明", category="不明" で続行

2. ユーザープロファイル取得（learned_preferences）
   └─ プロファイルなし: learned_preferences = []

3. Claude による好み抽出
   └─ call_claude(feedback_extraction_prompt, allowed_tools=None)
   └─ _parse_claude_response() でパース
   └─ パース失敗: preferences = []

4. 好みリストのマージ
   └─ _merge_preferences(existing, new_preferences) → merged

5. DynamoDB プロファイル更新
   └─ profile["learned_preferences"] = merged
   └─ db.put_user_profile(profile)

6. Slack 応答
   └─ preferences が 1 件以上: post_feedback_result(merged[-len(new):]...)
   └─ preferences が 0 件:    post_feedback_unextracted()
```

#### `_build_extraction_prompt` — Claude への入力

```
# フィードバック解析タスク

成果物に対するユーザーのフィードバックから、次回の生成に反映すべき具体的な「好み」を抽出してください。

## 元のトピック情報
- トピック: {topic}
- カテゴリ: {category}

## 現在登録されている好み（更新の参考に）
{existing_prefs_text | "（まだ登録なし）"}

## フィードバックテキスト
{feedback_text}

## 出力ルール
1. 好みは「成果物の生成プロンプトにそのまま使える命令形の文章」で表現する
   良い例: "Terraformコードはmodule分割してディレクトリ構造で管理する"
   悪い例: "コードをもっとモジュール化してほしいと言っていた"
2. 既存の好みと意味が重複・矛盾する場合は replaces_index でそのインデックスを指定する
   （インデックスは 0 始まり、既存リストの順序に対応）
3. 具体的な好みが読み取れない場合は preferences を空配列にする
4. 抽出できる好みは最大3件

**重要**: 前置き文・説明文は不要です。以下のJSON形式のみを```jsonブロックで出力してください。

```json
{
  "preferences": [
    {
      "text": "命令形の好み文章",
      "replaces_index": null
    }
  ]
}
```
```

#### `_merge_preferences` — マージロジック

```python
def _merge_preferences(
    existing: list[dict],
    new_preferences: list[dict],
    max_items: int = 10,
) -> list[dict]:
    """
    マージルール:
    1. new_preferences を順に処理
    2. replaces_index が None でない かつ 0 <= replaces_index < len(existing):
       existing[replaces_index] を新しい preference で置き換え（created_at も更新）
    3. replaces_index が None または範囲外:
       既存リストの末尾に追加
    4. マージ後に max_items を超えた場合、先頭から超過分を削除（古い順）

    ※ replaces_index が範囲外（負数または >= len(existing)）の場合は null と同様に扱う
    """
```

### 3.4 `SlackClient` の追加メソッド（`src/agent/notify/slack_client.py`）

```python
def post_feedback_result(
    self,
    channel: str,
    thread_ts: str,
    preferences: list[dict],
    total_count: int,
) -> None:
    """フィードバック記録完了通知"""
    lines = ["✅ フィードバックを記録しました。次回から以下の好みを反映します："]
    for p in preferences:
        lines.append(f"• {p['text']}")
    lines.append(f"（現在 {total_count} 件の好みが登録されています）")
    self._post_with_retry(channel, thread_ts, "\n".join(lines))


def post_feedback_unextracted(self, channel: str, thread_ts: str) -> None:
    """具体的な好みが抽出できなかった場合の通知"""
    text = (
        "📝 フィードバックを受け取りました。具体的な好みを抽出できませんでしたが、参考にします。\n"
        "もう少し詳しく教えていただけると、より精度の高い反映ができます。"
    )
    self._post_with_retry(channel, thread_ts, text)
```

### 3.5 `Orchestrator.run()` の変更（`src/agent/orchestrator.py`）

`profile_text` 構築部分のみ変更。それ以外の処理はすべて既存のまま。

```python
# 変更前
profile = self.db.get_user_profile(user_id) or {}
profile_text = json.dumps(profile, ensure_ascii=False) if profile else "プロファイル未登録"

# 変更後
profile = self.db.get_user_profile(user_id) or {}
profile_text_base = json.dumps(profile, ensure_ascii=False) if profile else "プロファイル未登録"
learned_prefs = profile.get("learned_preferences", [])
if learned_prefs:
    prefs_lines = "\n".join(f"- {p['text']}" for p in learned_prefs)
    profile_text = (
        f"{profile_text_base}\n\n"
        "## ユーザーの蓄積された好み（学習済み）\n"
        "以下の好みを成果物の生成方針に必ず反映してください：\n"
        f"{prefs_lines}"
    )
else:
    profile_text = profile_text_base
```

`profile_text` はこの後 `analysis_prompt`・`wf_prompt`・`gen_prompt` の3箇所で参照されるため、この1ヶ所の変更ですべてのプロンプトに反映される。

---

## 4. データ構造の変更

### `user-profiles` テーブル

スキーマレスのため既存レコードへの影響なし。`learned_preferences` フィールドが未存在のレコードは `profile.get("learned_preferences", [])` で空リストとして扱う。

```
learned_preferences: [
  {
    "text": string,        # 命令形の好み文章
    "created_at": string,  # ISO 8601 UTC（初回追加時 or 置き換え時に更新）
  },
  ...                      # 最大 10 件（超過時は先頭から削除）
]
```

### `workflow-executions` テーブル

変更なし。既存の `slack_thread_ts` フィールドと `user-id-index` GSI をフィードバック検出に流用する。

---

## 5. 影響範囲の分析

### 既存テストへの影響

| テストファイル | 影響 | 対処 |
|-------------|------|------|
| `tests/unit/trigger/test_app.py` | `app.py` に分岐追加 → 既存テストはパスするが網羅率が下がる | フィードバック検出ケースのテストを追加 |
| `tests/unit/agent/test_orchestrator.py` | `profile_text` 構築変更 → `learned_preferences` ありのケースが未テスト | `learned_preferences` 反映テストを追加 |
| `tests/unit/agent/test_slack_client.py` | 新規メソッド追加のみ → 既存テストはパス | 2メソッドのテスト追加 |
| その他 | 影響なし | 対処不要 |

### 既存動作への影響

- `learned_preferences` が空（既存ユーザー全員）の場合、`profile_text` 変更前後で出力が変わらない
- `TASK_TYPE` 未設定（既存フロー）の場合、`main.py` の分岐は `topic` ルートを通り既存動作を維持

---

## 6. 考慮事項・将来対応

### Slack イベント重複配信

Slack は Lambda タイムアウト時に同一イベントを再送する場合がある（`x-slack-retry-num` ヘッダー付き）。
Lambda はすでにリトライヘッダーを無視する実装があるため、フィードバック検出の重複処理は基本的に発生しない。

ただし Lambda タイムアウト以外の理由での重複（稀）では同一フィードバックが2回処理され、
`learned_preferences` に重複エントリが追加される可能性がある。
対処コスト（ECS 内で処理済み `feedback_ts` を管理）に対し発生頻度が極めて低いため、
**現時点では対処しない**。必要になった場合は `workflow-executions` に `last_feedback_ts` を追加する。

### チャンネルでのメンションなしフィードバック

現在は Option A（チャンネルでは `@mention` 必須）を採用。
将来的に `message.channels` サブスクリプションを追加する場合は、
`app.py` の イベントタイプ判定条件を `message.channels` に拡張し、
Slack App の OAuth Scope に `channels:history` を追加する。
