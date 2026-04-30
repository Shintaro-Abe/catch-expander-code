# 設計 — ワークフロー監視ダッシュボード (専用 Web UI)

> 関連: [`requirements.md`](./requirements.md) / `tasklist.md` (本設計確定後に作成)
>
> 本設計書は requirements.md の AC-1〜AC-10、オープン論点 1〜10 を解決した実装方針を提示する。

---

## 1. 全体アーキテクチャ

### 1.1 構成図

```
┌─────────────────────────────────────────────────────────────────────┐
│                         閲覧者 (ブラウザ)                            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTPS
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  CloudFront (Distribution)                                          │
│   - SPA 静的アセット配信  (キャッシュ)                              │
│   - /api/* を API Gateway へ origin                                 │
│   - カスタムヘッダー X-Origin-Verify で API GW 直叩き防止             │
└──────────┬──────────────────────────────────────┬──────────────────┘
           │                                       │ /api/*
           ▼                                       ▼
┌────────────────────┐                 ┌──────────────────────────────┐
│ S3 Bucket (private)│                 │ API Gateway HTTP API         │
│  /index.html       │                 │   - Custom Lambda Authorizer │
│  /assets/*.js,css  │                 │   - JWT 検証                 │
│  (Vite build 出力) │                 └─────────────┬────────────────┘
└────────────────────┘                               │
                                                     ▼
                                         ┌──────────────────────────┐
                                         │ Backend Lambdas (Python) │
                                         │  - auth_login            │
                                         │  - auth_callback         │
                                         │  - auth_logout           │
                                         │  - list_executions       │
                                         │  - get_execution         │
                                         │  - get_execution_events  │
                                         │  - get_metrics_summary   │
                                         │  - get_review_quality    │
                                         │  - get_errors            │
                                         │  - get_me                │
                                         └────────┬─────────────────┘
                                                  │
       ┌──────────────────────────┬───────────────┼─────────────────┐
       ▼                          ▼               ▼                 ▼
┌─────────────────┐  ┌────────────────────┐  ┌──────────┐  ┌─────────────────┐
│ DynamoDB        │  │ DynamoDB           │  │ DynamoDB │  │ Secrets Manager │
│   events (新規) │  │   workflows (既存) │  │ deliv.   │  │   - JWT signing │
│   PK/SK 後述    │  │                    │  │ (既存)   │  │   - Slack OAuth │
└──────▲──────────┘  └────────────────────┘  └──────────┘  └─────────────────┘
       │ writes (best-effort)
       │
┌──────┴────────────────────────┐
│ ECS Fargate (orchestrator)    │
│   - _emit_event() ヘルパー    │
│ Lambda Trigger                 │
│   - topic_received イベント   │
└───────────────────────────────┘
```

### 1.2 主要コンポーネント

| 層 | コンポーネント | 役割 |
|---|---|---|
| プレゼンテーション | CloudFront + S3 (SPA) | 静的ファイル配信、TLS 終端、エッジキャッシュ |
| API ルーティング | API Gateway HTTP API | リクエスト振り分け、Lambda Authorizer 経由の認証 |
| 認証 | Slack OAuth v2 + JWT (HttpOnly cookie) + Lambda Authorizer | ログイン/セッション管理/workspace 制限 |
| ビジネスロジック | Backend Lambdas (Python 3.13) | 各 API エンドポイント |
| データ | DynamoDB (events 新規 + workflows/deliverables 既存) | 永続化 |
| イベント書き込み | `_emit_event()` ヘルパー (orchestrator + Lambda Trigger に組み込み) | 構造化イベント永続化 (best-effort) |
| シークレット管理 | Secrets Manager | JWT signing key、Slack OAuth client secret |

### 1.3 設計原則

- **既存挙動の不可侵**: orchestrator / Lambda Trigger / Notion / GitHub / Slack 通知の主要パスには触れず、観測ポイントに `_emit_event()` のラッパー呼び出しを追加するのみ
- **best-effort 書き込み**: events DDB 書き込みが失敗しても業務継続。失敗時は `logging.error()` のみ、Slack 通知や Notion 格納はブロックしない
- **新インフラの最小化**: 既存 SAM template に追記する形で構築。新 AWS アカウント・新 VPC は不要
- **静的優先**: 動的サーバーサイドレンダリングはせず、SPA + REST API の素直な構成。デバッグ・キャッシュ戦略がシンプル

---

## 2. データ層設計

### 2.1 events テーブル スキーマ (新規)

```python
{
    "execution_id": "exec-abc123",          # PK (string)
    "sk": "2026-04-30T02:28:13.456Z#0001",  # SK (string): "<ISO8601>#<5桁sequence>"
    "event_type": "workflow_planned",       # 例: topic_received / workflow_planned / ...
    "timestamp": "2026-04-30T02:28:13.456Z",# 重複だが GSI 用に正規化フィールド
    "sequence_number": 1,                   # 同一 execution 内の発生順
    "status_at_emit": "in_progress",        # 観測時点の execution status (success / failed / partial / in_progress)
    "payload": { ... },                     # event_type 別の構造化 dict (詳細 §2.3)
    "ttl": 1722298093                       # epoch seconds、創出から 90 日後
}
```

**選定理由**:

- **PK = execution_id**: 「特定実行の全イベント取得」(AC-4) が最頻クエリ。`Query` 一発で取得可能
- **SK = timestamp#seq**: 同一実行内で時系列順に並べ、`sequence_number` で同一 ms に複数イベントが発生するケースに対応
- **TTL 90 日**: requirements.md スコープ「90 日以上は含まない」に整合。コスト抑制

### 2.2 events テーブル GSI (Global Secondary Index)

| GSI 名 | PK | SK | 用途 |
|---|---|---|---|
| `gsi_global_timestamp` | `gsi_pk` (静的値 `"GLOBAL"`) | `timestamp` | 「直近 N 件の実行」「期間内の全実行」(AC-3) |
| `gsi_status_timestamp` | `status_at_emit` | `timestamp` | 「失敗実行のみ」「24h 内の success」(AC-2 / AC-6) |
| `gsi_event_type_timestamp` | `event_type` | `timestamp` | 「`review_completed` イベントのみ」「`error` のみ」(AC-5 / AC-6) |

