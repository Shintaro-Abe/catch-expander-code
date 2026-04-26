# PoC 品質 - 本番利用前にセキュリティレビューおよび設定の見直しを行ってください
#
# 構成概要:
#   API Gateway (REST API, Active Tracing 有効)
#     └─ Lambda (Python 3.12, ADOT Layer による自動 OTel 計装)
#         └─ AWS X-Ray / CloudWatch Application Signals へトレース送信
#
# トレース伝播フロー:
#   クライアント → API GW (X-Amzn-Trace-Id 付与) → Lambda (ADOT が W3C + X-Ray 複合 Propagator で受信)
#     → OTLP 経由 X-Ray エクスポート

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}

locals {
  name_prefix = "${var.project_name}-${var.environment}"

  # ADOT Python Lambda Layer ARN
  # アーキテクチャ: x86_64（arm64 の場合は "arm64" に変更）
  adot_layer_arn = "arn:aws:lambda:${var.aws_region}:615299751070:layer:AWSOpenTelemetryDistroPython:${var.adot_layer_version}"
}

# ─────────────────────────────────────────────
# Lambda ソースコードのアーカイブ
# ─────────────────────────────────────────────
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda_src"
  output_path = "${path.module}/.build/lambda.zip"
}

# ─────────────────────────────────────────────
# IAM Role for Lambda
# ─────────────────────────────────────────────
resource "aws_iam_role" "lambda_exec" {
  name = "${local.name_prefix}-lambda-exec-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# X-Ray へのトレース書き込み権限
resource "aws_iam_role_policy" "xray_write" {
  name = "${local.name_prefix}-xray-write"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
        "xray:GetSamplingStatisticSummaries"
      ]
      Resource = "*"
    }]
  })
}

# CloudWatch Application Signals 用マネージドポリシー（オプション）
resource "aws_iam_role_policy_attachment" "application_signals" {
  count      = var.enable_application_signals ? 1 : 0
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchLambdaApplicationSignalsExecutionRolePolicy"
}

# ─────────────────────────────────────────────
# Lambda 関数
# ─────────────────────────────────────────────
resource "aws_lambda_function" "api_handler" {
  function_name    = "${local.name_prefix}-api-handler"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "handler.lambda_handler"
  runtime          = var.lambda_runtime
  memory_size      = var.lambda_memory_size
  timeout          = var.lambda_timeout
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  # X-Ray アクティブトレーシング（API Gateway と連携してエンドツーエンドトレースを確立）
  tracing_config {
    mode = "Active"
  }

  # ADOT Lambda Layer をアタッチ
  layers = [local.adot_layer_arn]

  environment {
    variables = {
      # ADOT 自動計装を有効化するラッパースクリプト
      AWS_LAMBDA_EXEC_WRAPPER = "/opt/otel-instrument"

      # OTel サービス識別情報
      OTEL_SERVICE_NAME    = var.otel_service_name
      OTEL_RESOURCE_ATTRIBUTES = "service.namespace=${var.project_name},deployment.environment=${var.environment}"

      # W3C Trace Context + X-Ray の複合 Propagator
      # API Gateway が付与する X-Amzn-Trace-Id と W3C traceparent の両方に対応
      OTEL_PROPAGATORS = "tracecontext,baggage,xray"

      # サンプリング設定（高スループット関数では比率を下げる）
      OTEL_TRACES_SAMPLER     = "traceidratio"
      OTEL_TRACES_SAMPLER_ARG = var.otel_traces_sampler_arg

      # メトリクスを CloudWatch EMF 形式で出力
      OTEL_METRICS_EXPORTER = "awsemf"

      # Cold Start 軽減: 必要な計装のみ有効化
      # "none" にすると全ライブラリが対象になるが Cold Start が増加する
      OTEL_PYTHON_DISABLED_INSTRUMENTATIONS = "django,flask,fastapi,grpc,mysql,psycopg2,redis,sqlalchemy"

      # Application Signals（有効な場合）
      OTEL_AWS_APPLICATION_SIGNALS_ENABLED = tostring(var.enable_application_signals)
    }
  }

  depends_on = [aws_iam_role_policy_attachment.lambda_basic]
}

