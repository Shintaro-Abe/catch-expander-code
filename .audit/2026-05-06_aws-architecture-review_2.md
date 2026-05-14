# AWS Architecture Review — 2026-05-06 (#2)

## サマリ
- Critical: 0 / High: 6 / Medium: 9 / Low: 6 / Info: 5
- 総合評価:
  - 個人利用 SAM スタックとして基礎は強固。ECR IMMUTABLE、DynamoDB 全テーブル PITR + Deletion Protection、S3 explicit Deny + OAC strict、CloudFront standard logging（legacy）、ECS readonly root + 非 root + init-perms、Public Subnet 上の SG egress 443 限定など、Defense-in-Depth が複数層で機能している。
  - 受容済みリスク（VPC Flow Logs / WAF 未導入、ECS Public Subnet 配置）はコスト合理性が文書化されており、cfn-guard / checkov がそれぞれ機械的に拾った関連 finding（`EC2_SECURITY_GROUP_EGRESS_OPEN_TO_WORLD_RULE`、`NO_UNRESTRICTED_ROUTE_TO_IGW`、`SUBNET_AUTO_ASSIGN_PUBLIC_IP_DISABLED`、CKV_AWS_68 / CKV_AWS_117）も本レビューでは Critical/High に昇格させない。
  - 一方で「ほぼゼロコストで埋められる薄い層」が依然複数残存: CloudFront viewer 側 TLS minimum policy、API Gateway access log / X-Ray、Lambda DLQ・X-Ray、CloudWatch Logs / S3 / DynamoDB の暗号化意図の IaC 明示、S3 SSL enforce、Cost Anomaly + Budgets、CloudWatch Alarms。これらは前回レポートでも指摘済みで、今回 cfn-guard / checkov が機械的に同じ箇所を裏付けた。
- 対象: `/workspaces/Catch-Expander/template.yaml`（SAM スタック単体、55 リソース）
- 使用ツール:
  - **cfn-lint 1.x**（`/home/vscode/.local/bin/cfn-lint`）: warnings/errors 0 件（Pydantic V1 互換警告 1 件は cfn-lint 自身の依存問題で対象外）
  - **checkov 3.2.526**（CloudFormation framework）: PASSED 96 / FAILED 95 / Resource count 55。固有 check_id は 17 種、Lambda 系（`CKV_AWS_115/116/117/173`）が 18 関数 × 4 種で件数を占める
  - **cfn-guard 3.x**（`/home/vscode/.cache/aws-guard-rules-registry/rules/aws` 全ルール）: FAIL 22 ルール検出。受容済みリスク（VPC/SG/IGW/Public IP）、暗号化（DynamoDB/S3/Logs/Secrets KMS CMK）、CloudFront（TLS minimum, custom SSL, SNI, origin failover）、Secrets rotation、IAM no-inline-policy、cfn-no-explicit-resource-names を検出
  - **AWS Security Hub**: `aws securityhub describe-hub` → `InvalidAccessException: Account is not subscribed`。本アカウントは Security Hub 未サブスクライブ（前回レポートと同様、AWS 認証ありでも Security Hub findings は取得不能）。代替として cfn-guard / checkov の機械検査を採用、Security Hub control ID（APIGateway.1/3、S3.4/5/9、CloudFront.5、Lambda.5 等）は公式ドキュメント引用で代替
  - **AWS Config**: `describe-configuration-recorders` でレコーダー有効を確認したが、`describe-config-rules` の応答が空でルール未デプロイ。conformance pack 未投入のため Config 由来 finding は 0 件
  - **AWS GuardDuty**: `list-detectors` 応答が空（Detector 未作成）
  - **AWS Trusted Advisor**: `SubscriptionRequiredException`（Premium Support 未契約）
  - **MCP awsknowledge**（過去レポートで使用済みの公式根拠）/ 静的読み込み（`docs/architecture.md`、`auto-memory/` 14 件、過去 `.audit/` レポート 1 件）

> 前回（114k token）からの差分: cfn-lint / checkov / cfn-guard のローカル CLI が**全て稼働**したことで、前回 MCP error -32000 で取れなかった compliance スキャンを完全網羅。新規発見として **CFN_NO_EXPLICIT_RESOURCE_NAMES**、**Origin Failover 不在**、**S3 Versioning 不在**、**Lambda Reserved Concurrency 未設定**、**Lambda env var 暗号化未設定**、**S3 Object Lock 不在** が追加検出された。Security Hub だけは認証ありでもアカウント未サブスクライブのため取得不能（本プロジェクトの構造的制約。サブスクライブには月額課金が発生するため要オプトイン判断）。

---

## 指摘事項

