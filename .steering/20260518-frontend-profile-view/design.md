# Frontend Profile View 設計書

## 1. 全体構成

```
┌─ Frontend (SPA, S3 + CloudFront) ─────────────────────────────┐
│  Layout (Sidebar に「マイプロファイル」追加)                       │
│   └─ /profile route                                           │
│       └─ MyProfile.tsx                                        │
│           ├─ ヘルプ文「編集は Slack で @CatchExpander profile」│
│           ├─ 6 軸セクション (read-only)                         │
│           └─ learned_preferences リスト                        │
│                  │                                            │
│                  │ GET /api/v1/profile/me (session cookie)    │
│                  ▼                                            │
└────────────────────┼──────────────────────────────────────────┘
                     ▼
┌─ API Gateway (HttpApi: DashboardApi) ─────────────────────────┐
│  GET /api/v1/profile/me  → Lambda Authorizer 経由              │
│   └─ requestContext.authorizer.lambda.user_sub                │
└────────────────────┼──────────────────────────────────────────┘
                     ▼
┌─ Lambda: DashboardGetMyProfileFunction ───────────────────────┐
│  Handler: get_my_profile.app.lambda_handler                   │
│  1. event から user_sub を取得                                  │
│  2. user_sub → Slack user_id を抽出 (§4.1)                     │
│  3. UserProfilesTable.get_item(Key={"user_id": ...})          │
│  4. 6 軸 + learned_preferences + updated_at を JSON 返却         │
└────────────────────┼──────────────────────────────────────────┘
                     ▼
┌─ DynamoDB: UserProfilesTable ─────────────────────────────────┐
│  PK: user_id (S)                                              │
│  Fields: role / interests / expertise / learning_goals /      │
│          background / output_preferences / learned_preferences│
│          updated_at                                           │
└───────────────────────────────────────────────────────────────┘
```

## 2. Backend Lambda 設計

### 2.1 ファイル配置

```
src/dashboard_api/
  get_my_profile/
    __init__.py
    app.py             # lambda_handler 実装
```

既存の `get_token_monitor_health/` と同じ並列構造。`_common.py` (json_response / error_response) は既存パッケージから import。

### 2.2 ハンドラ実装方針

```python
# src/dashboard_api/get_my_profile/app.py

import logging
import os

import boto3

from _common import error_response, json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")

# 6 軸キー一覧 (PROFILE_FIELDS と整合)
_PROFILE_KEYS = [
    "role", "interests", "expertise",
    "learning_goals", "background", "output_preferences",
]


def lambda_handler(event: dict, context: object) -> dict:
    request_id = getattr(context, "aws_request_id", "")
    auth_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    user_sub = auth_ctx.get("user_sub")
    if not user_sub:
        return error_response(401, "UNAUTHORIZED", "Missing user_sub", request_id)

    user_id = _extract_slack_user_id(user_sub)  # §4.1 参照
    if not user_id:
        return error_response(400, "INVALID_USER_ID", "Cannot extract Slack user_id", request_id)

    table = _dynamodb.Table(os.environ["USER_PROFILES_TABLE"])
    try:
        result = table.get_item(Key={"user_id": user_id})
    except Exception as e:
        logger.error("DDB get_item failed: %s", e)
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

**重要設計判断**:
- レコード未存在時 → HTTP 200 で `null` フィールド + 空配列を返す (404 ではない)。初見ユーザー UX を「エラー画面」ではなく「プレースホルダー画面」に統一するため
- 未設定フィールド (REMOVE 済み) も `None` → frontend で「未設定」表示にマップ
- learned_preferences は配列ない場合 `[]` に正規化

### 2.3 template.yaml 追加

既存の `DashboardGetTokenMonitorHealthFunction` (template.yaml:1187-1213) を雛形に追加:

```yaml
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

最小権限: **GetItem のみ** (UpdateItem は不要、編集は TriggerFunction 担当のため)。

## 3. Frontend 設計

### 3.1 ファイル追加 / 変更

| ファイル | 操作 | 内容 |
|---|---|---|
| `frontend/src/api/types.ts` | 追記 | `MyProfile` interface 追加 |
| `frontend/src/api/endpoints.ts` | 追記 | `getMyProfile(): Promise<MyProfile>` 追加 |
| `frontend/src/routes/MyProfile.tsx` | 新規 | ページコンポーネント |
| `frontend/src/App.tsx` | 1 行追加 | `<Route path="profile" element={<MyProfile />} />` |
| `frontend/src/components/Layout.tsx` | NAV 配列 1 エントリ追加 | `{ to: "/profile", icon: UserCircle, label: "マイプロファイル" }` |

