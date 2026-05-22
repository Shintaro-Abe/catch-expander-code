# Codex レビュー依頼 (1 回目): Frontend Profile View (F6 閲覧 UX)

## 役割

あなたは Catch-Expander プロジェクトのシニアレビュアーです。F6 (User Profile) の閲覧 UX を frontend SPA に追加する PR をレビューしてください。

対象 commit: `99d8abf feat(dashboard): add /profile read-only view for F6 user profile`

## 背景

### 機能の位置づけ

F6 = ユーザーの 5W1H 直交 6 軸プロファイル (役割・職業 / 関心分野 / 専門・得意領域 / 学習の目的 / 背景・状況 / 受け取り方の好み、各 500 字、任意)。

これまでの状態:
- 登録 / 編集 / 削除は **Slack Modal 経由で完了済み** (commits `20392bf` / `97b033e` / `73c9a56` / `c8f5e97`、`src/trigger/app.py` 内 `PROFILE_FIELDS` / `TestProfileModal` 7 ケース)
- DynamoDB `UserProfilesTable` (PK: `user_id`) に保存
- **閲覧する手段がない** — Slack Modal を再オープンしないと中身が見えない

本 PR はこの欠落を埋めるため:
- 新規 Lambda `DashboardGetMyProfileFunction`
- 新規 HttpApi route `GET /api/v1/profile/me`
- frontend に `/profile` サブページ + Sidebar 「マイプロファイル」
- すべて read-only (編集は Slack Modal 1 系統維持)

### スコープ外（意図的）

- 編集 UI は frontend 側に置かない (Slack Modal の 1 系統メンテに統一)
- 他ユーザー閲覧経路は作らない (API ルートは `/me` 固定、JWT 由来 user_id のみ参照)
- learned_preferences の編集 / リセット機能

### 設計判断 (`.steering/20260518-frontend-profile-view/design.md` §4.1)

**JWT sub → Slack user_id 変換**:
- 実機 cookie デコード結果 (2026-05-19): `sub` は `U04JBJU88A0` のような pure user_id 形式 (ハイフン区切りなし)
- ただし Slack OIDC 仕様上 `<user_id>-<team_id>` 形式の可能性も残るため、防御的に `split("-", 1)[0]` を採用
- Slack user_id は uppercase alphanumeric のみで hyphen を含まない仕様 → split は実害ゼロ

## レビュー対象

### 変更 1: Backend Lambda 新規 (`src/dashboard_api/get_my_profile/app.py`, +67 行)

```python
"""F6 User Profile 閲覧用 Lambda。

JWT 由来の user_sub から Slack user_id を導出し、UserProfilesTable から
本人プロファイル (6 軸 + learned_preferences) を read-only で取得する。

設計: .steering/20260518-frontend-profile-view/design.md
"""

import logging
import os

import boto3
from _common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

# 5W1H 6 軸キー (src/trigger/app.py:PROFILE_FIELDS と整合)
_PROFILE_KEYS = (
    "role",
    "interests",
    "expertise",
    "learning_goals",
    "background",
    "output_preferences",
)


def _extract_slack_user_id(user_sub: str | None) -> str | None:
    """Slack OIDC sub から Slack user_id を取り出す。

    本番 Slack OIDC は pure user_id 形式 (例: "U0XXXXXXXX") を返すことを
    実機 cookie デコードで確認済 (.steering/20260518-frontend-profile-view/tasklist.md T0-1)。
    将来 "<user_id>-<team_id>" 形式に変わっても壊れないよう split("-")[0] で防御。
    Slack user_id は uppercase alphanumeric のみで hyphen を含まない仕様。
    """
    if not user_sub:
        return None
    return user_sub.split("-", 1)[0]


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")

    auth_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    user_sub = auth_ctx.get("user_sub")
    user_id = _extract_slack_user_id(user_sub)
    if not user_id:
        return error_response(401, "UNAUTHORIZED", "Missing or invalid user_sub", request_id)

    table = _dynamodb.Table(os.environ["USER_PROFILES_TABLE"])
    try:
        result = table.get_item(Key={"user_id": user_id})
    except Exception as e:  # noqa: BLE001
        logger.error("DDB get_item failed for request %s: %s", request_id, type(e).__name__)
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    item = result.get("Item") or {}
    body = {
        "user_id": user_id,
        **{k: item.get(k) for k in _PROFILE_KEYS},
        "learned_preferences": item.get("learned_preferences") or [],
        "updated_at": item.get("updated_at"),
    }
    return json_response(200, {"data": body})
```

