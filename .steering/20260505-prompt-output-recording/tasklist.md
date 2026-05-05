# タスクリスト — サブエージェントのプロンプト・出力記録

> 関連: [`requirements.md`](./requirements.md) / [`design.md`](./design.md)

---

## T1: PromptRecorder モジュール

### T1-1 `src/observability/prompt_recorder.py` を作成する

完了条件:
- `PromptRecorder(execution_id)` でインスタンス生成できる
- `PROMPTS_BUCKET` 未設定時は graceful skip（boto3 を触らない）
- `record(subagent, index, prompt, output)` が S3 に JSON を put_object する
- S3 キー形式: `prompts/{execution_id}/{subagent}_{index}.json`
- put_object 失敗時は `logger.error` のみ、例外を伝播させない

### T1-2 `tests/unit/observability/test_prompt_recorder.py` を作成する

完了条件:
- `PROMPTS_BUCKET` 未設定時に S3 が呼ばれないことを確認
- `record()` が正しい S3 キー・JSON 構造で put_object することを確認
- put_object 失敗時に例外が伝播しないことを確認
- `uv run pytest tests/unit/observability/` が全件通過する

---

## T2: orchestrator.py への統合

### T2-1 `Orchestrator.__init__` に `PromptRecorder` インスタンスを追加する

完了条件:
- `self._prompt_recorder = PromptRecorder(execution_id)` が追加されている

### T2-2 リサーチャー呼び出し後に `record()` を追加する

対象: `_execute_research` 内の `call_claude` 直後

完了条件:
- `self._prompt_recorder.record("researcher", step_id, prompt, raw)` が追加されている

### T2-3 ジェネレーター呼び出し後に `record()` を追加する

対象: テキスト成果物経路（`call_claude`）とコード成果物経路（`call_claude_with_workspace`）の両方

完了条件:
- テキスト経路: `self._prompt_recorder.record("generator", "0", gen_prompt, gen_raw)` が追加されている
- コード経路: `self._prompt_recorder.record("generator", "0", prompt, raw_stdout)` が追加されている

### T2-4 レビュアー・評価呼び出し後に `record()` を追加する

対象: `_run_review_loop` 内の `call_codex` 直後

完了条件:
- `self._prompt_recorder.record("reviewer_eval", str(loop), review_prompt, raw)` が追加されている

### T2-5 レビュアー・修正試行呼び出し後に `record()` を追加する

対象: `_run_review_loop` 内の `call_claude(fix_prompt, ...)` 直後

完了条件:
- `self._prompt_recorder.record("reviewer_fix", str(loop), fix_prompt, fix_raw)` が追加されている

---

## T3: インフラ（`template.yaml`）

### T3-1 `PromptsBucket` S3 バケットを追加する

完了条件:
- `BucketName: !Sub catch-expander-prompts-${AWS::AccountId}`
- `PublicAccessBlockConfiguration` で全パブリックアクセスを禁止
- `LifecycleConfiguration` で `ExpirationInDays: 1825`（5年）

### T3-2 `AgentTaskRole` に S3 書き込み権限を追加する

完了条件:
- `s3:PutObject` が `!Sub ${PromptsBucket.Arn}/prompts/*` に許可されている

### T3-3 ECS タスク環境変数に `PROMPTS_BUCKET` を追加する

完了条件:
- `AgentTaskDefinition` の `Environment` に `PROMPTS_BUCKET: !Ref PromptsBucket` が追加されている

### T3-4 `GetSubagentIoFunction` Lambda を追加する

完了条件:
- `Timeout: 30`
- IAM: `s3:ListBucket`（バケット ARN 対象）+ `s3:GetObject`（`/prompts/*` 対象）をカスタムポリシーで付与
- イベント: `DashboardHttpApi` の `GET /executions/{execution_id}/subagent-io`
- `Auth: Authorizer: LambdaAuthorizer` が設定されている
- `PROMPTS_BUCKET` 環境変数が設定されている

---

## T4: Dashboard API

### T4-1 `src/dashboard_api/get_subagent_io/app.py` を作成する

完了条件:
- `s3.list_objects_v2` で `prompts/{execution_id}/` プレフィックスのオブジェクトを一覧取得する
- `ThreadPoolExecutor` で各オブジェクトを並列 `s3.get_object` して JSON パースする
- subagent 種別順（researcher < generator < reviewer_eval < reviewer_fix）でソートする
- 0 件の場合は `{"data": {"execution_id": "...", "records": []}}` を 200 で返す
- `_common.json_response` / `error_response` を使用する

---

## T5: フロントエンド

### T5-1 `frontend/src/api/types.ts` に型を追加する

完了条件:
- `SubagentIORecord` インターフェースが追加されている
- `SubagentIOResponse` インターフェースが追加されている
- `subagent` フィールドの型が `"researcher" | "generator" | "reviewer_eval" | "reviewer_fix"`

### T5-2 `frontend/src/api/endpoints.ts` にエンドポイントを追加する

完了条件:
- `endpoints.subagentIO(executionId)` が `GET /executions/{id}/subagent-io` を呼び出す

### T5-3 `frontend/src/routes/ExecutionDetail.tsx` に `SubagentIOSection` を追加する

完了条件:
- グリッド（タイムライン＋サイドバー）の閉じタグ直後に全幅カードとして配置されている
- `useQuery` でオンデマンド取得している
- `records` が空のとき「記録データなし（この機能追加前の実行）」を表示する
- researcher / generator / reviewer（eval + fix）のセクションに分けて表示する
- プロンプト・出力は折りたたみ（初期状態: 閉じている）で `<pre>` 表示する
- reviewer_fix は該当レコードが存在する場合のみ表示する

---

## T6: デプロイ・動作確認

### T6-1 テスト・型チェックを実行する

完了条件:
- `uv run pytest tests/unit/` が全件通過する
- `cd frontend && npx tsc --noEmit` がエラーなし

### T6-2 コミット・プッシュする

完了条件:
- gitleaks スキャンで機密情報が検出されない
- `main` ブランチにプッシュされ、CI が通過する

### T6-3 SAM デプロイを実行する

完了条件:
- `sam build && sam deploy` が成功する
- `PromptsBucket` が ap-northeast-1 に作成されている
- `catch-expander-get-subagent-io` Lambda が作成されている

### T6-4 実機動作確認を行う

完了条件:
- Catch Expander を1回実行し、S3 バケットにオブジェクトが作成されていることを確認する
- ダッシュボードの実行詳細画面でサブエージェント入出力セクションが表示される
- プロンプト・出力の展開・折りたたみが動作する
- 記録機能追加前の実行詳細を開くと「記録データなし」が表示される
