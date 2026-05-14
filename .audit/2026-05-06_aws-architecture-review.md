# AWS Architecture Review — 2026-05-06

## サマリ
- Critical: 0 / High: 4 / Medium: 9 / Low: 6 / Info: 4
- 総合評価:
  - 個人利用 SAM スタックとしては十分な品質。ECR IMMUTABLE、DynamoDB PITR + Deletion Protection、S3 explicit Deny + OAC、ReadonlyRootFilesystem + 非 root + init-perms など、Defense-in-Depth が複数層で効いている。
  - 受容済みリスク（VPC Flow Logs / WAF 未導入、ECS public subnet 配置）はコスト合理性が文書化されており、本レビューでも昇格させない。
  - 一方で、CloudFront viewer 側 TLS minimum policy、API Gateway access log / X-Ray、Lambda DLQ・X-Ray、S3 ログバケットの SSL/暗号化、コスト予算アラームなど、ほぼゼロコストで埋められる薄い層が複数残存。優先度を High → Medium → Low の順で詰めれば本番運用品質に近づく。
- 対象: `/workspaces/Catch-Expander/template.yaml`（SAM スタック単体）
- 使用ツール:
  - `mcp__plugin_deploy-on-aws_awsiac__validate_cloudformation_template`（簡略テンプレートで合格確認）
  - `mcp__plugin_deploy-on-aws_awsknowledge__aws___search_documentation`（CloudFront / S3 / API Gateway / Lambda DLQ / Fargate KMS / TLS / Cost Anomaly の公式根拠収集）
  - リポジトリ静的読み込み（`docs/architecture.md`、`auto-memory`、`.steering/` 履歴）
  - Security Hub findings: AWS 認証情報がレビュー環境に無いため取得スキップ。Security Hub control ID（APIGateway.1/3、S3.4/5/9、CloudFront.5 等）は公式ドキュメントから引用で代替。
  - cfn-guard MCP（`check_cloudformation_template_compliance`）: 1 度呼び出したが MCP error -32000（Connection closed）で失敗。再試行せず docs ベースで補完。

> 補足: 受容済みリスク（VPC Flow Logs / AWS WAF / ECS Public Subnet 配置）は本レポート末尾の「受容済みリスク」欄に列挙し、severity を Critical/High に昇格させていない。

---

## 指摘事項

### [High] CloudFront viewer 側の minimum TLS 1.2+ ポリシーが未指定（既定値依存）
- **カテゴリ**: Security / Data Protection
- **対象**: `template.yaml:1334-1386` `FrontendDistribution.DistributionConfig`
- **現状**: `ViewerCertificate` を未指定で CloudFront default certificate（`*.cloudfront.net`）を使用しているが、`MinimumProtocolVersion` を含むカスタム TLS ポリシーを設定していない。default certificate 使用時 CloudFront は TLSv1（古いクライアント互換）でも viewer 接続を受け付けるため、ブラウザ側が TLSv1.0/1.1 をネゴシエートする経路が論理的に開く。
- **リスク・影響**: TLS 1.0/1.1 は AWS Endpoints 自体は 2024-02 で廃止されているが、CloudFront default certificate 使用ディストリビューションは別系統で古いプロトコルを残す。Slack OAuth で発行した JWT cookie はこのエッジに乗るため、低品質な暗号スイートでネゴシエートされる弱い経路を残すのは Data Protection 上望ましくない。
- **推奨**: 独自ドメインに移行し ACM 証明書 + `MinimumProtocolVersion: TLSv1.2_2021` を設定する。当面 default certificate を継続するなら、OutputFormat 観点で本件は「ドメイン移行時に TLS ポリシーを必ず指定する」を ADR / steering に明記する。CDK `SecurityPolicyProtocol` 列挙は `TLS_V1_2_2021` 以降が現行推奨。
  ```yaml
  ViewerCertificate:
    AcmCertificateArn: !Ref AcmCertificateArn
    SslSupportMethod: sni-only
    MinimumProtocolVersion: TLSv1.2_2021
  ```
