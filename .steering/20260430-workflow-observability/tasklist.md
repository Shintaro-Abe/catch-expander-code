# タスクリスト — ワークフロー監視ダッシュボード (専用 Web UI)

> 関連: [`requirements.md`](./requirements.md) / [`design.md`](./design.md)
>
> 採用案: 案 C (専用 Web UI)。実装は **PR1 (バックエンド) → PR2 (フロントエンド + ドキュメント)** の 2 段階で進める ([design.md §10.3](./design.md))。
>
> 各 PR で Codex 独立レビューを 2〜3 回実施し、収束まで継続する ([obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md](../../obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md))。

---

## 進捗サマリ

### Phase 0: 前提条件確認

- [ ] T0-1: Slack OAuth クライアント作成方針の決定
- [ ] T0-2: CloudFront カスタムドメインの要否決定
- [ ] T0-3: Claude Design アクセス確認
- [ ] T0-4: PR1/PR2 分割方針の合意
- [ ] T0-5: コスト試算前提の再検証
- [ ] T0-6: events テーブル GSI 3 種の必要性最終判断
- [ ] T0-7: 既存 `workflows.execution_id` と新規 events `execution_id` の整合性確認

### Phase 1: PR1 — バックエンド基盤 (events + API + 認証)

- [ ] T1-1: 共通ヘルパー `src/observability/event_emitter.py` を実装
- [ ] T1-2: orchestrator (`src/agent/orchestrator.py`) に 9 観測ポイントの emit 呼び出しを追加
- [ ] T1-3: Lambda Trigger (`src/trigger/app.py`) に `topic_received` emit を追加
- [ ] T1-4: SAM template に events テーブル + GSI + TTL を定義
- [ ] T1-5: SAM template に OAuth state 一時保存テーブルを定義
- [ ] T1-6: SAM template に Secrets Manager (JWT key + Slack OAuth) を定義
- [ ] T1-7: SAM template に API Gateway HTTP API を定義
- [ ] T1-8: Lambda Authorizer の実装 + SAM 定義
- [ ] T1-9: Auth 系 Lambda (login / callback / logout / me) の実装 + SAM 定義
- [ ] T1-10: 実行データ系 Lambda (list_executions / get_execution / get_execution_events) の実装 + SAM 定義
- [ ] T1-11: メトリクス系 Lambda (get_metrics_summary / get_review_quality / get_errors) の実装 + SAM 定義
- [ ] T1-12: 既存 IAM ロールに events テーブル PutItem 権限追加
- [ ] T1-13: バックエンドユニットテスト追加
- [ ] T1-14: dev 環境への `sam deploy` + 手動 E2E (Slack OAuth ログインまで)
- [ ] T1-15: Codex 連続レビュー (2〜3 回、収束まで)
- [ ] T1-16: PR1 commit + push

### Phase 2: PR2 — フロントエンド SPA (Claude Design 連携) + ドキュメント

- [ ] T2-1: `frontend/` プロジェクト初期化 (Vite + React + TS + Tailwind + shadcn/ui)
- [ ] T2-2: Claude Design でデザインシステム確立 (色 / タイポ / コンポーネント)
- [ ] T2-3: 画面 1 (ダッシュボードトップ) — Claude Design モック → handoff → Claude Code 実装
- [ ] T2-4: 画面 2 (実行一覧) — Claude Design モック → handoff → Claude Code 実装
- [ ] T2-5: 画面 3 (実行詳細) — Claude Design モック → handoff → Claude Code 実装
- [ ] T2-6: 画面 4 (レビュー品質) — Claude Design モック → handoff → Claude Code 実装
- [ ] T2-7: 画面 5 (エラー一覧) — Claude Design モック → handoff → Claude Code 実装
- [ ] T2-8: 認証フローの SPA 統合 (auth/me ポーリング、401 ハンドリング、ログイン/ログアウト)
- [ ] T2-9: API client + TanStack Query + Zustand 統合
- [ ] T2-10: ルーティング統合 (React Router 6)
- [ ] T2-11: アクセシビリティ補強 (ARIA、キーボードナビ、コントラスト) とレスポンシブ調整
- [ ] T2-12: フロントエンドユニットテスト (Vitest + React Testing Library)
- [ ] T2-13: GitHub Actions ワークフロー `build-frontend.yml` 作成
- [ ] T2-14: SAM template に S3 / CloudFront / OAC を追加
- [ ] T2-15: ドキュメント更新 (architecture / functional-design / glossary / repository-structure / credential-setup / README)
- [ ] T2-16: dev 環境デプロイ + 手動 E2E (5 画面ブラウズ)
- [ ] T2-17: Codex 連続レビュー (2〜3 回、収束まで)
- [ ] T2-18: PR2 commit + push

### Phase 3: 実機検証 + クロージング