### [High] CloudFront viewer 側 minimum TLS policy が default certificate 由来で TLSv1 まで許容
- **カテゴリ**: Security / Data Protection
- **対象**: `template.yaml:1334-1385` `FrontendDistribution.DistributionConfig`（`ViewerCertificate` ブロック未指定）
- **現状**: cfn-guard `CLOUDFRONT_MINIMUM_PROTOCOL_VERSION_RULE` / `CLOUDFRONT_CUSTOM_SSL_CERTIFICATE` / `CLOUDFRONT_SNI_ENABLED` の 3 ルールが FAIL。checkov `CKV_AWS_174`（Verify CloudFront Distribution Viewer Certificate is using TLS v1.2 or higher）も FAIL。default certificate (`*.cloudfront.net`) を使うとき CloudFront は TLSv1 を viewer に許容する。
- **リスク・影響**: Slack OAuth → JWT cookie がこのエッジに乗るため、TLSv1.0/1.1 でネゴシエートされる経路を残すのは Data Protection 上の弱点。前回も同様指摘あり。
- **推奨**: 中期的に独自ドメインへ移行 + ACM 証明書 + `MinimumProtocolVersion: TLSv1.2_2021`。当面 default certificate を継続するなら ADR / steering に「ドメイン移行時に TLS policy を必ず指定」と明記し、移行までは「`x-content-type-options: nosniff` 等のレスポンスヘッダ強化」で部分緩和。
  ```yaml
  ViewerCertificate:
    AcmCertificateArn: !Ref AcmCertificateArn
    SslSupportMethod: sni-only
    MinimumProtocolVersion: TLSv1.2_2021
  ```
- **根拠**:
  - https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/secure-connections-supported-viewer-protocols-ciphers.html
  - cfn-guard rule: `cloudfront_minimum_protocol_version_rule.guard` / `cloudfront_custom_ssl_certificate.guard` / `cloudfront_sni_enabled.guard`
  - checkov: `CKV_AWS_174`（https://docs.prismacloud.io/en/enterprise-edition/policy-reference/aws-policies/aws-networking-policies/bc-aws-networking-63）
  - WA: Security / Data Protection — protect data in transit

### [High] 全 Lambda 関数で X-Ray Active Tracing が無効、API Gateway 両方も access log / tracing なし
- **カテゴリ**: Operational Excellence / Prepare（可観測性）
- **対象**:
  - `template.yaml:51-55` `Globals.Function`（`Tracing` 未指定）
  - `template.yaml:620-631` `SlackApi`（`TracingEnabled` / `AccessLogSetting` 未指定）
  - `template.yaml:773-789` `DashboardApi`（`AccessLogSettings` / `DefaultRouteSettings` 未指定）
- **現状**: checkov `CKV_AWS_73`（API Gateway X-Ray Tracing）/ `CKV_AWS_76`（API Gateway access logging）/ `CKV_AWS_95`（HTTP API access logging）/ `CKV_AWS_120`（API Gateway caching, REST API のみ）が FAIL。Slack 受信 → Lambda → ECS RunTask → DynamoDB → Slack/Notion/GitHub の分散トレースが取れない。
- **リスク・影響**: 障害時の RCA が CloudWatch Logs 横串の手作業に依存。Security Hub `APIGateway.1` / `APIGateway.3` 相当に違反。
- **推奨**:
  ```yaml
  Globals:
    Function:
      Tracing: Active
  SlackApi:
    Properties:
      TracingEnabled: true
      AccessLogSetting:
        DestinationArn: !GetAtt SlackApiAccessLogGroup.Arn
        Format: '{"requestId":"$context.requestId","ip":"$context.identity.sourceIp","status":"$context.status","integrationLatency":"$context.integrationLatency"}'
      MethodSettings:
        - HttpMethod: "*"
          ResourcePath: "/*"
          LoggingLevel: ERROR
          DataTraceEnabled: false
          MetricsEnabled: true
          ThrottlingBurstLimit: 20
          ThrottlingRateLimit: 10
  DashboardApi:
    Properties:
      AccessLogSettings:
        DestinationArn: !GetAtt DashboardApiAccessLogGroup.Arn
        Format: '$context.requestId $context.routeKey $context.status $context.integrationLatency'
      DefaultRouteSettings:
        ThrottlingBurstLimit: 20
        ThrottlingRateLimit: 10
  ```
- **根拠**:
  - https://docs.aws.amazon.com/lambda/latest/dg/services-xray.html
  - https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-logging.html
  - https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-logging.html
  - checkov: `CKV_AWS_73 / 76 / 95`
  - WA: Operational Excellence / Prepare & Operate

### [High] 18 個の Lambda 関数すべてで DLQ / OnFailure destination が未設定
- **カテゴリ**: Reliability / Failure Management
- **対象**: 全 `AWS::Serverless::Function` × 18（TriggerFunction、TokenMonitor、Dashboard 16 関数）
- **現状**: checkov `CKV_AWS_116`（Lambda DLQ）が 18/18 関数で FAIL。とくに非同期呼び出しを受ける `TokenMonitorFunction`（EventBridge Schedule `rate(12 hours)`）は失敗時 Lambda 内部キューで最大 2 リトライ + 6h discard で**完全消失**する。Slack 同期呼び出しの `TriggerFunction` も再試行されない。
- **リスク・影響**: Claude OAuth refresh が連続失敗してもメッセージが消え、検知経路が「Slack 通知の到達」のみ。auto-memory `project_slack_feedback_requires_mention.md`（"メンションなし破棄で DynamoDB 到達前に消える"）と**同形の構造的非対称**が refresh 経路でも起きうる。
- **推奨**: 最低限 `TokenMonitorFunction` に SQS DLQ または `EventInvokeConfig.OnFailure` を追加。同期呼び出しの Trigger / Dashboard 系は CloudWatch Alarm（Errors >= 1）で代替可。
  ```yaml
  TokenMonitorDlq:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: catch-expander-token-monitor-dlq
      MessageRetentionPeriod: 1209600  # 14 days
  TokenMonitorFunction:
    Properties:
      DeadLetterQueue:
        Type: SQS
        TargetArn: !GetAtt TokenMonitorDlq.Arn
  ```
