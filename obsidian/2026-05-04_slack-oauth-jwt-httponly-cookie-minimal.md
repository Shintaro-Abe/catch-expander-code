---
title: "Slack OAuth + JWT in HttpOnly Cookie の最小実装"
type: pattern
created: 2026-05-04
updated: 2026-05-04
expires_review: 2026-11-04
confidence: high
tags:
  - auth
  - security
  - slack
  - oauth
  - jwt
  - cookie
  - lambda
  - pattern
aliases:
  - Slack OAuth JWT 認証フロー
  - HttpOnly Cookie 認証パターン
  - Slack OpenID Connect 最小実装
related:
  - "[[2026-04-29_codex-iterative-review-finds-multilayer-misses]]"
---

# Slack OAuth + JWT in HttpOnly Cookie の最小実装

## TL;DR

- Slack OAuth 2.0 (OpenID Connect) を使い、JWT を **HttpOnly / Secure / SameSite=Lax cookie** として発行する最小認証フロー。
- CSRF は **state パラメータ + DynamoDB TTL 10 分 + 照合後即削除（単一回限り）+ IP/UA フィンガープリントバインド** の 4 層で防止。
- SPA は JWT を直接読めない（HttpOnly）→ **`/api/v1/auth/me` を 30 秒ポーリング** してユーザー情報と有効期限を監視。
- Lambda Authorizer の **5 分キャッシュ** は SPA の 30 秒ポーリングより長い。ローテーション直後に最大 5 分の過渡期が生じる点を設計上織り込む。
- **workspace_id 検証を必ず行う**。忘れると Slack アプリを知る人なら誰でもログインできてしまう。

---

## 何を解決するか / Problem Addressed

Slack ワークスペースを Identity Provider として使い、外部に認証 DB を持たない構成で SPA をセキュアに保護したい。具体的に解決する問題:

- パスワード管理・ユーザー登録フローを持ちたくない。
- JWT を localStorage に置くと XSS で盗まれる → HttpOnly cookie に封じ込めたい。
- CSRF・replay attack・OIDC state フィンガープリントの誤用を避けたい。
- サーバレス（API Gateway + Lambda）構成で最小実装にしたい。

---

## 前提・適用範囲 / Applicability

### 適用が効く条件

- 対象ユーザーが **特定の Slack ワークスペースに所属していること** を前提にできる（社内ツール・チーム向け SaaS など）。
- フロントエンドは SPA（React 等）で、API は API Gateway + Lambda。
- Cookie を送受信できる同一ドメイン or CORS 設定済みのクロスドメイン構成。
- **個人利用・少人数チーム前提**: IP/UA バインドによるモバイル誤検知を許容できる。

### 適用が弱い・不要な条件

- ユーザーが複数の Slack ワークスペースに跨がる場合（workspace_id 検証を複数 ID リスト化する拡張が必要）。
- モバイルネイティブアプリ（cookie ハンドリングが複雑）。
- IP/UA 切り替えが頻繁な環境（モバイルの Wi-Fi ↔ 4G 切り替えで誤検知率が上がる）。

---

## 実装 / Implementation

### フロー概要

```
ユーザー
  │  GET /api/v1/auth/login
  ▼
auth_login Lambda
  ├─ state（CSRF トークン）生成
  ├─ DynamoDB に state を TTL 10 分で保存（IP・UA も記録）
  └─ Slack OAuth URL へ 302 リダイレクト
         (scope: openid profile email)

Slack 認証画面
  │  コールバック
  ▼
auth_callback Lambda
  ├─ state 検証（DDB に存在するか）
  ├─ IP・UA フィンガープリント一致確認
  ├─ 照合後に DDB から state を即削除（単一回限り）
  ├─ Slack から access_token 取得 → userinfo エンドポイント呼び出し
  │     → sub（user_id）/ name / email を取得
  ├─ workspace_id 検証（許可ワークスペースのみ通過）
  ├─ JWT を HS256 で署名（鍵は Secrets Manager から取得）
  ├─ Set-Cookie: jwt=...; HttpOnly; Secure; SameSite=Lax; Path=/
  └─ SPA へ 302 リダイレクト

SPA（ブラウザ）
  ├─ /api/v1/auth/me を 30 秒ポーリング
  │     → 200: ユーザー情報表示
  │     → 401: ログイン画面へリダイレクト
  └─ ログアウト: DELETE /api/v1/auth/logout
         → cookie を Max-Age=0 で上書き削除

API Gateway（全保護エンドポイント）
  └─ Lambda Authorizer
       ├─ cookie から JWT を取り出し HS256 で検証
       ├─ 5 分キャッシュ（API Gateway レベル）
       └─ 通過 / 拒否
```

### 実装ファイル

| ファイル | 役割 |
|---|---|
| `src/dashboard_api/auth_login/app.py` | state 生成・DDB 保存・Slack OAuth URL 生成・リダイレクト |
| `src/dashboard_api/auth_callback/app.py` | state 検証・userinfo 取得・JWT 発行・cookie セット |
| `src/dashboard_api/auth_logout/app.py` | cookie を Max-Age=0 で削除 |
| `src/dashboard_api/auth_me/app.py` | cookie の JWT を検証してユーザー情報を返す（SPA ポーリング先） |
| `src/dashboard_api/authorizer/app.py` | Lambda Authorizer（JWT 検証・IAM policy 生成） |

