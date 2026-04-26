# PoC品質: 本番利用前にセキュリティ設定・スロットリング・WAF・カスタムドメインを追加してください

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_apigateway as apigw,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct


class OpenTelemetryStack(Stack):
    """
    Lambda + API Gateway + ADOT Lambda Layer による OpenTelemetry 計装スタック。

    アーキテクチャ:
      API Gateway (REST) → Lambda (Python 3.12 + ADOT Layer)
                                     ↓ OTLP/HTTP (localhost:4318)
                               ADOT Collector Extension
                                     ↓
                               AWS X-Ray

    ADOT Lambda Layer ARN は以下の公式ページで最新版を確認してください:
    https://aws-otel.github.io/docs/getting-started/lambda/lambda-python
    """

    # ADOT Python Layer の公式 ARN（us-east-1 / amd64 の例）
    # リージョン・バージョンは環境に合わせて変更してください
    ADOT_PYTHON_LAYER_ARN = (
        "arn:aws:lambda:{region}:615299751070:layer:AWSOpenTelemetryDistroPython:25"
    )

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # DynamoDB テーブル（Lambda のデータソース）
        # ------------------------------------------------------------------
        table = dynamodb.Table(
            self,
            "ItemsTable",
            partition_key=dynamodb.Attribute(
                name="id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,  # PoC 用。本番は RETAIN に変更
        )

        # ------------------------------------------------------------------
        # ADOT Lambda Layer（自動計装）
        # ------------------------------------------------------------------
        adot_layer = lambda_.LayerVersion.from_layer_version_arn(
            self,
            "AdotPythonLayer",
            self.ADOT_PYTHON_LAYER_ARN.format(region=self.region),
        )

        # ------------------------------------------------------------------
        # Lambda 関数
        # ------------------------------------------------------------------
        fn = lambda_.Function(
            self,
            "OtelLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda"),
            timeout=Duration.seconds(30),
            memory_size=256,
            tracing=lambda_.Tracing.ACTIVE,  # X-Ray アクティブトレーシング
            layers=[adot_layer],
            environment={
                # --- ADOT 自動計装の有効化 ---
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/otel-instrument",
                # --- サービス識別 ---
                "OTEL_SERVICE_NAME": "otel-demo-service",
                # --- エクスポーター設定（Lambda Extension 内蔵 Collector へ）---
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
                # --- 伝播方式: W3C Trace Context + Baggage + X-Ray 複合 ---
                "OTEL_PROPAGATORS": "tracecontext,baggage,xray",
                # --- メトリクスを CloudWatch EMF 形式でエクスポート ---
                "OTEL_METRICS_EXPORTER": "awsemf",
                # --- 選択的計装で Cold Start を抑制（未使用ライブラリを除外）---
                "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS": "flask,django,fastapi,grpc",
                # --- Collector 設定ファイルの場所（S3 URI も可）---
                "OPENTELEMETRY_COLLECTOR_CONFIG_URI": "/var/task/collector.yaml",
                # --- アプリケーション固有 ---
                "DYNAMODB_TABLE": table.table_name,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ------------------------------------------------------------------
        # IAM 権限
        # ------------------------------------------------------------------
        # X-Ray へのトレース書き込み
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            )
        )

        # CloudWatch Application Signals（APM ダッシュボード自動構成）
        fn.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "CloudWatchLambdaApplicationSignalsExecutionRolePolicy"
            )
        )

        # DynamoDB アクセス
        table.grant_read_data(fn)

        # ------------------------------------------------------------------
        # API Gateway (REST API v1)
        # ------------------------------------------------------------------
        # アクセスログ設定: requestId と X-Ray トレース ID を相関させる
        access_log_group = logs.LogGroup(
            self,
            "ApiAccessLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        api = apigw.RestApi(
            self,
            "OtelApi",
            rest_api_name="otel-demo-api",
            description="OpenTelemetry デモ API (PoC)",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                # API Gateway レベルのトレーシングを有効化
                tracing_enabled=True,
                # アクセスログに X-Ray トレース ID を記録（トレース相関用）
                access_log_destination=apigw.LogGroupLogDestination(access_log_group),
                access_log_format=apigw.AccessLogFormat.custom(
                    '{"requestId":"$context.requestId",'
                    '"xrayTraceId":"$context.xrayTraceId",'
                    '"ip":"$context.identity.sourceIp",'
                    '"method":"$context.httpMethod",'
                    '"path":"$context.resourcePath",'
                    '"status":"$context.status",'
                    '"responseLength":"$context.responseLength",'
                    '"latencyMs":"$context.responseLatency"}'
                ),
                # スロットリング設定（PoC 用の低い値）
                throttling_rate_limit=100,
                throttling_burst_limit=50,
            ),
        )

        # /items リソース
        items_resource = api.root.add_resource("items")
        items_resource.add_method(
            "GET",
            apigw.LambdaIntegration(
                fn,
                proxy=True,
                # タイムアウトは Lambda タイムアウト未満に設定
                timeout=Duration.seconds(29),
            ),
        )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(self, "ApiEndpoint", value=api.url)
        cdk.CfnOutput(self, "LambdaFunctionName", value=fn.function_name)
        cdk.CfnOutput(self, "DynamoDbTableName", value=table.table_name)


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
app = cdk.App()
OpenTelemetryStack(
    app,
    "OpenTelemetryStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account") or None,
        region=app.node.try_get_context("region") or "ap-northeast-1",
    ),
)
app.synth()
