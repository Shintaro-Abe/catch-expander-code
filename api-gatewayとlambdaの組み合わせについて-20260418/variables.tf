# PoC Quality — Not for production use without review

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ap-northeast-1"
}

variable "prefix" {
  description = "Name prefix applied to all resources"
  type        = string
  default     = "poc-apigw-lambda"
}

variable "lambda_runtime" {
  description = "Lambda runtime identifier"
  type        = string
  default     = "python3.12"  # python3.12 supports SnapStart; switch to nodejs20.x for lower cold start
}

variable "lambda_memory_mb" {
  description = "Lambda memory in MB (128–10240). CPU scales proportionally; 1769 MB = 1 vCPU."
  type        = number
  default     = 512
}

variable "lambda_timeout_sec" {
  description = "Lambda timeout in seconds (max 29 to align with API Gateway integration timeout)"
  type        = number
  default     = 29
}

variable "lambda_arch" {
  description = "Instruction set architecture: x86_64 or arm64 (Graviton — ~15-20% faster cold start, lower cost)"
  type        = string
  default     = "arm64"
}

variable "log_level" {
  description = "Application log level passed to Lambda via environment variable"
  type        = string
  default     = "INFO"
}

# Stage-level throttling (applies to all methods unless overridden by usage plan)
variable "stage_burst_limit" {
  description = "API Gateway stage burst limit (token bucket max tokens)"
  type        = number
  default     = 500
}

variable "stage_rate_limit" {
  description = "API Gateway stage steady-state rate limit (requests per second)"
  type        = number
  default     = 1000
}

# Usage plan throttling (per-client limit; must be <= stage limits)
variable "plan_burst_limit" {
  description = "Usage plan burst limit"
  type        = number
  default     = 200
}

variable "plan_rate_limit" {
  description = "Usage plan rate limit (RPS)"
  type        = number
  default     = 500
}

variable "plan_quota_limit" {
  description = "Usage plan daily quota (requests per day)"
  type        = number
  default     = 100000
}