- **根拠**:
  - https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/secure-connections-supported-viewer-protocols-ciphers.html (Security policy の挙動)
  - https://aws.amazon.com/blogs/security/tls-1-2-required-for-aws-endpoints/ (TLS 1.2 minimum 方針)
  - WA: Security / Data Protection — protect data in transit

### [High] 全 Lambda 関数で X-Ray Active Tracing が無効
- **カテゴリ**: Operational Excellence / Prepare（可観測性）
- **対象**: `template.yaml:51-55` Globals.Function、および全 `AWS::Serverless::Function`（TriggerFunction / TokenMonitor / Dashboard 13 関数）
- **現状**: `Globals.Function` に `Tracing: Active` 指定がなく、各関数も X-Ray を有効化していない。Slack イベント受領 → Lambda → ECS RunTask → DynamoDB → Slack/Notion/GitHub に至る分散トレースが取得できない。
- **リスク・影響**: 本番障害時に「Slack 受信から ACK までの遅延」「Lambda → DynamoDB のスローダウン」「Token Refresh の失敗位置」など、レイテンシ起点の RCA が CloudWatch Logs 横串の手作業に頼る。Dashboard observability の利用価値も限定的。
- **推奨**: SAM `Globals.Function.Tracing: Active` を設定し、API Gateway 側も `TracingEnabled: true` を有効にする（Security Hub APIGateway.3 と整合）。コストは月数百円〜数千円規模だが、本ワークロードのトレース量は微小。
  ```yaml
  Globals:
    Function:
      Runtime: python3.13
      Architectures: [arm64]
      Tracing: Active
  ```
- **根拠**:
  - https://docs.aws.amazon.com/lambda/latest/dg/services-xray.html
  - https://docs.aws.amazon.com/securityhub/latest/userguide/apigateway-controls.html (APIGateway.3)
  - WA: Operational Excellence / Prepare

### [High] 非同期 Lambda（TokenMonitorFunction）に DLQ / OnFailure destination が無い
- **カテゴリ**: Reliability / Failure Management
- **対象**: `template.yaml:698-741` `TokenMonitorFunction`
- **現状**: EventBridge schedule (`rate(12 hours)`) からの呼び出しは非同期。失敗時 Lambda は内部キューで最大 2 回リトライし、6 時間で discard する。`DeadLetterQueue` も `EventInvokeConfig.DestinationConfig.OnFailure` も未設定のため、refresh 失敗が起きても証跡が残らない。
- **リスク・影響**: Claude OAuth refresh が連続失敗していてもメッセージがロストし、唯一の検知経路は「Slack 通知の到達」だけになる。auto-memory の「メンションなし Slack feedback は Lambda 入口で破棄され DynamoDB 到達前に消える」と同形の構造的非対称が refresh 経路でも起きうる。
- **推奨**: SQS DLQ または `EventInvokeConfig` の `OnFailure` destination（SNS / SQS / EventBridge）を追加。最低限 SQS 1 本（月 0.4 USD 未満）。
  ```yaml
  TokenMonitorFunction:
    Properties:
      DeadLetterQueue:
        Type: SQS
        TargetArn: !GetAtt TokenMonitorDlq.Arn
  ```
- **根拠**:
  - https://docs.aws.amazon.com/lambda/latest/dg/invocation-async.html
  - https://aws.amazon.com/blogs/compute/robust-serverless-application-design-with-aws-lambda-dlq/
  - WA: Reliability / Failure Management — Capture errors at boundaries

