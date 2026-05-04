---
title: "CloudFront+S3(OAC)のSPA 403とsam deploy --template-fileによる依存欠落が連鎖した障害の診断と解決"
type: troubleshooting
created: 2026-05-04
updated: 2026-05-04
expires_review: 2026-11-04
confidence: medium
tags:
  - infra
  - troubleshooting
  - aws
  - cloudfront
  - sam
  - s3
  - lambda
aliases:
  - SPA ルートリフレッシュ 403
  - sam deploy template-file 依存欠落
  - CloudFront Function SPA ルーティング
---

# CloudFront+S3(OAC)のSPA 403とsam deploy --template-fileによる依存欠落が連鎖した障害の診断と解決

## TL;DR

S3 プライベートバケット（OAC 構成）は存在しないオブジェクトへのアクセスに **404 ではなく 403** を返す。SPA のディープリンクが `<AccessDenied>` になる原因はこれであり、`CustomErrorResponses` に 404 しか設定していないと修正が効かない。
解決策は CloudFront Function（viewer-request）で S3 にリクエストが届く前に `/index.html` へリライトすること。
そのデプロイに `sam deploy --template-file` を使うと `.aws-sam/build/` をバイパスして pip install が実行されず、Lambda の依存パッケージが ZIP に含まれない二次障害が発生する。両障害が連鎖するとブラウザに黒画面しか表示されず、層の特定に時間がかかる。

---

## 症状 / Symptoms

### 症状 A: SPA ディープリンクが `<AccessDenied>` XML を返す

```
# ルートは正常
GET https://example.cloudfront.net/
→ HTTP 200, React SPA が表示される

# ディープリンクまたはリロードで失敗
GET https://example.cloudfront.net/dashboard
→ HTTP 403, レスポンスボディ:
  <?xml version="1.0" encoding="UTF-8"?>
  <Error><Code>AccessDenied</Code><Message>Access Denied</Message>...</Error>
```

### 症状 B: Lambda が依存パッケージ欠落で 500 エラー

```
Runtime.ImportModuleError: No module named 'xxx'
```

CloudWatch Logs に記録。ローカルでは再現しない。直前に `sam deploy --template-file template.yaml --force-upload` を実行していた。

### 複合障害の見た目

上記 A と B が同時に発生すると、ブラウザには黒画面しか表示されない。

- CloudFront は HTTP 200 を返す（症状 A が CloudFront Function で修正済みの場合）
- React アプリが `/api/v1/auth/me` を呼ぶと Lambda が 500 を返す
- React アプリが認証失敗として `return null`（何もレンダリングしない）
- 結果: 黒画面。ChromeDevTools の Network タブを開かないと原因層が見えない

---

## 再現条件 / Reproduction

### 症状 A の再現条件

| 項目 | 条件 |
|------|------|
| S3 バケット | プライベートバケット（ACL 無効）、静的ウェブサイトホスティング無効 |
| CloudFront オリジン | OAC（Origin Access Control）経由で S3 に接続 |
| CloudFront エラーページ設定 | `CustomErrorResponses` に `ErrorCode: 404` → `/index.html` のみ設定（403 は未設定） |
| リクエスト | `https://example.cloudfront.net/some-path`（`some-path` というオブジェクトが S3 に存在しない） |

### 症状 B の再現条件

| 項目 | 条件 |
|------|------|
| デプロイコマンド | `sam deploy --template-file template.yaml --force-upload` |
| Lambda 関数 | `requirements.txt` に外部パッケージを記述（例: `PyJWT`） |
| ビルド | `sam build` を**実行していない**、または `.aws-sam/build/` が古い状態 |

---

## 影響範囲 / Impact

- **症状 A**: SPA の全ディープリンクおよびページリロードが完全に不可。ルートパス（`/`）のみ動作。
- **症状 B**: Lambda Authorizer を含む全 Lambda 関数が起動時にクラッシュ。認証が必要な全 API が 500。
- **複合**: フロントエンドの動作確認が完全にできない状態になる。

---

## 調査の流れ / Investigation

### 層ごとの切り分け手順

複合障害では層ごとに curl で確認する。ブラウザの黒画面は情報量がゼロに等しいため、CLI から始める。

