# PoC Quality — Not for production use without security review
# API Gateway (REST API) + Lambda proxy integration

terraform {
  required_version = ">= 1.5"
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

##############################
# Lambda
##############################

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda_src"
  output_path = "${path.module}/.build/lambda.zip"
}

resource "aws_iam_role" "lambda_exec" {
  name = "${var.prefix}-lambda-exec"

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

resource "aws_iam_role_policy_attachment" "lambda_xray" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_lambda_function" "api_handler" {
  function_name    = "${var.prefix}-api-handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  role             = aws_iam_role.lambda_exec.arn
  handler          = "index.handler"
  runtime          = var.lambda_runtime
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_sec
  architectures    = [var.lambda_arch]

  # X-Ray active tracing
  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      LOG_LEVEL = var.log_level
    }
  }

  lifecycle {
    ignore_changes = [environment[0].variables["DEPLOY_TS"]]
  }
}

# Reserved concurrency — 0 disables the function; set > 0 to cap scale-out
resource "aws_lambda_function_event_invoke_config" "api_handler" {
  function_name          = aws_lambda_function.api_handler.function_name
  maximum_retry_attempts = 0  # API proxy: no retry on failure
}

##############################
# API Gateway — REST API
##############################

resource "aws_api_gateway_rest_api" "main" {
  name        = "${var.prefix}-api"
  description = "REST API backed by Lambda (PoC)"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# ----- /items resource -----
resource "aws_api_gateway_resource" "items" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "items"
}

# GET /items
resource "aws_api_gateway_method" "items_get" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.items.id
  http_method   = "GET"
  authorization = "NONE"  # Replace with COGNITO_USER_POOLS or CUSTOM for auth
}

resource "aws_api_gateway_integration" "items_get" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.items.id
  http_method             = aws_api_gateway_method.items_get.http_method
  integration_http_method = "POST"  # Lambda invoke always uses POST
  type                    = "AWS_PROXY"  # Proxy integration: no mapping template needed
  uri                     = aws_lambda_function.api_handler.invoke_arn
  timeout_milliseconds    = 29000  # Max 29s
}

# POST /items
resource "aws_api_gateway_method" "items_post" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.items.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "items_post" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.items.id
  http_method             = aws_api_gateway_method.items_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api_handler.invoke_arn
  timeout_milliseconds    = 29000
}

# ----- /items/{itemId} resource -----
resource "aws_api_gateway_resource" "item" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.items.id
  path_part   = "{itemId}"
}

resource "aws_api_gateway_method" "item_get" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.item.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "item_get" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.item.id
  http_method             = aws_api_gateway_method.item_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api_handler.invoke_arn
  timeout_milliseconds    = 29000
}

# Lambda permission — allow API Gateway to invoke the function
resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_handler.function_name
  principal     = "apigateway.amazonaws.com"
  # Restrict to this API only
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

##############################
# Deployment & Stage
##############################

resource "aws_cloudwatch_log_group" "apigw_access" {
  name              = "/aws/apigateway/${var.prefix}-access"
  retention_in_days = 30
}

# CloudWatch role for API Gateway (account-level; create once per account/region)
resource "aws_iam_role" "apigw_cloudwatch" {
  name = "${var.prefix}-apigw-cw-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "apigw_cloudwatch" {
  role       = aws_iam_role.apigw_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "main" {
  cloudwatch_role_arn = aws_iam_role.apigw_cloudwatch.arn
}

resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.main.id

  # Redeploy when any method/integration changes
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.items,
      aws_api_gateway_method.items_get,
      aws_api_gateway_integration.items_get,
      aws_api_gateway_method.items_post,
      aws_api_gateway_integration.items_post,
      aws_api_gateway_resource.item,
      aws_api_gateway_method.item_get,
      aws_api_gateway_integration.item_get,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.items_get,
    aws_api_gateway_integration.items_post,
    aws_api_gateway_integration.item_get,
  ]
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  deployment_id = aws_api_gateway_deployment.main.id
  stage_name    = "prod"

  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw_access.arn
    # JSON structured log including requestId, IP, method, path, status, latency
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      caller         = "$context.identity.caller"
      user           = "$context.identity.user"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
      integrationLatency = "$context.integrationLatency"
      responseLatency    = "$context.responseLatency"
    })
  }

  # Method-level execution logging (INFO | ERROR | OFF)
  dynamic "default_route_settings" {
    for_each = []
    content {}
  }

  depends_on = [aws_api_gateway_account.main]
}

# Method-level logging settings for the stage
resource "aws_api_gateway_method_settings" "prod" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled        = true
    logging_level          = "INFO"
    data_trace_enabled     = false  # true logs full request/response body — avoid in prod
    throttling_burst_limit = var.stage_burst_limit
    throttling_rate_limit  = var.stage_rate_limit
  }
}

##############################
# Usage Plan + API Key (optional rate limiting)
##############################

resource "aws_api_gateway_usage_plan" "default" {
  name = "${var.prefix}-default-plan"

  api_stages {
    api_id = aws_api_gateway_rest_api.main.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }

  throttle_settings {
    burst_limit = var.plan_burst_limit
    rate_limit  = var.plan_rate_limit
  }

  quota_settings {
    limit  = var.plan_quota_limit
    period = "DAY"
  }
}

# Lambda log group
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${aws_lambda_function.api_handler.function_name}"
  retention_in_days = 30
}