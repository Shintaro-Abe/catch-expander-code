output "s3_bucket_name" {
  description = "ML artifacts S3 bucket name"
  value       = aws_s3_bucket.ml_artifacts.id
}

output "s3_bucket_arn" {
  description = "ML artifacts S3 bucket ARN"
  value       = aws_s3_bucket.ml_artifacts.arn
}

output "sagemaker_execution_role_arn" {
  description = "SageMaker execution IAM role ARN"
  value       = aws_iam_role.sagemaker_execution_role.arn
}

output "model_package_group_name" {
  description = "SageMaker Model Package Group (Model Registry)"
  value       = aws_sagemaker_model_package_group.ml_model_registry.model_package_group_name
}

output "sagemaker_domain_id" {
  description = "SageMaker Studio Domain ID"
  value       = aws_sagemaker_domain.ml_domain.id
}

output "sns_topic_arn" {
  description = "SNS topic ARN for pipeline notifications"
  value       = aws_sns_topic.pipeline_notifications.arn
}