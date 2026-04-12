# SageMaker Pipelines Infrastructure
# PoC品質: 本番利用前にセキュリティレビューが必要です

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  required_version = ">= 1.5"
}

provider "aws" { region = var.aws_region }

data "aws_caller_identity" "current" {}

# S3 Bucket
resource "aws_s3_bucket" "ml_artifacts" {
  bucket = "${var.project_name}-ml-artifacts-${var.environment}-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "${var.project_name}-ml-artifacts", Environment = var.environment }
}

resource "aws_s3_bucket_versioning" "ml_artifacts" {
  bucket = aws_s3_bucket.ml_artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ml_artifacts" {
  bucket = aws_s3_bucket.ml_artifacts.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }
}

resource "aws_s3_bucket_public_access_block" "ml_artifacts" {
  bucket                  = aws_s3_bucket.ml_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# IAM Role
resource "aws_iam_role" "sagemaker_execution_role" {
  name = "${var.project_name}-sagemaker-execution-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "sagemaker.amazonaws.com" }
    }]
  })
  tags = { Name = "${var.project_name}-sagemaker-role", Environment = var.environment }
}

resource "aws_iam_role_policy_attachment" "sagemaker_full_access" {
  role       = aws_iam_role.sagemaker_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
}

resource "aws_iam_role_policy" "s3_ml_access" {
  name = "s3-ml-artifacts-access"
  role = aws_iam_role.sagemaker_execution_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
      Resource = [aws_s3_bucket.ml_artifacts.arn, "${aws_s3_bucket.ml_artifacts.arn}/*"]
    }]
  })
}

# SageMaker Model Package Group (Model Registry)
resource "aws_sagemaker_model_package_group" "ml_model_registry" {
  model_package_group_name        = "${var.project_name}-model-registry"
  model_package_group_description = "Model registry for ${var.project_name}"
  tags = { Name = "${var.project_name}-model-registry", Environment = var.environment }
}

# SageMaker Studio Domain
resource "aws_sagemaker_domain" "ml_domain" {
  domain_name = "${var.project_name}-domain"
  auth_mode   = "IAM"
  vpc_id      = var.vpc_id
  subnet_ids  = var.subnet_ids
  default_user_settings { execution_role = aws_iam_role.sagemaker_execution_role.arn }
  tags = { Name = "${var.project_name}-domain", Environment = var.environment }
}

# EventBridge Scheduled Trigger
resource "aws_cloudwatch_event_rule" "pipeline_schedule" {
  name                = "${var.project_name}-pipeline-schedule"
  description         = "Scheduled trigger for SageMaker Pipeline"
  schedule_expression = var.pipeline_schedule
  tags = { Name = "${var.project_name}-schedule", Environment = var.environment }
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "pipeline_logs" {
  name              = "/aws/sagemaker/${var.project_name}-pipeline"
  retention_in_days = 30
  tags = { Name = "${var.project_name}-pipeline-logs", Environment = var.environment }
}

# SNS Notification
resource "aws_sns_topic" "pipeline_notifications" {
  name = "${var.project_name}-pipeline-notifications"
  tags = { Name = "${var.project_name}-notifications", Environment = var.environment }
}

resource "aws_sns_topic_subscription" "pipeline_email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.pipeline_notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

# CloudWatch Alarm: Pipeline Failure
resource "aws_cloudwatch_metric_alarm" "pipeline_failure" {
  alarm_name          = "${var.project_name}-pipeline-failure"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsFailed"
  namespace           = "AWS/SageMaker"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "SageMaker Pipeline execution failed"
  alarm_actions       = [aws_sns_topic.pipeline_notifications.arn]
  dimensions          = { PipelineName = "${var.project_name}-pipeline" }
  tags = { Name = "${var.project_name}-failure-alarm", Environment = var.environment }
}