**注**: `gsi_global_timestamp` の PK は単一値 `"GLOBAL"` だが、Catch-Expander の実行頻度 (~100/月) では hot partition にはならない。スループットが懸念になる規模ではない。

### 2.3 イベント payload スキーマ (event_type 別)

requirements.md AC-7 で挙げた 9 種類のイベントの payload 構造:

#### `topic_received` (Lambda Trigger 発火)

```python
{
    "topic": "AWS Route 53 の Resolver を Terraform で作って",  # raw 入力 (PII 注意)
    "user_id_hash": "sha256:abc123...",   # Slack user ID の SHA-256 ハッシュ
    "channel_id": "C12345",                # Slack channel ID
    "workflow_run_id": "abc-uuid-123"      # ECS RunTask 結果の workflow_run_id
}
```

#### `workflow_planned` (orchestrator)

```python
{
    "topic_category": "技術",             # 自律分類 (技術 / 時事 / 人物 等)
    "expected_deliverable_type": "iac_code",  # text / iac_code / program_code
    "planned_subagents": ["researcher", "generator", "reviewer"],
    "planning_summary": "Terraform で Route 53 Resolver を IaC..."  # 短い要約 (<= 500 文字)
}
```

#### `research_completed` (researcher)

```python
{
    "sources": [
        {"url": "https://docs.aws.amazon.com/...", "title": "...", "domain": "docs.aws.amazon.com"},
        ...
    ],
    "source_count": 12,
    "source_domains": ["docs.aws.amazon.com", "registry.terraform.io"],
    "total_tokens_used": 8523
}
```

#### `subagent_started` / `subagent_completed` / `subagent_failed`

```python
# started
{"subagent": "generator", "input_summary": "Terraform 実装"}

# completed
{
    "subagent": "generator",
    "duration_ms": 45230,
    "tokens_used": 12450,
    "output_summary": "main.tf + README.md を生成 (4 ファイル / 320 行)"
}

# failed
{
    "subagent": "generator",
    "duration_ms": 12100,
    "error_type": "TimeoutError",
    "error_message": "..."
}
```

#### `review_completed`

```python
{
    "iteration": 2,
    "passed": True,
    "issues_count": 3,
    "fixer_notes_count": 2,
    "code_related_unfixed_count": 0,  # 案 A 起票判断連動 (重要)
    "issues_summary": [
        {"severity": "P2", "target_field": "code_files", "message_excerpt": "..."},
        ...
    ]
}
```

#### `notion_stored` / `github_stored` / `slack_notified`

```python
# notion_stored
{"url": "https://www.notion.so/...", "page_id": "abc-..."}

# github_stored
{"url": "https://github.com/.../tree/main/...", "code_files_present": True, "files_count": 4}

# slack_notified
{"channel_id": "C12345", "ts": "1714694293.000100"}
```

#### `execution_completed`

```python
{
    "status": "success",                 # success / failed / partial
    "total_duration_ms": 156230,
    "total_tokens_used": 35420,
    "final_deliverable_url": "https://www.notion.so/...",
    "github_url": "https://github.com/..."  # null if no code
}
```

#### `error`

```python
{
    "error_type": "NotionAPIError",
    "error_message": "...",
    "stack_trace": "Traceback...",
    "stage": "notion_storage",            # どの段階で発生したか
    "is_recoverable": False
}
```

### 2.4 既存 DynamoDB テーブルとの関係

events テーブルと既存テーブルの役割分離:

| テーブル | 役割 | 本タスクでの変更 |
|---|---|---|
| `workflows` (既存) | 実行状態 (status, current_stage 等) | **変更なし**。UI でメタ情報取得用に Read のみ |
| `deliverables` (既存) | 成果物 URL (Notion / GitHub) | **変更なし**。UI で成果物リンク表示用に Read のみ |
| `events` (新規) | 構造化イベント時系列 | 本タスクで新規追加。orchestrator / Lambda Trigger が Write、Backend Lambda が Read |

execution_id は 3 テーブル間で共通キーとして機能する。

---

## 3. バックエンド API 設計

### 3.1 API エンドポイント一覧

| メソッド | パス | Lambda | 役割 |
|---|---|---|---|
| GET | `/api/v1/auth/login` | `auth_login` | Slack OAuth 認可エンドポイントへリダイレクト |
| GET | `/api/v1/auth/callback` | `auth_callback` | Slack callback 受信 → workspace 検証 → JWT 発行 → SPA へリダイレクト |
| POST | `/api/v1/auth/logout` | `auth_logout` | セッション cookie 削除 |
| GET | `/api/v1/auth/me` | `auth_me` | 現在のセッション情報 (user_name, expires_at) |
| GET | `/api/v1/executions` | `list_executions` | 実行一覧 (絞り込み対応) |
| GET | `/api/v1/executions/{execution_id}` | `get_execution` | 単一実行のメタ情報 |
| GET | `/api/v1/executions/{execution_id}/events` | `get_execution_events` | 単一実行の全イベント時系列 |
| GET | `/api/v1/metrics/summary` | `get_metrics_summary` | ダッシュボードトップ用集計 (期間指定) |
| GET | `/api/v1/metrics/review-quality` | `get_review_quality` | レビュー品質画面用集計 |
| GET | `/api/v1/errors` | `get_errors` | エラー一覧 (期間指定) |

### 3.2 Lambda 構成方針

**選定**: **per-endpoint Lambda** (10 個の独立 Lambda) を採用。

**理由**:
- 各 Lambda の責務が明確で単体テストしやすい
- IAM ポリシーを最小権限で個別付与可能 (events 読み取り専用、Secrets Manager 読み取り等)
- cold start 影響は API Gateway HTTP API + Lambda の組み合わせで実用的に許容範囲 (~200-500ms warm-up)
- 単一 proxy Lambda は内部ルーティングが煩雑になり、SAM の `Events` 定義の利点を失う