### state テーブルのスキーマ例

```python
{
    "state": "<UUID>",          # PK
    "ip": "1.2.3.4",
    "user_agent": "Mozilla/...",
    "ttl": 1234567890           # Unix timestamp（DDB TTL 属性）
}
```

### JWT payload 例

```json
{
  "sub": "U01XXXXXXX",
  "name": "Taro Yamada",
  "email": "taro@example.com",
  "workspace_id": "T01XXXXXXX",
  "iat": 1746316800,
  "exp": 1746403200
}
```

### Lambda Authorizer のキャッシュ設定

API Gateway コンソールまたは SAM/CDK で `AuthorizerResultTtlInSeconds: 300`（5 分）を設定することで、検証済み JWT のポリシーをキャッシュする。コールド検証コストを削減できる。

---

## 落とし穴 / Caveats

### 1. state の単一回限り削除を忘れると replay attack が成立する

`auth_callback` で state を DDB から **照合直後に削除** する。削除しないと、同一 state を使い回した 2 回目以降のコールバックリクエストを受け付けてしまう。TTL による自然消滅だけでは不十分。

```python
# 照合後に即削除する例
table.delete_item(Key={"state": state_param})
```

### 2. Lambda Authorizer の 5 分キャッシュは SPA の 30 秒ポーリングより長い

SPA が `/auth/me` を 30 秒ごとにポーリングして 401 を受け取っても、API Gateway の Authorizer キャッシュに古い「許可」ポリシーが残っていると、保護エンドポイントへのリクエストは最大 5 分通り続ける。

対策:
- **セキュリティ要件が厳しい場合**はキャッシュを短くするか無効化する（コスト増）。
- **ログアウト時**はキャッシュ無効化 API を呼ぶか、JWT のブラックリストを持つ（複雑化）。
- **個人利用・チーム内ツール前提**ならこの 5 分の窓を許容するのが最小実装として合理的。

### 3. IP/UA バインドはモバイル環境で誤検知する可能性がある

Wi-Fi ↔ 4G 切り替えで IP が変わると、callback の IP/UA 検証が失敗してログインが弾かれる。個人利用・PC 前提なら受容できるが、モバイルユーザーを含む場合は IP バインドを外すか、UA のみに緩和する。

### 4. workspace_id 検証を忘れると誰でもログインできる

Slack の `/openid.connect.token` は Slack アプリを知っている人なら誰でも呼べる（別ワークスペースのユーザーでも）。userinfo の `https://slack.com/team_id`（= workspace_id）を検証して許可リストと照合する。

```python
ALLOWED_WORKSPACE_IDS = os.environ["ALLOWED_WORKSPACE_IDS"].split(",")
if team_id not in ALLOWED_WORKSPACE_IDS:
    return {"statusCode": 403, "body": "Unauthorized workspace"}
```

### 5. SPA は HttpOnly cookie を JS から読めない

JWT は HttpOnly なので `document.cookie` でアクセスできない。SPA がユーザー情報を必要とする場合は **必ず `/auth/me` API 経由** で取得する設計にする。JWT をデコードして情報を取ろうとする実装は HttpOnly では不可能。

### 6. Secrets Manager からの鍵取得はコールドスタート時にレイテンシが発生する

JWT 署名鍵を Lambda の起動ごとに Secrets Manager から取得するとコールドスタートが遅くなる。**モジュールスコープでキャッシュ**（Lambda コンテナ再利用時に再取得しない）するのが標準的な緩和策。ただし鍵ローテーション時はコンテナが入れ替わるまで古い鍵が使われ続けるため、ローテーション頻度と照らして設計する。

---

## 関連ナレッジ / Related

- [[2026-04-29_codex-iterative-review-finds-multilayer-misses]] — 独立 LLM レビュー（Codex）を連続で回すパターン。本認証実装もレビュー対象として活用。
- [[feedback_anti_pattern_discipline]] — 再発バグエリアでの 3 層代替案規律（プロンプト / パイプライン / 型）。

---

## 参考文献 / References

- Slack 公式ドキュメント — Sign in with Slack (OpenID Connect): <https://api.slack.com/authentication/sign-in-with-slack>
- Slack 公式ドキュメント — OpenID Connect userinfo endpoint: <https://api.slack.com/methods/openid.connect.userinfo>
- RFC 7519 — JSON Web Token (JWT): <https://www.rfc-editor.org/rfc/rfc7519>
- RFC 6749 — The OAuth 2.0 Authorization Framework (state パラメータの仕様): <https://www.rfc-editor.org/rfc/rfc6749#section-4.1.1>
- OWASP — Cross-Site Request Forgery (CSRF) Prevention Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html>
- AWS 公式ドキュメント — API Gateway Lambda Authorizer のキャッシュ: <https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-use-lambda-authorizer.html>