- **根拠**:
  - https://docs.aws.amazon.com/lambda/latest/dg/invocation-async.html
  - checkov: `CKV_AWS_116`
  - auto-memory: `project_slack_feedback_requires_mention.md`
  - WA: Reliability / Failure Management — Capture errors at boundaries

### [High] CloudWatch Logs と DynamoDB / Secrets / ECR / S3 の暗号化が IaC で「明示」されていない
- **カテゴリ**: Security / Data Protection
- **対象**:
  - `template.yaml:399-403` `AgentLogGroup`、および 18 関数の暗黙生成 LogGroup（`KmsKeyId` 未指定）
  - `template.yaml:152-333` 全 7 DynamoDB Table（`SSESpecification` 未指定 → AWS-owned key default）
  - `template.yaml:338-355` 2 Secrets Manager Secret（`KmsKeyId` 未指定 → `aws/secretsmanager` AWS-managed default）
  - `template.yaml:360-386` `AgentRepository`（`EncryptionConfiguration` 未指定 → AES256 default）
  - `template.yaml:1234-1247 / 1291-1299 / 1307-1323` 3 S3 Bucket（`BucketEncryption` 未指定 → 2023-01 以降は SSE-S3 自動だが IaC 上不明示）
- **現状**: checkov `CKV_AWS_158`（CW Log Group KMS）/ `CKV_AWS_119`（DynamoDB CMK, 7 件）/ `CKV_AWS_149`（Secrets KMS CMK, 2 件）/ `CKV_AWS_136`（ECR KMS）が FAIL。cfn-guard `CLOUDWATCH_LOG_GROUP_ENCRYPTED` / `DYNAMODB_TABLE_ENCRYPTED_KMS` / `SECRETSMANAGER_USING_CMK` / `S3_DEFAULT_ENCRYPTION_KMS` / `S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED` も FAIL。
- **リスク・影響**:
  - 個人利用かつ AWS-owned/AWS-managed key で実害は限定的。
  - ただし IaC に encryption の意図が無いと、(a) compliance スキャン（Security Hub `S3.4`、`CW.7`、`DynamoDB.1` 等）で「設定不明」と評価される、(b) 将来 KMS CMK へ切り替えるときの差分が見えない、(c) 鍵ローテーションやアクセス監査の独立性が担保できない。
  - サブエージェント記録（プロンプト本文を含む）は機密度中〜高で `PromptsBucket` に書き込まれるため、最低限ここは KMS CMK + BucketKey 化が望ましい。
- **推奨**: 1 つの専用 KMS Key を作って LogGroup と PromptsBucket / DynamoDB に適用するのが費用対効果が高い（KMS Key 月 $1.00 + 10,000 リクエスト $0.03）。Secrets Manager / ECR は AWS-managed key 継続で良い旨を IaC コメントで明示。
  ```yaml
  AppKmsKey:
    Type: AWS::KMS::Key
    Properties:
      Description: App-level KMS CMK for catch-expander logs / prompts / dynamodb
      EnableKeyRotation: true
      KeyPolicy: {...}
  AgentLogGroup:
    Properties:
      LogGroupName: /ecs/catch-expander-agent
      RetentionInDays: 30
      KmsKeyId: !GetAtt AppKmsKey.Arn
  PromptsBucket:
    Properties:
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - BucketKeyEnabled: true
            ServerSideEncryptionByDefault:
              SSEAlgorithm: aws:kms
              KMSMasterKeyID: !GetAtt AppKmsKey.Arn
  ```
  AWS-managed key を継続するなら、各リソースの注釈（YAML コメント）に `# AWS-managed key (aws/logs) を使用。CMK 化は compliance 要件発生時に検討` のように意図を残す。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html
  - https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.howitworks.html
  - https://docs.aws.amazon.com/secretsmanager/latest/userguide/security-encryption.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucket-encryption.html
  - checkov: `CKV_AWS_119 / 136 / 149 / 158`
  - WA: Security / Data Protection — encryption at rest