**代替案として却下**:
- 単一 proxy Lambda (Flask/Mangum 等で内部ルーティング) → 非採用 (ルーティングが二重管理になる)
- AWS AppSync + GraphQL → 非採用 (新規概念導入で学習コスト増、本要件には過剰)

### 3.3 共通仕様

#### 認証

- 全エンドポイントは Lambda Authorizer 経由で JWT 検証 (例外: `/auth/login` `/auth/callback` のみパブリック)
- JWT は HttpOnly cookie で送受信 (`Secure` + `SameSite=Lax`)
- 検証失敗時は 401 Unauthorized

#### レスポンス形式

成功時:
```json
{
    "data": { ... },                   // または [...]
    "meta": {                          // ページネーション等
        "next_cursor": "...",
        "total": 50
    }
}
```

エラー時:
```json
{
    "error": {
        "code": "EXECUTION_NOT_FOUND",
        "message": "Execution not found",
        "request_id": "abc-uuid"        // CloudWatch Logs 検索用
    }
}
```

#### CORS

CloudFront 経由で同一オリジン (`/api/*` も `/` も同じドメイン) なので CORS preflight 不要。direct API GW URL は CORS 拒否 (`X-Origin-Verify` ヘッダ検証で防御)。

### 3.4 主要 API の詳細

#### `GET /api/v1/executions`

クエリパラメータ:
- `from`, `to`: ISO 8601 (デフォルト 7 日前 ~ 現在)
- `status`: success / failed / partial / in_progress (multi-select、カンマ区切り)
- `topic`: 部分一致キーワード
- `limit`: デフォルト 50、最大 200
- `cursor`: ページネーション用

実装:
- `gsi_global_timestamp` で期間範囲 Query
- アプリケーション側で status / topic フィルタ (DDB の制約上)
- 大量データ時は `Limit` + `LastEvaluatedKey` でページング

#### `GET /api/v1/executions/{execution_id}/events`

実装:
- events テーブルを `Query(KeyCondition=execution_id == :id)` で取得
- SK で時系列ソート済み
- ページネーション不要 (1 実行あたり最大 ~30 イベント想定)

#### `GET /api/v1/metrics/summary`

クエリパラメータ:
- `period`: `24h` / `7d` / `30d`

実装:
- `gsi_global_timestamp` で期間範囲を Query
- アプリケーション側で集計 (件数、status 別件数、平均 duration、レビュー pass 率)
- 結果は API Gateway の cache (60 秒) で軽量化 (任意、Phase 2 で追加可)

---

## 4. 認証フロー設計

### 4.1 Slack OAuth v2 + JWT cookie の実装パス

requirements.md オープン論点 1 の解決:

**選定**: **Slack OAuth v2 + 自前 JWT in HttpOnly cookie + 自前 Lambda Authorizer**。

**選定理由**:
- Cognito 経由の Slack ID provider 設定は overhead が大きい (User Pool / Identity Pool 設定、SDK 統合)
- 自前 OAuth 実装は ~200 行の Lambda で完結、シークレット管理は Secrets Manager に集約
- JWT in HttpOnly cookie は XSS 耐性 + CSRF は SameSite=Lax で緩和可能 (本ダッシュボードは write 操作なし)

### 4.2 ログインフロー (詳細)

```
[1] User → CloudFront → S3 (index.html)
[2] SPA loads, calls GET /api/v1/auth/me
[3] auth_me Lambda: cookie の JWT が無効/期限切れ → 401
[4] SPA detects 401 → window.location = /api/v1/auth/login
[5] auth_login Lambda:
     - state 値を生成 (CSRF 対策)
     - state を一時 DDB に保存 (TTL 10 分)
     - Slack OAuth URL にリダイレクト:
       https://slack.com/oauth/v2/authorize?
         client_id=<CATCH_EXPANDER_CLIENT_ID>&
         scope=openid,profile,email&
         state=<state>&
         redirect_uri=https://dashboard.../api/v1/auth/callback
[6] User signs in on Slack
[7] Slack → /api/v1/auth/callback?code=<code>&state=<state>
[8] auth_callback Lambda:
     - state を DDB から検証 (一致しなければ 400)
     - Slack /api/oauth.v2.access を呼び出して access_token を取得
     - access_token で Slack /api/openid.connect.userInfo を呼び出して
       {"https://slack.com/team_id": "T01234", "name": "Alice", "sub": "U..."} を取得
     - team_id が CATCH_EXPANDER_WORKSPACE_ID と一致するか検証
     - 一致しなければ 401
     - 一致すれば JWT を発行 (claims: sub, name, exp=24h)
     - HttpOnly cookie でセット
     - SPA トップ (/) にリダイレクト
[9] SPA reload → /api/v1/auth/me 成功 → 認証済み状態でダッシュボード表示
```

### 4.3 セッション期限と更新

- JWT の `exp` claim = 発行から 24 時間
- 期限切れの API 呼び出しは 401 → SPA が再ログインフロー誘導
- 「sliding window」(リフレッシュ) は Phase 2。MVP では固定 24h

### 4.4 シークレット管理

新規 Secrets Manager エントリ:

| シークレット ARN | 内容 |
|---|---|
| `catch-expander/dashboard-jwt-key` | JWT 署名用 HMAC-SHA256 キー (256 bit ランダム) |
| `catch-expander/dashboard-slack-oauth` | `{"client_id": "...", "client_secret": "...", "workspace_id": "T..."}` |

orchestrator 側のシークレット (`slack-bot-token` 等) とは **完全に分離** する (権限境界明確化)。

### 4.5 Lambda Authorizer

API Gateway HTTP API の Custom Lambda Authorizer を採用。