- [ ] T3-1: production デプロイ (`sam deploy` + GitHub Actions)
- [ ] T3-2: 実機検証 (Slack 投入 → 全イベント発火 → ダッシュボードで観測確認)
- [ ] T3-3: 1 週間のコストモニタリング
- [ ] T3-4: ナレッジ化判断 (obsidian ノート作成 or 不要判断)
- [ ] T3-5: ステアリングドキュメントの完了マーク + 末尾追記

---

## Phase 0: 前提条件確認

### T0-1: Slack OAuth クライアント作成方針の決定

#### 内容

design.md §11.1 のリスクとして「新規 Slack App を別に作るか、既存の Catch-Expander Bot に OAuth スコープを追加するか」が未決定。実装着手前に確定する。

#### 完了条件

- [ ] 方針 (新規 Slack App / 既存拡張) を確定
- [ ] 必要な OAuth scope (`openid`, `profile`, `email`) を Slack 側で許可可能か確認
- [ ] `client_id` / `client_secret` / `workspace_id` を取得できる経路を確保
- [ ] `docs/credential-setup.md` 更新方針の合意

#### メモ

新規 Slack App が安全 (権限境界明確化、間違ってダッシュボードのトークンで Slack 通知が動く事故を防ぐ)。ただし管理対象が増えるトレードオフ。

---

### T0-2: CloudFront カスタムドメインの要否決定

#### 内容

`dashboard.catch-expander.example.com` のような独自ドメインを当てるか、CloudFront デフォルトドメイン (`dXXXX.cloudfront.net`) で MVP を進めるか。

#### 完了条件

- [ ] MVP では CloudFront デフォルトドメインで進める方針を確定 (推奨) か、独自ドメインを当てる場合は ACM 証明書 / Route 53 レコードの手配方針を確定
- [ ] 方針を design.md §6.4 に追記反映

---

### T0-3: Claude Design アクセス確認

#### 内容

design.md §5.6 で前提となる Claude Design の利用可否を確認。

#### 完了条件

- [ ] 利用者が Claude Pro / Max / Team / Enterprise いずれかのプランを保有
- [ ] Claude Design (研究プレビュー) にアクセス可能
- [ ] 1 つのテストプロンプトで UI モック生成が成功 (動作確認)
- [ ] handoff 機能で Claude Code への引き継ぎが動作することを確認

#### メモ

アクセス不可の場合、PR2 のフロントエンド実装は **Claude Code 単独 (文字仕様ベース)** に切り替える。design.md §5.6 を「将来活用候補」に降格、tasklist の T2-2〜T2-7 を素直な実装手順に書き換える。

---

### T0-4: PR1/PR2 分割方針の合意

#### 内容

design.md §10.3 の 2 PR 構成で進めるか、1 PR にまとめるかを決定。

#### 完了条件

- [ ] 2 PR 構成 (推奨) で進める方針を確定
- [ ] 1 PR 案を選ぶ場合、Codex 連続レビューを最後に 3 回まとめて回す方針に修正

---

### T0-5: コスト試算前提の再検証

#### 内容

design.md §10.2 のコスト試算 ($9〜16/月) の前提 (実行頻度 100/月) が現状と整合するか確認。

#### 完了条件

- [ ] 直近 1 ヶ月の実行頻度を `aws dynamodb scan --table-name catch-expander-workflows --select COUNT --filter-expression ...` で算出
- [ ] 試算と乖離が大きい場合は試算をアップデート
- [ ] NFR-2 上限 ($30/月) を超えそうな場合は GSI 削減や DDB 設計見直しを検討

---

### T0-6: events テーブル GSI 3 種の必要性最終判断

#### 内容

design.md §2.2 の GSI 3 種のうち `gsi_event_type_timestamp` は使用頻度が低い場合に削減候補。コスト・実装複雑度との trade-off で判断。

#### 完了条件

- [ ] `gsi_event_type_timestamp` は AC-6 (エラー一覧) で `event_type=error` 抽出に使うか、`status_at_emit=failed` で代替できるかを判断
- [ ] 削減判断ならば design.md §2.2 を更新し、GSI 2 種に絞る
- [ ] 残す判断ならばそのまま実装

---

### T0-7: 既存 `workflows.execution_id` と新規 events `execution_id` の整合性確認

#### 内容

design.md §11.5 のリスク。現行 `workflows` テーブルの `execution_id` (主キー) と events テーブルの `execution_id` が必ず一致する設計になっているか確認。

#### 完了条件

- [ ] 現状コードで `execution_id` の生成箇所と発番タイミングを把握
- [ ] orchestrator / Lambda Trigger で `EventEmitter(execution_id)` に渡す ID が `workflows.execution_id` と同一であることを確認
- [ ] 不一致がある場合は emitter 初期化前に変換ロジック追加

---

## Phase 1: PR1 — バックエンド基盤

### T1-1: 共通ヘルパー `src/observability/event_emitter.py` を実装

#### 内容

design.md §7.1 の `EventEmitter` クラスを新規ファイルとして実装。

#### 完了条件