**重要な設計判断**:
- レコード未存在 → HTTP 200 + null フィールド (404 ではない)。初見ユーザー UX を「エラー画面」ではなく「プレースホルダー画面」に統一するため
- 個人情報保護: profile 内容は CloudWatch Logs に書き出さない (logger.error は request_id + 例外型のみ)

### 変更 2: template.yaml に Lambda + Event + IAM 追加 (+29 行)

```yaml
  # [F6] User Profile 閲覧 Lambda: UserProfilesTable から本人プロファイルを read-only で返す。
  # 編集は src/trigger/app.py の Slack Modal フローで完結 (UpdateItem は付与しない)。
  DashboardGetMyProfileFunction:
    Type: AWS::Serverless::Function
    Properties:
      FunctionName: catch-expander-dashboard-get-my-profile
      Handler: get_my_profile.app.lambda_handler
      CodeUri: src/dashboard_api/
      MemorySize: 128
      Timeout: 10
      Environment:
        Variables:
          USER_PROFILES_TABLE: !Ref UserProfilesTable
      Events:
        GetMyProfile:
          Type: HttpApi
          Properties:
            ApiId: !Ref DashboardApi
            Path: /api/v1/profile/me
            Method: GET
      Policies:
        - Version: "2012-10-17"
          Statement:
            - Effect: Allow
              Action:
                - dynamodb:GetItem
              Resource:
                - !GetAtt UserProfilesTable.Arn
```

**最小権限**: GetItem のみ (UpdateItem は付与しない)。

### 変更 3: Frontend ページ新規 (`frontend/src/routes/MyProfile.tsx`, +118 行)

```tsx
import { useQuery } from "@tanstack/react-query"
import { Info } from "lucide-react"

import { endpoints } from "@/api/endpoints"
import type { MyProfile } from "@/api/types"
import { fmtRelative } from "@/lib/time"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

// 5W1H 6 軸キーとラベルの対応 (backend get_my_profile.app._PROFILE_KEYS と整合)
const AXIS_FIELDS: ReadonlyArray<{ key: keyof Omit<MyProfile, "user_id" | "learned_preferences" | "updated_at">; label: string }> = [
  { key: "role",                label: "役割・職業" },
  { key: "interests",           label: "関心分野" },
  { key: "expertise",           label: "専門・得意領域" },
  { key: "learning_goals",      label: "学習の目的" },
  { key: "background",          label: "背景・状況" },
  { key: "output_preferences",  label: "受け取り方の好み" },
]

function AxisRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-[200px_1fr] gap-2 py-3 border-b border-border last:border-b-0">
      <div className="text-sm font-medium text-foreground">{label}</div>
      {value ? (
        <div className="text-sm text-foreground whitespace-pre-wrap break-words">{value}</div>
      ) : (
        <div className="text-sm italic text-muted-foreground">未設定</div>
      )}
    </div>
  )
}

export function MyProfile() {
  const q = useQuery({
    queryKey: ["my-profile"],
    queryFn: () => endpoints.myProfile().then((r) => r.data),
    staleTime: 60_000,
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">マイプロファイル</h1>
        <p className="text-sm text-muted-foreground mt-1">
          AI に伝わっているあなたの 5W1H 6 軸プロファイル (read-only) と、自動学習された好みの一覧です。
        </p>
      </div>

      {/* 編集導線 banner */}
      <div className="flex items-start gap-3 p-4 rounded-md border border-border bg-muted/30">
        <Info size={16} className="shrink-0 mt-0.5 text-muted-foreground" />
        <div className="text-sm">
          編集は Slack で <code className="px-1.5 py-0.5 rounded bg-muted text-foreground font-mono text-xs">@CatchExpander profile</code> を実行してください。
        </div>
      </div>

      {q.isLoading && (
        <Card>
          <CardContent className="pt-6 space-y-3">
            {AXIS_FIELDS.map((f) => (
              <Skeleton key={f.key} className="h-16 w-full" />
            ))}
          </CardContent>
        </Card>
      )}

      {q.isError && (
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-destructive">
              プロファイルの取得に失敗しました。時間を置いて再読み込みしてください。
            </div>
          </CardContent>
        </Card>
      )}

      {q.data && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>プロファイル</CardTitle>
            </CardHeader>
            <CardContent>
              {AXIS_FIELDS.map((f) => (
                <AxisRow key={f.key} label={f.label} value={q.data[f.key]} />
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>学習履歴 (learned_preferences)</CardTitle>
            </CardHeader>
            <CardContent>
              {q.data.learned_preferences.length === 0 ? (
                <div className="text-sm italic text-muted-foreground">
                  学習履歴はまだありません。フィードバックを送ると AI が自動で好みを学習します。
                </div>
              ) : (
                <ul className="space-y-2 list-disc list-inside text-sm">
                  {q.data.learned_preferences.map((pref, i) => (
                    <li key={i} className="text-foreground break-words">{pref}</li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          {q.data.updated_at && (
            <div className="text-xs text-muted-foreground text-right">
              最終更新: {fmtRelative(q.data.updated_at)}
            </div>
          )}
        </>
      )}
    </div>
  )
}
```