### [High] S3 全 3 バケットに HTTPS-only enforcement (`aws:SecureTransport=false` Deny) が無い
- **カテゴリ**: Security / Data Protection（転送時暗号化）
- **対象**: `template.yaml:1252-1265` `PromptsBucketPolicy` / `1390-1412` `FrontendBucketPolicy` / `1307-1323` `CloudFrontLogsBucket`（policy 自体無し）
- **現状**: 既存 `BucketPolicy` は GetObject の許可/拒否範囲のみで、HTTP（非 TLS）アクセスを Deny する Statement が無い。`CloudFrontLogsBucket` は BucketPolicy 自体が存在しない。cfn-guard `S3_BUCKET_SSL_REQUESTS_ONLY` で 3 バケット全 FAIL を確認。
- **リスク・影響**: AWS Foundational Security Best Practices `S3.5`（"S3 general purpose buckets should require requests to use SSL"）に該当。CloudFront ログは PII（IP、UA、URL クエリ）を含むため転送時 TLS 強制は推奨。Frontend / Prompts は内部経路（CloudFront / Lambda）からのアクセスが主なので実害は極小だが、deploy 時の `aws s3 sync` も TLS 強制下で問題なく動作する。
- **推奨**: 全 3 バケットに `DenyInsecureTransport` を追加。
  ```yaml
  - Sid: DenyInsecureTransport
    Effect: Deny
    Principal: "*"
    Action: s3:*
    Resource:
      - !GetAtt PromptsBucket.Arn
      - !Sub ${PromptsBucket.Arn}/*
    Condition:
      Bool: { aws:SecureTransport: false }
  ```
- **根拠**:
  - https://docs.aws.amazon.com/securityhub/latest/userguide/s3-controls.html （S3.5）
  - cfn-guard rule: `s3_bucket_ssl_requests_only.guard`
  - WA: Security / Data Protection

### [High] Lambda 関数の Reserved Concurrency が一切設定されていない（個別暴走の波及防止なし）
- **カテゴリ**: Reliability / Foundations（クォータ防御）
- **対象**: 全 `AWS::Serverless::Function` × 18
- **現状**: checkov `CKV_AWS_115`（Lambda function-level concurrent execution limit）が 18/18 で FAIL。`ReservedConcurrentExecutions` が一切無く、1 関数の暴走が他 17 関数とリージョン共有プール（既定 1000）を圧迫しうる。とくに Dashboard API 群はパブリックエンドポイント（CloudFront → API Gateway HTTP API）配下にあり、Authorizer 突破時の暴走耐性が弱い。
- **リスク・影響**: Slack 連携の `TriggerFunction`（同期、ユーザー応答性必須）が他関数の暴走に巻き込まれて throttle される可能性。個人利用 dev 環境では実害は低いが、観測性アラームと合わせて防御層が薄い。
- **推奨**: 「ユーザー応答性が必須な関数」と「定期実行の関数」で reserved を分けて上限を当てる。Dashboard 系は controlled だが、念のため最低限の cap を入れる。
  ```yaml
  TriggerFunction:
    Properties:
      ReservedConcurrentExecutions: 5
  TokenMonitorFunction:
    Properties:
      ReservedConcurrentExecutions: 1
  # Dashboard 系は default route throttle で代替するなら個別設定は省略可
  ```
- **根拠**:
  - https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html
  - checkov: `CKV_AWS_115`
  - WA: Reliability / Foundations — Manage service quotas

### [Medium] Lambda 環境変数の暗号化（KMS CMK）が未設定
- **カテゴリ**: Security / Data Protection
- **対象**: 全 `AWS::Serverless::Function` × 17（環境変数を持つ関数）
- **現状**: checkov `CKV_AWS_173`（Check encryption settings for Lambda environment variable）が 17/17 FAIL。Lambda の `Environment.Variables` は AWS-managed key (`aws/lambda`) で暗号化されるが CMK は未指定。`SECRET_ARN` / `OAUTH_STATE_TABLE` / `EVENTS_TABLE` 等のリソース参照しか格納していないため秘匿情報の流出リスクは低い。
- **リスク・影響**: 実害は極小。compliance 観点で IaC に CMK 指定が無いと評価される。
- **推奨**: KMS CMK を導入する場合は High 指摘の `AppKmsKey` に `Environment.KmsKeyArn` で寄せる。AWS-managed key 継続でも可。意図を IaC コメントで残す。
  ```yaml
  Globals:
    Function:
      KmsKeyArn: !GetAtt AppKmsKey.Arn  # 採用時のみ
  ```
- **根拠**:
  - https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html#configuration-envvars-encryption
  - checkov: `CKV_AWS_173`
  - WA: Security / Data Protection

### [Medium] S3 全 3 バケットに versioning が無効（誤削除・上書きからの復元不能）
- **カテゴリ**: Reliability / Failure Management
- **対象**: `PromptsBucket` / `FrontendBucket` / `CloudFrontLogsBucket`
- **現状**: checkov `CKV_AWS_21`（S3 versioning enabled）が 3/3 FAIL。cfn-guard `S3_BUCKET_VERSIONING_ENABLED` も FAIL。
- **リスク・影響**:
  - `FrontendBucket`: `aws s3 sync --delete` 運用なら、誤って古いビルドを sync すると現行バージョンを上書きする経路がある。CloudFront 側のキャッシュが残るため即時影響は限定的だが、復旧時の選択肢が減る。
  - `PromptsBucket`: ECS タスクからの追記系（`prompts/*`）のため上書き頻度は低いが、`s3:PutObject` 権限を持つ TaskRole が同一 key で上書きすると過去版を失う。
  - `CloudFrontLogsBucket`: ログは CloudFront 由来で IDempotent な書き込みのため versioning 不要。
