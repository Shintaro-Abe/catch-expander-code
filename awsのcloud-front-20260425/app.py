# PoC品質 - 本番利用前に十分なレビューとセキュリティ評価を実施してください
import aws_cdk as cdk
from cloudfront_stack import CloudFrontApiStack

app = cdk.App()

CloudFrontApiStack(
    app,
    "CloudFrontApiStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "ap-northeast-1",
    ),
)

app.synth()