```python
# lambda_authorizer
def lambda_handler(event, context):
    cookie = event["cookies"]
    jwt_token = parse_cookie(cookie, "session")
    if not jwt_token:
        return {"isAuthorized": False}
    try:
        claims = jwt.decode(jwt_token, signing_key, algorithms=["HS256"])
        return {
            "isAuthorized": True,
            "context": {"user_sub": claims["sub"], "user_name": claims["name"]}
        }
    except jwt.PyJWTError:
        return {"isAuthorized": False}
```

- レスポンスは 5 分キャッシュ (cookie ヘッダの hash でキー化)
- 各 Lambda の `event.requestContext.authorizer.lambda` で認証情報を受け取る

---

## 5. フロントエンド設計

### 5.1 技術スタック (オープン論点 2 解決)

| 層 | 採用ライブラリ | 理由 |
|---|---|---|
| ビルドツール | **Vite 5** | 高速ビルド、HMR、TS 標準対応 |
| 言語 | **TypeScript 5** | 型安全性、既存 Catch-Expander の品質規律と整合 |
| UI フレームワーク | **React 18** | デファクト、エコシステム豊富 |
| ルーティング | **React Router 6** | SPA 標準 |
| データフェッチ | **TanStack Query 5** | キャッシュ、refetch、loading state を宣言的に処理 |
| 状態管理 | **Zustand** (シンプル global state) + TanStack Query (サーバー状態) | Redux は過剰、Context は性能課題、Zustand が中庸 |
| UI コンポーネント | **shadcn/ui** (Radix + Tailwind ベース) | カスタマイズ容易、デザインシステム束縛なし、軽量 (オープン論点 8 解決) |
| スタイリング | **Tailwind CSS 3** | shadcn/ui と整合、ユーティリティファースト |
| チャート | **Recharts** | React ネイティブ、軽量、宣言的 |
| アイコン | **lucide-react** | shadcn/ui 標準 |
| HTTP クライアント | **fetch (組み込み)** + 薄いラッパー | axios は不要、Vite の TS 環境で十分 |
| 認証検証 | **TanStack Query で auth/me を 30 秒ごと poll** | session expire を能動検知 |

却下した選択肢:
- Material UI / Chakra UI: shadcn/ui よりカスタマイズ柔軟性が低い、bundle size が大きい
- Redux / Redux Toolkit: 本要件 (read-only ダッシュボード) には過剰
- Next.js: SPA で十分、SSR / ISR の利点が活きない

### 5.2 画面一覧と URL 構造

| URL | 画面 | 対応 AC |
|---|---|---|
| `/` | ダッシュボードトップ | AC-2 |
| `/executions` | 実行一覧 | AC-3 |
| `/executions/:execution_id` | 実行詳細 | AC-4 |
| `/quality` | レビュー品質 | AC-5 |
| `/errors` | エラー一覧 | AC-6 |
| `/login` | (auth_login への redirect 専用) | AC-1 |

### 5.3 共通レイアウト

```
┌──────────────────────────────────────────────────────────────┐
│ [Catch-Expander 監視]              user_name [▼ logout]        │  ← Header
├──────────┬──────────────────────────────────────────────────┤
│ Sidebar  │                                                    │
│ - Top    │   <Page content>                                   │
│ - Exec   │                                                    │
│ - Quality│                                                    │
│ - Errors │                                                    │
└──────────┴──────────────────────────────────────────────────┘
```

- Tailwind の grid + sidebar は collapse on mobile (768px 未満)
- shadcn/ui の `Sidebar` コンポーネント or 自前実装

### 5.4 主要画面の実装方針

#### `/executions/:execution_id` (詳細画面、最も複雑)

```
┌──────────────────────────────────────────────────────────────┐
│ ← 一覧へ戻る                                                   │
│                                                                │
│ exec-abc123  [success]  2026-04-30 02:28  duration: 156s       │
│ Topic: "AWS Route 53 の Resolver を Terraform で作って"         │
│ Final: 📝 Notion / 💻 GitHub                                   │
├──────────────────────────────────────────────────────────────┤
│ ▼ Workflow Plan                                                │
│   - planned_subagents: researcher, generator, reviewer         │
│   - topic_category: 技術                                        │
│   - expected_deliverable_type: iac_code                         │
├──────────────────────────────────────────────────────────────┤
│ ▼ Timeline (15 events)                                          │
│   ⏺ 02:28:13 topic_received                                    │
│     └ topic: "..." channel: C12345                             │
│   ⏺ 02:28:14 workflow_planned                                  │
│     └ planned_subagents, planning_summary                      │
│   ⏺ 02:28:14 subagent_started: researcher                      │
│   ⏺ 02:28:25 research_completed                                │
│     └ source_count: 12, sources [▶ 展開]                        │
│   ⏺ 02:28:25 subagent_completed: researcher (11s)              │
│   ⏺ ... (折りたたみ可能)                                        │
├──────────────────────────────────────────────────────────────┤
│ ▼ Review Results                                                │
│   Loop 1: 3 issues (all text-related), fixer_notes: 0          │
│   Loop 2: ✅ passed                                             │
├──────────────────────────────────────────────────────────────┤
│ ▼ Deliverables                                                 │
│   📝 Notion: https://www.notion.so/...                          │
│   💻 GitHub: https://github.com/.../tree/main/...               │
└──────────────────────────────────────────────────────────────┘
```

実装ポイント:
- Timeline は仮想スクロール不要 (max ~30 events)
- 各イベントの payload はクリックで展開 (collapsible)
- JSON は `react-json-view` 等の整形コンポーネント or 自前 `<pre>` ブロック

### 5.5 配置 (オープン論点 6 解決)

```
catch-expander/
├── frontend/                 # 新規
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── tailwind.config.js
│   ├── index.html
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── routes/
│   │   ├── components/
│   │   ├── lib/
│   │   └── api/
│   └── public/
└── ... (既存)
```

ビルド出力 (`frontend/dist/`) を S3 へ sync。

### 5.6 Claude Design 連携ワークフロー