- **推奨**: `FrontendBucket` と `PromptsBucket` のみ `VersioningConfiguration: { Status: Enabled }` を入れ、Lifecycle で「NoncurrentVersionExpiration」を設定（コスト膨張防止）。
  ```yaml
  PromptsBucket:
    Properties:
      VersioningConfiguration:
        Status: Enabled
      LifecycleConfiguration:
        Rules:
          - Id: expire-after-5-years
            Status: Enabled
            ExpirationInDays: 1825
            NoncurrentVersionExpirationInDays: 30
  ```
- **根拠**:
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html
  - checkov: `CKV_AWS_21`
  - cfn-guard: `s3_bucket_versioning_enabled.guard`
  - WA: Reliability / Failure Management — Backup and restore

### [Medium] S3 アクセスログ（server access logging）が 3 バケット共に無い
- **カテゴリ**: Security / Detection
- **対象**: `PromptsBucket` / `FrontendBucket` / `CloudFrontLogsBucket`
- **現状**: checkov `CKV_AWS_18`（S3 access logging）が 3/3 FAIL。cfn-guard `S3_BUCKET_LOGGING_ENABLED` も FAIL。`FrontendBucket` は CloudFront を経由するためほぼ Standard Logging で代替できているが、`PromptsBucket` の直接 Get/Put（IAM 経由）と `CloudFrontLogsBucket` 自体への異常アクセスは記録されない。
- **リスク・影響**: 個人利用で実害は低い。事故時に「誰がいつ Get したか」の独立証跡が無い。CloudTrail data events を有効にすれば部分代替できるが課金別。
- **推奨**: `PromptsBucket` のみ server access logging を `CloudFrontLogsBucket` に転送する形で最小コスト導入。Frontend は CloudFront のログで代替する旨をコメント化。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html
  - checkov: `CKV_AWS_18`
  - WA: Security / Detection

### [Medium] CloudFront に origin failover（OriginGroup）が無く、API Gateway / S3 が単一障害点
- **カテゴリ**: Reliability / Workload Architecture
- **対象**: `template.yaml:1334-1385` `FrontendDistribution.Origins`
- **現状**: cfn-guard `CLOUDFRONT_ORIGIN_FAILOVER_ENABLED` が FAIL。`Origins` 配列に S3 と API Gateway が独立に並んでおり、それぞれ単一 origin の死活がそのまま user 影響に直結する。
- **リスク・影響**: 個人利用 dev 環境では Multi-Region は前提しない（受容済み）が、リージョン内 S3 / API Gateway 障害時に CloudFront 側でのフェイルオーバーが効かない。
- **推奨**: 当面の dev 環境では現状維持で良いが、将来 SLA を出す段階では「`OriginGroup` で primary/secondary を組む」「`CustomErrorResponses` で 5xx 時の fallback HTML を返す」を ADR に残す。即時対応としては `CustomErrorResponses` に 502/503/504 → maintenance.html を追加する程度。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html
  - cfn-guard: `cloudfront_origin_failover_enabled.guard`
  - WA: Reliability / Workload Architecture

### [Medium] ECS Cluster の Container Insights が `disabled`
- **カテゴリ**: Operational Excellence / Operate
- **対象**: `template.yaml:391-397` `AgentCluster`
- **現状**: checkov `CKV_AWS_65` で FAIL（明示的に `Value: disabled`）。
- **リスク・影響**: ECS タスクの CPU/メモリ使用率、状態変化の集約メトリクスが取れない。OOM / CPU 張り付きの事後判定が困難。
- **推奨**: 月数十タスク規模なら enhanced ではなく旧 `enabled` で ~$0.5/月に収まる見込み。コスト懸念で disabled を維持するなら IaC コメントで根拠を残す。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cloudwatch-container-insights.html
  - checkov: `CKV_AWS_65`
  - WA: Operational Excellence / Operate

### [Medium] AgentTaskExecutionRole に CodexAuthSecret の権限が無い（execution / task role 非対称）
- **カテゴリ**: Reliability / Workload Architecture
- **対象**: `template.yaml:419-431` `AgentTaskExecutionRole.Policies.SecretsManagerAccess`
- **現状**: `AgentTaskExecutionRole` の許可 secret 一覧に `CodexAuthSecretArn` が無い一方、`AgentTaskRole`（task runtime）には含まれている。現運用は「main.py が runtime に取得」なので即時障害は出ないが、IaC 上で意図が不明。
- **リスク・影響**: 将来 `Secrets` セクション（execution role 経由解決）への切替時に Codex のみ起動失敗する可能性。
- **推奨**: 現状維持なら `AgentTaskExecutionRole` に「`CodexAuthSecretArn` は不要（runtime 取得方式のため）」のコメントを追加。または対称性のため execution role 側にも追加して将来切替に備える。
- **根拠**:
  - WA: Reliability — design for failure
  - auto-memory: `project_codex_auth_rotation_complete.md`

