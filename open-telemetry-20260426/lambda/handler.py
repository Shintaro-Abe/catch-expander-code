# PoC品質: 本番利用前に認証・エラーハンドリング・ビジネスロジックを追加してください

import json
import os

import boto3
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace import SpanKind, StatusCode

# ---------------------------------------------------------------------------
# SDK 初期化（Lambda コンテナ再利用を想定し、グローバルスコープで一度だけ実行）
# ---------------------------------------------------------------------------

_tracer: trace.Tracer | None = None


def _init_tracer() -> trace.Tracer:
    """
    ADOT Lambda Layer 自動計装を使用しない場合の手動初期化。
    自動計装（AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument）を使用する場合は
    この初期化ブロックを削除し、trace.get_tracer() のみ呼び出せばよい。
    """
    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME", "my-lambda-service"),
            ResourceAttributes.CLOUD_PROVIDER: "aws",
            ResourceAttributes.FAAS_NAME: os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "unknown"),
            ResourceAttributes.FAAS_VERSION: os.environ.get("AWS_LAMBDA_FUNCTION_VERSION", "$LATEST"),
            ResourceAttributes.CLOUD_REGION: os.environ.get("AWS_REGION", "ap-northeast-1"),
        }
    )

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces")

    provider = TracerProvider(resource=resource)
    # decouple プロセッサーを ADOT Collector 側で設定する場合、ここは BatchSpanProcessor で十分
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # botocore（boto3）の自動計装
    BotocoreInstrumentor().instrument()

    return trace.get_tracer(__name__)


def _get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = _init_tracer()
    return _tracer


# ---------------------------------------------------------------------------
# コンテキスト伝播ヘルパー
# ---------------------------------------------------------------------------

def _extract_context(event: dict) -> context.Context:
    """
    API Gateway v1 (REST) / v2 (HTTP) の両形式に対応するコンテキスト抽出。
    W3C traceparent → X-Amzn-Trace-Id の順でフォールバックする。
    OTEL_PROPAGATORS=tracecontext,baggage,xray を環境変数で設定すること。
    """
    headers: dict = event.get("headers") or {}
    # HTTP ヘッダーはケースが不定のため小文字に正規化
    normalized = {k.lower(): v for k, v in headers.items()}
    return extract(normalized)


# ---------------------------------------------------------------------------
# ダウンストリームサービス呼び出しサンプル
# ---------------------------------------------------------------------------

def _call_downstream(tracer: trace.Tracer, table_name: str) -> list:
    """DynamoDB へのサンプル呼び出し。botocore 自動計装でスパンが自動生成される。"""
    with tracer.start_as_current_span(
        "dynamodb.scan",
        kind=SpanKind.CLIENT,
        attributes={
            SpanAttributes.DB_SYSTEM: "dynamodb",
            SpanAttributes.DB_NAME: table_name,
            SpanAttributes.DB_OPERATION: "Scan",
        },
    ) as span:
        try:
            dynamodb = boto3.resource("dynamodb")
            table = dynamodb.Table(table_name)
            response = table.scan(Limit=10)
            items = response.get("Items", [])
            span.set_attribute("db.dynamodb.item_count", len(items))
            return items
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise


# ---------------------------------------------------------------------------
# Lambda ハンドラ
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, lambda_context) -> dict:
    tracer = _get_tracer()
    parent_ctx = _extract_context(event)

    http_method = (
        event.get("requestContext", {}).get("http", {}).get("method")  # v2
        or event.get("httpMethod")  # v1
        or "UNKNOWN"
    )
    route = (
        event.get("requestContext", {}).get("http", {}).get("path")  # v2
        or event.get("resource")  # v1
        or "/"
    )

    with tracer.start_as_current_span(
        "lambda.handler",
        context=parent_ctx,
        kind=SpanKind.SERVER,
        attributes={
            SpanAttributes.FAAS_TRIGGER: "http",
            SpanAttributes.HTTP_METHOD: http_method,
            SpanAttributes.HTTP_ROUTE: route,
            "faas.invocation_id": lambda_context.aws_request_id,
            "faas.coldstart": _is_cold_start(),
        },
    ) as span:
        try:
            table_name = os.environ.get("DYNAMODB_TABLE", "items")
            items = _call_downstream(tracer, table_name)

            span.set_attribute(SpanAttributes.HTTP_STATUS_CODE, 200)
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"items": items}),
            }
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Internal Server Error"}),
            }


# ---------------------------------------------------------------------------
# コールドスタート検出
# ---------------------------------------------------------------------------
_cold_start = True


def _is_cold_start() -> bool:
    global _cold_start
    result = _cold_start
    _cold_start = False
    return result
