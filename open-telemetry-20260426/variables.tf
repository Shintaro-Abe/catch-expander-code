# PoC 品質 - 本番利用前にセキュリティレビューおよび設定の見直しを行ってください

variable "aws_region" {
  description = "AWSリージョン"
  type        = string
  default     = "ap-northeast-1"
}

variable "project_name" {
  description = "プロジェクト名（リソース命名プレフィックスに使用）"
  type        = string
  default     = "otel-demo"
}

variable "environment" {
  description = "デプロイ環境（dev / stg / prod）"
  type        = string
  default     = "dev"
}

variable "lambda_runtime" {
  description = "Lambda ランタイム"
  type        = string
  default     = "python3.12"
}

variable "lambda_memory_size" {
  description = "Lambda メモリサイズ (MB)"
  type        = number
  default     = 256
}

variable "lambda_timeout" {
  description = "Lambda タイムアウト (秒)"
  type        = number
  default     = 30
}

# ADOT Lambda Layer バージョン
# 最新バージョンは https://aws-otel.github.io/docs/getting-started/lambda で確認
variable "adot_layer_version" {
  description = "ADOT Python Lambda Layer バージョン番号"
  type        = number
  default     = 25
}

variable "otel_service_name" {
  description = "OTel サービス名（X-Ray / CloudWatch Application Signals に表示される名前）"
  type        = string
  default     = "otel-demo-api"
}

# サンプリング設定
# 本番環境では 0.1（10%）など低い値を推奨
variable "otel_traces_sampler_arg" {
  description = "トレースサンプリング率（0.0〜1.0）"
  type        = string
  default     = "1.0"
}

variable "api_gateway_stage_name" {
  description = "API Gateway ステージ名"
  type        = string
  default     = "v1"
}

variable "enable_application_signals" {
  description = "CloudWatch Application Signals を有効化するか"
  type        = bool
  default     = true
}