UI 実装は **Claude Design** (2026-04-17 リリース、Anthropic Labs) で生成したモックを **Claude Code に handoff する公式経路** で進める。design.md の文字仕様を要件定義の正準として維持しつつ、UI ビジュアル実装の効率を最大化する。

#### 前提

- Claude Design は Claude Pro / Max / Team / Enterprise プランの研究プレビュー機能 (Claude Opus 4.7 ベース)
- 本タスク着手時点で利用者がいずれかのプランを保有していることを確認 (tasklist の前提条件タスクで担保)
- 公式リリース: <https://www.anthropic.com/news/claude-design-anthropic-labs>
- ヘルプセンター: <https://support.claude.com/en/articles/14604397-set-up-your-design-system-in-claude-design>

#### 全体フロー

```
[1] Claude Design で UI モック生成
       (本 design.md §5.2-5.4 の画面要件をプロンプトとして投入)
       ↓
[2] Catch-Expander のデザインシステム (色/タイポ/コンポーネント) を学習
       (既存コードベースまたは shadcn/ui のデフォルトを起点に)
       ↓
[3] 細部のイテレーション
       (レイアウト調整、画面遷移、状態バリエーション等)
       ↓
[4] handoff バンドルにパッケージ化
       ↓
[5] Claude Code へ単一指示で引き継ぎ
       (`frontend/` 以下の React コンポーネント生成)
       ↓
[6] Claude Code 側で動的部分を補完
       (TanStack Query / Zustand 統合、API client 結線、テスト追加、a11y 補強)
```

#### 各画面の役割分担

| 画面 | Claude Design が担う部分 | Claude Code が補完する部分 |
|---|---|---|
| ダッシュボードトップ | レイアウト / KPI スコアカード / Recharts チャート枠 | API client 結線 / 期間切替 state / refetch |
| 実行一覧 | テーブル UI / 検索バー / ページネーション枠 | クエリパラメータ管理 / TanStack Query / フィルタ動作 |
| 実行詳細 | タイムライン UI / イベントカードレイアウト / JSON 表示エリア | events fetch / 展開 state / JSON 整形 / 共有 URL |
| レビュー品質 | グラフ枠 / フィルタ済み一覧テーブル | 集計クエリ / `code_related_unfixed_count > 0` 絞り込み |
| エラー一覧 | エラーカード / stack trace 折りたたみ UI | error 取得 / 期間絞り込み / グルーピング |

#### 制約と運用ルール

- **静的 UI 重視**: handoff の対象は主にビジュアルレイアウト。動的データ取得・状態管理・アクセシビリティ細部は Claude Code が補完する責務
- **コミット前レビュー必須**: handoff バンドルは `frontend/` にコミットする前に必ず手作業でレビュー。Claude Design は研究プレビュー段階のため生成品質の再現性は検証必要
- **デザインシステムの統一**: 5 画面で色/フォント/コンポーネントが揃うよう、最初に Catch-Expander 用のデザインシステムを Claude Design に確立してから各画面を生成
- **Figma MCP との併用余地**: Claude Code は Figma MCP サーバーとも統合可能。Figma 上の既存資産があれば併用可だが、本タスクでは Claude Design 単独経路を主とする

#### tasklist への反映ポイント (`tasklist.md` で詳細化)

- T0-x: 利用者の Claude プラン (Pro/Max/Team/Enterprise) の確認 + Claude Design へのアクセス確認
- T-frontend-1: Catch-Expander 用デザインシステムを Claude Design で確立
- T-frontend-2 〜 T-frontend-6: 各画面 (5 画面) の UI モック生成 → handoff → Claude Code 実装
- T-frontend-7: 動的部分 (TanStack Query / Zustand / API client) の Claude Code 単独実装
- T-frontend-8: アクセシビリティ補強 / レスポンシブ調整

---

## 6. インフラ (SAM template) 設計

### 6.1 新規 AWS リソース

`template.yaml` に追記する形で以下を定義:

| リソース | タイプ | 役割 |
|---|---|---|
| `DashboardEventsTable` | `AWS::DynamoDB::Table` | events テーブル (PK/SK + 3 GSI + TTL) |
| `DashboardOAuthStateTable` | `AWS::DynamoDB::Table` | OAuth state 一時保存 (PK = state, TTL 10 分) |
| `DashboardBucket` | `AWS::S3::Bucket` | SPA 静的アセット (private) |
| `DashboardOriginAccessControl` | `AWS::CloudFront::OriginAccessControl` | CloudFront → S3 認証 |
| `DashboardDistribution` | `AWS::CloudFront::Distribution` | CDN |
| `DashboardApi` | `AWS::Serverless::HttpApi` | REST API |
| `DashboardAuthorizerFunction` | `AWS::Serverless::Function` | Lambda Authorizer |
| `Dashboard*Function` (×10) | `AWS::Serverless::Function` | 各エンドポイント Lambda |
| `DashboardJwtKeySecret` | `AWS::SecretsManager::Secret` | JWT 署名キー |
| `DashboardSlackOAuthSecret` | `AWS::SecretsManager::Secret` | Slack OAuth client_id/secret/workspace_id |
| `DashboardCloudFrontFunctionRewrite` | `AWS::CloudFront::Function` | SPA 用 history fallback (`/* → /index.html`) |

### 6.2 既存リソースへの IAM 追加

- ECS Task Role: `dynamodb:PutItem` 権限を `DashboardEventsTable` に追加
- Lambda Trigger Role: 同上 (`topic_received` イベント書き込み用)

### 6.3 デプロイフロー (オープン論点 6 解決)

**選定**: **フロントエンドビルドは GitHub Actions の独立ジョブで実施し、S3 sync する**。

理由:
- SAM の `package` ステップで frontend を含めると bucket prefix と build 工程の整合が複雑
- GitHub Actions で `npm ci && npm run build && aws s3 sync frontend/dist/ s3://...` のほうが見通し良い
- フロントエンドのみの変更で SAM スタック更新が不要 (cache invalidation のみ)

