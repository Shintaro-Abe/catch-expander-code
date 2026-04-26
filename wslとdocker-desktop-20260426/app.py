# PoC品質: このコードは概念実証用のスケルトンです。本番環境での使用前に十分なレビューを行ってください。
#!/usr/bin/env python3
"""
WSL2 + Docker Desktop 環境向け AWS CDK アプリケーションエントリーポイント
Lambda + API Gateway 構成のデモ
"""

import aws_cdk as cdk
from lambda_stack import WslDockerLambdaStack

app = cdk.App()

WslDockerLambdaStack(
    app,
    "WslDockerLambdaStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "ap-northeast-1",
    ),
    description="WSL2 + Docker Desktop 開発環境向け Lambda + API Gateway PoC スタック",
)

app.synth()
