# design.md — Slack Modal によるユーザープロファイル登録 (F6)

## 実装アプローチ

### 全体方針

- 既存 `src/trigger/app.py` の `lambda_handler` に **interactive payload 判別ロジック** を追加し、Event API path と Interactive Components path を早期分岐
- Slack `block_actions` と `view_submission` の 2 種のインタラクションを処理
- `slack_sdk.WebClient` （既存依存）の `views.open` API で Modal を表示
- DynamoDB は `UpdateItem` で **SET（値あり）** と **REMOVE（空欄）** を 1 呼び出しで同時実行（B 方式・AC-3.5）
- 既存 `learned_preferences` は触らない（マージで保持）

## 1. interactive payload の判別と分岐

### 1.1 Slack Interactive Components の HTTP リクエスト形式

Slack は Event API と異なり、interactive payload を以下の形式で POST する:

```
POST /slack/events
Content-Type: application/x-www-form-urlencoded

payload=<URL encoded JSON>
```

JSON の中身は `type` フィールドで識別:
- `type: "block_actions"` — ボタンクリック等
- `type: "view_submission"` — Modal の Submit
- `type: "view_closed"` — Modal のキャンセル（本作業では未使用）

### 1.2 lambda_handler の分岐構造

`src/trigger/app.py:212` 付近 の `body = json.loads(body_str)` の **前** に以下の早期分岐を追加:

```python
# Content-Type で Interactive Components を判別
content_type = headers_lower.get("content-type", "")
if "application/x-www-form-urlencoded" in content_type:
    # Interactive payload: payload=<URL encoded JSON> 形式
    from urllib.parse import parse_qs
    parsed = parse_qs(body_str)
    payload_str = parsed.get("payload", [None])[0]
    if not payload_str:
        return {"statusCode": 400, "body": "Missing payload"}
    payload = json.loads(payload_str)
    return _handle_interactive(payload, slack_token=_get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"]))

# 以下、既存の Event API 経路（body = json.loads(body_str)）
```

### 1.3 _handle_interactive 関数（新規）

```python
def _handle_interactive(payload: dict, slack_token: str) -> dict:
    """Slack Interactive Components のディスパッチ。"""
    payload_type = payload.get("type")
    if payload_type == "block_actions":
        return _handle_block_actions(payload, slack_token)
    elif payload_type == "view_submission":
        return _handle_view_submission(payload, slack_token)
    else:
        logger.info("Ignoring unknown interactive payload", extra={"type": payload_type})
        return {"statusCode": 200, "body": ""}
```

## 2. `@CatchExpander profile` 検出と起動メッセージ

### 2.1 検出ロジック

`lambda_handler` の既存の topic 抽出処理直後（L240 付近）に追加:

```python
# プロファイル登録コマンド検出（トップレベル投稿のみ、トリム後 "profile" 完全一致）
if not is_thread_reply and topic.strip().lower() == "profile":
    slack_token = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
    _post_profile_open_button(slack_token, channel, user_id)
    return {"statusCode": 200, "body": ""}
```

### 2.2 ボタン付き起動メッセージ（Block Kit）

```python
def _post_profile_open_button(slack_token: str, channel: str, user_id: str) -> None:
    """プロファイル登録 Modal を開くボタン付きメッセージを投稿。"""
    client = WebClient(token=slack_token)
    client.chat_postMessage(
        channel=channel,
        text="プロファイル登録",  # フォールバック text
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "プロファイルを登録・編集できます。"},
            },
            {
                "type": "actions",
                "block_id": "profile_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "open_profile_modal",
                        "text": {"type": "plain_text", "text": "プロファイルを開く"},
                        "style": "primary",
                    }
                ],
            },
        ],
    )
```

## 3. Modal の表示（block_actions ハンドラ）

### 3.1 _handle_block_actions

```python
def _handle_block_actions(payload: dict, slack_token: str) -> dict:
    action = payload["actions"][0]
    action_id = action.get("action_id")
    if action_id != "open_profile_modal":
        return {"statusCode": 200, "body": ""}

    trigger_id = payload["trigger_id"]
    user_id = payload["user"]["id"]
    channel_id = payload.get("channel", {}).get("id", "")

    # 既存プロファイル取得
    existing = _get_user_profile(user_id) or {}

    # views.open で Modal を即時表示（trigger_id は 3 秒以内に使う必要あり）
    client = WebClient(token=slack_token)
    client.views_open(
        trigger_id=trigger_id,
        view=_build_profile_modal(existing, callback_metadata={"channel_id": channel_id}),
    )
    return {"statusCode": 200, "body": ""}
```