新ワークフロー: `.github/workflows/build-frontend.yml`
- トリガー: `push: branches: [main], paths: ["frontend/**"]`
- ステップ: `npm ci` → `npm run build` → `aws s3 sync` → `aws cloudfront create-invalidation`

既存 `.github/workflows/build-agent.yml` (ECR push) と並列。

### 6.4 環境変数 / パラメータ

`template.yaml` の `Parameters` セクションに追加:

```yaml
DashboardDomainName:
  Type: String
  Default: ""
  Description: Custom domain (optional). If empty, uses CloudFront default.
SlackWorkspaceId:
  Type: String
  Description: Slack workspace ID (T...) to allow.
```

各 Lambda の環境変数:
- `EVENTS_TABLE`: `DashboardEventsTable` の Ref
- `JWT_KEY_SECRET_ARN`: `DashboardJwtKeySecret` の ARN
- `SLACK_OAUTH_SECRET_ARN`: `DashboardSlackOAuthSecret` の ARN
- 等

---

## 7. 構造化イベント書き込み設計

### 7.1 共通ヘルパー (`src/observability/event_emitter.py`)

新規モジュール:

```python
import boto3
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(os.environ.get("EVENTS_TABLE", ""))
_TTL_DAYS = 90


class EventEmitter:
    """構造化イベントを events DDB に書き込む best-effort emitter。"""

    def __init__(self, execution_id: str):
        self.execution_id = execution_id
        self._sequence = 0

    def emit(self, event_type: str, payload: dict[str, Any], status_at_emit: str = "in_progress") -> None:
        if not _table.name:
            logger.warning("EVENTS_TABLE env var not set, skipping event %s", event_type)
            return
        self._sequence += 1
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        item = {
            "execution_id": self.execution_id,
            "sk": f"{timestamp}#{self._sequence:05d}",
            "event_type": event_type,
            "timestamp": timestamp,
            "sequence_number": self._sequence,
            "status_at_emit": status_at_emit,
            "payload": payload,
            "ttl": int(time.time()) + _TTL_DAYS * 86400,
            "gsi_pk": "GLOBAL",
        }
        try:
            _table.put_item(Item=item)
        except Exception as e:
            # best-effort: ログのみ、業務継続
            logger.error("Failed to emit event %s: %s", event_type, e)
```

### 7.2 観測ポイントの組み込み (orchestrator)

`src/agent/orchestrator.py` の各観測ポイントに 1 行ずつ追加:

```python
from src.observability.event_emitter import EventEmitter

def _run(execution_id: str, ...):
    emitter = EventEmitter(execution_id)

    # ワークフロー設計後
    emitter.emit("workflow_planned", {
        "topic_category": ...,
        "expected_deliverable_type": ...,
        "planned_subagents": [...],
        "planning_summary": ...,
    })

    # researcher 実行
    emitter.emit("subagent_started", {"subagent": "researcher"})
    # ... (既存処理) ...
    emitter.emit("subagent_completed", {
        "subagent": "researcher",
        "duration_ms": ...,
        "tokens_used": ...,
        "output_summary": ...,
    })

    # researcher 完了後
    emitter.emit("research_completed", {
        "sources": [...],
        "source_count": ...,
        "source_domains": [...],
        "total_tokens_used": ...,
    })

    # 以下、generator / reviewer / 格納処理 / 完了 で同様
```

orchestrator.py への追加は **5〜10 行 × 9 観測ポイント** = +50〜90 行程度。既存ロジックには触れない。

### 7.3 Lambda Trigger 側 (`src/trigger/app.py`)

Slack トピック受領時に `topic_received` イベント書き込み:

```python
from src.observability.event_emitter import EventEmitter

def lambda_handler(event, context):
    # ... 既存処理 ...
    workflow_run_id = ecs_response["tasks"][0]["taskArn"].split("/")[-1]
    execution_id = workflow_run_id  # or 別の id 体系

    emitter = EventEmitter(execution_id)
    emitter.emit("topic_received", {
        "topic": text,
        "user_id_hash": hashlib.sha256(user_id.encode()).hexdigest()[:16],
        "channel_id": channel_id,
        "workflow_run_id": workflow_run_id,
    })
    # ... 既存処理 ...
```

### 7.4 PII 取り扱い (オープン論点 9 解決)

| 情報 | 処理 |
|---|---|
| Slack user ID | **SHA-256 ハッシュ化して `user_id_hash` で保存**。元の user ID は events に保存しない |
| Slack channel ID | そのまま保存 (チャネル ID は機密性低) |
| topic raw text | **そのまま保存**。UI 側で「PII を含む可能性、機密プロジェクトでは注意」の警告バナーを表示 |
| research_results 内の URL | そのまま保存 (公開情報前提) |
| code_files の中身 | events には **保存しない** (DDB 容量・コスト懸念)。詳細は Notion / GitHub URL から参照 |
| エラー stack trace | そのまま保存 |

### 7.5 書き込みパフォーマンス

- DDB の `put_item` は p99 で 10ms 程度
- 1 実行あたり 9〜15 イベント = 90〜150ms 増加
- orchestrator の総処理時間 (~2 分) に対して **+1〜2% 以内** (NFR-1 の +5% 以内に余裕で収まる)

batch_writer による一括書き込みは Phase 2 で検討 (現状の規模では不要)。

---

## 8. オープン論点の解決状況

| # | 論点 | 解決 |
|---|---|---|
| 1 | 認証実装パスとセッション管理 | §4 で解決: Slack OAuth v2 + 自前 JWT in HttpOnly cookie + Lambda Authorizer |
| 2 | フロントエンド技術スタック | §5.1 で解決: Vite + React + TS + Tailwind + shadcn/ui + TanStack Query + Zustand + Recharts |
| 3 | events テーブルのスキーマ | §2 で解決: PK execution_id + SK timestamp#seq + 3 GSI + TTL 90 日 |
| 4 | events 書き込みの実装 | §7 で解決: orchestrator/Lambda Trigger から DDB direct write、best-effort、batch_writer は Phase 2 |
| 5 | API 構成 | §3.2 で解決: API Gateway HTTP API + per-endpoint Lambda |
| 6 | デプロイ運用 | §6.3 で解決: フロントエンドは GitHub Actions 独立ジョブ |
| 7 | モバイル/タブレット | §5.3 で解決: Tailwind grid + sidebar collapse on 768px 未満 |
| 8 | デザインシステム選定 | §5.1 で解決: shadcn/ui (Radix + Tailwind) |
| 9 | PII 取り扱い | §7.4 で解決: user_id ハッシュ化、topic は raw 保持で UI 警告 |
| 10 | コスト見積もり再検証 | §10.2 で再試算 |

