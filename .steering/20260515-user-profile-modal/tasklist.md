# tasklist.md — Slack Modal によるユーザープロファイル登録 (F6)

design.md の実装計画を具体的なタスクに分解した一覧。

## タスク一覧

| ID | 内容 | 完了条件 | 状態 |
|---|---|---|---|
| T1 | `src/trigger/app.py` に Interactive Components 経路を実装（interactive payload 判別 + `_handle_block_actions` + `_handle_view_submission` + Modal 構築 + DynamoDB UpdateItem ヘルパー）| 6 関数追加、既存 Event API path に影響なし | pending |
| T2 | `template.yaml` の `TriggerFunction` Policies に UserProfilesTable への `dynamodb:GetItem` / `dynamodb:UpdateItem` を追加 | cfn-lint エラーなし | pending |
| T3 | `tests/unit/trigger/test_app.py` に 7 ケース追加（design.md §8.1）| 全 7 ケース pass | pending |
| T4 | `pytest tests/unit/trigger -v` で対象テストグリーン | 新規 + 既存全 pass | pending |
| T5 | `pytest tests/` 全件実行で回帰確認 | pre-existing 26 failures から増えないこと | pending |
| T6 | `docs/functional-design.md` §5.2 の「実装ステータス: 設計のみ」を「実装済み (commit XXX)」に更新、mermaid 例の IT 偏重を汎用版に書き換え | 注釈差し替え、shape は維持 | pending |
| T7 | pre-commit-secret-scan Skill 起動 → diff に新規 leak が無いことを確認 | exit 0 または既知 pre-existing 誤検知のみ | pending |
| T8 | commit (app.py + template + tests + docs を 1 commit) → push | working tree clean、main 反映 | pending |
| T9 | `sam build --cached` → `sam deploy --no-confirm-changeset` | UPDATE_COMPLETE、Lambda code + IAM 更新成功 | pending |
| T10 | **ユーザー作業**: Slack app 管理画面で Interactivity & Shortcuts を ON、Request URL に既存 `/slack/events` を設定 | Slack app 設定完了 | pending |
| T11 | Slack 実機検証（AC-7）: `@CatchExpander profile` → ボタン → Modal → 6 フィールド入力 → 保存 → 確認メッセージ + DynamoDB Console で書込確認 | 全ステップ動作 OK | pending |
| T12 | **削除挙動の実機検証**: 既存値ありで Modal を開き、いくつかのフィールドを空欄にして保存 → DynamoDB Console で該当キーが消えていることを確認 | B 方式の REMOVE が動作 | pending |
| T13 | memory 更新（本 steering 完了状態の記録、`project_2026-05-15-session-end.md` を更新）| MEMORY.md エントリ更新 | pending |

## タスク依存関係

```
T1 ─┐
T2 ─┼─→ T3 ─→ T4 ─→ T5 ─→ T6 ─→ T7 ─→ T8 ─→ T9 ─→ T10 ─→ T11 ─→ T12 ─→ T13
```

- T1, T2 は独立（別ファイル）、並列着手可能
- T3 は T1/T2 実装後にテスト追加（実装と整合させる）
- T4 → T5 の二段階で対象テスト先、全件回帰後
- T7（secret-scan）は commit 直前必須（memory: feedback_pre_commit_secret_scan_skill.md）
- T9 デプロイ後 T10 のユーザー作業 → T11/T12 で実機検証
- agent image 変更がないため、CI 完了待ちは不要（memory: feedback_deploy_after_ci_completion.md は agent image 含む場合のルール）

## 各タスクの実装メモ

### T1: src/trigger/app.py 大規模追加

挿入順序とサブステップ:

#### T1-a: 定数追加（ファイル上部、imports の直後あたり）

