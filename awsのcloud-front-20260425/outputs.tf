# PoC品質: 出力値はデプロイ後の動作確認・後続設定に使用

output "cloudfront_domain_name" {
  description = "CloudFrontディストリビューションのドメイン名（xxxx.cloudfront.net）"
  value       = aws_cloudfront_distribution.main.domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFrontディストリビューションID（キャッシュ無効化コマンドで使用）"
  value       = aws_cloudfront_distribution.main.id
}

output "cloudfront_distribution_arn" {
  description = "CloudFrontディストリビューションARN（WAF・S3バケットポリシー参照用）"
  value       = aws_cloudfront_distribution.main.arn
}

output "s3_bucket_name" {
  description = "静的コンテンツS3バケット名（aws s3 sync コマンドで使用）"
  value       = aws_s3_bucket.static.bucket
}

output "s3_bucket_regional_domain" {
  description = "S3バケットのリージョナルドメイン（OACで使用）"
  value       = aws_s3_bucket.static.bucket_regional_domain_name
}

output "api_gateway_id" {
  description = "API Gateway REST API ID"
  value       = aws_api_gateway_rest_api.main.id
}

output "api_gateway_stage_url" {
  description = "API Gatewayのデプロイ済みステージURL（CloudFront経由でのみアクセス推奨）"
  value       = aws_api_gateway_stage.main.invoke_url
}

output "api_via_cloudfront_url" {
  description = "CloudFront経由でAPIにアクセスするURL（/api/* パスを使用）"
  value       = "https://${aws_cloudfront_distribution.main.domain_name}/api/"
}

output "lambda_function_name" {
  description = "Lambda関数名（CloudWatchログ確認に使用）"
  value       = aws_lambda_function.api.function_name
}

output "lambda_function_arn" {
  description = "Lambda関数ARN"
  value       = aws_lambda_function.api.arn
}

output "waf_web_acl_arn" {
  description = "WAF WebACL ARN（CloudFrontに関連付け済み）"
  value       = aws_wafv2_web_acl.main.arn
}

output "cache_invalidation_command" {
  description = "静的コンテンツ更新後にCloudFrontキャッシュを無効化するコマンド例"
  value       = "aws cloudfront create-invalidation --distribution-id ${aws_cloudfront_distribution.main.id} --paths '/*'"
}

output "s3_sync_command" {
  description = "静的ファイルをS3にアップロードするコマンド例"
  value       = "aws s3 sync ./dist s3://${aws_s3_bucket.static.bucket}/ --delete"
}