- [ ] `src/observability/__init__.py` を作成
- [ ] `src/observability/event_emitter.py` に `EventEmitter` クラスを実装 (design.md §7.1 のコード例ベース)
- [ ] `emit()` メソッドが `event_type` / `payload` / `status_at_emit` を受け取る
- [ ] DDB 書き込み失敗時は `logging.error` のみで例外を投げない (best-effort)
- [ ] `EVENTS_TABLE` 環境変数が未設定でも import エラーにならない (graceful skip)
- [ ] `sequence_number` がインスタンス内で単調増加
- [ ] TTL は emit 時の epoch + 90 日

---

### T1-2: orchestrator に 9 観測ポイントの emit 呼び出しを追加

#### 内容

`src/agent/orchestrator.py` の主要処理ステップに `EventEmitter` の呼び出しを差し込む (design.md §7.2)。

#### 観測ポイント (9 種)

- [ ] `workflow_planned` (ワークフロー設計直後)
- [ ] `research_completed` (researcher 結果確定後)
- [ ] `subagent_started` × 3 (researcher / generator / reviewer 各開始時)
- [ ] `subagent_completed` × 3 (各正常終了時)
- [ ] `subagent_failed` (失敗時、いずれかの sub-agent)
- [ ] `review_completed` (各 fix loop iteration 完了時)
- [ ] `notion_stored` (Notion 格納成功時)
- [ ] `github_stored` (GitHub 格納成功時、コード成果物がある場合のみ)
- [ ] `slack_notified` (完了通知送信時)
- [ ] `execution_completed` (実行全体終了時、成功/失敗とも)
- [ ] `error` (例外発生時、try/except 内で emit)

#### 完了条件

- [ ] 各観測ポイントで `EventEmitter` がインスタンス化される (`execution_id` は `workflows.execution_id` と同一)
- [ ] `_run_review_loop` の **シグネチャ・ロジック・戻り値は変更しない** (`memory/project_review_loop_recurring_patch_site.md` 規律遵守)
- [ ] emit 呼び出し追加で既存の単体テストが回帰しない
- [ ] orchestrator 全体の処理時間増加が +5% 以内 (NFR-1)

#### メモ

- `_PRESERVED_DELIVERABLE_FIELDS` / `_accumulate_fixer_notes` / `_apply_accumulated_fixer_notes` (5 件目パッチ由来) には触れない
- review_completed イベントの `code_related_unfixed_count` は `accumulated_fixer_notes` から「コード関連指摘 N 件は本ループ未修正」の N を抽出する
- 提案前に [obsidian/2026-04-26_symptomatic-fix-anti-pattern.md](../../obsidian/2026-04-26_symptomatic-fix-anti-pattern.md) のチェックリストを通す

---

### T1-3: Lambda Trigger に `topic_received` emit を追加

#### 内容

`src/trigger/app.py` で Slack トピック受領 (RunTask 呼び出し直後) に `topic_received` イベントを書き込む (design.md §7.3)。

#### 完了条件

- [ ] `EventEmitter` が ECS RunTask 結果から取得した `execution_id` で初期化される
- [ ] payload には `topic` / `user_id_hash` (SHA-256) / `channel_id` / `workflow_run_id` を含める
- [ ] PII 配慮: Slack user ID は raw ではなくハッシュで保存 (design.md §7.4)
- [ ] 既存の Slack ACK / DDB 書き込み / RunTask の挙動は変更しない

---

### T1-4: SAM template に events テーブル + GSI + TTL を定義

#### 内容

`template.yaml` に新規 DynamoDB テーブル `DashboardEventsTable` を定義 (design.md §6.1)。

#### 完了条件

- [ ] PK = `execution_id` (S), SK = `sk` (S) の構造
- [ ] T0-6 で確定した GSI 構成 (2 〜 3 種) を `GlobalSecondaryIndexes` で定義
- [ ] `TimeToLiveSpecification` で `ttl` 属性を有効化
- [ ] `BillingMode: PAY_PER_REQUEST`
- [ ] `sam validate` が通る

---

### T1-5: SAM template に OAuth state 一時保存テーブルを定義

#### 内容

design.md §4.2 のステップ [5] で必要な state 一時保存用 DDB を定義。

#### 完了条件

- [ ] テーブル名 `DashboardOAuthStateTable`
- [ ] PK = `state` (S), TTL 属性 = `ttl` (epoch)
- [ ] `BillingMode: PAY_PER_REQUEST`
- [ ] `sam validate` が通る

---

### T1-6: SAM template に Secrets Manager (JWT key + Slack OAuth) を定義

#### 内容

design.md §4.4 の 2 シークレットを SAM で定義。

#### 完了条件

- [ ] `DashboardJwtKeySecret` (`AWS::SecretsManager::Secret`、`GenerateSecretString` で 256-bit ランダム)
- [ ] `DashboardSlackOAuthSecret` (空の SecretString で作成、後で手動投入)
- [ ] `docs/credential-setup.md` に手動投入手順を追記する旨をメモ
- [ ] `sam validate` が通る

---

### T1-7: SAM template に API Gateway HTTP API を定義

