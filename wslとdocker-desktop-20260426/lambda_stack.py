# PoC品質: このコードは概念実証用のスケルトンです。本番環境での使用前に十分なレビューを行ってください。
"""
AWS CDK スタック定義: Lambda + API Gateway 統合
WSL2 + Docker Desktop 環境での開発・ローカルテストを想定した構成

ローカルテスト手順:
  cdk synth
  sam local start-api -t cdk.out/WslDockerLambdaStack.template.json
  curl http://127.0.0.1:3000/hello
  curl -X POST http://127.0.0.1:3000/items -d '{"name":"test"}' -H 'Content-Type: application/json'
"""

from __future__ import annotations

import os
from constructs import Construct

import aws_cdk as cdk
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_apigateway as apigw
import aws_cdk.aws_iam as iam
import aws_cdk.aws_logs as logs


class WslDockerLambdaStack(cdk.Stack):
    """Lambda + API Gateway を統合した CDK スタック"""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Lambda 実行ロール ---
        lambda_role = iam.Role(
            self,
            "LambdaExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # --- Lambda レイヤー（共通ユーティリティ用、任意） ---
        # Docker Desktop WSL2 バックエンドを使用したコンテナビルドが自動で利用される
        # （esbuild / pip install は CDK が Lambda 互換コンテナ内で実行）

        # --- GET /hello ハンドラー Lambda ---
        hello_fn = lambda_.Function(
            self,
            "HelloFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.hello",
            code=lambda_.Code.from_asset(
                "lambda",
                # Docker Desktop WSL2 統合が有効なら bundling はコンテナ内で実行される
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                ),
            ),
            role=lambda_role,
            memory_size=256,
            timeout=cdk.Duration.seconds(30),
            environment={
                "LOG_LEVEL": "INFO",
                "STAGE": os.getenv("STAGE", "dev"),
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
            description="GET /hello エンドポイント",
        )

        # --- POST /items ハンドラー Lambda ---
        items_fn = lambda_.Function(
            self,
            "ItemsFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.create_item",
            code=lambda_.Code.from_asset(
                "lambda",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                ),
            ),
            role=lambda_role,
            memory_size=256,
            timeout=cdk.Duration.seconds(30),
            environment={
                "LOG_LEVEL": "INFO",
                "STAGE": os.getenv("STAGE", "dev"),
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
            description="POST /items エンドポイント",
        )

        # --- API Gateway REST API ---
        api = apigw.RestApi(
            self,
            "WslDockerApi",
            rest_api_name="wsl-docker-api",
            description="WSL2 + Docker Desktop PoC: Lambda + API Gateway",
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                logging_level=apigw.MethodLoggingLevel.INFO,
                data_trace_enabled=True,
                metrics_enabled=True,
                throttling_burst_limit=100,
                throttling_rate_limit=50,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "Authorization"],
            ),
        )

        # --- /hello リソース: GET ---
        hello_resource = api.root.add_resource("hello")
        hello_resource.add_method(
            "GET",
            apigw.LambdaIntegration(
                hello_fn,
                proxy=True,  # Lambda プロキシ統合（リクエスト/レスポンスをそのまま転送）
                passthrough_behavior=apigw.PassthroughBehavior.WHEN_NO_MATCH,
            ),
            method_responses=[
                apigw.MethodResponse(
                    status_code="200",
                    response_models={"application/json": apigw.Model.EMPTY_MODEL},
                )
            ],
        )

        # --- /items リソース: POST ---
        items_resource = api.root.add_resource("items")
        items_resource.add_method(
            "POST",
            apigw.LambdaIntegration(
                items_fn,
                proxy=True,
                passthrough_behavior=apigw.PassthroughBehavior.WHEN_NO_MATCH,
            ),
            method_responses=[
                apigw.MethodResponse(
                    status_code="201",
                    response_models={"application/json": apigw.Model.EMPTY_MODEL},
                ),
                apigw.MethodResponse(status_code="400"),
            ],
        )

        # --- 出力 ---
        cdk.CfnOutput(
            self,
            "ApiEndpoint",
            value=api.url,
            description="API Gateway エンドポイント URL",
        )
        cdk.CfnOutput(
            self,
            "HelloFunctionArn",
            value=hello_fn.function_arn,
            description="HelloFunction ARN",
        )
        cdk.CfnOutput(
            self,
            "ItemsFunctionArn",
            value=items_fn.function_arn,
            description="ItemsFunction ARN",
        )

        self.api = api
        self.hello_fn = hello_fn
        self.items_fn = items_fn