### 3.2 型定義 (types.ts に追加)

```typescript
export interface MyProfile {
  user_id: string
  role: string | null
  interests: string | null
  expertise: string | null
  learning_goals: string | null
  background: string | null
  output_preferences: string | null
  learned_preferences: string[]   // 配列要素は仮に string とする (§3.5 参照)
  updated_at: string | null
}
```

### 3.3 ページコンポーネント設計 (MyProfile.tsx)

- 既存ページ (DashboardHome.tsx 等) と同じパターン:
  - useEffect で `getMyProfile()` 呼び出し
  - loading / error / data の 3 状態
  - エラー時は既存の error banner コンポーネントを再利用
- レイアウト:
  ```
  ┌─ ヘルプ banner (info アイコン + 案内文) ─┐
  └─────────────────────────────────────┘
  ┌─ 6 軸セクション (card layout, 縦並び) ─┐
  │  [役割・職業]      │
  │  値ありなら whitespace-pre-wrap で全文 │
  │  null なら text-muted-foreground「未設定」│
  │  ...同じパターン×6                    │
  └─────────────────────────────────────┘
  ┌─ learned_preferences セクション ─┐
  │  空なら「学習履歴はまだありません」│
  │  あれば ul で要素を一覧表示       │
  └─────────────────────────────────┘
  ┌─ updated_at footer (ISO → ローカル時刻) ─┐
  └─────────────────────────────────────┘
  ```
- Sidebar アイコン: `lucide-react` の `UserCircle` (既存の `User` は logout 行で使用済み)

### 3.4 Sidebar への追加 (Layout.tsx NAV 配列)

既存:
```typescript
const NAV = [
  { to: "/dashboard",      icon: LayoutDashboard, label: "ダッシュボード"    },
  { to: "/executions",     icon: List,             label: "実行一覧"          },
  { to: "/review-quality", icon: Star,             label: "レビュー品質"      },
  { to: "/errors",         icon: AlertTriangle,    label: "エラー"            },
  { to: "/feedback",       icon: MessageSquare,    label: "フィードバック分析" },
]
```

末尾に追加 (UserCircle import も追加):
```typescript
  { to: "/profile",        icon: UserCircle,       label: "マイプロファイル"   },
```

### 3.5 learned_preferences の型確定

`requirements.md` AC-1 では `learned_preferences: [...]` とのみ記載。実態は DynamoDB の自由形式。実装時に DDB の現在データを参照して以下のいずれかに確定:

- **A. `string[]` の単純リスト** (例: `["長めのサマリを好む", "コード例を望む"]`)
- **B. `{label: string, count: number}[]` の構造化リスト** (頻度情報込み)

初期実装は **A (単純リスト + 文字列レンダ)** を採用し、構造化が必要なら後続 steering で拡張。

## 4. 主要な設計論点

### 4.1 ⚠ JWT sub から Slack user_id を取り出す方式 (要事前確認)

**問題**: 現状の JWT (`src/dashboard_api/auth_callback/app.py:128`) は Slack OIDC userInfo の `sub` を**そのまま**保存している。一方 UserProfilesTable の PK は Slack `user_id` (例: `U0XXXXXXX`)。

Slack OIDC 仕様上、`sub` の形式は `<user_id>-<team_id>` (例: `U0XXXXXXX-T0XXXXXXX`)。

**実装オプション**:

| 案 | 内容 | 影響範囲 | 推奨度 |
|---|---|---|---|
| A | get_my_profile 内で `sub.split("-")[0]` で user_id 抽出 | get_my_profile のみ | ★★ (実装軽量・既存契約に依存) |
| B | auth_callback で JWT に `slack_user_id` クレームを追加保存 | auth_callback + authorizer + JWT TTL 期間中の全セッション再ログイン | ★ (クリーン・スコープ広い) |
| C | 環境変数で固定 user_id を埋め込む (個人利用前提) | template.yaml + Lambda env | ☆ (技術的負債、複数ユーザー化で破綻) |

**推奨**: **案 A** + tasklist 冒頭に **「現行 JWT cookie をブラウザ DevTools で取り出してデコードし、`sub` の実形式を確認する」検証タスク**を入れる。
- 形式が `U...-T...` なら `.split("-")[0]`
- 形式が純粋 `U...` のみなら抽出不要 (そのまま使用)
- どちらでも案 A の 1 行で吸収できる

**理由**: 案 B は影響範囲が広い (全ユーザー再ログイン必要)、案 C は memory `feedback_no_unsolicited_build_deploy_codex_required` の精神 (将来拡張余地を残す) に反する。