### [Medium] AgentTaskRole の DynamoDB GSI スコープが部分的（DashboardEventsTable の 3 GSI 不在）
- **カテゴリ**: Security / Identity & Access Management
- **対象**: `template.yaml:445-463` `AgentTaskRole.Policies.DynamoDBAccess`
- **現状**: `WorkflowExecutionsTable/index/user-id-index` は許可されているが、`DashboardEventsTable` の 3 GSI（`gsi_global_timestamp` / `gsi_status_timestamp` / `gsi_event_type_timestamp`）への Query 権限が無い。
- **リスク・影響**: 現状 agent は GSI を使わない設計なら問題なし。意図不明だと将来の機能追加で `AccessDeniedException` が runtime で出る。
- **推奨**: 現状の意図（agent は base table への put のみ）を IaC コメントで明記。または将来追加経路用の steering / ADR に手順を残す。
- **根拠**: WA: Security / IAM — least privilege

### [Medium] DashboardAuthorizer が `ReauthorizeEvery: 300` で 5 分キャッシュ、即時失効が効かない
- **カテゴリ**: Security / Incident Response
- **対象**: `template.yaml:773-789` `DashboardApi.Auth.DashboardAuthorizer.Identity`
- **現状**: `ReauthorizeEvery: 300` + `Identity.Headers: [Cookie]`。Cookie 値ハッシュ単位の 5 分キャッシュ。JWT を即時無効化（ログアウト）しても 5 分間は authorizer がキャッシュ判定で通す。
- **リスク・影響**: 緊急ログアウト・トークン失効・侵害対応で即時遮断ができない。
- **推奨**: ログアウト即時遮断を優先するなら `ReauthorizeEvery: 0`、コスト低減を優先するなら現状維持で「即時遮断は JWT 署名鍵ローテーション（DashboardJwtKeySecret 入れ替え）」と文書化。
- **根拠**:
  - https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-lambda-authorizer.html
  - WA: Security / Incident Response

### [Medium] IAM Inline Policy の多用（5 ロール × 複数 inline policy）
- **カテゴリ**: Security / Identity & Access Management
- **対象**: `AgentTaskExecutionRole` / `AgentTaskRole` / 各 Lambda の `Policies` ブロック × 18 関数
- **現状**: cfn-guard `IAM_NO_INLINE_POLICY_CHECK` が FAIL。SAM の `Policies` プロパティは inline policy として展開される。
- **リスク・影響**: 個人利用では実害は低い。複数ロールで同じ権限を再利用するとき、inline policy だと重複が増えて可読性が下がる。AWS Foundational Security `IAM.16`（IAM customer managed policies should be in use rather than inline policies）相当。
- **推奨**: 当面のままで良いが、Secrets Manager 取得など複数ロールで重複する権限は Customer Managed Policy として切り出してアタッチする方式に整理すると保守性が上がる。
- **根拠**:
  - https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_managed-vs-inline.html
  - cfn-guard: `iam_no_inline_policy_check.guard`
  - WA: Security / IAM

### [Low] DynamoDB / Lambda / S3 / ECS にコスト配分タグ・Owner タグが無い
- **カテゴリ**: Cost Optimization / Cloud Financial Management
- **対象**: 全リソース（VPC/Subnet には `Name` のみあり）
- **現状**: `Project` / `Env` / `Owner` 等のコスト配分タグなし。
- **リスク・影響**: 将来複数プロジェクトを 1 アカウントに同居させたとき、コスト按分の根拠が無くなる。
- **推奨**: SAM `Globals.Function.Tags` と template-wide の Tag macro。
  ```yaml
  Globals:
    Function:
      Tags: { Project: catch-expander, Env: dev, Owner: shintaro-abe }
  ```
- **根拠**: WA: Cost Optimization / Cloud Financial Management — cost allocation tags

### [Low] AWS Budgets / Cost Anomaly Detection が IaC で未定義
- **カテゴリ**: Cost Optimization / Cloud Financial Management
- **対象**: スタック全体
- **現状**: `AWS::Budgets::Budget` / `AWS::CE::AnomalyMonitor` 共に無し。`docs/architecture.md §10` で月 $5.85 と見積もり済みだが、実支出のアラームは無い。
- **リスク・影響**: ECS タスク暴走（Claude OAuth 失敗 + 起動ループ等）で急騰した場合の検知が遅れる。
- **推奨**: 月 $20 / 日 $1 の予算アラーム + Cost Anomaly Detection（無料）を IaC 化。Slack 通知チャンネルへ SNS 経由で再利用。
- **根拠**:
  - https://aws.amazon.com/aws-cost-management/aws-cost-anomaly-detection/
  - WA: Cost Optimization

### [Low] CloudWatch Alarms（Lambda Errors / ECS 失敗 / TokenMonitor 失敗）が IaC で未定義
- **カテゴリ**: Operational Excellence / Operate
- **対象**: スタック全体
- **現状**: `docs/architecture.md §8` の Alarm 設計（Lambda エラー率 > 10%、ECS 失敗 > 0、Maxプラン上限）が template に存在しない。設計と実装の乖離。
- **リスク・影響**: SLO/監視の最低ラインが未実装。
- **推奨**: 設計書 §8 の 3 アラームを最初に IaC 化し、SNS topic + Slack 通知に紐付け。Token Monitor の失敗カウントもアラーム化。
- **根拠**: WA: Operational Excellence / Operate; プロジェクト固有（docs/architecture.md §8）