### [High] CloudWatch Logs の各ロググループに KMS 暗号化キーが指定されていない
- **カテゴリ**: Security / Data Protection
- **対象**: `template.yaml:399-403` `AgentLogGroup`、および全 Lambda 関数の暗黙的 LogGroup
- **現状**: `AgentLogGroup` は `LogGroupName` と `RetentionInDays: 30` のみ。`KmsKeyId` が無いため CloudWatch Logs はサービス所有鍵で暗号化される（明示的 CMK ではない）。Lambda 各関数は LogGroup を SAM 暗黙生成に任せており、こちらも CMK 無し。サブエージェントが Slack/Notion/GitHub への入力を書き込むため、ログには query テキスト・URL・user_id 等の PII 候補が混入する可能性がある。
- **リスク・影響**: 鍵ローテーションやアクセス監査の独立性が保てない。今後 Security Hub / CIS Benchmark を有効化した際に違反指摘される（CW.7 系の前段階に相当）。
- **推奨**: AWS-managed key で十分であれば `aws/logs` を、CMK が要件なら専用 KMS Key を作成して `KmsKeyId` を設定。SAM では `LogGroup` を明示的にリソース化し、Function に紐付け。少なくとも `AgentLogGroup` は単純に追加できる。
  ```yaml
  AgentLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: /ecs/catch-expander-agent
      RetentionInDays: 30
      KmsKeyId: !GetAtt LogsKey.Arn  # 専用 KMS Key を別途定義
  ```
- **根拠**:
  - https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html
  - WA: Security / Data Protection — encryption at rest

### [Medium] SlackApi（REST API）に access log / X-Ray tracing / execution log が未設定
- **カテゴリ**: Operational Excellence / Operate（可観測性）
- **対象**: `template.yaml:620-631` `SlackApi`
- **現状**: `MethodSettings` に throttle のみ設定。`AccessLogSetting`（destination + format）も `TracingEnabled` も `LoggingLevel` も未設定。Slack 署名検証失敗・タイムアウト・5xx の根本調査が CloudWatch のメトリクス数値しか手掛かりが無い。
- **リスク・影響**: Slack 連携の異常事象（メンションなし破棄、署名失敗、IPv6 経路問題等）の RCA が困難。Security Hub `APIGateway.1`（execution logging 必須）と `APIGateway.3`（X-Ray）の両方に違反。
- **推奨**: access log を CloudWatch Logs ロググループへ JSON 形式で吐く。最低 `requestId`、`extendedRequestId`、`httpMethod`、`status`、`integrationLatency` を含める。
  ```yaml
  SlackApi:
    Properties:
      TracingEnabled: true
      AccessLogSetting:
        DestinationArn: !GetAtt SlackApiAccessLogGroup.Arn
        Format: '{"requestId":"$context.requestId","ip":"$context.identity.sourceIp","status":"$context.status",...}'
      MethodSettings:
        - HttpMethod: "*"
          ResourcePath: "/*"
          LoggingLevel: ERROR
          DataTraceEnabled: false
          MetricsEnabled: true
          ThrottlingBurstLimit: 20
          ThrottlingRateLimit: 10
  ```
- **根拠**:
  - https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-logging.html
  - https://docs.aws.amazon.com/securityhub/latest/userguide/apigateway-controls.html (APIGateway.1, APIGateway.3)
  - WA: Operational Excellence / Operate