### 変更 4: API クライアント + ルート + Sidebar (合計 +27 行)

`frontend/src/api/types.ts` (+18 行):

```typescript
export interface MyProfile {
  user_id: string
  role: string | null
  interests: string | null
  expertise: string | null
  learning_goals: string | null
  background: string | null
  output_preferences: string | null
  learned_preferences: string[]
  updated_at: string | null
}

export interface MyProfileResponse {
  data: MyProfile
}
```

`frontend/src/api/endpoints.ts` に追加:

```typescript
  myProfile: () =>
    api.get<MyProfileResponse>("/api/v1/profile/me"),
```

`frontend/src/App.tsx` に追加:

```tsx
<Route path="profile" element={<MyProfile />} />
```

`frontend/src/components/Layout.tsx` の NAV 配列末尾に:

```typescript
  { to: "/profile",        icon: UserCircle,       label: "マイプロファイル"   },
```

### テスト (`tests/unit/dashboard_api/test_get_my_profile.py` 新規, +168 行)

`TestGetMyProfile` クラスに 7 ケース:

1. `test_returns_full_profile` — 6 軸全部設定済み + learned_preferences あり → 200
2. `test_returns_placeholder_when_record_missing` — レコード未存在 → 200 + 全 null + 空配列
3. `test_partial_fields_missing` — 一部フィールド REMOVE 済み → 200 + 該当 null
4. `test_strips_team_suffix_from_sub_defensive` — sub が `U...-T...` 形式でも user_id 抽出
5. `test_missing_user_sub_returns_401` — user_sub 欠落 → 401 UNAUTHORIZED
6. `test_empty_user_sub_returns_401` — user_sub が空文字 → 401 UNAUTHORIZED
7. `test_ddb_failure_returns_500` — DDB raise → 500 INTERNAL_ERROR

```
$ uv run pytest tests/unit/dashboard_api/test_get_my_profile.py -v
7 passed in 0.36s
```

### 変更ファイル統計

```
src/dashboard_api/get_my_profile/__init__.py      |   0 行 (空)
src/dashboard_api/get_my_profile/app.py           | +67 行
template.yaml                                     | +29 行
tests/unit/dashboard_api/conftest.py              |  +1 行
tests/unit/dashboard_api/test_get_my_profile.py   | +168 行
frontend/src/api/types.ts                         | +18 行
frontend/src/api/endpoints.ts                     |  +4 行
frontend/src/routes/MyProfile.tsx                 | +118 行
frontend/src/App.tsx                              |  +2 行
frontend/src/components/Layout.tsx                |  +3 / -1 行
合計: +411 行 / -1 行 (steering 3 文書 +760 行を除く)
```

## レビュー観点

以下を **Critical / High / Medium / Low / Info** の 5 段階で分類してください。

### A. 設計・構造の妥当性

- A1: backend Lambda は既存 `src/dashboard_api/get_token_monitor_health/app.py` のパターンを正しく踏襲しているか。`_common.py` の使い方、boto3 リソース初期化、HttpApi event 構造の扱いに齟齬はないか
- A2: API ルート `/api/v1/profile/me` の固定 (path parameter なし) は「他ユーザー閲覧経路を作らない」設計意図と整合しているか。Authorizer 経由で取得する `user_sub` のみを信用する経路に閉じているか
- A3: レコード未存在時に 404 ではなく 200 + null を返す判断は、frontend UX (プレースホルダー表示) を考慮した妥当な設計か。API のセマンティクスとして API 利用者を混乱させないか
- A4: `_PROFILE_KEYS` (Lambda 側) と `AXIS_FIELDS` (frontend 側) と `PROFILE_FIELDS` (`src/trigger/app.py`) の 3 箇所で 6 軸定義が分散している。整合性を担保する仕組み (テスト / 型) は十分か、それとも分散自体が後の drift リスクか
- A5: IAM ポリシーは `dynamodb:GetItem` のみで最小権限。Resource は `UserProfilesTable.Arn` 単一。これに過不足はないか