### 3.2 _build_profile_modal（Block Kit 構造）

```python
PROFILE_FIELDS = [
    # (key, label, placeholder, multiline)
    ("role", "役割・職業",
     "クラウドエンジニア（AWS 中心、SRE 担当） / マーケター（B2B SaaS）", False),
    ("interests", "関心分野",
     "AI（LLM のビジネス活用）、料理（時短レシピ）、投資（インデックス中心）", True),
    ("expertise", "専門・得意領域",
     "インフラ設計（5 年）、Python・SQL は実務レベル、統計は基礎のみ", True),
    ("learning_goals", "学習の目的",
     "副業として Web 制作で案件獲得したい。HTML/CSS は理解しているが営業ノウハウが弱い", True),
    ("background", "背景・状況",
     "30 代後半、子育て中、SaaS 企業のインフラチーム所属、転職活動準備中", False),
    ("output_preferences", "受け取り方の好み",
     "箇条書き重視、実例 3 つ必須、専門用語は注釈付き、英語ソース引用 OK", True),
]

def _build_profile_modal(existing: dict, callback_metadata: dict) -> dict:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "💡 *具体例を交えて 100〜300 字で書くと、出力の精度が上がります*。各フィールドの placeholder を参考にしてください。",
            },
        },
    ]
    for key, label, placeholder, multiline in PROFILE_FIELDS:
        element = {
            "type": "plain_text_input",
            "action_id": f"input_{key}",
            "multiline": multiline,
            "max_length": 500,
            "placeholder": {"type": "plain_text", "text": placeholder[:150]},  # Slack 上限 150
        }
        if existing.get(key):
            element["initial_value"] = existing[key]
        blocks.append({
            "type": "input",
            "block_id": f"block_{key}",
            "label": {"type": "plain_text", "text": label},
            "element": element,
            "optional": True,  # 任意フィールド（空欄可）
        })
    return {
        "type": "modal",
        "callback_id": "profile_submit",
        "title": {"type": "plain_text", "text": "プロファイル登録"},
        "submit": {"type": "plain_text", "text": "保存"},
        "close": {"type": "plain_text", "text": "キャンセル"},
        "private_metadata": json.dumps(callback_metadata),  # チャンネル ID を保持
        "blocks": blocks,
    }
```

## 4. Submit 処理（view_submission ハンドラ）

### 4.1 _handle_view_submission

```python
def _handle_view_submission(payload: dict, slack_token: str) -> dict:
    view = payload["view"]
    if view.get("callback_id") != "profile_submit":
        return {"statusCode": 200, "body": ""}

    user_id = payload["user"]["id"]
    metadata = json.loads(view.get("private_metadata", "{}"))
    channel_id = metadata.get("channel_id", "")

    values = view["state"]["values"]
    # 6 フィールドを抽出（前後空白 trim）
    new_fields = {}
    for key, _label, _placeholder, _multiline in PROFILE_FIELDS:
        v = values.get(f"block_{key}", {}).get(f"input_{key}", {}).get("value") or ""
        v = v.strip()
        new_fields[key] = v  # 空文字も含む（後で SET/REMOVE 振り分け）

    # 長さチェック（500 文字超は errors レスポンス）
    long_keys = [k for k, v in new_fields.items() if len(v) > 500]
    if long_keys:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "response_action": "errors",
                "errors": {f"block_{k}": "500 文字以内で入力してください" for k in long_keys},
            }),
        }

    # DynamoDB に SET（値あり）+ REMOVE（空欄）を 1 呼び出しで反映
    _update_user_profile_fields(user_id, new_fields)

    # 確認メッセージを起動チャンネル（または DM）に投稿
    if channel_id:
        _post_profile_saved(slack_token, channel_id, user_id, new_fields)

    return {"statusCode": 200, "body": ""}
```

### 4.2 _update_user_profile_fields（DynamoDB UpdateItem）

