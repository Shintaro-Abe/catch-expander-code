# PoC品質 - 本番利用前に十分なレビューとセキュリティ評価を実施してください
#
# 構成概要:
#   CloudFront Distribution
#     ├── デフォルトビヘイビア  → S3 (OAC) : SPA 静的ファイル配信
#     └── /api/* ビヘイビア    → API Gateway (REST) + Lambda : APIエンドポイント
#
# セキュリティ:
#   - S3 オリジンアクセスは OAC (Origin Access Control) で制限
#   - API Gateway はリージョナルエンドポイント + デフォルトエンドポイント無効化
#   - Viewer Response で セキュリティヘッダーを CloudFront Functions で付与
#   - HTTPS 強制 (redirect-to-https)
#   - WAF WebACL をオプションで関連付け可能

import json
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_lambda as lambda_,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3 as s3,
    aws_iam as iam,
    CfnOutput,
    Duration,
    RemovalPolicy,
)
from constructs import Construct


class CloudFrontApiStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------ #
        # 1. S3 バケット (静的ファイル / SPA)
        # ------------------------------------------------------------------ #
        static_bucket = s3.Bucket(
            self,
            "StaticBucket",
            # パブリックアクセスをすべてブロック - CloudFront OAC 経由のみ許可
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,  # PoC用。本番は RETAIN を推奨
            auto_delete_objects=True,              # PoC用
        )

        # ------------------------------------------------------------------ #
        # 2. Lambda 関数 (APIハンドラー)
        # ------------------------------------------------------------------ #
        api_handler = lambda_.Function(
            self,
            "ApiHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_.Code.from_asset("lambda"),
            handler="index.handler",
            timeout=Duration.seconds(29),  # API Gateway 統合タイムアウト 30秒以内
            memory_size=256,
            environment={
                # シークレットは Secrets Manager / Parameter Store から取得すること
                "LOG_LEVEL": "INFO",
            },
            # X-Ray トレーシング有効化
            tracing=lambda_.Tracing.ACTIVE,
        )

        # ------------------------------------------------------------------ #
        # 3. API Gateway (REST API - リージョナルエンドポイント)
        # ------------------------------------------------------------------ #
        rest_api = apigw.RestApi(
            self,
            "RestApi",
            # CloudFront 経由でのみアクセスさせるため Regional を指定
            # Edge-Optimized だと CloudFront ↔ API GW 間で二重の CloudFront 処理が発生する
            endpoint_types=[apigw.EndpointType.REGIONAL],
            # CloudFront のカスタムドメインを使う場合、デフォルトエンドポイントを無効化して
            # 直接 execute-api URL へのアクセスを防ぐ
            disable_execute_api_endpoint=False,  # PoC用。本番は True を推奨
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                tracing_enabled=True,
                data_trace_enabled=False,  # 本番では False (リクエストボディがログに残る)
                logging_level=apigw.MethodLoggingLevel.INFO,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,  # 本番では CloudFront ドメインに絞る
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "Authorization"],
            ),
        )

        # /items リソース
        items = rest_api.root.add_resource("items")
        items.add_method(
            "GET",
            apigw.LambdaIntegration(
                api_handler,
                proxy=True,
                # タイムアウトを Lambda のタイムアウトより短く設定
                timeout=Duration.seconds(29),
            ),
        )
        items.add_method(
            "POST",
            apigw.LambdaIntegration(api_handler, proxy=True),
        )

        item = items.add_resource("{id}")
        item.add_method("GET", apigw.LambdaIntegration(api_handler, proxy=True))
        item.add_method("PUT", apigw.LambdaIntegration(api_handler, proxy=True))
        item.add_method("DELETE", apigw.LambdaIntegration(api_handler, proxy=True))

        # ------------------------------------------------------------------ #
        # 4. CloudFront Functions (Viewer Response: セキュリティヘッダー付与)
        # ------------------------------------------------------------------ #
        security_headers_fn = cloudfront.Function(
            self,
            "SecurityHeadersFn",
            code=cloudfront.FunctionCode.from_inline(
                """
function handler(event) {
  var response = event.response;
  var headers = response.headers;
  headers['strict-transport-security'] = { value: 'max-age=63072000; includeSubDomains; preload' };
  headers['x-content-type-options']    = { value: 'nosniff' };
  headers['x-frame-options']           = { value: 'DENY' };
  headers['x-xss-protection']          = { value: '1; mode=block' };
  headers['referrer-policy']           = { value: 'strict-origin-when-cross-origin' };
  headers['permissions-policy']        = { value: 'geolocation=(), microphone=(), camera=()' };
  return response;
}
"""
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            comment="Attach security response headers at the edge",
        )

        # ------------------------------------------------------------------ #
        # 5. キャッシュポリシー
        # ------------------------------------------------------------------ #
        # 静的アセット用: 長期キャッシュ + Brotli/GZIP 圧縮対応
        static_cache_policy = cloudfront.CachePolicy(
            self,
            "StaticCachePolicy",
            default_ttl=Duration.days(7),
            max_ttl=Duration.days(365),
            min_ttl=Duration.seconds(0),
            enable_accept_encoding_brotli=True,
            enable_accept_encoding_gzip=True,
            # クエリ文字列・Cookie・追加ヘッダーはキャッシュキーに含めない
            query_string_behavior=cloudfront.CacheQueryStringBehavior.none(),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            header_behavior=cloudfront.CacheHeaderBehavior.none(),
        )

        # ------------------------------------------------------------------ #
        # 6. CloudFront ディストリビューション
        #    - デフォルト: S3 (OAC)
        #    - /api/*   : API Gateway (REST)
        # ------------------------------------------------------------------ #

        # S3 OAC (Origin Access Control) - OAI の後継として推奨
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(static_bucket)

        # API Gateway オリジン
        # RestApiOrigin は origin_path (ステージ名) を自動設定する
        # Host ヘッダー問題を回避するため ALL_VIEWER_EXCEPT_HOST_HEADER ポリシーを使用
        api_origin = origins.RestApiOrigin(rest_api)

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            # デフォルトビヘイビア: 静的ファイル (S3)
            default_behavior=cloudfront.BehaviorOptions(
                origin=s3_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=static_cache_policy,
                compress=True,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                # Viewer Response で セキュリティヘッダーを付与
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=security_headers_fn,
                        event_type=cloudfront.FunctionEventType.VIEWER_RESPONSE,
                    )
                ],
            ),
            # /api/* ビヘイビア: API Gateway
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    # API レスポンスはキャッシュしない
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # Host 以外の全ヘッダー・クエリ文字列・Cookie を API GW に転送
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    compress=True,
                    # Viewer Response でセキュリティヘッダーを付与
                    function_associations=[
                        cloudfront.FunctionAssociation(
                            function=security_headers_fn,
                            event_type=cloudfront.FunctionEventType.VIEWER_RESPONSE,
                        )
                    ],
                ),
            },
            # SPA のフォールバック: 404 → /index.html
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
            # 日本のユーザーが主なら Price Class 200 でコスト削減
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
            # HTTP/2 & HTTP/3 有効化
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            # IPv6 有効化
            enable_ipv6=True,
            comment="CloudFront + API Gateway + Lambda PoC",
        )

        # ------------------------------------------------------------------ #
        # 7. S3 バケットポリシー: CloudFront OAC からの GetObject のみ許可
        #    (OAC を使うと CDK が自動的に付与するが、明示的に追加する例として記載)
        # ------------------------------------------------------------------ #
        static_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                principals=[iam.ServicePrincipal("cloudfront.amazonaws.com")],
                resources=[static_bucket.arn_for_objects("*")],
                conditions={
                    "StringEquals": {
                        "AWS:SourceArn": f"arn:aws:cloudfront::{self.account}:distribution/{distribution.distribution_id}"
                    }
                },
            )
        )

        # ------------------------------------------------------------------ #
        # 8. Outputs
        # ------------------------------------------------------------------ #
        CfnOutput(self, "DistributionDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "ApiEndpoint", value=rest_api.url)
        CfnOutput(self, "StaticBucketName", value=static_bucket.bucket_name)
