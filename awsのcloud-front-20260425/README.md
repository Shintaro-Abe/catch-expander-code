## IaCコード（Terraform または CloudFormation）

# CloudFront + API Gateway + Lambda — Terraform PoC

> **PoC品質**: 動作確認・学習用スケルトンです。本番利用前にセキュリティレビューと設定調整を行ってください。

## アーキテクチャ

```
Internet
  │
  ▼
CloudFront Distribution
  ├── WAF WebACL (OWASP・IP評価・レートリミット)
  ├── CloudFront Function (セキュリティヘッダー)
  │
  ├── デフォルト (*):  S3バケット (OAC)       ← 静的SPA / ファイル配信
  └── /api/*:          API Gateway (REGIONAL)  ← Lambda プロキシ統合
                           └── Lambda Function (Python 3.12)
```

## ファイル構成

| ファイル | 内容 |
|---|---|
| `main.tf` | 全リソース定義（S3・Lambda・API Gateway・CloudFront・WAF） |
| `variables.tf` | 入力変数（プロジェクト名・環境・地理制限 等） |
| `outputs.tf` | 出力値（URL・ID・運用コマンド例） |
| `security_headers.js` | CloudFront Function: viewer-responseでセキュリティヘッダー付与 |

## 主要な設計ポイント

### Lambda + API Gateway 統合
- API GatewayはREGIONALエンドポイントを使用（CloudFront経由時はEdge-Optimizedを避ける）
- `/{proxy+}` + ルート `/` の両方にLambda AWS_PROXYを設定
- `integration_http_method = "POST"` はAWS_PROXY統合の仕様（LambdaはHTTPメソッドを問わずPOSTで受信）

### CloudFront → API Gateway のHostヘッダー問題
CloudFrontがCloudFrontドメインの`Host`ヘッダーをそのままオリジンに転送すると、  
API Gatewayは自身のドメインを期待しているため **403 Forbidden** を返す。

**対策**: `AllViewerExceptHostHeader` マネージドポリシー (ID: `b689b0a8-53d0-40ab-baf2-68738e2966ac`) を使用。  
Host以外の全ヘッダー・クエリ・Cookieを転送し、CloudFrontがオリジンドメインをHostに自動設定する。

### OAC (Origin Access Control)
S3バケットへのアクセスをこのCloudFrontディストリビューションのみに限定。  
SigV4署名による短期クレデンシャルで、Confused Deputy攻撃への耐性を確保。

### パス・ステージのマッピング
```
CloudFront /api/items  →  origin_path=/{stage}  →  API Gateway /{stage}/api/items
                                                       ↓
                                                  Lambda: path="/api/items"
```

### WAF (us-east-1)
CloudFront用WAFはus-east-1にのみデプロイ可能。  
`provider = aws.us_east_1` エイリアスで対応。

## デプロイ手順

```bash
# 1. 初期化
terraform init

# 2. 変数ファイルを作成（シークレットはコミットしない）
cat > terraform.tfvars <<EOF
project_name         = "myapp"
environment          = "prod"
aws_region           = "ap-northeast-1"
price_class          = "PriceClass_All"
origin_verify_secret = "$(openssl rand -hex 32)"
EOF

# 3. プラン確認
terraform plan -var-file=terraform.tfvars

# 4. デプロイ（約5〜10分）
terraform apply -var-file=terraform.tfvars

# 5. 静的ファイルをアップロード
aws s3 sync ./dist s3://$(terraform output -raw s3_bucket_name)/ --delete

# 6. CloudFrontキャッシュを無効化（静的ファイル更新後）
aws cloudfront create-invalidation \
  --distribution-id $(terraform output -raw cloudfront_distribution_id) \
  --paths '/*'
```

## 本番化チェックリスト

- [ ] ACM証明書 (us-east-1) を取得してカスタムドメインに変更
- [ ] `origin_verify_secret` を Secrets Manager で管理・定期ローテーション
- [ ] API GatewayにリソースポリシーでX-Origin-Verifyヘッダー検証を追加
- [ ] `security_headers.js` のCSPポリシーをアプリ要件に合わせて調整
- [ ] Lambda関数に実際の業務ロジックを実装
- [ ] CloudFrontのアクセスログを有効化（S3バケットに出力）
- [ ] WAFのサンプリングログをKinesis Firehoseで収集
- [ ] `price_class` をユーザー分布に合わせて最適化
- [ ] API Gatewayのデフォルトエンドポイントを無効化 (`disableExecuteApiEndpoint`)
- [ ] Origin Shield の導入を検討（グローバルトラフィックが多い場合）


## プログラムコード（Python またはユーザープロファイルの技術スタック）

# CloudFront + API Gateway + Lambda — CDK PoC

> **PoC品質**: 動作確認用のスケルトン実装です。本番利用前にセキュリティレビューと適切な設定変更を行ってください。

## 構成

```
Internet
  │  HTTPS only
  ▼
CloudFront Distribution  (Price Class 200 / HTTP2+3 / IPv6)
  │  CloudFront Function: セキュリティヘッダー付与 (Viewer Response)
  ├─ デフォルト /*   → S3 Bucket (OAC)          静的ファイル / SPA
  └─ /api/*         → API Gateway REST (Regional) → Lambda (Python 3.12)
```

## ファイル構成

| ファイル | 説明 |
|---|---|
| `app.py` | CDK アプリエントリポイント |
| `cloudfront_stack.py` | メインスタック (CloudFront / S3 / API GW / Lambda / CF Functions) |
| `lambda/index.py` | Lambda ハンドラー (REST CRUD スケルトン) |
| `requirements.txt` | Python 依存パッケージ |

## セットアップ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap aws://<ACCOUNT>/<REGION>
cdk deploy -c account=<ACCOUNT> -c region=ap-northeast-1
```

## 主要な設計ポイント

### CloudFront ↔ API Gateway の Host ヘッダー問題
`origins.RestApiOrigin` は `OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER` を自動適用します。
手動で `HttpOrigin` を使う場合は明示的に同ポリシーを指定してください（`AllViewer` だと API GW が 403 を返します）。

### S3 OAC (Origin Access Control)
`S3BucketOrigin.with_origin_access_control()` で OAC を自動設定します。
バケットはパブリックアクセスをすべてブロックし、CloudFront ディストリビューション ARN を条件にした S3 バケットポリシーのみでアクセスを許可します。

### キャッシュ戦略
- 静的アセット: `Default TTL = 7日 / Max TTL = 365日`。ファイル名にハッシュを付与して無効化コストをゼロに。
- API エンドポイント (`/api/*`): `CACHING_DISABLED` で常にオリジンに転送。

### セキュリティヘッダー
CloudFront Functions (JS 2.0 Runtime) を Viewer Response に紐づけ、`Strict-Transport-Security` / `X-Frame-Options` 等を全レスポンスに付与します。

## 本番化チェックリスト

- [ ] `disable_execute_api_endpoint=True` で execute-api URL への直接アクセスを無効化
- [ ] ACM 証明書 (us-east-1) とカスタムドメインの設定
- [ ] WAF WebACL の関連付け (OWASP Managed Rules + レートリミット)
- [ ] `removal_policy=RemovalPolicy.RETAIN` に変更 (S3 バケット)
- [ ] Lambda 環境変数のシークレットを Secrets Manager / Parameter Store に移行
- [ ] CORS の `allow_origins` を CloudFront ドメインに限定
- [ ] CloudWatch アラーム: `5XXErrorRate`, `CacheHitRate` の監視設定


---

📝 [Notionで詳細を見る](https://www.notion.so/AWS-Cloud-Front-34d47b55202e81db84b7d0eb3134fca2)