#### 内容

`AWS::Serverless::HttpApi` リソースを追加し、後続 Lambda の `Events` で参照できる状態にする。

#### 完了条件

- [ ] `DashboardApi` という論理 ID で定義
- [ ] CORS 設定は不要 (CloudFront 同一オリジン経路のみ)
- [ ] Custom Lambda Authorizer の参照を仕込む (T1-8 で実装)
- [ ] `sam validate` が通る

---

### T1-8: Lambda Authorizer の実装 + SAM 定義

#### 内容

design.md §4.5 のコード例ベースで Lambda Authorizer を実装。

#### 完了条件

- [ ] `src/dashboard_api/authorizer/` ディレクトリ + `app.py` + `requirements.txt`
- [ ] cookie から JWT を取り出して `pyjwt` で検証
- [ ] 検証成功 → `isAuthorized: true` + `context: {user_sub, user_name}`
- [ ] 検証失敗 → `isAuthorized: false`
- [ ] SAM 上で 5 分のキャッシュ設定
- [ ] ユニットテスト (有効 JWT / 期限切れ / 不正署名 / cookie なし)

---

### T1-9: Auth 系 Lambda (login / callback / logout / me) の実装 + SAM 定義

#### 内容

design.md §4.2 のフローに従い 4 関数を実装。

#### 完了条件

- [ ] `auth_login`: state 生成 → DDB 保存 → Slack OAuth URL リダイレクト
- [ ] `auth_callback`: state 検証 → Slack OAuth トークン交換 → workspace_id 検証 → JWT 発行 → cookie set → SPA リダイレクト
- [ ] `auth_logout`: cookie 削除 (Set-Cookie で expire)
- [ ] `auth_me`: cookie の JWT を検証 → `{user_name, expires_at}` を返却
- [ ] 各 Lambda は独立したディレクトリ (`src/dashboard_api/auth_login/` 等)
- [ ] SAM template で `Events` を `HttpApi` の各パスに紐付け
- [ ] login / callback はパブリック (Authorizer なし)、logout / me は Authorizer 経由
- [ ] ユニットテスト (各関数の正常系 + 異常系)

---

### T1-10: 実行データ系 Lambda (list_executions / get_execution / get_execution_events)

#### 内容

design.md §3.4 の API 仕様で 3 関数を実装。

#### 完了条件

- [ ] `list_executions`: クエリパラメータ (from/to/status/topic/limit/cursor) で events GSI を Query
- [ ] `get_execution`: 既存 `workflows` + `deliverables` テーブルから単一実行のメタを取得
- [ ] `get_execution_events`: events テーブルから PK Query で全イベント取得 (時系列ソート済み)
- [ ] レスポンス形式は design.md §3.3 (`{data, meta}` / エラー時 `{error}`)
- [ ] IAM ロールには events / workflows / deliverables の Read 権限のみ (最小権限)
- [ ] ユニットテスト

---

### T1-11: メトリクス系 Lambda (get_metrics_summary / get_review_quality / get_errors)

#### 内容

ダッシュボードトップ / レビュー品質 / エラー一覧の集計 API を実装。

#### 完了条件

- [ ] `get_metrics_summary`: 期間指定で execution 件数 / status 別 / 平均 duration / レビュー pass 率を集計
- [ ] `get_review_quality`: `review_completed` イベントを GSI で抽出して集計、`code_related_unfixed_count > 0` 一覧も返却
- [ ] `get_errors`: `error` イベントを GSI で抽出して時系列 + エラータイプ集計
- [ ] レスポンス形式は design.md §3.3
- [ ] ユニットテスト

---

### T1-12: 既存 IAM ロールに events テーブル PutItem 権限追加

#### 内容

orchestrator (ECS Task Role) と Lambda Trigger Role に events テーブルへの書き込み権限を追加。

#### 完了条件

- [ ] ECS Task Role の `Policies` に `dynamodb:PutItem` (events テーブル ARN) を追加
- [ ] Lambda Trigger Role の `Policies` に同上を追加
- [ ] 環境変数 `EVENTS_TABLE` を ECS タスク定義と Lambda Trigger に渡す
- [ ] `sam validate` が通る

---

### T1-13: バックエンドユニットテスト追加

#### 内容

新規ファイル群に対する単体テストを `tests/unit/` 配下に追加。

#### 完了条件

- [ ] `tests/unit/observability/test_event_emitter.py` (emit 正常系 / TTL / 失敗時 logging / EVENTS_TABLE 未設定時の skip)
- [ ] `tests/unit/dashboard_api/test_authorizer.py`
- [ ] `tests/unit/dashboard_api/test_auth_callback.py`
- [ ] `tests/unit/dashboard_api/test_list_executions.py`
- [ ] `tests/unit/dashboard_api/test_get_execution_events.py`
- [ ] `tests/unit/dashboard_api/test_get_metrics_summary.py` (他 metrics/errors も)
- [ ] 既存 `tests/unit/` 全件 pass を維持 (回帰なし)
- [ ] `pytest tests/unit/ -v` で全件 pass を確認