# ─────────────────────────────────────────────
# CloudWatch Log Group（明示的に作成してリテンション管理）
# ─────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${aws_lambda_function.api_handler.function_name}"
  retention_in_days = 30
}

# ─────────────────────────────────────────────
# API Gateway (REST API)
# ─────────────────────────────────────────────
resource "aws_api_gateway_rest_api" "main" {
  name        = "${local.name_prefix}-api"
  description = "OpenTelemetry デモ用 REST API"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# /items リソース
resource "aws_api_gateway_resource" "items" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "items"
}

# GET /items メソッド
resource "aws_api_gateway_method" "get_items" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.items.id
  http_method   = "GET"
  authorization = "NONE"
}

# Lambda 統合
resource "aws_api_gateway_integration" "get_items" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.items.id
  http_method             = aws_api_gateway_method.get_items.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api_handler.invoke_arn
}

# POST /items メソッド
resource "aws_api_gateway_method" "post_items" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.items.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "post_items" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.items.id
  http_method             = aws_api_gateway_method.post_items.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api_handler.invoke_arn
}

# デプロイ
resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.main.id

  triggers = {
    # メソッド・統合が変更されたら再デプロイ
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.items.id,
      aws_api_gateway_method.get_items.id,
      aws_api_gateway_integration.get_items.id,
      aws_api_gateway_method.post_items.id,
      aws_api_gateway_integration.post_items.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.get_items,
    aws_api_gateway_integration.post_items,
  ]
}

# ステージ（Active Tracing 有効化でゲートウェイレベルのトレースを X-Ray へ送信）
resource "aws_api_gateway_stage" "main" {
  deployment_id = aws_api_gateway_deployment.main.id
  rest_api_id   = aws_api_gateway_rest_api.main.id
  stage_name    = var.api_gateway_stage_name

  # API Gateway 自身が X-Ray セグメントを生成し、Lambda に X-Amzn-Trace-Id を伝播
  xray_tracing_enabled = true

  # アクセスログ: requestId と xrayTraceId を記録してトレース ID を相関
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access_log.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      xrayTraceId    = "$context.xrayTraceId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      integrationLatency = "$context.integrationLatency"
    })
  }

  depends_on = [aws_cloudwatch_log_group.api_access_log]
}

# API Gateway アクセスログ用 CloudWatch Log Group
resource "aws_cloudwatch_log_group" "api_access_log" {
  name              = "/aws/apigateway/${local.name_prefix}-access"
  retention_in_days = 30
}

# API Gateway が CloudWatch Logs へ書き込む権限（アカウント単位の設定）
resource "aws_api_gateway_account" "main" {
  cloudwatch_role_arn = aws_iam_role.api_gateway_cloudwatch.arn
}

resource "aws_iam_role" "api_gateway_cloudwatch" {
  name = "${local.name_prefix}-apigw-cw-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "api_gateway_cloudwatch" {
  role       = aws_iam_role.api_gateway_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

# API Gateway → Lambda 呼び出し権限
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# ─────────────────────────────────────────────
# X-Ray サンプリングルール（オプション）
# デフォルトルールより低レートをサービス固有に設定する例
# ─────────────────────────────────────────────
resource "aws_xray_sampling_rule" "api_handler" {
  rule_name      = "${local.name_prefix}-sampling"
  priority       = 1000
  version        = 1
  reservoir_size = 5     # 1秒あたりの固定取得スパン数
  fixed_rate     = 0.05  # 5% のサンプリング率
  url_path       = "*"
  host           = "*"
  http_method    = "*"
  service_type   = "*"
  service_name   = var.otel_service_name
  resource_arn   = "*"
}
