variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-northeast-1"
}

variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "dev"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "vpc_id" {
  description = "VPC ID for SageMaker Domain"
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for SageMaker Domain"
  type        = list(string)
}

variable "pipeline_schedule" {
  description = "EventBridge cron expression for pipeline execution"
  type        = string
  default     = "cron(0 2 * * ? *)"  # 毎日UTC 2:00
}

variable "notification_email" {
  description = "Email for pipeline failure notifications (optional)"
  type        = string
  default     = ""
}

variable "instance_type_training" {
  description = "Instance type for training jobs"
  type        = string
  default     = "ml.m5.xlarge"
}

variable "instance_type_processing" {
  description = "Instance type for processing jobs"
  type        = string
  default     = "ml.m5.large"
}

variable "use_spot_instances" {
  description = "Enable Managed Spot Training (最大90%コスト削減)"
  type        = bool
  default     = true
}