# PoC 品質 - 本番利用前にセキュリティレビューおよび設定の見直しを行ってください

output "api_endpoint" {
  description = "API Gateway エンドポイント URL"
  value       = "${aws_api_gateway_stage.main.invoke_url}/items"
}

output "api_gateway_id" {
  description = "API Gateway REST API ID"
  value       = aws_api_gateway_rest_api.main.id
}

output "lambda_function_name" {
  description = "Lambda 関数名"
  value       = aws_lambda_function.api_handler.function_name
}

output "lambda_function_arn" {
  description = "Lambda 関数 ARN"
  value       = aws_lambda_function.api_handler.arn
}

output "lambda_log_group" {
  description = "Lambda CloudWatch ロググループ名"
  value       = aws_cloudwatch_log_group.lambda.name
}

output "api_access_log_group" {
  description = "API Gateway アクセスログ CloudWatch ロググループ名"
  value       = aws_cloudwatch_log_group.api_access_log.name
}

output "xray_console_url" {
  description = "X-Ray トレース確認 URL（マネジメントコンソール）"
  value       = "https://${var.aws_region}.console.aws.amazon.com/xray/home?region=${var.aws_region}#/traces"
}

output "cloudwatch_application_signals_url" {
  description = "CloudWatch Application Signals 確認 URL"
  value       = var.enable_application_signals ? "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#application-signals:services" : "Application Signals 無効"
}

output "adot_layer_arn_used" {
  description = "デプロイに使用した ADOT Lambda Layer ARN"
  value       = local.adot_layer_arn
}

# サンプル curl コマンド
output "sample_curl_command" {
  description = "動作確認用 curl コマンド（GET /items）"
  value       = "curl -s ${aws_api_gateway_stage.main.invoke_url}/items | jq ."
}
