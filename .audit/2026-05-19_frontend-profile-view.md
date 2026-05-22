# Codex Review Result: frontend-profile-view (Pass 1)

- Pass: 1
- Date: 2026-05-19
- Model: gpt-5.5 (Codex v0.125.0)
- Prompt: `.audit/2026-05-19_frontend-profile-view.prompt.md`
- 対象 commit: `99d8abf feat(dashboard): add /profile read-only view for F6 user profile`
- Raw output (with thinking trace): `.audit/2026-05-19_frontend-profile-view.raw.txt` (121 KB)

---

# Codex Review — 2026-05-19 frontend-profile-view (Pass 1)

## サマリ
- Critical: 0 / High: 1 / Medium: 0 / Low: 2 / Info: 4
- 総合所感: 設計方向は妥当です。`/me` 固定、authorizer context のみ参照、IAM `GetItem` 限定はいずれも意図と整合しています。
- ただし、既存実データの `learned_preferences` 形状と今回の API / frontend 型が食い違っており、学習履歴ありユーザーで画面が壊れる可能性があります。
- それ以外は主に drift 防止・入力検証の補強レベルです。
- Slack 公式 docs でも `sub` は pure user id 例で、team id は別 claim として示されています: https://docs.slack.dev/reference/methods/openid.connect.userInfo/ / https://docs.slack.dev/reference/methods/openid.connect.token/

## 指摘事項

### [High] [src/dashboard_api/get_my_profile/app.py:64] learned_preferences の実保存形式と API / frontend 型が不一致
- 問題: 既存の feedback 経路は `learned_preferences` を `{"text": "...", "replaces_index": ...}` の dict 配列として保存しています。例: `src/agent/feedback/feedback_processor.py:51`, `src/agent/feedback/feedback_processor.py:74`, `src/agent/orchestrator.py:1265`。一方、本 PR の API はそのまま返し、frontend は `string[]` として `pref` を React child に渡しています。
- 影響: 学習履歴が 1 件以上ある実ユーザーで、`Objects are not valid as a React child` 系の runtime error になり、`/profile` 全体が表示不能になる可能性があります。新規テストが文字列配列だけを使っているため、この実データ契約を検知できていません。
- 推奨修正: read-only 表示用途なら backend で `text` だけに正規化し、API contract を `string[]` に固定するのが最小です。既存形式と将来の壊れたデータも吸収できます。

```python
def _serialize_learned_preferences(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []

    result: list[str] = []
    for pref in raw:
        if isinstance(pref, str):
            text = pref
        elif isinstance(pref, dict):
            text = pref.get("text")
        else:
            continue

        if isinstance(text, str) and text.strip():
            result.append(text)

    return result
```

```python
"learned_preferences": _serialize_learned_preferences(item.get("learned_preferences")),
```

テストも dict 配列を本命ケースにしてください。

```python
"learned_preferences": [
    {"text": "長めのサマリを好む", "replaces_index": None},
    {"text": "コード例を望む", "replaces_index": None},
]
```

期待値:

```python
assert body["learned_preferences"] == ["長めのサマリを好む", "コード例を望む"]
```

### [Low] [src/dashboard_api/get_my_profile/app.py:31] user_sub の形式検証がなく、仕様 drift を静かに placeholder 化する
- 問題: `_extract_slack_user_id()` は空文字以外なら `split("-", 1)[0]` をそのまま採用します。authorizer 由来なので外部入力としての危険度は低いですが、`"abc"`, `"123"`, `"U04*"` なども DynamoDB key として照会されます。
- 影響: Slack / auth_callback 側の claim 形状が壊れた場合に 401 や明示ログではなく「未設定プロファイル」に見え、原因特定が遅れます。
- 推奨修正: split 後に Slack user id として最低限の形式検証を入れてください。Slack 公式 userInfo docs は `sub`, `https://slack.com/user_id`, `https://slack.com/team_id` を別 field として示しており、現時点では pure user id 前提が公式例とも整合します。

```python
import re

_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")

def _extract_slack_user_id(user_sub: str | None) -> str | None:
    if not user_sub:
        return None
    candidate = user_sub.split("-", 1)[0]
    if not _SLACK_USER_ID_RE.fullmatch(candidate):
        return None
    return candidate
```

### [Low] [src/dashboard_api/get_my_profile/app.py:20] 6 軸定義が 3 箇所に分散しており drift 検知が弱い
- 問題: `PROFILE_FIELDS`、backend `_PROFILE_KEYS`、frontend `AXIS_FIELDS` が手動同期です。今回の分離自体は trigger modal の再発バグ領域に触れない判断として妥当ですが、キー追加・名称変更時の検知が型だけでは足りません。
- 影響: 将来 6 軸に変更が入ったとき、保存はできるが閲覧に出ない、または frontend だけに幽霊フィールドが残る drift が起きます。
- 推奨修正: 共有 import までは不要です。まずは軽い単体テストで十分です。

```python
def test_profile_keys_match_trigger_profile_fields():
    from src.dashboard_api.get_my_profile.app import _PROFILE_KEYS
    from src.trigger.app import PROFILE_FIELDS

    assert tuple(k for k, *_ in PROFILE_FIELDS) == _PROFILE_KEYS
```

frontend 側は `AXIS_FIELDS` のキーが `MyProfile` に存在することは TypeScript で担保されていますが、backend との一致までは担保されません。

### [Info] [template.yaml:1217] `/api/v1/profile/me` 固定と IAM は設計意図どおり
- 問題: なし。
- 影響: path parameter を持たず、`requestContext.authorizer.lambda.user_sub` のみから user id を導出しているため、他ユーザー閲覧経路を作らない設計に合っています。IAM も `dynamodb:GetItem` + `UserProfilesTable.Arn` 単一で過不足ありません。
- 推奨修正: 不要です。

### [Info] [src/dashboard_api/get_my_profile/app.py:53] env 不在の KeyError は IaC 信頼で許容
- 問題: `USER_PROFILES_TABLE` 未設定時は `KeyError` で Lambda 失敗になります。
- 影響: template.yaml で注入されているため、本番構成上の実害は低いです。既存 dashboard API も `os.environ[...]` 前提のためパターン整合しています。
- 推奨修正: 不要です。構成ミスを早く落とす挙動として妥当です。

### [Info] [src/dashboard_api/get_my_profile/app.py:56] DynamoDB 例外の広め捕捉は既存 dashboard API と整合
- 問題: `except Exception` は広いですが、既存 `get_token_monitor_health` なども同系統です。
- 影響: read-only API の 500 変換としては許容範囲です。profile 内容をログに出していない点も良いです。
- 推奨修正: 不要です。より厳密にするなら `botocore.exceptions.ClientError` + 想定外再 raise ですが、本 PR で必須ではありません。

### [Info] [frontend/src/api/client.ts:8] 401 redirect は共通 client で処理済み
- 問題: `MyProfile.tsx` 単体では 401 を一般エラー文言に見せますが、実際は `api/client.ts` が 401 で `/api/v1/auth/login` に遷移します。
- 影響: B5 の懸念は既存設計で解消されています。
- 推奨修正: 不要です。

## 総合評価
- 設計の妥当性: ✅
- エッジケース網羅: ⚠️
- アンチパターン回避: ✅
- テストカバレッジ: ⚠️

## 結論
- マージ可否: 条件付き可 (High の `learned_preferences` 形状不一致対応後)
