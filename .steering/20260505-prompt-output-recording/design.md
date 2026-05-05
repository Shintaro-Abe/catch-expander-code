# 設計書 — サブエージェントのプロンプト・出力記録

> 関連: [`requirements.md`](./requirements.md) / [`tasklist.md`](./tasklist.md)

---

## 1. 全体構成

```
orchestrator.py
  ↓ call_claude / call_codex 呼び出し直後
PromptRecorder.record(subagent, index, prompt, output)
  ↓ boto3 s3.put_object (best-effort)
S3: catch-expander-prompts-{AccountId}
  prompts/{execution_id}/researcher_{step_id}.json
  prompts/{execution_id}/generator_0.json
  prompts/{execution_id}/reviewer_{loop}_eval.json   ← レビュー評価 (call_codex)
  prompts/{execution_id}/reviewer_{loop}_fix.json    ← 修正試行 (call_claude)
                ↕
Dashboard API: GET /executions/{id}/subagent-io
  ↓ s3.list_objects_v2 + s3.get_object
  ↓ JSON レスポンス
Frontend: ExecutionDetail.tsx のグリッド下・全幅セクション
```

---

## 2. 新規モジュール: `src/observability/prompt_recorder.py`

### 2.1 設計方針

EventEmitter と同じ best-effort パターンを採用する。

| 項目 | EventEmitter | PromptRecorder |
|------|-------------|----------------|
| 書き込み先 | DynamoDB | S3 |
| データサイズ | <1KB | 50〜200KB |
| env 未設定 | graceful skip | graceful skip |
| 例外伝播 | しない | しない |

### 2.2 クラス仕様

```python
class PromptRecorder:
    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        self._bucket = os.environ.get("PROMPTS_BUCKET", "")
        self._s3 = boto3.client("s3") if self._bucket else None

    def record(
        self,
        subagent: str,   # "researcher" / "generator" / "reviewer_eval" / "reviewer_fix"
        index: str,      # researcher: step_id, generator: "0", reviewer_*: "0"/"1"
        prompt: str,
        output: str,
    ) -> None:
        """best-effort: PROMPTS_BUCKET 未設定または失敗しても例外を伝播させない"""
```

### 2.3 S3 キー設計

```
prompts/{execution_id}/researcher_{step_id}.json
prompts/{execution_id}/generator_0.json
prompts/{execution_id}/reviewer_{loop}_eval.json   ← call_codex によるレビュー評価
prompts/{execution_id}/reviewer_{loop}_fix.json    ← call_claude による修正試行
```

- `reviewer_fix` はレビューが通過しなかった場合のみ存在する（最大 `MAX_REVIEW_LOOPS` 件）
- S3 キーは execution_id + subagent + index から一意に決定できるため、**DynamoDB へのキー保存は行わない**
- Dashboard API は `prompts/{execution_id}/` プレフィックスでリストアップする

### 2.4 各 JSON ファイルの構造

```json
{
  "subagent": "reviewer_eval",
  "index": "0",
  "prompt": "...(全文)...",
  "output": "...(全文)...",
  "recorded_at": "2026-05-05T10:00:00.000Z"
}
```

---

## 3. orchestrator.py への統合

### 3.1 PromptRecorder インスタンス生成

`Orchestrator.__init__` 内で EventEmitter と並べて生成する。

```python
from src.observability.prompt_recorder import PromptRecorder

self._prompt_recorder = PromptRecorder(execution_id)
```

### 3.2 リサーチャー（`_execute_research` 内）

```python
raw = call_claude(prompt, allowed_tools=["WebSearch", "WebFetch"], ...)
result = _parse_claude_response(raw)
self._prompt_recorder.record("researcher", step_id, prompt, raw)  # 追加
```

### 3.3 ジェネレーター・テキスト成果物経路

```python
gen_raw = call_claude(gen_prompt, emitter=self._emitter, ...)
self._prompt_recorder.record("generator", "0", gen_prompt, gen_raw)  # 追加
```

### 3.4 ジェネレーター・コード成果物経路（`call_claude_with_workspace`）

```python
raw_stdout, files, outcome = call_claude_with_workspace(prompt, ...)
self._prompt_recorder.record("generator", "0", prompt, raw_stdout)  # 追加
```

### 3.5 レビュアー・評価（`_run_review_loop` 内）

```python
raw = call_codex(review_prompt, emitter=self._emitter, ...)
self._prompt_recorder.record("reviewer_eval", str(loop), review_prompt, raw)  # 追加
```

### 3.6 レビュアー・修正試行（`_run_review_loop` 内）

```python
fix_raw = call_claude(fix_prompt, emitter=self._emitter, ...)
self._prompt_recorder.record("reviewer_fix", str(loop), fix_prompt, fix_raw)  # 追加
```

---

## 4. インフラ変更（`template.yaml`）

### 4.1 新規 S3 バケット

```yaml
PromptsBucket:
  Type: AWS::S3::Bucket
  Properties:
    BucketName: !Sub catch-expander-prompts-${AWS::AccountId}
    PublicAccessBlockConfiguration:
      BlockPublicAcls: true
      BlockPublicPolicy: true
      IgnorePublicAcls: true
      RestrictPublicBuckets: true
    LifecycleConfiguration:
      Rules:
        - Id: expire-after-5-years
          Status: Enabled
          ExpirationInDays: 1825
```

### 4.2 AgentTaskRole に S3 書き込み権限を追加

```yaml
- PolicyName: PromptsS3WriteAccess
  PolicyDocument:
    Version: "2012-10-17"
    Statement:
      - Effect: Allow
        Action: s3:PutObject
        Resource: !Sub ${PromptsBucket.Arn}/prompts/*
```