```python
def _update_user_profile_fields(user_id: str, fields: dict[str, str]) -> None:
    """値ありは SET、空欄は REMOVE する UpdateItem 1 発で反映。learned_preferences は触らない。"""
    table = dynamodb.Table(f"{os.environ['DYNAMODB_TABLE_PREFIX']}-user-profiles")
    set_keys = {k: v for k, v in fields.items() if v}
    remove_keys = [k for k, v in fields.items() if not v]

    now = datetime.now(UTC).isoformat()
    set_expr_parts = []
    remove_expr_parts = []
    expr_attr_names = {}
    expr_attr_values = {}

    for i, (k, v) in enumerate(set_keys.items()):
        name_alias = f"#k{i}"
        value_alias = f":v{i}"
        set_expr_parts.append(f"{name_alias} = {value_alias}")
        expr_attr_names[name_alias] = k
        expr_attr_values[value_alias] = v

    for i, k in enumerate(remove_keys):
        name_alias = f"#r{i}"
        remove_expr_parts.append(name_alias)
        expr_attr_names[name_alias] = k

    # 常に updated_at を SET、created_at は if_not_exists で初回のみ
    expr_attr_names["#u"] = "updated_at"
    expr_attr_values[":u"] = now
    set_expr_parts.append("#u = :u")
    expr_attr_names["#c"] = "created_at"
    expr_attr_values[":c"] = now
    set_expr_parts.append("#c = if_not_exists(#c, :c)")

    update_expr_parts = []
    if set_expr_parts:
        update_expr_parts.append("SET " + ", ".join(set_expr_parts))
    if remove_expr_parts:
        update_expr_parts.append("REMOVE " + ", ".join(remove_expr_parts))
    update_expression = " ".join(update_expr_parts)

    table.update_item(
        Key={"user_id": user_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames=expr_attr_names,
        ExpressionAttributeValues=expr_attr_values,
    )
```

### 4.3 確認メッセージ

```python
def _post_profile_saved(slack_token: str, channel: str, user_id: str, fields: dict) -> None:
    client = WebClient(token=slack_token)
    set_fields = [(k, v) for k, v in fields.items() if v]
    removed_fields = [k for k, v in fields.items() if not v]

    text_parts = ["✅ プロファイルを保存しました"]
    if set_fields:
        text_parts.append("\n*登録された内容:*")
        for k, v in set_fields:
            label = dict((kk, lbl) for kk, lbl, _, _ in PROFILE_FIELDS)[k]
            preview = v if len(v) <= 80 else v[:80] + "..."
            text_parts.append(f"• {label}: {preview}")
    if removed_fields:
        text_parts.append("\n*削除されたフィールド:*")
        for k in removed_fields:
            label = dict((kk, lbl) for kk, lbl, _, _ in PROFILE_FIELDS)[k]
            text_parts.append(f"• {label}")

    client.chat_postMessage(channel=channel, text="\n".join(text_parts))
```

## 5. _get_user_profile ヘルパー（trigger 内ローカル）

`src/agent/state/dynamodb_client.py` の `get_user_profile` は `src/agent/` 配下にあり、`src/trigger/` Lambda zip には含まれない。**trigger 内に同等のローカル関数を追加**:

```python
def _get_user_profile(user_id: str) -> dict | None:
    table = dynamodb.Table(f"{os.environ['DYNAMODB_TABLE_PREFIX']}-user-profiles")
    response = table.get_item(Key={"user_id": user_id})
    return response.get("Item")
```

## 6. template.yaml の変更

### 6.1 TriggerFunction の IAM Policy 拡張

`template.yaml:666-674` の `TriggerFunction` Policies に UserProfilesTable へのアクセス追加:

```yaml
Policies:
  - Version: "2012-10-17"
    Statement:
      - Effect: Allow
        Action:
          - dynamodb:PutItem
        Resource:
          - !GetAtt WorkflowExecutionsTable.Arn
          - !GetAtt DashboardEventsTable.Arn
      - Effect: Allow                          # ← 新規追加
        Action:
          - dynamodb:GetItem
          - dynamodb:UpdateItem
        Resource:
          - !GetAtt UserProfilesTable.Arn
```

### 6.2 既存リソースは変更なし

- `SlackApi` の `/slack/events` エンドポイント: 既存のまま（同じ URL で interactive payload を受ける）
- TriggerFunction の他の Env / Memory / Timeout: 変更不要（10 秒で十分、views.open は数百ms）

## 7. Slack app 設定（ユーザー手動作業）

sam deploy 後、ユーザーが Slack app 管理画面で以下を実施:

1. **Interactivity & Shortcuts** を ON
2. **Request URL**: 既存の `/slack/events` を設定（Event API と同じ URL）
   - 例: `https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/Prod/slack/events`
3. Bot Token Scopes 確認（既に必要なものは揃っている想定）:
   - `chat:write` (起動メッセージ・確認メッセージ投稿)
   - `commands` は **不要**（Slash command を使わないため）

## 8. テスト計画