---

### T1-14: dev 環境への `sam deploy` + 手動 E2E (Slack OAuth ログインまで)

#### 内容

dev 環境 (個人検証用 AWS アカウント) に PR1 の変更をデプロイし、最小フローを通す。

#### 完了条件

- [ ] `sam build && sam deploy` で全リソースが作成・更新される
- [ ] Slack OAuth クライアント (T0-1 で作成済み) の credentials を `DashboardSlackOAuthSecret` に手動投入
- [ ] Slack でトピックを 1 件投入 → DDB events テーブルに 9 種以上のイベントが書き込まれることを確認
- [ ] CloudFront URL を直接叩く (SPA はまだプレースホルダー HTML) → `/api/v1/auth/login` リダイレクトが動作
- [ ] Slack OAuth ログイン完了 → cookie 発行 → `/api/v1/auth/me` で 200 が返る
- [ ] 認証なしで `/api/v1/executions` を叩くと 401 が返る

---

### T1-15: Codex 連続レビュー (2〜3 回、収束まで)

#### 内容

PR1 の差分に対し独立 LLM レビューを連続で回す ([obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md](../../obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md))。

#### 完了条件

- [ ] WSL2 環境では `codex review --uncommitted -c sandbox_mode="danger-full-access"` で実行
- [ ] 1 回目で出た P2 以上の指摘に対応
- [ ] 2 回目を回し、新規 P2 指摘があれば対応
- [ ] 3 回目で新規 P2 がなければ収束判定
- [ ] 収束しない場合は対症療法アンチパターン圏内疑い、設計層から見直し ([obsidian/2026-04-26_symptomatic-fix-anti-pattern.md](../../obsidian/2026-04-26_symptomatic-fix-anti-pattern.md))

---

### T1-16: PR1 commit + push

#### 内容

PR1 をまとめてコミット + push。

#### 完了条件

- [ ] コミットメッセージ起案 (例: `feat: add events store and dashboard backend API`)
- [ ] 明示的にステージ対象を指定して `git add`
- [ ] `git diff --cached` で意図したファイルのみがステージされていることを確認
- [ ] `git commit` 実行 → `git push origin main`
- [ ] GitHub Actions `build-agent.yml` が `src/agent/**` 変更検知で発火することを確認

---

## Phase 2: PR2 — フロントエンド SPA + ドキュメント

### T2-1: `frontend/` プロジェクト初期化

#### 内容

design.md §5.5 の構成で Vite プロジェクトを初期化。

#### 完了条件

- [ ] `npm create vite@latest frontend -- --template react-ts`
- [ ] `tailwind.config.js` 作成 + `index.css` で Tailwind ディレクティブ追加
- [ ] shadcn/ui 初期化 (`npx shadcn-ui@latest init`)
- [ ] 主要依存追加: `react-router-dom`, `@tanstack/react-query`, `zustand`, `recharts`, `lucide-react`
- [ ] TypeScript / ESLint / Prettier 設定
- [ ] `frontend/.gitignore` 確認 (`node_modules/`, `dist/` 除外)
- [ ] `npm run dev` が起動することを確認

---

### T2-2: Claude Design でデザインシステム確立

#### 内容

design.md §5.6 のステップ [2]。Catch-Expander 用のデザインシステムを Claude Design に学習させる。

#### 完了条件

- [ ] ブランドカラー / フォント / コンポーネントの方針をまとめた指示を Claude Design に投入
- [ ] shadcn/ui のデフォルトを起点に色 (primary / accent / muted / destructive) を確定
- [ ] タイポグラフィ (見出しサイズ / body / monospace) を確定
- [ ] アイコンセット (lucide-react 前提) を確定
- [ ] デザインシステム概要を `frontend/DESIGN_SYSTEM.md` または同等ドキュメントに保存

---

### T2-3 〜 T2-7: 5 画面の Claude Design モック → handoff → Claude Code 実装

各画面を以下のテンプレートで進める。

#### 画面ごとの進め方

1. design.md §5.4 / §5.6 の役割分担表 (画面別 Claude Design / Claude Code 担当) を Claude Design に投入
2. UI モック生成
3. レイアウト・状態バリエーション (ローディング / エラー / 空) のイテレーション
4. handoff バンドル化
5. Claude Code に handoff → `frontend/src/routes/` 配下に React コンポーネント生成
6. Claude Code 側で動的データフェッチ (API client / TanStack Query) を結線
7. ストーリーブック相当のコンポーネント単体テスト追加
8. `npm run dev` で目視確認

#### 画面別タスク

- [ ] **T2-3: ダッシュボードトップ** (`/`)
  - [ ] Claude Design モック生成
  - [ ] handoff → Claude Code 実装
  - [ ] `/api/v1/metrics/summary` 結線、期間切替 state、Recharts チャート組み込み