### 4.2 認可: 他ユーザー閲覧経路を作らない

- API Gateway ルートは `/api/v1/profile/me` 固定 (`/api/v1/profile/{userId}` のような可変ルートは作らない)
- Lambda 内で `user_id` は **JWT 由来のみ** から導出し、query string / path parameter は一切参照しない
- これにより「URL 細工で他ユーザー閲覧」攻撃面を発生させない

### 4.3 エラーレスポンス契約

| ケース | HTTP | code |
|---|---|---|
| 認証なし (cookie 期限切れ等) | 401 | (Authorizer 拒否、frontend は既存 redirect 処理に乗る) |
| user_sub クレーム欠落 | 401 | `UNAUTHORIZED` |
| user_id 抽出失敗 (sub 形式異常) | 400 | `INVALID_USER_ID` |
| DDB エラー | 500 | `INTERNAL_ERROR` |
| 正常 (レコード未存在含む) | 200 | (`data` フィールドに null 多数) |

### 4.4 個人情報保護

- profile は本人の入力した個人情報を含む (requirements.md §4.1 参照)
- Lambda 環境変数 / CloudWatch Logs に profile 内容を一切書き出さない
- ログ出力: request_id, user_sub 末尾 4 文字, DDB エラー型のみ
- frontend は HTTPS Only (CloudFront 強制)、cookie HttpOnly Secure SameSite=Lax (既存設定)

## 5. テスト方針

### 5.1 Backend unit test (`tests/unit/dashboard_api/test_get_my_profile.py` 新規)

| ケース | 期待 |
|---|---|
| 正常: 6 軸全部設定済み + learned_preferences あり | 200, 全フィールド非 null |
| 正常: レコード未存在 (新規ユーザー) | 200, 6 軸全部 null, learned_preferences=[] |
| 正常: 一部フィールドが REMOVE 済み | 200, REMOVE フィールドは null |
| 異常: user_sub 欠落 | 401 UNAUTHORIZED |
| 異常: sub 形式異常で user_id 抽出失敗 | 400 INVALID_USER_ID |
| 異常: DDB エラー (mock で raise) | 500 INTERNAL_ERROR |

### 5.2 Frontend test (任意)

既存 frontend に jest 系の単体テスト整備があれば追加。なければ実機検証 (CloudFront URL でログイン → `/profile` で確認) のみ。memory `project_frontend_deploy_state.md` を参照。

### 5.3 実機検証

- 自分のプロファイル (6 軸全部設定済み) で `/profile` 表示確認
- Modal で 1 軸を空欄保存 → `/profile` リロード → 該当軸が「未設定」表示になることを確認
- DevTools で session cookie を一時削除 → `/profile` がログインページに redirect されることを確認

## 6. デプロイ手順 (実装後に tasklist で具体化)

1. `sam build` → `sam deploy` で新 Lambda + API GW ルート反映
2. `cd frontend && npm run build`
3. `aws s3 sync dist/ s3://<bucket>/`
4. CloudFront invalidation `/*`
5. 実機検証 (CloudFront URL ログイン → `/profile`)
6. CloudWatch Logs で `catch-expander-dashboard-get-my-profile` 起動確認

memory [[feedback_frontend_deploy_separate_from_sam]] 準拠 (sam だけでは frontend bundle 古いまま)。

## 7. 想定工数

| フェーズ | 工数 |
|---|---|
| §4.1 検証 (sub 形式デコード) | 5 分 |
| Backend Lambda + template.yaml | 30 分 |
| Backend unit test 6 ケース | 30 分 |
| Frontend type + endpoint + page + Layout 修正 | 40 分 |
| ローカル動作確認 (frontend dev server) | 15 分 |
| sam deploy + frontend deploy | 15 分 |
| 実機検証 | 15 分 |
| Codex レビュー 1 pass | 30 分 |
| **合計** | **~3 時間 (1 セッション内に収まる)** |

## 8. 関連 memory / commit

- 既存パターン参照:
  - `src/dashboard_api/get_token_monitor_health/app.py` (Lambda 雛形)
  - `src/dashboard_api/auth_callback/app.py` (JWT 発行)
  - `src/dashboard_api/authorizer/app.py` (user_sub コンテキスト)
  - `frontend/src/routes/DashboardHome.tsx` (ページ雛形)
- memory:
  - [[feedback_frontend_deploy_separate_from_sam]] — frontend デプロイ手順
  - [[feedback_no_unsolicited_build_deploy_codex_required]] — push 後 Codex ゲート必須
  - [[feedback_codex_review_via_audit_dir]] — Codex レビュー結果は `.audit/` に prompt + 結果ペアで残す