### 8.1 新規テストケース（`tests/unit/trigger/test_app.py`）

| # | テスト名 | 検証内容 |
|---|---|---|
| 1 | `test_profile_keyword_opens_button_message` | `topic == "profile"` で chat.postMessage に actions block が含まれる |
| 2 | `test_block_actions_opens_modal_with_existing_values` | DynamoDB に既存 profile がある状態で `block_actions` payload を渡すと、views.open の view.blocks の initial_value に既存値が入る |
| 3 | `test_block_actions_opens_empty_modal_when_no_profile` | DynamoDB に profile が無い場合、initial_value 無しで views.open が呼ばれる |
| 4 | `test_view_submission_writes_to_dynamodb_set_and_remove` | 一部空欄、一部値ありの payload で update_item が SET + REMOVE 両方を含む式で呼ばれる |
| 5 | `test_view_submission_preserves_learned_preferences` | DynamoDB に既存の learned_preferences がある状態で submit しても、UpdateExpression が learned_preferences を触らない |
| 6 | `test_view_submission_returns_errors_for_long_fields` | 500 文字超の field を含む submit に対し response_action:errors が返る |
| 7 | `test_interactive_payload_parsing` | `application/x-www-form-urlencoded` body から payload 取り出しが正しく動く |

### 8.2 既存テストの非干渉確認

- 既存の Event API path（app_mention、message、F8 feedback、F9 history）が変わらず動作
- 既存テスト全 pass

## 9. デプロイ手順

```bash
# 1. ビルド
sam build --cached

# 2. デプロイ（Lambda code + IAM policy 変更のみ）
sam deploy --no-confirm-changeset --no-fail-on-empty-changeset

# 3. Slack app 管理画面で Interactivity 設定（ユーザー作業）

# 4. 動作確認（AC-7）
#   - Slack で @CatchExpander profile
#   - ボタン押下 → Modal
#   - 6 フィールド入力 → 保存
#   - 確認メッセージ
#   - DynamoDB Console で UserProfilesTable に書き込み確認
```

## 10. 影響範囲

| レイヤ | ファイル | 変更 |
|---|---|---|
| Lambda handler | `src/trigger/app.py` | +約 250 行（interactive 判別、block_actions/view_submission ハンドラ、Modal 構築、DynamoDB UpdateItem） |
| Lambda 依存 | `src/trigger/requirements.txt` | 変更なし（slack_sdk は既存） |
| IaC | `template.yaml` | TriggerFunction の Policies に UserProfilesTable Statement 追加（+5 行） |
| テスト | `tests/unit/trigger/test_app.py` | +約 200 行（7 ケース） |
| docs | `docs/functional-design.md` §5.2 | 実装ステータスを「設計のみ」→「実装済み (commit XXX)」に更新。mermaid 例の文言は汎用版に変更（IT 偏重を解消） |
| Frontend / API GW / DynamoDB スキーマ | （変更なし） | UserProfilesTable は既存スキーマで運用、frontend は別 steering |

## 11. セキュリティ

- **Slack 署名検証**: 既存 `verify_slack_signature` を interactive payload にも適用（同じヘッダ・ボディで検証）
- **user_id の信頼性**: Slack 署名検証通過後の `payload["user"]["id"]` をそのまま信頼（既存 app_mention 経路と同じ）
- **DynamoDB 権限**: 最小権限（UserProfilesTable の GetItem / UpdateItem のみ追加、他 Item にアクセスしない）
- **PII の扱い**: プロファイルは自己申告であり、Slack OAuth で識別された本人のみが自分のレコードに書き込み可能

## 12. ロールバック計画

万一バグが発生した場合:
- `src/trigger/app.py` の interactive 分岐部分を revert → sam deploy
- 既に書き込まれた UserProfilesTable のレコードは TTL なしで残存（次回登録時に上書き）
- IAM policy ロールバックは差し戻し commit で対応

## 13. 関連メモリ・ドキュメント

- memory: `feedback_pre_commit_secret_scan_skill.md` (commit 前必須)
- memory: `feedback_deploy_after_ci_completion.md` (push 後 CI 完了確認、ただし今回は agent image 不変なので待ち不要)
- memory: `project_slack_feedback_requires_mention.md` (Lambda 入口でのメンション要件、本作業は Modal 経由なので影響なし)
- `docs/functional-design.md` §5.2 (シーケンス図、本作業で「設計のみ」表記を解消)
- `docs/product-requirements.md` F6 (本作業で完全実装に到達)