---

## 9. 影響範囲分析

### 9.1 新規ファイル / ディレクトリ

```
catch-expander/
├── frontend/                              # 新規 (SPA)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── tailwind.config.js
│   ├── index.html
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── routes/
│   │   │   ├── DashboardHome.tsx
│   │   │   ├── ExecutionList.tsx
│   │   │   ├── ExecutionDetail.tsx
│   │   │   ├── ReviewQuality.tsx
│   │   │   └── ErrorList.tsx
│   │   ├── components/
│   │   │   ├── Layout.tsx
│   │   │   ├── Timeline.tsx
│   │   │   ├── EventCard.tsx
│   │   │   └── ui/  (shadcn/ui コンポーネント)
│   │   ├── api/
│   │   │   └── client.ts
│   │   └── lib/
│   │       └── auth.ts
│   └── public/
├── src/
│   ├── dashboard_api/                     # 新規 (Backend Lambdas)
│   │   ├── auth_login/
│   │   ├── auth_callback/
│   │   ├── auth_logout/
│   │   ├── auth_me/
│   │   ├── authorizer/
│   │   ├── list_executions/
│   │   ├── get_execution/
│   │   ├── get_execution_events/
│   │   ├── get_metrics_summary/
│   │   ├── get_review_quality/
│   │   └── get_errors/
│   └── observability/                     # 新規 (共通ヘルパー)
│       ├── __init__.py
│       └── event_emitter.py
├── tests/
│   ├── unit/
│   │   ├── observability/
│   │   │   └── test_event_emitter.py
│   │   └── dashboard_api/
│   │       ├── test_auth_callback.py
│   │       ├── test_list_executions.py
│   │       └── ...
│   └── integration/                       # 任意 (フロントエンド E2E)
└── .github/workflows/
    └── build-frontend.yml                 # 新規
```

### 9.2 既存ファイルへの追記 (ロジックは変更しない)

| ファイル | 変更内容 |
|---|---|
| `template.yaml` | 新リソース 11 種類 + 既存 IAM ロール拡張 |
| `src/agent/orchestrator.py` | `_emit_event()` 呼び出しを 9 観測ポイントに追加 (~50 行) |
| `src/trigger/app.py` | `topic_received` イベント書き込み (~10 行) |
| `requirements.txt` (各 Lambda) | `pyjwt`, `requests` 等の追加 |
| `docs/architecture.md` | 新節「監視ダッシュボード」追加 |
| `docs/functional-design.md` | 同上 |
| `docs/glossary.md` | 用語追加 (events, JWT, OAuth, SPA, etc.) |
| `docs/repository-structure.md` | `frontend/`, `src/dashboard_api/`, `src/observability/` を追記 |
| `docs/credential-setup.md` | Slack OAuth クライアント作成手順を追記 |

### 9.3 既存ロジックへの侵襲