```python
PROFILE_FIELDS = [
    ("role", "役割・職業", "クラウドエンジニア（AWS 中心、SRE 担当） / マーケター（B2B SaaS）", False),
    ("interests", "関心分野", "AI（LLM のビジネス活用）、料理（時短レシピ）、投資（インデックス中心）", True),
    ("expertise", "専門・得意領域", "インフラ設計（5 年）、Python・SQL は実務レベル、統計は基礎のみ", True),
    ("learning_goals", "学習の目的", "副業として Web 制作で案件獲得したい。HTML/CSS は理解しているが営業ノウハウが弱い", True),
    ("background", "背景・状況", "30 代後半、子育て中、SaaS 企業のインフラチーム所属、転職活動準備中", False),
    ("output_preferences", "受け取り方の好み", "箇条書き重視、実例 3 つ必須、専門用語は注釈付き、英語ソース引用 OK", True),
]
```

#### T1-b: ヘルパー関数 6 つ追加（既存関数群の末尾、lambda_handler の前）

- `_get_user_profile(user_id)` — DynamoDB get_item
- `_update_user_profile_fields(user_id, fields)` — SET + REMOVE 同時 UpdateItem
- `_post_profile_open_button(slack_token, channel, user_id)` — ボタン付きメッセージ投稿
- `_build_profile_modal(existing, callback_metadata)` — Modal Block Kit 生成
- `_post_profile_saved(slack_token, channel, user_id, fields)` — 確認メッセージ投稿
- `_handle_interactive(payload, slack_token)` — Interactive payload ディスパッチャ
  - 内部で `_handle_block_actions(payload, slack_token)` と `_handle_view_submission(payload, slack_token)` を呼ぶ

#### T1-c: lambda_handler の早期分岐追加

`body = json.loads(body_str)` の直前（L212 付近）に Content-Type 判別を追加:

```python
content_type = headers_lower.get("content-type", "")
if "application/x-www-form-urlencoded" in content_type:
    from urllib.parse import parse_qs
    parsed = parse_qs(body_str)
    payload_str = parsed.get("payload", [None])[0]
    if not payload_str:
        return {"statusCode": 400, "body": "Missing payload"}
    payload = json.loads(payload_str)
    slack_token = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
    return _handle_interactive(payload, slack_token)
```

#### T1-d: profile コマンド検出を既存 topic 分岐に追加

`if not is_thread_reply and _is_history_command(topic):` の直前または直後あたりに追加:

```python
if not is_thread_reply and topic.strip().lower() == "profile":
    slack_token_p = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
    _post_profile_open_button(slack_token_p, channel, user_id)
    return {"statusCode": 200, "body": ""}
```

### T2: template.yaml 編集

`template.yaml:666-674` の Policies に Statement を追加:

```yaml
- Effect: Allow
  Action:
    - dynamodb:GetItem
    - dynamodb:UpdateItem
  Resource:
    - !GetAtt UserProfilesTable.Arn
```

### T3: テスト追加（design.md §8.1 の 7 ケース）

既存 `TestLambdaHandlerFlow` クラス（or `TestHistoryCommand` クラス）と並列に `TestProfileModal` クラスを新設。
mock 対象:
- `_get_secret` (Slack token)
- `WebClient` (chat_postMessage / views_open)
- `dynamodb.Table` (get_item / update_item)

### T6: docs/functional-design.md §5.2 の更新

- 「実装ステータス: 以下のシーケンスは設計のみ。`@CatchExpander profile` コマンドの対話式登録ハンドラは ... に **未実装**。」
- ↓ 書き換え
- 「実装ステータス: 本機能は commit XXX で **実装済み**。Modal ベース（mermaid シーケンスとは UX が異なるが、登録項目は同等の 5W1H 6 軸）。」
- mermaid シーケンスの例文（「クラウドエンジニア」「AWS, Google Cloud」「Terraform, Python」など）を汎用版に書き換え:
  - 「クラウドエンジニア / マーケター / 大学院生 などあなたの役割」
  - 「AI、料理、投資 などの関心分野」など
  - shape はそのまま、placeholder 的な例文だけ汎用化

### T8: commit message

