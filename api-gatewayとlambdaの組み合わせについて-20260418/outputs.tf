# PoC Quality — Not for production use without review

output "api_invoke_url" {
  description = "Base invoke URL for the prod stage"
  value       = aws_api_gateway_stage.prod.invoke_url
}

output "items_endpoint" {
  description = "GET /items endpoint URL"
  value       = "${aws_api_gateway_stage.prod.invoke_url}/items"
}

output "lambda_function_name" {
  description = "Lambda function name"
  value       = aws_lambda_function.api_handler.function_name
}

output "lambda_function_arn" {
  description = "Lambda function ARN"
  value       = aws_lambda_function.api_handler.arn
}

output "lambda_log_group" {
  description = "CloudWatch log group for Lambda"
  value       = aws_cloudwatch_log_group.lambda.name
}

output "apigw_access_log_group" {
  description = "CloudWatch log group for API Gateway access logs"
  value       = aws_cloudwatch_log_group.apigw_access.name
}

output "rest_api_id" {
  description = "REST API ID (use for further Terraform references or aws CLI calls)"
  value       = aws_api_gateway_rest_api.main.id
}

output "usage_plan_id" {
  description = "Usage plan ID (associate API keys here)"
  value       = aws_api_gateway_usage_plan.default.id
}