- [ ] **T2-4: 実行一覧** (`/executions`)
  - [ ] Claude Design モック生成
  - [ ] handoff → Claude Code 実装
  - [ ] `/api/v1/executions` 結線、検索/絞り込み state、ページネーション
- [ ] **T2-5: 実行詳細** (`/executions/:execution_id`)
  - [ ] Claude Design モック生成 (タイムライン UI が要)
  - [ ] handoff → Claude Code 実装
  - [ ] `/api/v1/executions/{id}` + `/api/v1/executions/{id}/events` 結線、イベント展開 state、JSON 整形表示
- [ ] **T2-6: レビュー品質** (`/quality`)
  - [ ] Claude Design モック生成
  - [ ] handoff → Claude Code 実装
  - [ ] `/api/v1/metrics/review-quality` 結線、`code_related_unfixed_count > 0` 一覧
- [ ] **T2-7: エラー一覧** (`/errors`)
  - [ ] Claude Design モック生成
  - [ ] handoff → Claude Code 実装
  - [ ] `/api/v1/errors` 結線、エラータイプ集計

#### 完了条件 (各画面共通)

- [ ] 該当 AC (AC-2 〜 AC-6 のいずれか) を満たす
- [ ] handoff バンドルは `frontend/` にコミット前に手作業レビュー済み
- [ ] ローディング / エラー / 空状態が破綻しない

---

### T2-8: 認証フローの SPA 統合

#### 内容

design.md §5.1 で言及した「auth/me を 30 秒ごと poll」と 401 → ログインリダイレクトを実装。

#### 完了条件

- [ ] TanStack Query で `/api/v1/auth/me` を 30 秒間隔で refetch
- [ ] 401 を受信したら `window.location = /api/v1/auth/login` にリダイレクト
- [ ] ログアウトボタンが Header に存在し、`POST /api/v1/auth/logout` を呼んだ後にログイン画面へ遷移
- [ ] cookie 操作は SPA 側で直接行わない (HttpOnly のため)

---

### T2-9: API client + TanStack Query + Zustand 統合

#### 内容

API 呼び出しの共通基盤を整備。

#### 完了条件

- [ ] `frontend/src/api/client.ts` に fetch ラッパーを実装 (credentials: include、エラー処理、レスポンス整形)
- [ ] TanStack Query Provider をルートに配置
- [ ] Zustand store を準備 (期間フィルタ / ソート設定など UI 状態)
- [ ] 各画面のコンポーネントは TanStack Query の hook 経由でのみデータ取得 (生 fetch を直接書かない)

---

### T2-10: ルーティング統合 (React Router 6)

#### 内容

5 画面 + ログイン関連を React Router でルーティング。

#### 完了条件

- [ ] `<BrowserRouter>` / `<Routes>` / `<Route>` で URL マッピング
- [ ] 認証必須ルートは `<RequireAuth>` ラッパーで保護 (auth/me で 401 なら login へ)
- [ ] 404 ページ用ルート追加
- [ ] CloudFront Function (or S3 Error Document) で history fallback (`/* → /index.html`) を確認

---

### T2-11: アクセシビリティ補強 + レスポンシブ調整

#### 内容

shadcn/ui (Radix ベース) の標準 a11y を活用しつつ、追加補強と mobile/tablet 対応。

#### 完了条件

- [ ] キーボードナビゲーション (Tab/Enter/Escape) で全機能操作可能
- [ ] ARIA ラベル / role が主要要素に付与されている
- [ ] コントラスト比 WCAG AA 以上
- [ ] 768px 未満では sidebar が collapse、主要画面が縦並びで読める
- [ ] 768px 未満は警告表示 (「PC 推奨」) でも可 (要件 NFR スコープ)

---

### T2-12: フロントエンドユニットテスト

#### 内容

主要コンポーネントの単体テスト + 認証 hook のテスト。

#### 完了条件

- [ ] Vitest + React Testing Library 環境構築
- [ ] 各 routes コンポーネントの最小レンダーテスト (props モックで描画確認)
- [ ] 認証 hook の 401 検知テスト
- [ ] `npm run test` で全件 pass

---

### T2-13: GitHub Actions ワークフロー `build-frontend.yml` 作成

#### 内容

design.md §6.3 の独立 CI/CD ワークフロー。

#### 完了条件

- [ ] `.github/workflows/build-frontend.yml` 新規作成
- [ ] トリガー: `push: branches: [main], paths: ["frontend/**"]`
- [ ] ステップ: `npm ci` → `npm run build` → `aws s3 sync frontend/dist/ s3://...` → `aws cloudfront create-invalidation`
- [ ] OIDC 経由で AWS 認証 (PAT を使わない)
- [ ] `pull_request` トリガーで lint + test も走る (任意)

---

### T2-14: SAM template に S3 / CloudFront / OAC を追加

#### 内容

design.md §6.1 の S3 + CloudFront + OAC + CloudFront Function を追加。

#### 完了条件

