## IaCコード（Terraform または CloudFormation）

# OpenTelemetry on AWS Lambda + API Gateway — Terraform PoC

> **PoC 品質**: 動作確認・学習目的のサンプルです。本番利用前にセキュリティレビューおよび設定の見直しを行ってください。

## 構成概要

```
クライアント
    │ HTTP リクエスト
    ▼
API Gateway (REST API)
    │ Active Tracing 有効 → X-Ray にゲートウェイセグメント生成
    │ X-Amzn-Trace-Id ヘッダを Lambda イベントに付与
    ▼
Lambda (Python 3.12)
    │ ADOT Layer (/opt/otel-instrument) が自動計装
    │ W3C Trace Context + X-Ray 複合 Propagator で親コンテキスト継承
    ▼
AWS X-Ray / CloudWatch Application Signals
```

## ファイル構成

```
.
├── main.tf               # メインインフラ（Lambda, API GW, IAM, X-Ray サンプリングルール）
├── variables.tf          # 入力変数
├── outputs.tf            # 出力値（エンドポイント URL, コンソールリンク等）
├── lambda_src/
│   └── handler.py        # Lambda ハンドラ（カスタムスパン追加例付き）
└── README.md             # このファイル
```

## デプロイ手順

### 前提条件

- Terraform >= 1.5.0
- AWS CLI 設定済み（`aws configure` または環境変数）
- 必要な IAM 権限: Lambda, API Gateway, IAM, X-Ray, CloudWatch

### 1. 初期化

```bash
terraform init
```

### 2. 変数のカスタマイズ（オプション）

```bash
# terraform.tfvars を作成して上書き
cat > terraform.tfvars <<EOF
aws_region              = "ap-northeast-1"
project_name            = "my-otel-project"
environment             = "dev"
otel_service_name       = "my-api-service"
otel_traces_sampler_arg = "0.1"   # 本番: 10% サンプリング
EOF
```

### 3. プランの確認

```bash
terraform plan
```

### 4. デプロイ

```bash
terraform apply
```

### 5. 動作確認

```bash
# エンドポイント URL を取得
API_URL=$(terraform output -raw api_endpoint)

# GET リクエスト
curl -s "$API_URL" | jq .

# POST リクエスト
curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -d '{"name": "test-item"}' | jq .
```

### 6. トレース確認

```bash
# X-Ray コンソール URL を表示
terraform output xray_console_url

# Application Signals コンソール URL を表示（有効時）
terraform output cloudwatch_application_signals_url
```

## 主要設定の解説

### ADOT Lambda Layer（自動計装）

| 環境変数 | 役割 |
|---------|------|
| `AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument` | ADOT 自動計装を有効化 |
| `OTEL_PROPAGATORS=tracecontext,baggage,xray` | W3C + X-Ray 複合 Propagator（API GW との連携に必須） |
| `OTEL_METRICS_EXPORTER=awsemf` | CloudWatch Embedded Metrics Format でメトリクス出力 |
| `OTEL_TRACES_SAMPLER=traceidratio` | サンプリング率をコードではなく環境変数で制御 |
| `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` | 不要ライブラリの計装を無効化して Cold Start を削減 |

### API Gateway Active Tracing

`xray_tracing_enabled = true` を設定することで:
- API Gateway 自身が X-Ray セグメントを生成
- `X-Amzn-Trace-Id` ヘッダが Lambda に自動付与
- エンドツーエンドのサービスマップが X-Ray コンソールに表示される

### Cold Start 最適化

本 PoC では以下の対策を実装済み:
- `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` で未使用ライブラリを除外
- `OTEL_TRACES_SAMPLER=traceidratio` でサンプリング率を制御

さらに Cold Start を削減したい場合:
- `OTEL_NODE_ENABLED_INSTRUMENTATIONS`（Node.js）で必要な計装のみ有効化
- Provisioned Concurrency を使用（コスト増加のトレードオフあり）

## ADOT Layer バージョンの更新

```bash
# 最新バージョンを確認
aws lambda list-layer-versions \
  --layer-name arn:aws:lambda:ap-northeast-1:615299751070:layer:AWSOpenTelemetryDistroPython \
  --query 'LayerVersions[0].Version'

# terraform.tfvars でバージョンを更新
echo 'adot_layer_version = 26' >> terraform.tfvars
terraform apply
```

## 削除

```bash
terraform destroy
```

## 参考リンク

- [AWS Distro for OpenTelemetry - Lambda](https://aws-otel.github.io/docs/getting-started/lambda)
- [OpenTelemetry Semantic Conventions v1.40.0](https://github.com/open-telemetry/semantic-conventions)
- [CloudWatch Application Signals](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Application-Signals.html)
- [X-Ray Sampling Rules](https://docs.aws.amazon.com/xray/latest/devguide/xray-console-sampling.html)


## プログラムコード（Python またはユーザープロファイルの技術スタック）

# OpenTelemetry × AWS Lambda + API Gateway (PoC)

> **PoC品質**: 本番利用前に認証・セキュリティ・エラーハンドリングを追加してください。

## 構成

```
API Gateway (REST)
  └─ Lambda (Python 3.12)
       ├─ ADOT Lambda Layer（自動計装）
       ├─ ADOT Collector Extension（Lambda Extension として動作）
       │    └─ AWS X-Ray（トレース）
       │    └─ CloudWatch EMF（メトリクス）
       └─ DynamoDB
```

## ファイル一覧

| ファイル | 説明 |
|---------|------|
| `lambda/handler.py` | Lambda ハンドラ（手動計装サンプル付き） |
| `lambda/collector.yaml` | ADOT Collector 設定（decouple プロセッサー含む） |
| `cdk_stack.py` | AWS CDK スタック（Lambda + API Gateway + ADOT） |
| `requirements.txt` | Python 依存パッケージ |

## セットアップ

```bash
# 依存パッケージのインストール
pip install -r requirements.txt
pip install aws-cdk-lib constructs

# CDK デプロイ
cdk deploy --context region=ap-northeast-1
```

## 主要な環境変数

| 変数名 | 値 | 説明 |
|-------|----|------|
| `AWS_LAMBDA_EXEC_WRAPPER` | `/opt/otel-instrument` | ADOT 自動計装の有効化 |
| `OTEL_SERVICE_NAME` | 任意の文字列 | X-Ray・CloudWatch でのサービス名 |
| `OTEL_PROPAGATORS` | `tracecontext,baggage,xray` | W3C + X-Ray 複合伝播 |
| `OTEL_METRICS_EXPORTER` | `awsemf` | CloudWatch EMF メトリクス出力 |
| `OPENTELEMETRY_COLLECTOR_CONFIG_URI` | ファイルパス or S3 URI | Collector 設定の場所 |

## Cold Start 最適化ポイント

- `decouple` プロセッサー: OTLP 送信を Lambda 実行完了後に非同期化
- `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`: 未使用ライブラリの計装を無効化
- Provisioned Concurrency: レイテンシ要件が厳しい場合

## ADOT Layer ARN の更新

Layer ARN は `cdk_stack.py` の `ADOT_PYTHON_LAYER_ARN` で管理しています。
最新バージョンは[公式ドキュメント](https://aws-otel.github.io/docs/getting-started/lambda/lambda-python)で確認してください。


---

📝 [Notionで詳細を見る](https://www.notion.so/Open-Telemetry-34e47b55202e810f8e8de3fa6ecfb9d4)