### [Low] 明示的リソース名の多用（`RoleName` / `BucketName` / `TableName` / `FunctionName`）
- **カテゴリ**: Operational Excellence / Prepare（IaC 安全性）
- **対象**: 全リソース（命名規約として明示的に付与）
- **現状**: cfn-guard `CFN_NO_EXPLICIT_RESOURCE_NAMES` が FAIL。明示名は CFN の immutable update（rename = replace）を引き起こすため、in-place update 中に名前変更すると downtime が発生する。Stack 並行運用（dev/prod）も難しい。
- **リスク・影響**: 単一環境（dev）運用なので実害は低いが、将来 staging を作るときの障害源。
- **推奨**: 単一環境で困らない命名（`!Sub catch-expander-...-${AWS::StackName}` 等）に整理する。当面のまま運用するなら、将来 multi-env 化の steering で「明示名から StackName ベース命名へ」のマイグレーション手順を残す。
- **根拠**:
  - https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-name-type.html
  - cfn-guard: `cfn_no_explicit_resource_names.guard`
  - WA: Operational Excellence

### [Low] DashboardSlackOAuthSecret は手動 populate のため CFN drift が常に検出される
- **カテゴリ**: Operational Excellence / Prepare
- **対象**: `template.yaml:347-355` `DashboardSlackOAuthSecret`
- **現状**: `SecretString: "{}"` で初期化し、運用者がコンソール / CLI で書き換え。`describe-stack-drift-detection-status` を回すと常に drift。
- **リスク・影響**: 軽微。drift detection 結果がノイズ化する可能性。
- **推奨**: `IgnoreChanges: [SecretString]` 相当の Update Replace ポリシーを文書化。または Drift 対象から除外する運用ガイダンスを steering に。
- **根拠**: WA: Operational Excellence / Prepare — IaC drift control

### [Low] ECR ライフサイクルが「直近 5 イメージ」のみ。短期 rollback の余地が薄い
- **カテゴリ**: Cost Optimization / Optimize Over Time
- **対象**: `template.yaml:360-386` `AgentRepository.LifecyclePolicy`
- **現状**: `imageCountMoreThan: 5`。IMMUTABLE と整合した運用で、ECR 容量コストは月 $0.05 程度。
- **リスク・影響**: 直近 2 SHA に戻したい時、5 世代しか残らないため重なって push が起きると古い image が消える。
- **推奨**: 5 → 10 程度に緩める、または `tagPrefixList: ["v"]` で「v タグ付きはより長期保存」の階層を入れる。
- **根拠**: WA: Cost Optimization — Optimize over time

### [Info] ARM64 + Fargate + Python 3.13 + PriceClass_100 + http2and3 の選択は Sustainability に整合
- **カテゴリ**: Sustainability / Hardware & Services
- **対象**: `Globals.Function.Architectures: arm64`、`AgentTaskDefinition.RuntimePlatform.CpuArchitecture: ARM64`、`FrontendDistribution.PriceClass: PriceClass_100` / `HttpVersion: http2and3`
- **現状**: 全 Lambda / ECS Task が ARM64（Graviton）。CloudFront でエッジロケーション抑制。ECR image expire、DynamoDB TTL もすべてサステナビリティ・コスト両方で整合。
- **推奨**: 現状維持。新機能追加時も ARM64 を default に。
- **根拠**: WA: Sustainability / Hardware & Services; Cost Optimization

### [Info] CloudFront standard logging（legacy）採用の整合性
- **カテゴリ**: Security / Detection
- **対象**: `template.yaml:1342-1347` `FrontendDistribution.Logging`、`1307-1323` `CloudFrontLogsBucket`（`BucketOwnerPreferred`）
- **現状**: CloudFront.5（Security Hub）を満たすため legacy logging を採用、`BucketOwnerPreferred` + BPA Block の組合せで ACL を通す注釈もコード内に明示。`IncludeCookies: false` で JWT cookie 値が log に書き出されない設計。
- **推奨**: 現状維持。将来 v2 logging（CloudWatch / Firehose 直）に移行する場合は CloudFront.5 評価方式の差異を再確認。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/standard-logging-legacy-s3.html
  - https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/standard-logging.html

### [Info] Slack Bot / Notion / GitHub の secret rotation が未自動化（外部発行 secret は構造的）
- **カテゴリ**: Security / Application Security
- **対象**: `template.yaml:5-23`（Secrets Arn パラメータ群）/ cfn-guard `SECRETSMANAGER_ROTATION_ENABLED_CHECK` FAIL
- **現状**: 6 種類の secret は Parameter として参照のみ。`AWS::SecretsManager::RotationSchedule` が無い。Slack Bot / Notion / GitHub PAT は外部発行のため Lambda 自動 rotation 対象外。Claude OAuth は `TokenMonitorFunction` が実質的 rotation を担う。
- **推奨**: 「外部発行 secret は手動 rotation」「Claude OAuth は Lambda rotation」「Codex は ECS task writeback」の役割分担を ADR / docs にまとめる。可能なら Slack Bot を Slack OAuth 経由に切り替えれば rotation を見える化できる（中期改善）。
- **根拠**:
  - cfn-guard: `secretsmanager_rotation_enabled_check.guard`
  - auto-memory: `project_codex_auth_rotation_complete.md`
  - WA: Security / Application Security