- [ ] `DashboardBucket` (private S3)
- [ ] `DashboardOriginAccessControl`
- [ ] `DashboardDistribution` (Behaviors: `/api/*` → API GW、`/*` → S3)
- [ ] `DashboardCloudFrontFunctionRewrite` (history fallback)
- [ ] CloudFront → API GW のカスタムヘッダ `X-Origin-Verify` 設定
- [ ] API GW 側でカスタムヘッダ検証ロジックを追加 (Lambda Authorizer 内 or 別 Resource Policy)
- [ ] `sam validate` が通る

---

### T2-15: ドキュメント更新

#### 内容

requirements.md AC-10 の対応。

#### 完了条件

- [ ] `docs/architecture.md`: 「監視ダッシュボード」節を追加 (構成図 + 新リソース一覧)
- [ ] `docs/functional-design.md`: ダッシュボード機能の節を追加 (画面構成 / API / データフロー)
- [ ] `docs/glossary.md`: 新用語追加 (events table / Slack OAuth / SPA / JWT / handoff bundle / Lambda Authorizer 等)
- [ ] `docs/repository-structure.md`: ツリーに `frontend/`, `src/dashboard_api/`, `src/observability/` を追加
- [ ] `docs/credential-setup.md`: Slack OAuth クライアント作成手順を追加 + シークレット投入コマンドを追加
- [ ] `README.md`: 「ダッシュボード URL」と「アクセス方法」を追加

---

### T2-16: dev 環境デプロイ + 手動 E2E (5 画面ブラウズ)

#### 内容

dev 環境に PR2 の変更をデプロイし、ダッシュボード画面を手動でブラウズ。

#### 完了条件

- [ ] `sam build && sam deploy` 完了
- [ ] GitHub Actions `build-frontend.yml` が `frontend/**` 変更で発火
- [ ] CloudFront URL でログイン画面表示 → Slack OAuth → 認証通過
- [ ] 5 画面それぞれが想定通り表示される (期間切替 / 行クリック / タイムライン展開 / フィルタが動作)
- [ ] 古い実行 (PR1 デプロイ前) は events なしのため、新規 1 件で実機確認

---

### T2-17: Codex 連続レビュー (2〜3 回、収束まで)

#### 内容

PR2 の差分に対し独立 LLM レビューを連続で回す。

#### 完了条件

- [ ] T1-15 と同様の手順で 2〜3 回回し、新規 P2 が出ないところまで収束
- [ ] フロントエンド特有の観点 (a11y / レスポンシブ / 認証エラーハンドリング / Claude Design handoff の品質) も検査対象に含める

---

### T2-18: PR2 commit + push

#### 完了条件

- [ ] コミットメッセージ起案 (例: `feat: add observability dashboard SPA with Claude Design integration`)
- [ ] 明示的にステージ対象を指定
- [ ] `git diff --cached` 確認
- [ ] `git commit` → `git push origin main`
- [ ] GitHub Actions `build-frontend.yml` の発火を確認、ECR / ECS への影響がないことを確認 (paths filter で隔離)

---

## Phase 3: 実機検証 + クロージング

### T3-1: production デプロイ

#### 完了条件

- [ ] PR1 + PR2 が main にマージ済み
- [ ] `sam build && sam deploy` を production 環境で実行
- [ ] GitHub Actions `build-agent.yml` + `build-frontend.yml` が発火し成功
- [ ] CloudFront / API GW / Lambda / DDB events / Secrets Manager が production にプロビジョン済み

---

### T3-2: 実機検証 (Slack 投入 → 全イベント発火 → ダッシュボードで観測確認)

#### 完了条件

- [ ] Slack で 1 件のトピック (コード成果物が出るもの推奨) を投入
- [ ] DynamoDB events テーブルに 9 種以上のイベントが書き込まれる
- [ ] ダッシュボードトップで実行件数が +1 された
- [ ] 実行一覧に当該 execution が現れる
- [ ] 実行詳細でタイムライン全イベントが時系列表示される
- [ ] レビュー品質画面で `review_completed` イベントが集計に反映
- [ ] エラーが発生していないこと (もしくは `error` イベントが正しく書き込まれていること)
- [ ] 観測結果を [obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md](../../obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md) の実機検証ログに 1 行追記

---

### T3-3: 1 週間のコストモニタリング

#### 完了条件

- [ ] AWS Cost Explorer で `DashboardEventsTable` / `DashboardBucket` / `DashboardDistribution` / `DashboardApi` のコスト推移を 7 日後に確認
- [ ] NFR-2 (月額 +$30 以内) を超える兆候があれば対応 (DDB アクセスパターン / Lambda メモリ / CloudFront キャッシュ調整)
- [ ] 結果を本 tasklist 末尾に追記

---

### T3-4: ナレッジ化判断

#### 完了条件

- [ ] 本実装で得られた学びを obsidian ノート化するかを判断
- [ ] 候補: 「専用 Web UI を AWS Serverless で構築するパターン」「Slack OAuth + JWT in HttpOnly cookie の最小実装」「Claude Design → Claude Code 連携の実運用知見」「best-effort イベント書き込み設計」
- [ ] 残す場合: `obsidian/2026-04-XX_*.md` を作成 (フォーマットは既存ノートに準拠)
- [ ] 残さない場合: 判断理由を本 tasklist 末尾に短く記録