```
feat(trigger): implement user profile registration via Slack Modal (F6)

Implements the `@CatchExpander profile` interactive flow. Users now have a
zero-CLI registration UX for their user profile (role / interests / expertise /
learning_goals / background / output_preferences — 5W1H frame).

- src/trigger/app.py: branch on Content-Type to detect interactive payloads,
  add block_actions handler (views.open with pre-filled values from
  UserProfilesTable), view_submission handler (UpdateItem with SET for filled
  fields + REMOVE for cleared fields, preserves learned_preferences)
- template.yaml: TriggerFunction IAM policy gets GetItem/UpdateItem on
  UserProfilesTable (minimum privilege)
- docs/functional-design.md §5.2: mark profile registration as implemented,
  replace IT-centric mermaid examples with domain-agnostic ones
- tests: add 7 unit cases covering payload parsing, modal build, SET/REMOVE
  update expression, learned_preferences preservation, validation errors

Closes the partial-implementation status of F6 in product-requirements.md.
```

### T9: デプロイ

memory `feedback_deploy_after_ci_completion.md` の制約は agent image を含む変更時のみ。本作業は trigger Lambda code + IAM 変更のみで agent image 不変のため、CI 完了待ち不要。

### T10: ユーザー作業（Slack app 設定）

ユーザーが Slack app 管理画面 (https://api.slack.com/apps) で以下を実施:

1. 対象 app を選択
2. 左メニュー「Interactivity & Shortcuts」
3. **Interactivity を ON**
4. **Request URL** に既存の Event API と同じ URL を設定:
   `https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/Prod/slack/events`
5. 「Save Changes」

### T11/T12: 実機検証チェックリスト

| # | 操作 | 期待結果 |
|---|---|---|
| 1 | Slack で `@CatchExpander profile` 投稿 | ボタン付きメッセージが返信される |
| 2 | 「プロファイルを開く」ボタン押下 | Modal が即時開く（既存値があれば初期表示） |
| 3 | 6 フィールド全部入力して保存 | 「✅ プロファイルを保存しました」+ サマリが投稿される |
| 4 | DynamoDB Console で UserProfilesTable 確認 | user_id 単位で全 6 軸 + created_at + updated_at が書き込まれている |
| 5 | 再度 `@CatchExpander profile` → ボタン押下 | Modal に **前回入力した値が初期表示** される |
| 6 | 2〜3 フィールドを空欄にして保存 | 「削除されたフィールド: ...」を含む確認メッセージ |
| 7 | DynamoDB Console で再確認 | **空欄にしたフィールドキーが消えている**（B 方式の REMOVE が動作）|
| 8 | learned_preferences が存在する場合（事前に CLI で put しておく）| 6 軸 update 後も learned_preferences が **保持されている** |
| 9 | 500 文字超のフィールドで保存試行 | Modal 内でエラー表示、保存されない |

### T13: memory 更新方針

- `project_2026-05-15-session-end.md` を「user-profile-modal 完了」に更新
- 新規 feedback メモリ追加候補: Slack Interactive Components の Content-Type 判別パターン（汎用的に再利用可能）
- 削除: 該当なし

## 受け入れ条件との対応

| AC | 対応タスク |
|---|---|
| AC-1 (起動コマンド検出) | T1-d + T3 (#1) |
| AC-2 (Modal 表示・placeholder・ヘルプ) | T1-b + T3 (#2, #3) |
| AC-2.5 (ベストプラクティス反映) | T1-b の placeholder 文言で実現 |
| AC-3 (Submit 処理) | T1-b の `_update_user_profile_fields` + `_post_profile_saved` |
| AC-3.5 (フィールド削除挙動 B 方式) | T1-b の SET + REMOVE UpdateItem + T3 (#4) + T12 実機検証 |
| AC-4 (バリデーション) | T1-b の `_handle_view_submission` 内 500 字チェック + T3 (#6) |
| AC-5 (既存機能非干渉) | T1-c の early branch + T5 全件回帰 + 既存 Event API テスト |
| AC-6 (テスト) | T3 + T4 |
| AC-7 (デプロイ後の動作確認) | T11 + T12 |