### [Info] Multi-AZ / Region DR は対象外（明示的に dev 単一環境）
- **カテゴリ**: Reliability / Failure Management
- **対象**: スタック全体
- **現状**: ap-northeast-1 のみ、Multi-AZ は VPC subnet で確保（Public 1/2）、Region 冗長は無し。`docs/architecture.md` で「個人利用 dev 単一環境」と明記。
- **推奨**: 現状維持。将来 SLA 要件が出たら DynamoDB Global Tables / S3 CRR / Route53 failover を検討する旨を ADR に。
- **根拠**: プロジェクト固有判断（docs/architecture.md §9）

### [Info] S3 Object Lock 未設定（CKV/cfn-guard 検出だが用途不適）
- **カテゴリ**: Security / Data Protection
- **対象**: 3 S3 バケット
- **現状**: cfn-guard `S3_BUCKET_DEFAULT_LOCK_ENABLED` が FAIL。Object Lock は WORM（書込禁止/法的保留）用途で、本プロジェクトの「プロンプト記録 / フロント / CloudFront ログ」のいずれにも適合しない。
- **推奨**: 不要。compliance 要件（金融/医療/監査ログ WORM 化）が出るまで対象外として明示。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-overview.html

---

## 受容済みリスク（参考）

cfn-guard / checkov が機械的に拾った以下の指摘は、auto-memory の判断記録に基づき severity を昇格させない:

- **VPC Flow Logs 未導入** — 月 $5-10 増 vs プロジェクト総コスト $3-5/月のため受容（auto-memory: `feedback_logging_waf_cost_rejected.md`）
- **AWS WAF 未導入**（CloudFront / API Gateway 共に） — 月 $5-10 増のため受容（同上）
  - 関連 finding: checkov `CKV_AWS_68`（CloudFront Distribution should have WAF enabled）
- **ECS タスクの Public Subnet 配置** — NAT $32/月 or VPC Endpoint $36/月 vs プロジェクト総コスト $3-5/月のため受容（auto-memory: `feedback_ecs_private_subnet_rejected.md`）。`AgentSecurityGroup` egress 443/HTTPS 限定 + Fargate 短命タスクで補償。
  - 関連 finding: cfn-guard `EC2_SECURITY_GROUP_EGRESS_OPEN_TO_WORLD_RULE`（egress 0.0.0.0/0:443）/ `NO_UNRESTRICTED_ROUTE_TO_IGW`（Public Route）/ `SUBNET_AUTO_ASSIGN_PUBLIC_IP_DISABLED`（Public Subnet 1/2）/ checkov `CKV_AWS_117`（Lambda VPC、Lambda は VPC 外運用が前提）
- **S3 Replication 未設定** — 単一環境のため受容（cfn-guard `S3_BUCKET_REPLICATION_ENABLED` FAIL）

---

## 次アクション提案（優先度順）

1. **TokenMonitorFunction に SQS DLQ + CloudWatch Alarm を追加（最小侵襲・最高効果）**
   - 推奨理由: refresh 失敗が「気付かれずに 6 時間消える」構造的非対称（auto-memory `project_slack_feedback_requires_mention.md` と同形）の予防。Lambda 全体への DLQ 一括展開の前哨戦。
   - 工数: 30〜60 分（SQS リソース 1 + DLQ 設定 + Errors アラーム 1）。

2. **Lambda Globals に `Tracing: Active` + API Gateway 両方に AccessLog/Tracing を一括投入**
   - 推奨理由: ほぼゼロコストで可観測性が大幅改善。checkov `CKV_AWS_73 / 76 / 95` と Security Hub `APIGateway.1` / `.3` を同時解消。
   - 工数: 1〜2 時間（Globals 修正 + 2 API リソース修正 + AccessLog 用ロググループ 2 個追加）。

3. **専用 KMS CMK を 1 つ作って LogGroup と PromptsBucket / DynamoDB に適用 + S3 全バケットに DenyInsecureTransport**
   - 推奨理由: コードに encryption の意図を残すことで、将来の compliance スキャン（Security Hub `S3.4 / .5`、`CW.7`、`DynamoDB.1`）に耐える。月 $1 + 数 cent。
   - 工数: 2〜3 時間（KMS Key 1 個 + 各リソースで KmsKeyId 紐付け + 3 BucketPolicy に DenyInsecureTransport 追加）。

4. **AWS Budgets + Cost Anomaly Detection（無料）を IaC 化**
   - 推奨理由: ECS 起動ループ時の急騰検知。設計書 §10 試算が実支出と乖離した瞬間に気付ける。
   - 工数: 30 分。

5. **CloudWatch Alarms（Lambda Errors / ECS 失敗 / TokenMonitor 失敗）を IaC 化 + Reserved Concurrency を `TriggerFunction` / `TokenMonitor` に最低限設定**
   - 推奨理由: docs/architecture.md §8 と実装の乖離を埋める。reserved concurrency で他関数暴走時の `TriggerFunction` 影響を切り離す。
   - 工数: 2〜3 時間。