### B. エッジケース・例外経路

- B1: `_extract_slack_user_id` の防御挙動 (`split("-", 1)[0]`)。実機 `sub` が pure user_id (例: `U04JBJU88A0`) であることは検証済だが、将来 Slack OIDC 仕様変更で sub が `<user>-<team>` 形式になっても安全か。逆に sub が想定外形式 (空文字 / 数字のみ / 特殊文字含む) のとき抽出ロジックが不正値を返す経路は塞がれているか
- B2: `os.environ["USER_PROFILES_TABLE"]` が未設定の場合 KeyError → Lambda 500 になる。テストでは monkeypatch で env を設定しているが、本番デプロイでは template.yaml で確実に注入される。env 不在ケースに防御を追加すべきか、それとも IaC 信頼で十分か
- B3: DynamoDB エラー時の `except Exception as e: # noqa: BLE001` は過剰捕捉。`ClientError` のみに絞るべきか、それとも Lambda の包括的失敗防御として現状で良いか
- B4: `result.get("Item") or {}` で None / 空 dict を吸収しているが、UserProfilesTable の item.learned_preferences が「文字列」「dict」「None」など想定外型を保持していた場合 (旧データ移行など) `or []` で吸収しきれず frontend で render エラーになる可能性。型契約の防御は十分か
- B5: frontend `MyProfile.tsx` の `q.isError` ハンドリング。401 (cookie 期限切れ) でも単に「取得に失敗しました」を出すだけで、本来は login redirect が期待される。axios interceptor / client の 401 ハンドリングはどこで行われているか確認の必要

### C. 再発バグ領域 / アンチパターン回避

- C1: F6 関連 (`src/trigger/app.py` の Modal フロー) では過去 4 連続パッチ (`20392bf` / `97b033e` / `73c9a56` / `c8f5e97`) が発生したエリア。本 PR は **同ファイルに触れない別 Lambda** を追加する形で、再発リスクの構造的隔離は適切か。`_PROFILE_KEYS` のような共通定義を `trigger/app.py:PROFILE_FIELDS` から import する選択肢を取らなかった (key だけ手動同期する) 判断はリスクか
- C2: `memory/feedback_anti_pattern_discipline.md` の 3 層代替案規律 (プロンプト層 / パイプライン層 / 型層)。本 PR は「新規機能追加」であって既存バグ修正ではないが、design.md §4.1 (sub 抽出の案 A/B/C 比較) は規律を満たしているか
- C3: `memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` の知見 (2-3 pass で次層が剥がれる)。Pass 1 で見落としがちな次層 (例: frontend 401 ハンドリング / DDB 型契約) を本観点で先に潰す
- C4: `memory/feedback_codex_finding_interpretation_pitfall.md`: 外部仕様の実態確認なしで Codex 指摘を拡大解釈しない。今回の sub 形式確認は実機 cookie デコードで実証済なので OK と判断するが、Slack OIDC の公式仕様 (URL 引用希望) と照合して本実装は妥当か

## 出力フォーマット

```markdown
# Codex Review — 2026-05-19 frontend-profile-view (Pass 1)

## サマリ
- Critical: N / High: N / Medium: N / Low: N / Info: N
- 総合所感 (5 行以内)

## 指摘事項

### [severity] [path:line] 短いタイトル
- 問題: ...
- 影響: ...
- 推奨修正: ...

(severity 順に列挙、severity は Critical / High / Medium / Low / Info)

## 総合評価
- 設計の妥当性: ✅ / ⚠️ / ❌
- エッジケース網羅: ✅ / ⚠️ / ❌
- アンチパターン回避: ✅ / ⚠️ / ❌
- テストカバレッジ: ✅ / ⚠️ / ❌

## 結論
- マージ可否: 可 / 条件付き可 (指摘対応後) / 不可
```

注意:
- 「対応が完全 / 不要修正なし」も明示してほしい
- 公式仕様への引用があれば URL 付きで (例: Slack OIDC sub 仕様)
- 推奨修正は具体的なコードスニペットで示してほしい