```bash
# ステップ 1: CloudFront/S3 層の確認
curl -sI https://example.cloudfront.net/
# → HTTP 200 なら CloudFront と S3 はルートを提供できている

curl -sI https://example.cloudfront.net/dashboard
# → HTTP 403 なら S3 層の問題（症状 A の確認）
# → HTTP 200 なら CloudFront Function が効いている

# ステップ 2: API 層の確認
curl -s -o /dev/null -w "%{http_code}" https://example.cloudfront.net/api/v1/auth/me
# → 500 なら Lambda 層の問題（症状 B の確認）
# → 401/403 なら認証設定の問題（Lambda は起動している）

# ステップ 3: Lambda ログで根本原因を確認
aws logs tail /aws/lambda/function-name --since 30m
# → "No module named 'xxx'" なら症状 B（依存パッケージ欠落）
```

### 今回の調査経緯（時系列）

1. SPA ルーティング修正のため CloudFront Function を追加し、`sam deploy --template-file template.yaml --force-upload` でデプロイ
2. `curl -sI https://example.cloudfront.net/dashboard` が HTTP 200 を返すことを確認 → 症状 A は解消
3. ブラウザで確認すると黒画面
4. ChromeDevTools → Network タブ → `/api/v1/auth/me` が 500 を返していることを発見
5. CloudWatch Logs を確認 → `Runtime.ImportModuleError: No module named 'PyJWT'`
6. `sam build && sam deploy` を再実行 → 解消

**仮説と棄却の記録**:
- 仮説「CloudFront Function の実装ミス」→ curl で HTTP 200 確認により棄却
- 仮説「React の状態管理バグ」→ Network タブで 500 を確認により棄却
- 仮説「Lambda の IAM 権限不足」→ ログのエラーメッセージが `ImportModuleError` であることにより棄却

---

## 根本原因 / Root Cause

### 根本原因 A: S3 OAC 構成は存在しないオブジェクトに 403 を返す

AWS 公式ドキュメント「HTTP 403 status code (Permission Denied) - Amazon CloudFront」に明記：

> Amazon S3 origin returns a 403 error — The requested object doesn't exist.

S3 をプライベートバケット（OAC 構成）として使う場合、存在しないオブジェクトへのリクエストは **403 AccessDenied** を返す。パブリックバケットや静的ウェブサイトホスティングモードは 404 を返す。

CloudFront の `CustomErrorResponses` に `ErrorCode: 404` しか設定していない場合、S3 が返す 403 はそのままクライアントに転送されるため、SPA のディープリンクが `<AccessDenied>` XML になる。

**なぜ `CustomErrorResponses` への 403 追加で解決しなかったか**（今回採用しなかった理由）:

`CustomErrorResponses` はパス問わず全ての 403 を対象にする。AWS CloudFront API リファレンスによると `CustomErrorResponse` のフィールドは `ErrorCode`・`ErrorCachingMinTTL`・`ResponseCode`・`ResponsePagePath` のみで、パス条件の指定はできない（参照: [CustomErrorResponse - Amazon CloudFront API Reference](https://docs.aws.amazon.com/cloudfront/latest/APIReference/API_CustomErrorResponse.html)、参照日: 2026-05-04）。

そのため、API Gateway の Lambda Authorizer が返す 403（認証失敗の正当なレスポンス）も `index.html` に書き換えてしまい、API クライアントが認証エラーを検知できなくなる。パス別の条件分岐は CloudFront Function または Lambda@Edge が必要。

### 根本原因 B: `sam deploy --template-file` はビルドアーティファクトをバイパスする

AWS SAM CLI 公式リファレンス「sam deploy」に明記：

> `--template-file, --template, -t PATH` — If you specify this option, then AWS SAM deploys only the template and the local resources that it points to.

`sam deploy`（引数なし）は `.aws-sam/build/` 配下のビルド済みアーティファクト（`sam build` により pip install 済みの ZIP）を使う。

`--template-file template.yaml` を指定すると、`.aws-sam/build/` ではなくソースディレクトリから直接 ZIP を生成する。このとき pip install は実行されないため、`requirements.txt` に記載した外部パッケージが ZIP に含まれない。

---

## 解決策 / Solution

### 解決策 A: CloudFront Function（viewer-request）で SPA ルートをリライト

S3 にリクエストが届く前に、拡張子なし・`/api/*` 以外のパスを `/index.html` に書き換える。これにより S3 には常に存在するオブジェクト（`index.html`）へのリクエストが届く。

**CloudFront Function のコード（未検証スニペット。本番適用前にテストを実施すること）**:

```javascript
// Runtime: cloudfront-js-2.0
function handler(event) {
  var uri = event.request.uri;
  // /api/ 配下と拡張子あり（静的アセット）はそのまま通す
  if (!uri.startsWith('/api/') && !uri.match(/\.[a-zA-Z0-9]+$/)) {
    event.request.uri = '/index.html';
  }
  return event.request;
}
```

**CloudFormation / SAM テンプレートでの定義**:

```yaml
SpaRoutingFunction:
  Type: AWS::CloudFront::Function
  Properties:
    Name: my-spa-routing
    AutoPublish: true
    FunctionConfig:
      Comment: Rewrite SPA routes to index.html
      Runtime: cloudfront-js-2.0
    FunctionCode: |
      function handler(event) {
        var uri = event.request.uri;
        if (!uri.startsWith('/api/') && !uri.match(/\.[a-zA-Z0-9]+$/)) {
          event.request.uri = '/index.html';
        }
        return event.request;
      }
```

`DefaultCacheBehavior` への関連付け:

```yaml
FunctionAssociations:
  - EventType: viewer-request
    FunctionARN: !GetAtt SpaRoutingFunction.FunctionARN
```

**動作確認方法**:

```bash
# リロード後 HTTP 200 になることを確認
curl -sI https://example.cloudfront.net/dashboard
# → HTTP/2 200

# 静的アセットへのアクセスが引き続き動作することを確認
curl -sI https://example.cloudfront.net/assets/main.js
# → HTTP/2 200

# API へのリクエストがリライトされないことを確認
curl -s -o /dev/null -w "%{http_code}" https://example.cloudfront.net/api/v1/auth/me
# → 401 または 403（Lambda が正常起動しているなら）。200 になってはいけない
```

### 解決策 B: `sam build && sam deploy` を常に使う

```bash
# 正しいデプロイ手順
sam build && sam deploy
```

`--template-file` は「`sam build` を経由しない CloudFormation テンプレートをそのままデプロイしたい場合」（例: 純 CloudFormation リソースのみでコンパイルが不要なテンプレート）にのみ使用する。Lambda 関数に `requirements.txt` が存在する場合は使ってはならない。

---

## 恒久対策 / Prevention

### 症状 A の再発防止

- CloudFront + S3(OAC) 構成で SPA を配信する場合は、初期セットアップ時に viewer-request CloudFront Function を組み込む。`CustomErrorResponses` の 403 追加のみで解決しようとするパターンは回避する。
- アーキテクチャレビューのチェック項目として「プライベート S3 + OAC 構成では 404 ではなく 403 が返る」を追記する。

### 症状 B の再発防止

- CI/CD パイプラインで `sam deploy` 前に `sam build` を必ず実行するよう強制する。
- チームの開発ガイドラインに「`--template-file` フラグは Lambda 依存パッケージがない純 CloudFormation 構成専用」と明記する。
- `sam build` が必須のリポジトリでは `Makefile` や npm scripts に `deploy` ターゲットを定義して `sam deploy` を直接叩かせない運用を検討する。

---

## 関連ナレッジ / Related

- [[2026-04-04_agent-runtime-selection-claude-code-cli-on-ecs]] — エージェント実行基盤の技術選定（ECS + Claude Code CLI + Max プラン）

---

## 参考文献 / References

- [HTTP 403 status code (Permission Denied) - Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/http-403-permission-denied.html)（参照日: 2026-05-04）
- [CustomErrorResponse - Amazon CloudFront API Reference](https://docs.aws.amazon.com/cloudfront/latest/APIReference/API_CustomErrorResponse.html)（参照日: 2026-05-04）
- [Restrict access to an Amazon S3 origin - Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html)（参照日: 2026-05-04）
- [sam deploy - AWS Serverless Application Model](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-cli-command-reference-sam-deploy.html)（参照日: 2026-05-04）
- [Introduction to deploying with AWS SAM - AWS Serverless Application Model](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/using-sam-cli-deploy.html)（参照日: 2026-05-04）
- [CloudFront Functions event structure - Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/functions-event-structure.html)（参照日: 2026-05-04）
- [JavaScript runtime 2.0 features for CloudFront Functions - Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/functions-javascript-runtime-20.html)（参照日: 2026-05-04）
- [Add index.html to request URLs without a file name in a CloudFront Functions viewer request event - Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/example_cloudfront_functions_url_rewrite_single_page_apps_section.html)（参照日: 2026-05-04）
