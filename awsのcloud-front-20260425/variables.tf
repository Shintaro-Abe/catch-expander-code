# PoC品質: 本番利用前にデフォルト値・バリデーションを見直すこと

variable "project_name" {
  type        = string
  description = "プロジェクト名（リソース命名プレフィックスに使用）"
  default     = "myapp"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.project_name))
    error_message = "project_name は小文字英数字とハイフンのみ使用可能です"
  }
}

variable "environment" {
  type        = string
  description = "環境名（API Gatewayステージ名にも使用）"
  default     = "prod"

  validation {
    condition     = contains(["dev", "stg", "prod"], var.environment)
    error_message = "environment は dev / stg / prod のいずれかを指定してください"
  }
}

variable "aws_region" {
  type        = string
  description = "AWSリージョン（API Gatewayのデプロイ先）"
  default     = "ap-northeast-1"
}

variable "price_class" {
  type        = string
  description = <<-EOT
    CloudFrontのPriceClass。
    PriceClass_100: 北米・欧州のみ（最安値）
    PriceClass_200: + アジア・中東・南アフリカ
    PriceClass_All: 全エッジロケーション（最低レイテンシー）
  EOT
  default     = "PriceClass_All"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class は PriceClass_100 / PriceClass_200 / PriceClass_All のいずれかを指定してください"
  }
}

variable "origin_verify_secret" {
  type        = string
  description = <<-EOT
    X-Origin-Verify ヘッダーの値。CloudFrontがAPI Gatewayに転送するシークレット。
    API GatewayのリソースポリシーまたはLambda内でこの値を検証し、
    CloudFront経由でないリクエストを拒否する。
    本番環境ではAWS Secrets Managerで管理してローテーションすること。
  EOT
  sensitive   = true

  validation {
    condition     = length(var.origin_verify_secret) >= 16
    error_message = "origin_verify_secret は16文字以上を設定してください"
  }
}

variable "geo_restriction_type" {
  type        = string
  description = "地理制限タイプ: none（制限なし）/ whitelist（許可）/ blacklist（拒否）"
  default     = "none"

  validation {
    condition     = contains(["none", "whitelist", "blacklist"], var.geo_restriction_type)
    error_message = "geo_restriction_type は none / whitelist / blacklist のいずれかを指定してください"
  }
}

variable "geo_restriction_locations" {
  type        = list(string)
  description = "地理制限の対象国コードリスト（ISO 3166-1 alpha-2）。geo_restriction_type が none の場合は空リスト"
  default     = []
}
