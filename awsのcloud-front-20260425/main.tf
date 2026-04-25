# PoC品質: 本番利用前にセキュリティレビュー・設定調整が必要
# 構成: S3(静的) + Lambda + API Gateway(REST) + CloudFront + WAF
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# CloudFront用WAF WebACLはus-east-1で作成する必要がある
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

# ==========================================================
# S3 バケット（静的コンテンツ）
# ==========================================================

resource "aws_s3_bucket" "static" {
  bucket = "${var.project_name}-static-${var.environment}"

  tags = local.common_tags
}

resource "aws_s3_bucket_public_access_block" "static" {
  bucket                  = aws_s3_bucket.static.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "static" {
  bucket = aws_s3_bucket.static.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "static" {
  bucket = aws_s3_bucket.static.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# OAC: CloudFrontディストリビューション作成後にバケットポリシーを設定する
# （aws_cloudfront_distribution.main.arn が必要なため循環参照にならないよう分離）
resource "aws_s3_bucket_policy" "static" {
  bucket = aws_s3_bucket.static.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.static.arn}/*"
      Condition = {
        StringEquals = {
          # 特定ディストリビューションのみを許可（Confused Deputy攻撃対策）
          "AWS:SourceArn" = aws_cloudfront_distribution.main.arn
        }
      }
    }]
  })
}

# ==========================================================
# OAC（Origin Access Control）
# OAI(旧)の後継: SigV4署名・KMS暗号化バケット対応
# ==========================================================

resource "aws_cloudfront_origin_access_control" "static" {
  name                              = "${var.project_name}-oac-${var.environment}"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# ==========================================================
# Lambda 関数（API バックエンド）
# archive_fileでインラインPythonコードをZIP化
# ==========================================================

data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/lambda_handler.zip"

  source {
    # PoC用スケルトン: 実際の業務ロジックはここに実装する
    content  = <<-PYTHON
      import json
      import os

      ENVIRONMENT = os.environ.get("ENVIRONMENT", "unknown")

      def handler(event, context):
          method  = event.get("httpMethod", "GET")
          path    = event.get("path", "/")
          headers = event.get("headers") or {}
          params  = event.get("queryStringParameters") or {}

          body = {
              "message": "Hello from Lambda",
              "method":  method,
              "path":    path,
              "env":     ENVIRONMENT,
              "params":  params,
          }

          return {
              "statusCode": 200,
              "headers": {
                  "Content-Type":  "application/json",
                  "Cache-Control": "no-store, no-cache",
              },
              "body": json.dumps(body),
          }
    PYTHON
    filename = "index.py"
  }
}

resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-lambda-exec-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.project_name}-api-${var.environment}"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  role             = aws_iam_role.lambda.arn
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      ENVIRONMENT = var.environment
    }
  }

  tags = local.common_tags
}

# ==========================================================
# API Gateway REST API（REGIONAL）
# CloudFront経由の場合はREGIONALエンドポイントが必須
# Edge-Optimizedエンドポイントとの二重CloudFrontを避けるため
# ==========================================================

resource "aws_api_gateway_rest_api" "main" {
  name = "${var.project_name}-api-${var.environment}"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = local.common_tags
}

# /{proxy+} でLambdaプロキシ統合: すべてのパスを1つのLambdaで処理
resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "proxy_any" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "proxy_lambda" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = aws_api_gateway_resource.proxy.id
  http_method = aws_api_gateway_method.proxy_any.http_method

  # Lambda統合のintegration_http_methodは常にPOST（AWS_PROXYの仕様）
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

# ルートパス "/" の設定（/{proxy+} はルートにマッチしないため別途必要）
resource "aws_api_gateway_method" "root_any" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_rest_api.main.root_resource_id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "root_lambda" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_rest_api.main.root_resource_id
  http_method             = aws_api_gateway_method.root_any.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.main.id

  # 統合設定が完了してからデプロイ
  depends_on = [
    aws_api_gateway_integration.proxy_lambda,
    aws_api_gateway_integration.root_lambda,
  ]

  # メソッド変更時に新しいデプロイメントを作成
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "main" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  deployment_id = aws_api_gateway_deployment.main.id
  stage_name    = var.environment

  tags = local.common_tags
}

# API GatewayからLambdaを呼び出す権限
# source_arn でAPIとメソッドを限定してLeast Privilege原則を適用
resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# ==========================================================
# WAF WebACL（us-east-1 必須）
# CloudFront用WAFはus-east-1でのみ作成可能
# ==========================================================

resource "aws_wafv2_web_acl" "main" {
  provider    = aws.us_east_1
  name        = "${var.project_name}-waf-${var.environment}"
  description = "WAF for CloudFront - ${var.project_name}"
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  # OWASP Top10対策マネージドルール（SQLi, XSS等）
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-common-rules"
      sampled_requests_enabled   = true
    }
  }

  # 既知の悪意あるIPブロック
  rule {
    name     = "AWSManagedRulesAmazonIpReputationList"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  # IPあたりのレートリミット（5分間で2000リクエスト）
  rule {
    name     = "RateLimitPerIP"
    priority = 3

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-waf"
    sampled_requests_enabled   = true
  }

  tags = local.common_tags
}

# ==========================================================
# CloudFront Function（セキュリティヘッダー付与）
# viewer-responseで実行: 全レスポンスにセキュリティヘッダーを追加
# Lambda@Edgeより1/6のコストで高スループット処理
# ==========================================================

resource "aws_cloudfront_function" "security_headers" {
  name    = "${var.project_name}-security-headers"
  runtime = "cloudfront-js-2.0"
  comment = "Viewer Response: HSTS・CSP等のセキュリティヘッダーを付与"
  publish = true
  code    = file("${path.module}/security_headers.js")
}

# ==========================================================
# キャッシュポリシー
# ==========================================================

# 静的アセット用: 長期キャッシュ + Brotli/GZIP圧縮
resource "aws_cloudfront_cache_policy" "static_assets" {
  name        = "${var.project_name}-static-assets"
  min_ttl     = 1
  default_ttl = 86400    # 24時間（ファイルバージョニングと併用を推奨）
  max_ttl     = 2592000  # 30日

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true

    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

# API用: キャッシュ無効（TTL=0）
resource "aws_cloudfront_cache_policy" "api_no_cache" {
  name        = "${var.project_name}-api-no-cache"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

# ==========================================================
# CloudFront ディストリビューション
# オリジン構成:
#   デフォルト (*):  S3バケット（静的SPA/ファイル）+ OAC
#   /api/*:          API Gateway + Lambda（動的API）
# ==========================================================

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  price_class         = var.price_class
  comment             = "${var.project_name}-${var.environment}"
  default_root_object = "index.html"

  # WAF WebACL: エッジでリクエストをインスペクション
  web_acl_id = aws_wafv2_web_acl.main.arn

  # ---- オリジン1: S3（静的コンテンツ）----
  origin {
    origin_id                = "s3-static"
    domain_name              = aws_s3_bucket.static.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.static.id
  }

  # ---- オリジン2: API Gateway ----
  origin {
    origin_id   = "apigw"
    domain_name = "${aws_api_gateway_rest_api.main.id}.execute-api.${var.aws_region}.amazonaws.com"

    # CloudFrontのorigin_pathにステージ名を指定:
    # CloudFrontの /api/foo → API Gatewayの /{stage}/api/foo に転送される
    # ビヘイビアの path_pattern と組み合わせてパスを正しくマッピング
    origin_path = "/${var.environment}"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    # カスタムヘッダー: CloudFront経由のリクエストのみを識別するシークレット
    # API GatewayのリソースポリシーまたはLambdaでこのヘッダーを検証する
    custom_header {
      name  = "X-Origin-Verify"
      value = var.origin_verify_secret
    }
  }

  # ---- デフォルトビヘイビア: S3（SPA・静的ファイル）----
  default_cache_behavior {
    target_origin_id       = "s3-static"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.static_assets.id
    compress               = true

    # Viewer Response: セキュリティヘッダーを付与
    function_association {
      event_type   = "viewer-response"
      function_arn = aws_cloudfront_function.security_headers.arn
    }
  }

  # ---- /api/* ビヘイビア: API Gateway + Lambda ----
  ordered_cache_behavior {
    path_pattern     = "/api/*"
    target_origin_id = "apigw"

    # APIはHTTPSのみ許可（リダイレクトなし）
    viewer_protocol_policy = "https-only"

    # PUT/POST/DELETE等のミューテーション操作を許可
    allowed_methods = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods  = ["GET", "HEAD"]

    cache_policy_id = aws_cloudfront_cache_policy.api_no_cache.id

    # AllViewerExceptHostHeader:
    # API GatewayはHostヘッダーに自分自身のドメインを期待する。
    # CloudFrontのHostヘッダー（CFのドメイン）をそのまま転送すると403になるため
    # Host以外のすべてのヘッダー・クエリ・Cookieを転送するこのポリシーを使用する。
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac"

    compress = true

    function_association {
      event_type   = "viewer-response"
      function_arn = aws_cloudfront_function.security_headers.arn
    }
  }

  # SPA用フォールバック: S3の403/404をindex.htmlに変換（クライアントサイドルーティング対応）
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  restrictions {
    geo_restriction {
      restriction_type = var.geo_restriction_type
      locations        = var.geo_restriction_locations
    }
  }

  viewer_certificate {
    # PoC: デフォルト証明書を使用
    # 本番環境では ACM証明書(us-east-1)を使用:
    #   acm_certificate_arn      = var.acm_certificate_arn
    #   ssl_support_method       = "sni-only"
    #   minimum_protocol_version = "TLSv1.2_2021"
    cloudfront_default_certificate = true
  }

  tags = local.common_tags
}

# ==========================================================
# ローカル変数
# ==========================================================

locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