### [Medium] DashboardApi（HTTP API）に access log / DefaultRouteSettings 未設定
- **カテゴリ**: Operational Excellence / Operate
- **対象**: `template.yaml:773-789` `DashboardApi`
- **現状**: `AccessLogSettings` も `DefaultRouteSettings.ThrottlingBurstLimit/ThrottlingRateLimit` も未設定。/api/* 配下のメトリクス（`get_*` Lambda 群の遅延、認証失敗）を API レイヤで横断把握できない。
- **リスク・影響**: 認証失敗・5xx の頻度や、特定エンドポイントへのバースト負荷が把握できない。CloudFront 標準ログから推定するしかなく粒度が粗い。HTTP API の sane な default も throttle なし（無制限）なので、Authorizer 突破時の暴走耐性が弱い。
- **推奨**: HTTP API でも access log と route-level throttle を設定。
  ```yaml
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
  - https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-logging.html
  - WA: Operational Excellence / Operate; Reliability / Foundations (quotas)

### [Medium] S3 バケットに既定の SSE 設定（`BucketEncryption`）が明示されていない
- **カテゴリ**: Security / Data Protection
- **対象**: `template.yaml:1234-1247` `PromptsBucket`、`1291-1299` `FrontendBucket`、`1307-1323` `CloudFrontLogsBucket`
- **現状**: 3 バケットいずれも `BucketEncryption` プロパティ未指定。
- **リスク・影響**: 2023-01 以降 S3 は自動的に SSE-S3 を適用するため実害は限定的（"data is encrypted at rest by default"）。ただし IaC 上に encryption の意図が記述されていないと、(a) 将来 KMS CMK へ切り替える時の差分が見えにくい、(b) Security Hub `S3.4`（SSE 有効）等の compliance スキャンで「設定が確認できない」評価になる場合がある、(c) `BucketKeyEnabled` などコスト最適オプションも未設定のまま。
- **推奨**: 明示的に `BucketEncryption` を記述する（特に `CloudFrontLogsBucket` と `PromptsBucket`）。プロンプト記録は機密度が中〜高なので KMS CMK + BucketKey 化を検討。
  ```yaml
  PromptsBucket:
    Properties:
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - BucketKeyEnabled: true
            ServerSideEncryptionByDefault:
              SSEAlgorithm: aws:kms
              KMSMasterKeyID: !Ref PromptsKmsKey
  ```
- **根拠**:
  - https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-s3-bucket-serversideencryptionrule.html
  - https://aws.amazon.com/blogs/aws/amazon-s3-encrypts-new-objects-by-default/
  - WA: Security / Data Protection

### [Medium] S3 全バケットに HTTPS-only enforcement (`aws:SecureTransport=false` Deny) が無い
- **カテゴリ**: Security / Data Protection（転送時暗号化）
- **対象**: `template.yaml:1252-1265` `PromptsBucketPolicy`、`1390-1412` `FrontendBucketPolicy`、`CloudFrontLogsBucket`（policy 自体無し）
- **現状**: 既存 BucketPolicy は GetObject の許可/拒否範囲制御のみで、HTTP（非 TLS）アクセスを Deny する Statement が無い。`CloudFrontLogsBucket` は BucketPolicy 自体が存在しない。
- **リスク・影響**: AWS Foundational Security Best Practices `S3.5`（"S3 general purpose buckets should require requests to use SSL"）の指摘対象。CloudFront ログは PII（IP、UA、URL クエリ）を含むため転送時 TLS 強制は推奨。
- **推奨**: 全 S3 BucketPolicy に以下の Deny Statement を追加。
  ```yaml
  - Sid: DenyInsecureTransport
    Effect: Deny
    Principal: "*"
    Action: s3:*
    Resource:
      - !GetAtt PromptsBucket.Arn
      - !Sub ${PromptsBucket.Arn}/*
    Condition:
      Bool:
        aws:SecureTransport: false
  ```
- **根拠**:
  - https://docs.aws.amazon.com/securityhub/latest/userguide/s3-controls.html (S3.5)
  - WA: Security / Data Protection

### [Medium] AgentTaskExecutionRole に CodexAuthSecret の権限が無い
- **カテゴリ**: Reliability / Workload Architecture（起動時失敗の検出）
- **対象**: `template.yaml:419-431` `AgentTaskExecutionRole.Policies.SecretsManagerAccess`
- **現状**: `AgentTaskExecutionRole`（ECS が起動時に環境変数 `valueFrom: <secret-arn>` で secret を解決する用途のロール）の許可 secret 一覧に `CodexAuthSecretArn` が含まれていない。一方 `AgentTaskRole`（task が runtime に取得するロール）には含まれている。
- **リスク・影響**: 現状 TaskDefinition は `Environment` で SECRET_ARN（文字列）だけ渡し、実際の取得は `AgentTaskRole` で task 内 main.py が行う構成のため即座の障害は出ない。しかし将来 `Secrets` セクションで execution role 経由の解決に切り替えたとき、Codex のみ取得失敗しタスクが起動しない症状が出る。差異が「意図的か」が IaC 上で読み取れない。
- **推奨**: 現運用に合わせて execution role 側は不要のままにするのなら、この対称性の差を IaC コメントで明示する。あるいは将来の切替を見据えて Codex も execution role に追加して統一。
- **根拠**: WA: Reliability — design for failure; 経験則（auto-memory: `project_codex_auth_rotation_complete.md`）

### [Medium] ECS Cluster の Container Insights が `disabled`
- **カテゴリ**: Operational Excellence / Operate
- **対象**: `template.yaml:391-397` `AgentCluster`
- **現状**: `containerInsights: disabled`。CloudWatch Container Insights を明示的に切っている。
- **リスク・影響**: ECS タスクの CPU/メモリ使用率、タスク状態変化、ステータス遷移が CloudWatch Metrics で集約されない。コンテナが OOM したか、CPU が張り付いていたかの事後判定が困難。
- **推奨**: 個人利用かつ月数十タスクの規模では Container Insights は ~$0.5/月程度に収まる見込み。最低限 `enhanced` ではなく旧 `enabled` に切り替え、必要時に詳細化する。コスト懸念なら steering / ADR にこの判断を明記し、`disabled` の理由を IaC コメントで残す。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cloudwatch-container-insights.html
  - WA: Operational Excellence / Operate

### [Medium] AgentTaskRole の DynamoDB アクションスコープが Index ARN を一部しか含めていない
- **カテゴリ**: Security / Identity & Access Management
- **対象**: `template.yaml:445-463` `AgentTaskRole.Policies.DynamoDBAccess`
- **現状**: `WorkflowExecutionsTable` は `/index/user-id-index` を resource に含めているが、`DashboardEventsTable` の 3 GSI（`gsi_global_timestamp`、`gsi_status_timestamp`、`gsi_event_type_timestamp`）に対する `dynamodb:Query` 権限が含まれていない。一方 Lambda 群（dashboard 系）は同 GSI を使うため別途許可済み。Agent 自身が GSI を使わない設計なら問題ないが、現状コメントにその意図が無い。
- **リスク・影響**: 将来エージェントから GSI Query を呼ぶ機能が増えた際に不可解な `AccessDeniedException` が runtime で出る。逆に意図的に絞っているなら、最小権限の根拠が IaC 上に残らない。
- **推奨**: 現状の意図（agent は GSI を使わない）が正なら、`AgentTaskRole` のコメントにその旨を残す。あるいは、後日 GSI 利用が発生した際に追加する手順を steering に紐付け。
- **根拠**:
  - https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/specifying-conditions.html
  - WA: Security / IAM — least privilege

### [Medium] AgentTaskRole の S3 PutObject 範囲は十分絞られているが、Object 削除/上書き経路の権限が暗黙
- **カテゴリ**: Security / IAM
- **対象**: `template.yaml:494-500` `AgentTaskRole.Policies.PromptsS3WriteAccess`
- **現状**: `s3:PutObject` を `prompts/*` 配下のみに限定しており適切。ただし将来「失敗ファイルの roll-back」「subagent_io の overwrite」を要求した場合、現在は明示的に `s3:DeleteObject` / `s3:AbortMultipartUpload` が無いことを忘れて再追加する経路に脆弱。
- **リスク・影響**: 短期的なリスクは無し。将来の権限追加時に PutObject と同じ Resource スコープが書かれない可能性がある（ヒューマンミス由来の権限拡張）。
- **推奨**: 当面のままで良い。将来追加する場合は `Resource: !Sub ${PromptsBucket.Arn}/prompts/*` を厳守する旨を steering に明記。
- **根拠**: WA: Security / IAM — least privilege

### [Medium] DashboardListExecutionsFunction に DefaultAuthorizer NONE 化されていないが、Cookie ヘッダ依存で 5 分キャッシュ
- **カテゴリ**: Security / Application Security
- **対象**: `template.yaml:773-789` `DashboardApi.Auth.DashboardAuthorizer`
- **現状**: `ReauthorizeEvery: 300`（5 分キャッシュ）+ `Identity: { Headers: [Cookie] }`。Cookie 値ハッシュ単位でキャッシュされるため、JWT を即時無効化（ログアウト）しても 5 分間は authorizer がキャッシュ判定で通す。
- **リスク・影響**: 緊急ログアウト・トークン失効・侵害対応で「即時遮断」が出来ない。
- **推奨**: ログアウトは認可の即時無効化を優先するなら `ReauthorizeEvery: 0`（毎回 invoke）にし、コスト低減を優先するなら現状維持で「即時遮断は KMS Key 退役 / JWT 署名鍵ローテーション」で対処する旨を文書化。
  ```yaml
  Identity:
    ReauthorizeEvery: 0
    Headers: [Cookie]
  ```
- **根拠**:
  - https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-lambda-authorizer.html
  - WA: Security / Incident Response

### [Low] DynamoDB Tables にコスト配分タグ・Owner タグが無い
- **カテゴリ**: Cost Optimization / Cloud Financial Management
- **対象**: `template.yaml:152-333`（全 DynamoDB Table）
- **現状**: VPC / Subnet には `Name` タグがあるが、DynamoDB / Lambda / S3 / ECS Cluster / ECR にコスト配分タグ（`Project`, `Env`, `Owner`）が無い。
- **リスク・影響**: 個人利用 1 アカウントなら影響少。将来複数プロジェクトを 1 アカウントに同居させたとき、コスト按分の根拠が無くなる。
- **推奨**: SAM `Globals` に `Tags` を入れるか、template 全体で共通タグ Macro を導入。
  ```yaml
  Globals:
    Function:
      Tags:
        Project: catch-expander
        Env: dev
        Owner: shintaro-abe
  ```
- **根拠**: WA: Cost Optimization / Cloud Financial Management — cost allocation tags

### [Low] AWS Budgets / Cost Anomaly Detection が IaC で定義されていない
- **カテゴリ**: Cost Optimization / Cloud Financial Management
- **対象**: スタック全体
- **現状**: `AWS::Budgets::Budget` も `AWS::CE::AnomalyMonitor` も無い。`docs/architecture.md §10` で「月 $5.85（Max 除く）」と見積もり済みだが、実支出のアラームは仕掛けられていない。
- **リスク・影響**: ECS タスクが暴走（Claude OAuth が refresh 失敗で起動ループ等）して急に十数 USD/日の支出に化けた場合、検知が遅れる。
- **推奨**: 月 $20 / 日 $1 の予算アラーム + Cost Anomaly Detection（無料）を IaC 化。Slack 通知チャンネルがあれば SNS 経由で再利用可能。
  ```yaml
  MonthlyBudget:
    Type: AWS::Budgets::Budget
    Properties:
      Budget:
        BudgetName: catch-expander-monthly
        BudgetLimit: { Amount: "20", Unit: USD }
        TimeUnit: MONTHLY
        BudgetType: COST
      NotificationsWithSubscribers:
        - Notification: { ... }
          Subscribers: [...]
  ```
- **根拠**:
  - https://aws.amazon.com/aws-cost-management/aws-cost-anomaly-detection/
  - WA: Cost Optimization / Cloud Financial Management

### [Low] CloudWatch Alarms が IaC で定義されていない
- **カテゴリ**: Operational Excellence / Operate
- **対象**: スタック全体
- **現状**: `docs/architecture.md §8` に Alarm 設計（Lambda エラー率 > 10%、ECS 失敗 > 0、Maxプラン上限接近）が記述されているが、`AWS::CloudWatch::Alarm` リソースは template に存在しない。
- **リスク・影響**: 設計書と実装の乖離。SLO/監視の最低ライン（Lambda エラー、ECS タスク失敗）が未実装。
- **推奨**: 設計書記載の 3 アラームをまず IaC 化し、SNS topic + Slack 通知に紐付け。Token Monitor の失敗カウントもアラーム化。
- **根拠**: WA: Operational Excellence / Operate; プロジェクト固有（docs/architecture.md §8）

### [Low] CloudFrontLogsBucket に SSL enforcement / 暗号化 / Object Ownership 以外の防護が薄い
- **カテゴリ**: Security / Data Protection
- **対象**: `template.yaml:1307-1323` `CloudFrontLogsBucket`
- **現状**: ACL ベース legacy logging のため `BucketOwnerPreferred` + BPA Block で正しく構成済み。一方 BucketPolicy が無く、SSE 既定指定も無い。
- **リスク・影響**: 軽微。S3 default SSE-S3 で暗号化はされるが、SSL enforcement と policy ベースの最小化が無い。
- **推奨**: 上の Medium「S3 SSE 明示 / SSL enforcement」と合わせて対応。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/standard-logging-legacy-s3.html
  - WA: Security / Data Protection

### [Low] DashboardSlackOAuthSecret は手動 populate のため drift 検知不能
- **カテゴリ**: Operational Excellence / Prepare
- **対象**: `template.yaml:347-355` `DashboardSlackOAuthSecret`
- **現状**: `SecretString: "{}"` で初期化し、運用者がコンソール / CLI で書き換え。CloudFormation drift detection を回したとき、SecretString が `{}` から変わって検出される運用上の挙動を持つ。
- **リスク・影響**: 軽微。drift detection 結果が常に「drift」になりノイズ化する可能性。
- **推奨**: `IgnoreChanges: [SecretString]` 相当の Update Replace ポリシーを文書化、または CFN drift 対象から外す運用ガイダンスを steering に書く。
- **根拠**: WA: Operational Excellence / Prepare — IaC drift control

### [Low] ECS Fargate ephemeral storage に CMK 指定なし（AWS-owned key）
- **カテゴリ**: Security / Data Protection
- **対象**: `template.yaml:391-397` `AgentCluster`
- **現状**: `ManagedStorageConfiguration.fargateEphemeralStorageKmsKeyId` 未指定。Fargate 1.4.0+ は AWS-owned key で ephemeral storage を暗号化するが、CMK ではない。プロンプトや Codex auth.json が `/tmp` `/home/appuser` に短時間存在するため対象。
- **リスク・影響**: 個人利用かつタスク終了時に scratch volume が消えるため実害は極小。compliance 要件（HIPAA / 金融）が出た時に CMK 化が必要になる程度。
- **推奨**: 当面 AWS-owned key で問題なし。compliance 要件が発生した時点で `ManagedStorageConfiguration` を追加。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-create-storage-key.html
  - WA: Security / Data Protection

### [Low] ECR ライフサイクルが「直近 5 イメージ」のみ。SHA タグの整合
- **カテゴリ**: Cost Optimization / Optimize Over Time
- **対象**: `template.yaml:360-386` `AgentRepository.LifecyclePolicy`
- **現状**: `countType: imageCountMoreThan, countNumber: 5`。IMMUTABLE と整合した運用で、ECR 容量コストは月 $0.05 程度。
- **リスク・影響**: 短期 rollback で「直前直前」の 2 SHA に戻したい時、5 世代しか残らないため重なって push が起きると古い image が消える可能性。
- **推奨**: 5 → 10 程度に緩めるか、`tagPrefixList: ["v"]` 指定で「v タグ付きはより長期保存」のような階層を入れる。
- **根拠**: WA: Cost Optimization

### [Info] ARM64 + Fargate + Python 3.13 の選択は Sustainability に整合
- **カテゴリ**: Sustainability / Hardware & Services
- **対象**: `Globals.Function.Architectures: arm64`、`AgentTaskDefinition.RuntimePlatform.CpuArchitecture: ARM64`
- **現状**: 全 Lambda と ECS Task が ARM64（Graviton）。CloudFront `PriceClass_100` でエッジロケーション抑制。CloudFront `http2and3`、ECR の image expire、DynamoDB TTL もすべてサステナビリティ・コスト両方で整合。
- **推奨**: 現状維持。新機能追加時も ARM64 を default に。
- **根拠**: WA: Sustainability / Hardware & Services; Cost Optimization

### [Info] 受容済みリスクと CloudFront standard logging（legacy）採用の整合
- **カテゴリ**: Security / Detection
- **対象**: `template.yaml:1342-1347` `FrontendDistribution.Logging`
- **現状**: CloudFront.5（Security Hub）を満たすため legacy logging を採用、`BucketOwnerPreferred` + BPA Block の組合せで ACL を通す注釈もコード内に記述済み。
- **推奨**: 現状維持。将来 v2 logging（CloudWatch / Firehose 直）に移行する場合は CloudFront.5 評価方式の差異を再確認。
- **根拠**:
  - https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/standard-logging-legacy-s3.html
  - https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/standard-logging.html

### [Info] Slack Bot Token 等の secret rotation がスタック上に未定義
- **カテゴリ**: Security / Application Security
- **対象**: `template.yaml:5-23`（Secrets Arn パラメータ群）
- **現状**: 6 種類の secret はパラメータ参照のみで、`AWS::SecretsManager::RotationSchedule` が無い。Slack Bot Token / Notion Token / GitHub PAT は外部発行のため自動 rotation 困難。Claude OAuth は Token Monitor Lambda が実質 rotation を担っている。
- **リスク・影響**: 既知。実害は無い。
- **推奨**: 「外部発行 secret は手動 rotation」「Claude OAuth は Lambda rotation」の役割分担を ADR / docs にまとめる。可能なら Slack Bot Token を Slack OAuth 経由に切り替えれば rotation 自動化が見える化できる（中期改善）。
- **根拠**: WA: Security / Application Security; プロジェクト固有

### [Info] Multi-AZ / Region DR は対象外（明示的に dev 単一環境）
- **カテゴリ**: Reliability / Failure Management
- **対象**: スタック全体
- **現状**: ap-northeast-1 のみ、Multi-AZ は VPC subnet で確保（PublicSubnet1/2）、Region 冗長は無し。`docs/architecture.md` で「個人利用 dev 単一環境」と明記。
- **推奨**: 現状維持。将来 SLA 要件が出たら DynamoDB Global Tables / S3 CRR / Route53 failover を検討する旨だけ ADR に残す。
- **根拠**: プロジェクト固有判断（docs/architecture.md §9）

---

## 受容済みリスク（参考）
- VPC Flow Logs 未導入 — 月 $5-10 増 vs プロジェクト総コスト $3-5/月のため受容（auto-memory: `feedback_logging_waf_cost_rejected.md`）
- AWS WAF 未導入 — 月 $5-10 増 vs プロジェクト総コスト $3-5/月のため受容（同上）
- ECS タスクの Public Subnet 配置 — NAT $32/月 or VPC Endpoint $36/月 vs プロジェクト総コスト $3-5/月のため受容（auto-memory: `feedback_ecs_private_subnet_rejected.md`）。本テンプレでは Security Group egress 443/HTTPS 限定 + Fargate 短命タスクで補償。

---

## 次アクション提案（優先度順）

1. **Lambda 全関数に X-Ray Active Tracing を有効化** + **API Gateway 両方に AccessLog/Tracing 設定**
   - 推奨理由: ほぼゼロコストで観測性が大幅改善。Security Hub `APIGateway.1`/`APIGateway.3` の指摘もまとめて解消。
   - 工数: 1〜2 時間（template.yaml の Globals + 2 API リソース修正、AccessLog 用ロググループ追加）。
2. **TokenMonitorFunction に SQS DLQ を追加**
   - 推奨理由: refresh 失敗が「気付かれずに 6 時間消える」構造的非対称（auto-memory の Slack feedback と同形）の予防。
   - 工数: 30 分（SQS リソース 1 つ + DLQ 設定）。
3. **CloudWatch Logs / S3 の暗号化と HTTPS 強制を IaC で明示**
   - 推奨理由: コードに意図を残すことで、将来の compliance スキャン（Security Hub `S3.5`、`CW.7` 系）に耐える。LogGroup KMS は専用 CMK でも AWS-managed key でも良い。
   - 工数: 1〜2 時間（KMS Key 1〜2 個 + 各 BucketPolicy に DenyInsecureTransport 追加）。
4. **AWS Budgets + Cost Anomaly Detection を IaC 化（月 $20 / 日 $1 アラーム）**
   - 推奨理由: ECS 起動ループ時の急騰検知。設計書の §10 試算が実支出と乖離した時点で気付ける仕組み。
   - 工数: 30 分。
5. **CloudWatch Alarms（Lambda エラー率 / ECS 失敗数 / Token Monitor 失敗）を IaC 化**
   - 推奨理由: docs/architecture.md §8 の設計と実装の乖離を埋める。observability dashboard と並行して、最低限のアラート経路を SNS → Slack で確立。
   - 工数: 2〜3 時間。