---

### T3-5: ステアリングドキュメントの完了マーク + 末尾追記

#### 完了条件

- [ ] 本ファイルの全 `[ ]` を `[x]` に更新 (実機検証で確認できなかった項目はその旨を注記)
- [ ] 末尾に完了日と本実装で判明した特記事項を追記
- [ ] `memory/` への反映が必要な学びがあれば該当ファイル更新

---

## 受け入れ条件サマリ (requirements.md AC との対応)

| requirements.md AC | 対応する tasklist タスク |
|---|---|
| AC-1: 認証 / アクセス制御 | T1-8, T1-9, T2-8, T2-10 |
| AC-2: ダッシュボードトップ画面 | T1-11 (metrics_summary), T2-3 |
| AC-3: 実行一覧画面 | T1-10 (list_executions), T2-4 |
| AC-4: 実行詳細画面 | T1-10 (get_execution_events), T2-5 |
| AC-5: レビュー品質画面 | T1-11 (review_quality), T2-6 |
| AC-6: エラー一覧画面 | T1-11 (get_errors), T2-7 |
| AC-7: イベント観測ポイント | T1-1, T1-2, T1-3 |
| AC-8: パフォーマンス | T1-14, T2-16 (デプロイ後の手動確認) |
| AC-9: 既存挙動の回帰なし | T1-13 (既存テスト pass)、T2-12 |
| AC-10: ドキュメント更新 | T2-15 |

すべての T が完了 = すべての AC が満たされる、の整合を保つ。

---

## 段階展開計画 (再掲)

| Phase | スコープ | 想定期間 |
|---|---|---|
| Phase 0 | 前提条件確認 (T0-1〜T0-7) | 1〜2 日 |
| Phase 1 | バックエンド基盤 (T1-1〜T1-16) | 4〜6 日 |
| Phase 2 | フロントエンド SPA (T2-1〜T2-18) | 5〜7 日 |
| Phase 3 | 実機検証 + クロージング (T3-1〜T3-5) | 2 日 + 1 週間モニタ |
| **合計** | | **2〜3 週間** |

---

## リスクと対応

| リスク | 対応 |
|---|---|
| Claude Design アクセス不可 | T0-3 で確認、不可なら Claude Code 単独実装に切り替え |
| Slack OAuth クライアント作成が時間かかる | T0-1 を最優先、ブロッカーなら他タスクと並行 |
| 既存 `_run_review_loop` への意図せぬ侵襲 | T1-2 で `memory/project_review_loop_recurring_patch_site.md` を必ず参照、Codex レビュー (T1-15) で侵襲チェック |
| events 書き込みのパフォーマンス劣化 | T1-2 完了後にローカルで `pytest` を 1 回回し、orchestrator のテスト時間を計測。+5% 超なら batch_writer 化検討 |
| コスト超過 | T3-3 で 1 週間モニタ、超過なら GSI 削減 / Lambda メモリ調整 |
| handoff バンドルの品質ばらつき | T2-3〜T2-7 で各画面のコミット前に手作業レビュー必須 |
| CloudFront → API GW のオリジン保護漏れ | T2-14 で `X-Origin-Verify` 検証ロジックを実装、Codex レビュー (T2-17) で確認 |

---

## 完了条件

- [ ] 全テスト pass (`pytest tests/ -v` + `npm run test`)
- [ ] Lint / Format / Type check クリーン (`ruff check src/ tests/` + frontend の eslint / tsc)
- [ ] 新規ワークフローで events が DynamoDB に保存されている (T3-2)
- [ ] ダッシュボードで 5 画面すべてが想定通り表示される (T3-2)
- [ ] 既存挙動の回帰なし (orchestrator / Lambda Trigger / F8 / F9 すべて従来通り動作、T1-14 / T2-16 / T3-2 で確認)
- [ ] `docs/` の関連ドキュメントが現状を正しく記述している (T2-15)
- [ ] PR1 / PR2 がそれぞれ Codex 連続レビューで収束済み (T1-15, T2-17)

---

## 実装着手前の最終確認

実装に着手する前に、design.md §12 のチェックリスト 8 項目を確定させる:

- [ ] Slack OAuth クライアント作成方針 (T0-1)
- [ ] CloudFront カスタムドメイン要否 (T0-2)
- [ ] events テーブル GSI 3 種の必要性 (T0-6)
- [ ] フロントエンド `react-json-view` 等のライブラリ最終選定 (T2-1 で確定可)
- [ ] PR1 / PR2 分割の合意 (T0-4)
- [ ] コスト試算前提の再確認 (T0-5)
- [ ] Claude Design アクセス確認 (T0-3)
- [ ] Claude Design 利用方針の合意 (5 画面すべて or 一部) (T0-3 と一体)

論点が確定したら T1-1 から順に着手する。