### 4.3 ECS タスク環境変数に追加

```yaml
- Name: PROMPTS_BUCKET
  Value: !Ref PromptsBucket
```

### 4.4 新規 Lambda: `GetSubagentIoFunction`

`S3ReadPolicy` は `s3:GetObject` のみ付与するため `list_objects_v2` に必要な
`s3:ListBucket` が不足する。カスタムインラインポリシーで両権限を明示する。

```yaml
GetSubagentIoFunction:
  Type: AWS::Serverless::Function
  Properties:
    FunctionName: catch-expander-get-subagent-io
    Handler: app.lambda_handler
    CodeUri: src/dashboard_api/get_subagent_io/
    Timeout: 30
    Environment:
      Variables:
        PROMPTS_BUCKET: !Ref PromptsBucket
    Policies:
      - Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Action: s3:ListBucket
            Resource: !GetAtt PromptsBucket.Arn
            Condition:
              StringLike:
                s3:prefix: "prompts/*"
          - Effect: Allow
            Action: s3:GetObject
            Resource: !Sub ${PromptsBucket.Arn}/prompts/*
    Events:
      Api:
        Type: HttpApi
        Properties:
          ApiId: !Ref DashboardHttpApi
          Method: GET
          Path: /executions/{execution_id}/subagent-io
          Auth:
            Authorizer: LambdaAuthorizer
```

---

## 5. Dashboard API: `get_subagent_io`

### 5.1 エンドポイント

```
GET /executions/{execution_id}/subagent-io
```

### 5.2 レスポンス仕様

```json
{
  "data": {
    "execution_id": "exec-20260501-001",
    "records": [
      {
        "subagent": "researcher",
        "index": "step-001",
        "prompt": "...",
        "output": "...",
        "recorded_at": "2026-05-01T10:00:00.000Z"
      },
      {
        "subagent": "reviewer_eval",
        "index": "0",
        "prompt": "...",
        "output": "...",
        "recorded_at": "2026-05-01T10:05:00.000Z"
      },
      {
        "subagent": "reviewer_fix",
        "index": "0",
        "prompt": "...",
        "output": "...",
        "recorded_at": "2026-05-01T10:06:00.000Z"
      }
    ]
  }
}
```

- データが 0 件（本機能デプロイ前の実行）: `"records": []` を返す（404 ではなく 200）
- 表示順: researcher → generator → reviewer_eval/reviewer_fix（S3 キー名でソート）

### 5.3 実装フロー

1. `s3.list_objects_v2(Bucket=bucket, Prefix=f"prompts/{execution_id}/")`
2. 各オブジェクトを `ThreadPoolExecutor` で並列 `s3.get_object` して JSON パース
3. subagent 種別順（researcher < generator < reviewer_eval < reviewer_fix）でソート
4. レスポンス返却

並列取得により逐次実行時のネットワーク往復遅延を回避する。
件数は最大でも researcher(N) + generator(1) + reviewer_eval(M) + reviewer_fix(M) 程度であり、
合計レスポンスサイズは Lambda HTTP API の上限（6MB）に対して十分な余裕がある。

---

## 6. フロントエンド

### 6.1 配置

`ExecutionDetail.tsx` の `<div className="grid ...">` の閉じタグ直後、全幅カードとして追加する。

```tsx
{/* 既存グリッド */}
<div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
  ...
</div>

{/* 追加: サブエージェント入出力（全幅） */}
<SubagentIOSection executionId={executionId!} />
```

### 6.2 データ取得

```tsx
const ioQ = useQuery({
  queryKey: ["subagentIO", executionId],
  queryFn: () => endpoints.subagentIO(executionId!),
  enabled: !!executionId,
  staleTime: 60_000,
})
```

### 6.3 表示構造

```
Card: サブエージェント入出力
  ├── records が空 → "記録データなし（この機能追加前の実行）"
  └── records あり
        ├── リサーチャー（複数ステップ）
        │     ├── ステップ名
        │     ├── [プロンプトを表示 ▶] → <pre> 展開
        │     └── [出力を表示 ▶] → <pre> 展開
        ├── ジェネレーター
        │     ├── [プロンプトを表示 ▶]
        │     └── [出力を表示 ▶]
        └── レビュアー（複数ループ）
              └── ループ 1
                    ├── レビュー評価
                    │     ├── [プロンプトを表示 ▶]
                    │     └── [出力を表示 ▶]
                    └── 修正試行（存在する場合のみ）
                          ├── [プロンプトを表示 ▶]
                          └── [出力を表示 ▶]
```

- 展開ボタンの初期状態: 閉じている
- プロンプト・出力は `<pre>` で等幅フォント表示

### 6.4 型定義（`frontend/src/api/types.ts` に追加）

```ts
export interface SubagentIORecord {
  subagent: "researcher" | "generator" | "reviewer_eval" | "reviewer_fix"
  index: string
  prompt: string
  output: string
  recorded_at: string
}

export interface SubagentIOResponse {
  data: {
    execution_id: string
    records: SubagentIORecord[]
  }
}
```

---

## 7. 変更ファイル一覧

| ファイル | 変更種別 |
|---------|---------|
| `src/observability/prompt_recorder.py` | 新規作成 |
| `src/agent/orchestrator.py` | record 呼び出し追加（4箇所） |
| `src/dashboard_api/get_subagent_io/app.py` | 新規作成 |
| `template.yaml` | S3バケット・IAM・Lambda・env 追加 |
| `frontend/src/api/types.ts` | SubagentIORecord 型追加 |
| `frontend/src/api/endpoints.ts` | subagentIO エンドポイント追加 |
| `frontend/src/routes/ExecutionDetail.tsx` | SubagentIOSection 追加 |