**侵襲なし**:
- orchestrator のワークフロー判断・サブエージェント呼び出し・レビュー判定・格納処理: 一切変更しない
- Lambda Trigger の Slack 受信・ACK・RunTask: 一切変更しない
- prompts/*.md: 一切変更しない
- 既存テスト: 期待値変更なし、新規ケースのみ追加

**唯一の変更**: 観測ポイントに `_emit_event()` 呼び出しを追加するのみ。失敗時は logging.error のみで業務継続。

### 9.4 過去 5 件 review_loop パッチとの関係

`memory/project_review_loop_recurring_patch_site.md` で「`_run_review_loop` は再発パッチ密集地点」と記録されている。本タスクは:

- `_run_review_loop` の **シグネチャ・ロジック・戻り値** には触れない
- レビュー完了後に `emitter.emit("review_completed", {...code_related_unfixed_count...})` を **1 行追加** するのみ
- 6 件目のパッチには **該当しない** (review_loop のロジック修正ではないため)

case A escalation steering (`obsidian/2026-04-30_case-A-pipeline-steering-draft.md`) のデータソースを自動取得する仕組みとして連動するが、case A 自体の起票は本タスクの完了とは独立。

---

## 10. テスト戦略 / コスト見積もり / 段階展開

### 10.1 テスト戦略

#### バックエンド (Python)

- **新規ユニットテスト**:
  - `event_emitter.py`: emit / TTL 計算 / DDB 書き込み失敗時の logging
  - 各 dashboard_api Lambda: 正常系 / 401 / 404 / クエリパラメータバリデーション
- **モック**: `boto3` の DDB クライアント、Slack API、Secrets Manager
- **回帰テスト**: 既存 `tests/unit/` 全 241 件 pass を維持

#### フロントエンド (TypeScript / React)

- **コンポーネントテスト**: Vitest + React Testing Library
- **対象**: 主要画面 (ExecutionDetail / ExecutionList) と認証 hook
- **E2E**: Phase 2 (Playwright で `/login → /executions → /executions/:id` フロー)

#### 認証フロー

- **手動 E2E**: dev 環境で実際に Slack OAuth を通す確認 (CI 化は Phase 2)

### 10.2 コスト再試算 (オープン論点 10 解決)

ap-northeast-1 リージョン、現状の Catch-Expander 利用実績 (~100 実行/月) ベース:

| サービス | 試算 (月額) | 算定根拠 |
|---|---|---|
| S3 (静的アセット) | $0.10 | 100MB ストレージ + 転送 |
| CloudFront | $1.00 | 数万リクエスト/月 + 1GB 転送 |
| API Gateway HTTP API | $1.00 | 数千リクエスト/月 |
| Lambda (10 個 × ~低頻度) | $0.10 | 無料枠内、ほぼゼロ |
| DynamoDB events (PAY_PER_REQUEST) | $5〜10 | 100 実行 × 15 events × 365 = ~$3、Read は閲覧頻度依存で $2〜7 |
| DynamoDB OAuth state (TTL) | $0.10 | 数百 write/月 |
| Secrets Manager (2 シークレット) | $0.80 | $0.40 × 2 |
| データ転送 (US→JP cross-region 等) | $1〜3 | 想定低 |
| **合計** | **$9〜16** | NFR-2 上限 $30 内に収まる見込み |

検証時期: SAM deploy 後 1 ヶ月で実測値を再評価。$30 を超える場合は DDB アクセスパターン見直し / Lambda メモリチューニング。

### 10.3 段階展開計画 (推奨: 2 PR 構成)

`obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` の連続レビュー知見を活用するため、2 PR に分割:

#### PR1: バックエンド基盤 (events 書き込み + API + 認証)

- 新規ファイル: `src/observability/`, `src/dashboard_api/`, `template.yaml` 拡張
- 既存ファイル変更: `orchestrator.py`, `app.py`, `requirements.txt`, IAM
- frontend は **空のプレースホルダー HTML のみ** (S3 配置 + CloudFront 経路確認用)
- Codex 連続レビュー対象: バックエンドの認証・DDB スキーマ・Lambda 構成

#### PR2: フロントエンド SPA + ドキュメント (Claude Design 連携)

- 新規ファイル: `frontend/` 全体、`build-frontend.yml`
- **UI 実装フロー** (§5.6 参照):
  1. Catch-Expander 用デザインシステムを Claude Design で確立
  2. 5 画面それぞれを Claude Design でモック化 → handoff
  3. Claude Code で `frontend/` 以下のコンポーネント生成
  4. 動的部分 (TanStack Query / Zustand / API client) を Claude Code が補完
  5. アクセシビリティ補強 / レスポンシブ調整
- ドキュメント: `docs/*.md` 更新
- Codex 連続レビュー対象: SPA 認証フロー、データフェッチ、エラーハンドリング、UI アクセシビリティ、Claude Design handoff 後の追加品質チェック

各 PR で Codex 独立レビューを 2〜3 回回し、収束まで継続。Claude Design 出力は handoff 直後とコミット直前の 2 回チェックポイントを設ける。

---

## 11. 残るリスク / 設計上の懸念

### 11.1 Slack OAuth クライアント作成の前提

新規 Slack App を別に作る必要があるか、既存の Catch-Expander Bot に OAuth スコープを追加するかは要確認。`docs/credential-setup.md` 更新時に手順を確定。

→ **対応**: tasklist 段階で Slack admin 権限を持つ利用者と相談して確定。

### 11.2 CloudFront のオリジン保護

API Gateway HTTP API のデフォルトドメインは public。CloudFront 経由のみで応答させる必要がある。

→ **対応**: API GW で `X-Origin-Verify` カスタムヘッダ検証 (CloudFront が付与する secret 値) + WAF Rule で direct アクセス拒否。

### 11.3 DynamoDB hot partition

`gsi_global_timestamp` の PK が単一値 `"GLOBAL"` のため、極端な実行頻度増 (>1000/秒) では hot partition 化リスク。

→ **対応**: 現状規模 (~100/月) では問題なし。スケール時は PK を `bucket_id = hash(execution_id) % 10` にシャーディング。本タスクでは Phase 2 候補として残す。

### 11.4 events 書き込みの順序保証

`sequence_number` は emitter のメモリ上カウンタ。プロセスがクラッシュ → 再起動した場合、順序が破綻する可能性。

→ **対応**: 1 実行は 1 ECS タスク内で完結するため、再起動は execution failure を意味する。`error` イベント書き込みでカバー。順序保証より timestamp 整合性を優先。

### 11.5 既存 workflows / deliverables テーブルとの execution_id 整合

events テーブルの `execution_id` が既存 `workflows.execution_id` と一致するか要検証。

→ **対応**: tasklist の T1 (実装着手前確認) で既存スキーマを確認、`workflow_run_id` ベースで揃える。

### 11.6 フロントエンドビルドの secrets 漏洩

Vite ビルド時に `import.meta.env.VITE_*` で埋め込む値は public。秘密情報を含めない設計が必要。

→ **対応**: 環境変数経由の secret は使わず、すべて API 経由でセッション付き取得。Slack OAuth client_id だけは public でよい (client_secret は Lambda 側のみ)。

---

## 12. 設計確認チェックリスト (実装着手前)

tasklist 起草前に以下を最終確認:

- [ ] Slack OAuth クライアント作成方針 (新規 Slack App or 既存拡張) を決定
- [ ] CloudFront カスタムドメイン (`dashboard.catch-expander.example.com` 等) の必要性を決定。MVP は CloudFront デフォルトドメインで OK
- [ ] events テーブルの GSI 3 つすべてが必要か再評価 (gsi_event_type_timestamp は使用頻度低い場合は省略候補)
- [ ] フロントエンドの `react-json-view` 等のライブラリ最終選定
- [ ] PR1 / PR2 分割の合意 (1 PR で進める案も検討余地あり、独立 LLM レビューの恩恵とのトレードオフ)
- [ ] コスト試算の前提 (実行頻度 100/月) が現実的か再確認
- [ ] **Claude Design へのアクセス確認** (Claude Pro / Max / Team / Enterprise いずれかのプラン保有 + 研究プレビュー機能の利用可否)
- [ ] **Claude Design 利用方針の合意** (§5.6: 5 画面すべてを Claude Design 経由で生成するか、一部のみ Claude Code 単独で実装するかの粒度判断)

これらが確定したら tasklist.md 起草に進む。
