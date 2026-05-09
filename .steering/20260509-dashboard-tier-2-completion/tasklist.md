# タスクリスト: ダッシュボード Tier 2 完成 + 画面 5 タブ統合

参照: [requirements.md](./requirements.md) / [design.md](./design.md)

進捗マーク: `[ ]` 未着手 / `[~]` 進行中 / `[x]` 完了 / `[!]` ブロック

---

## Phase 1: データパイプライン投入 (P1)

**目的**: reviewer prompt → orchestrator emit → backend に新フィールド `issue_category` を流す。UI は触らない。

### T1-1: reviewer prompt に issue_category を追加 [x]

#### 内容
`src/agent/prompts/reviewer.md` の出力 JSON スキーマに `issues[].issue_category` を追加し、5 値域 (`terraform_schema` / `iam_action` / `syntax` / `api_version` / `other`) と分類ヒューリスティクスをプロンプトに記述。

#### 完了条件
- [x] `reviewer.md` の出力例 JSON に `issue_category` フィールドが含まれる
- [x] 5 値域の判定ヒューリスティクスが箇条書きで記述されている (カテゴリ表 + 曖昧時の優先順位 3 件)
- [x] 既存 `severity` / `description` / `fix_instruction` フィールドは変更されていない
- [x] プロンプト末尾の制約セクションに「`issue_category` は必ず 5 値のいずれかから選ぶ」を追記

---

### T1-2: orchestrator の review_completed emit 拡張 [x]

#### 内容
`src/agent/orchestrator.py` の `_run_review_loop` 内 `review_completed` 発火部に `issue_categories` (dict[str, int]) を追加。LLM 応答が malformed の場合は `"other"` フォールバック。

#### 完了条件
- [x] `review_completed` の payload に `issue_categories` フィールドが追加される (orchestrator.py:1538)
- [x] `isinstance(raw_issues, list)` / `isinstance(issue, dict)` / `isinstance(cat, str)` のガードが入っている (`_aggregate_issue_categories` 内 3 段ガード)
- [x] `_VALID_ISSUE_CATEGORIES` モジュール定数で 5 値を定義 (orchestrator.py:57, frozenset)
- [x] LLM が `issue_category` を出さなかった場合 `"other"` で集計される
- [x] 既存の `issues` リストはそのまま payload に保持され、各 issue dict に `issue_category` が含まれる
- [x] T1-2 単体では emit 内に 1 行追加のみ (`issue_categories: _aggregate_issue_categories(...)`)。fix loop / accumulator / 型ガードロジックは T1-2 では変更しない。**malformed 耐性の補強 (fallback dict / errors 走査ガード) は T1-2b として追加** (Codex 1 回目 P1-2/P1-3 対応)

---

### T1-2b: Codex 1 回目 P1 対応 — `_run_review_loop` の malformed 耐性補強 [x]

#### 内容
Codex 1 回目レビューで検出された 2 件の P1 (再発パッチ密集地点の既存脆弱性) を補強。データパイプライン投入と同 PR で潰す:
1. P1-3: `_parse_claude_response` が非 dict (None/list/scalar) を返した場合のクラッシュ → fallback dict 化
2. P1-2: `errors = [i for i in review_result.get("issues", []) ...]` の post-emit 走査でクラッシュ → 既ガード済みの `review_issues_for_emit` を再利用 + isinstance dict フィルタ追加

#### 完了条件
- [x] `_parse_claude_response` 結果に対する `isinstance(parsed_review, dict)` ガード追加 (orchestrator.py:1521-1538)
- [x] 非 dict 時は `passed=False` + `issues=[]` + notes 警告のフォールバック dict を構築
- [x] line 1548 の errors 走査で `review_issues_for_emit` を再利用し isinstance dict フィルタ追加
- [x] `TestReviewLoopMalformedGuards` クラス 2 ケース追加 (`_parse_claude_response` 非 dict 戻り値の確認 + 集計フォールバック)
- [x] 全テスト (17 件) pass

---

### T1-3: backend get_review_quality の軽微拡張 [x]

#### 内容
`src/dashboard_api/get_review_quality/app.py` のレスポンスに `issue_categories` 集計を追加。既存フィールドは変更しない。

**Codex 1 回目 P1-1 対応**: DDB は Number 属性を `decimal.Decimal` で返すため、`isinstance(v, int)` 単独だと本番データを全スキップする。`_coerce_count` ヘルパーで Decimal を許容、bool / 負数 / 非整数を除外。
**Codex 2 回目 P2-B 対応**: `_coerce_count` に `is_finite()` ガードと `OverflowError` / `ValueError` 捕捉を追加し、Decimal NaN / Infinity / -Infinity を安全に除外。
**Codex 2 回目 P2-C 対応**: `seen_issue_categories_payload` フラグで「全 events `{}` (新 pipeline 動作中だが categories 件数 0)」と「全件 field 欠損 (past data のみ)」を区別。前者は `{}`、後者は `None` を返す。

#### 完了条件
- [x] レスポンス `data.issue_categories` に dict[str, int] が返される
- [x] events に `issue_categories` が無い past data は集計から除外される (`isinstance(cats, dict)` ガード)
- [x] 既存のレスポンスフィールド (`total_reviews` / `pass_count` / `pass_rate` / `unfixed_code_issues`) は値が変わらない
- [x] `_coerce_count` で Decimal を整数化、bool / 負数 / 非整数 / 非有限値 (NaN / Infinity) を除外
- [x] 全件欠損 → `None`、全件 `{}` → `{}` の区別が成立
- [x] `test_handles_decimal_and_rejects_bool_and_non_integer` / `test_decimal_values_aggregate_across_events` / `test_rejects_decimal_special_values` / `test_returns_empty_dict_when_all_events_have_empty_categories` / `test_returns_none_only_when_all_events_lack_field` で本番データ互換性 + 境界条件を担保

---

### T1-4: P1 ユニットテスト [x]

#### 完了条件
- [x] `tests/unit/agent/test_orchestrator.py` に `TestAggregateIssueCategories` クラス追加（7 ケース pass）:
  - 通常パス（5 値域それぞれ集計）
  - malformed パス: missing field / unknown value / non-list input / non-dict issue / non-string value / mixed
  - 既存 `_run_review_loop` テストが回帰しないことを確認 (pre-existing 失敗 16 件は **私の変更前から存在**、`_run_review_loop` が `call_codex` を使うのに既存テストが `call_claude` をモックしている既存不整合。本 steering スコープ外)
- [x] `tests/unit/dashboard_api/test_get_metrics_summary.py` の `TestGetReviewQuality` に 3 ケース追加（全 pass）:
  - `test_aggregates_issue_categories_across_events`: 複数 event 横断の集計
  - `test_returns_none_when_no_issue_categories_in_any_event`: 全件欠損時 None
  - `test_skips_malformed_issue_categories`: past data + 不正値混在のスキップ
- [x] T1-1〜T1-4 で追加した 13 ケース全 pass

#### 別途記録: 既存テストの pre-existing 失敗

`TestReviewLoop` 16 件（および `TestCallClaude::test_call_claude_raises_after_max_retries`）が main 状態でも失敗。原因は `_run_review_loop` が `call_codex` を使うのに既存テストが `call_claude` をモックしているため。これは別 steering で対応すべき技術負債で、本 steering の作業範囲外。git stash pop で確認済み。

---

### T1-5: P1 dev デプロイ + 実機検証

#### 完了条件
- [ ] `./scripts/deploy-agent.sh` で ECR イメージ更新 (新 reviewer prompt 反映)
- [ ] `sam build && sam deploy` 実行
- [ ] Slack で 1 件のコード成果物が出るトピック投入
- [ ] DynamoDB events テーブルを直接 query し、`payload.issue_categories` が dict として保存されていることを確認
- [ ] 既存ダッシュボード画面 4 が壊れていない（KPI カード + 未修正一覧が従来通り表示）

---

### T1-6: P1 Codex 連続レビュー (再発パッチ密集地点警戒) [x]

#### 内容
`_run_review_loop` 周辺は再発パッチ密集地点 (`memory/project_review_loop_recurring_patch_site.md`)。1 回目で出た指摘 → 修正 → 2 回目、を新規指摘ゼロ収束まで繰り返す。

#### 完了条件
- [x] WSL2 で `codex exec --skip-git-repo-check -c sandbox_mode="danger-full-access"` を T1-2 / T1-3 の差分に対し 1 回目実行
- [x] P2 以上の指摘があれば修正 → 2 回目実行
- [x] 新規 P2 が連続で出なくなるまで繰り返し（最大 3 回目）
- [x] レビュー結果サマリを本 tasklist 末尾に追記

#### 実施結果サマリ (2026-05-09)

**1 回目** (P1 = 3 件 / P3 = 1 件):
- P1-1: backend `_coerce_count` ヘルパー導入 (Decimal 受容)
- P1-2: `_run_review_loop` errors 走査の isinstance dict フィルタ追加
- P1-3: `_parse_claude_response` 非 dict 戻り値の fallback dict ガード追加
- P3: reviewer.md に provider version 境界 1 行追加

**2 回目** (P1 = 0 件 / P2 = 2 件 / P3 = 2 件):
- P2-B: `_coerce_count` に `is_finite()` ガード + `OverflowError`/`ValueError` 捕捉
- P2-C: `seen_issue_categories_payload` フラグで全 `{}` と全件欠損を区別
- P3: design.md / tasklist.md の文言整合修正

**3 回目** (P1 = 0 件 / P2 = 0 件 / P3 = 2 件):
- 新規ブロッカーなし → **収束判定**
- P3: 境界テスト 2 件追加 (sNaN / 全 fallback emit) + design.md サンプルコード更新

最終テスト件数: 22 件全 PASS

#### 知見
3 回連続で次層が剥がれた (`obsidian/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` のパターンに沿う)。各層は前段対応によって初めて視野に入る関係:
- 1 回目: 既存コードの脆弱性 (Decimal / errors 走査 / fallback)
- 2 回目: 1 回目修正で生まれた新コード (`_coerce_count`) の境界条件
- 3 回目: 新コードの磨き込み (テストカバレッジ・ドキュメント整合) のみ → 収束

---

### T1-7: P1 commit + push

#### 完了条件
- [ ] `pre-commit-secret-scan` Skill を実行（gitleaks スキャン）
- [ ] `src/agent/prompts/reviewer.md` / `src/agent/orchestrator.py` / `src/dashboard_api/get_review_quality/app.py` / `tests/` を明示的にステージ
- [ ] `git diff --cached` 確認
- [ ] コミットメッセージ: `feat(observability): add issue_category to review_completed events (P1 of dashboard tier-2)`
- [ ] `git commit` → `git push origin main`
- [ ] GitHub Actions が成功することを確認

---

## Phase 2: 画面 4 完成 (P2)

**目的**: P1 で蓄積開始した `issue_category` を画面 4 に可視化し、AC-5 を完全達成する。

**依存**: P1 完了から最低 24h 待機（events テーブルにデータが蓄積されないと UI 検証不能）

### T2-1: backend get_review_quality 全機能拡張

#### 内容
P1 の軽微拡張に対し、以下の 5 機能をバックエンドで実装:
1. `?category=` クエリパラメータでのフィルタ
2. pass 率推移時系列 (日次バケット)
3. fix loop 発火回数分布 (iteration 別度数)
4. 同種 issue 繰り返し検出
5. トピック別 pass 率 (workflow_planned との join)

#### 完了条件
- [ ] レスポンスに `pass_rate_timeseries` / `fix_loop_distribution` / `recurring_issues` / `pass_rate_by_topic` の 4 フィールドが追加される
- [ ] `?category=` 指定時に `unfixed_code_issues` がフィルタされる
- [ ] `?category=` の不正値 (5 値域外) で 400 を返す
- [ ] events テーブル query は同一 execution_id への join で N+1 を許容するが、events 件数 < 100/月想定下で許容範囲（実測 p95 < 300ms）

---

### T2-2: P2 backend ユニットテスト

#### 完了条件
- [ ] `tests/unit/dashboard_api/test_get_review_quality.py` に追加:
  - 各新フィールドの集計ロジック
  - `?category=` フィルタ動作
  - past data (issue_categories 欠損) と新 data の混在ケース
  - workflow_planned が無い execution_id の扱い
  - 不正カテゴリ値での 400 返却
- [ ] `pytest tests/unit/dashboard_api/` 全件 pass

---

### T2-3: frontend ReviewQuality.tsx チャート追加

#### 内容
recharts (既存導入済み) を使い、以下 5 コンポーネントを新規追加:
- `PassRateTrendCard` (LineChart)
- `FixLoopDistributionCard` (BarChart 横)
- `IssueCategoryPieCard` (PieChart, クリックハンドラ付き)
- `PassRateByTopicCard` (BarChart 縦)
- `RecurringIssuesCard` (Table)

#### 完了条件
- [ ] 5 コンポーネントが個別ファイル or 同 routes/ファイル内 sub-component として実装される
- [ ] Loading 状態は既存 `<Skeleton>` パターンで統一
- [ ] Empty 状態は `<EmptyState>` パターンで統一
- [ ] 既存 `KpiRow` / `UnfixedIssuesCard` は変更されない（追加配置のみ）

---

### T2-4: frontend フィルタ UI + URL 状態管理

#### 内容
- `useSearchParams` で `?days=&category=` を管理
- `<UnfixedIssuesCard>` 上部にカテゴリフィルタチップ列を追加
- カテゴリ円グラフのスライスクリックで `setSearchParams({...prev, category: "..."})`
- 「すべて」チップで `category` パラメータ削除

#### 完了条件
- [ ] URL クエリで `?category=iam_action` 直接アクセス時、初回レンダリングからフィルタ済み
- [ ] 円グラフクリックで URL 更新 + 一覧連動
- [ ] フィルタチップで `[すべて N]` `[terraform N]` ... の度数表示
- [ ] テーブルに `issue_category` 列が 1 列追加される

---

### T2-5: P2 frontend ユニットテスト

#### 完了条件
- [ ] `frontend/src/test/ReviewQuality.test.tsx` に追加:
  - モックレスポンスでチャート 5 種が描画される
  - フィルタチップクリックで URL 更新
  - 円グラフスライスクリックで URL 更新
  - URL 直接アクセスでフィルタ初期状態が反映
- [ ] `npm run test` 全件 pass
- [ ] Vitest coverage の既存基準を下回らない

---

### T2-6: P2 dev デプロイ + E2E

#### 完了条件
- [ ] `cd frontend && npm ci && npm run build && aws s3 sync dist/ s3://catch-expander-frontend-{account}/ --delete && aws cloudfront create-invalidation --distribution-id <ID> --paths "/index.html"`
- [ ] `sam build && sam deploy` (backend Lambda 更新)
- [ ] CloudFront URL でログイン → 画面 4 を開く
- [ ] AC-5 7 項目すべて視認可能 + 動作確認:
  - [ ] pass 率推移折れ線
  - [ ] fix loop 発火回数分布棒
  - [ ] `code_related_unfixed_count > 0` 一覧
  - [ ] 行から実行詳細遷移
  - [ ] issue カテゴリ別円グラフ
  - [ ] 同種 issue 繰り返し検出表
  - [ ] トピックカテゴリ別 pass 率比較棒
- [ ] フィルタ UI E2E:
  - [ ] フィルタチップで一覧絞り込み
  - [ ] 円グラフクリックで連動
  - [ ] URL 共有可能（リロードで状態復元）

---

### T2-7: P2 Codex レビュー

#### 完了条件
- [ ] backend / frontend 差分に対し Codex 1 回目実行
- [ ] P2 以上の指摘があれば修正 → 2 回目（収束まで）

---

### T2-8: P2 commit + push

#### 完了条件
- [ ] `pre-commit-secret-scan` Skill 実行
- [ ] 明示的にステージ + `git diff --cached` 確認
- [ ] コミットメッセージ: `feat(dashboard): complete review-quality screen with Tier 2.2 charts and filters (P2 of dashboard tier-2)`
- [ ] `git push origin main` → GitHub Actions 成功確認

---

## Phase 3: 画面 5 タブ統合 (P3)

**目的**: AC-6 のタブ式 4 区画 (エラー / API 健全性 / token_monitor / 再発検出) を実装。

**依存**: P2 完了。`_run_review_loop` を再度触らないため、P1 / P2 の作業領域とは独立。

### T3-1: shadcn tabs コンポーネント導入

#### 完了条件
- [ ] `cd frontend && npx shadcn@latest add tabs` で `frontend/src/components/ui/tabs.tsx` 追加
- [ ] `package.json` に `@radix-ui/react-tabs` が追加される
- [ ] `npm run build` でバンドルが成功
- [ ] バンドルサイズ増加が +10KB 以下であることを確認

---

### T3-2: backend get_recurrence Lambda 新規追加

#### 内容
新規 Lambda `src/dashboard_api/get_recurrence/app.py` を追加し、events テーブルから error イベントを取得して再発パターンを集計。

#### 完了条件
- [ ] `src/dashboard_api/get_recurrence/app.py` 実装
  - error_type 別時系列バケット
  - stage 別失敗頻度
  - 7 日 3 回ルール (highlight 対象抽出)
- [ ] `template.yaml` に以下追加:
  - `GetRecurrenceFunction` リソース
  - API Gateway ルート `GET /api/v1/metrics/recurrence`
  - IAM: events テーブル query 権限のみ (既存パターン踏襲)
- [ ] `sam validate --lint` PASS

---

### T3-3: P3 backend ユニットテスト

#### 完了条件
- [ ] `tests/unit/dashboard_api/test_get_recurrence.py` に追加:
  - 7 日以内 3 回ルール
  - error_type / stage 別集計
  - 期間外データの除外
- [ ] `pytest tests/unit/dashboard_api/test_get_recurrence.py` 全件 pass

---

### T3-4: ErrorList.tsx → ErrorAndHealth.tsx 再構成

#### 内容
`/errors` URL は維持し、内部コンポーネントを ErrorAndHealth.tsx にリネーム。Tabs ベースに再構成。

#### 完了条件
- [ ] ファイル名 `ErrorList.tsx` → `ErrorAndHealth.tsx`
- [ ] `frontend/src/App.tsx` の route 定義を新コンポーネントに切替（URL は `/errors` 維持）
- [ ] ナビゲーション側 (Sidebar 等) のラベルを「エラー」→「エラー & 健全性」に変更
- [ ] 既存テスト `frontend/src/test/ErrorList.test.tsx` (存在すれば) を `ErrorAndHealth.test.tsx` にリネーム

---

### T3-5: 4 タブ実装

#### 内容
| タブ | 実装 |
|---|---|
| エラー | 既存 `<Card>` + Table をそのまま `<ErrorsTab>` として移植 |
| API 健全性 | サブタイプ別折れ線 / レイテンシ p50/p95/p99 / rate_limit_hit 時系列 / 失敗率 >10% バナー |
| Token Monitor | 直近 30 日 OAuth refresh 折れ線 / 残存時間ヒストグラム / 失敗時バナー |
| 再発検出 | error_type 別時系列クラスター / stage 別失敗頻度 / 7 日 3 回ハイライト |

#### 完了条件
- [ ] 4 タブが `<Tabs>` / `<TabsList>` / `<TabsTrigger>` / `<TabsContent>` で実装される
- [ ] デフォルトタブは `errors`
- [ ] URL クエリ `?tab=` でタブ状態を共有可能
- [ ] 各タブのデータ取得は対応する API endpoint:
  - エラー: 既存 `/api/v1/errors`
  - API 健全性: 既存 `/api/v1/metrics/api-health`
  - Token Monitor: 既存 `/api/v1/metrics/token-monitor`
  - 再発検出: 新規 `/api/v1/metrics/recurrence`

---

### T3-6: P3 frontend ユニットテスト

#### 完了条件
- [ ] `frontend/src/test/ErrorAndHealth.test.tsx`:
  - 4 タブの切替動作
  - 各タブのモックレスポンス描画
  - URL `?tab=` 直接アクセスでタブ状態復元
- [ ] `npm run test` 全件 pass

---

### T3-7: P3 dev デプロイ + E2E

#### 完了条件
- [ ] frontend ビルド + S3 sync + CloudFront invalidation
- [ ] `sam deploy` (新 Lambda 反映)
- [ ] `/errors` を開き 4 タブ全部にデータ表示確認:
  - [ ] エラータブ: 既存挙動と同一
  - [ ] API 健全性タブ: notion / github / slack / anthropic 各サブタイプの折れ線
  - [ ] Token Monitor タブ: 時系列折れ線 + 残存時間ヒストグラム
  - [ ] 再発検出タブ: error_type 別グラフ + 7 日 3 回ハイライト
- [ ] メインダッシュボード `/` の Token Monitor カードが従来通り表示されること（回帰確認）
- [ ] URL `?tab=token-monitor` 直接アクセス → Token Monitor タブが初回表示

---

### T3-8: P3 Codex レビュー

#### 完了条件
- [ ] frontend / backend 差分に対し Codex 1 回目実行
- [ ] P2 以上の指摘があれば修正 → 2 回目

---

### T3-9: P3 commit + push

#### 完了条件
- [ ] `pre-commit-secret-scan` Skill 実行
- [ ] コミットメッセージ: `feat(dashboard): integrate error & health screen with 4 tabs (P3 of dashboard tier-2)`
- [ ] `git push origin main` → GitHub Actions 成功確認

---

## クロージング (TC)

### TC-1: ドキュメント整合更新

#### 完了条件
- [ ] `docs/functional-design.md` の画面 4 / 画面 5 セクションを最新化
  - 画面 4 のグラフ構成を 5 種類記載
  - 画面 5 のタブ構成を 4 タブで記載
- [ ] `docs/architecture.md` の Lambda API 一覧に `?category=` パラメータと `/api/v1/metrics/recurrence` を追記
- [ ] `docs/glossary.md` に `issue_category` 関連用語追加 (terraform_schema / iam_action / syntax / api_version / other)

---

### TC-2: 親 steering の引き継ぎ完了

#### 完了条件
- [ ] `.steering/20260430-workflow-observability/tasklist.md` の T2-6 / T2-7 / T2-16 の `[~]` を `[x]` に戻し、本 steering での解消を末尾に追記
- [ ] AC-5 / AC-6 マッピング行の「本 steering で対応予定」注記を完了表記に更新

---

### TC-3: production デプロイ

#### 完了条件
- [ ] `./scripts/deploy-agent.sh` (ECR 更新)
- [ ] `sam build && sam deploy` (production)
- [ ] frontend ビルド + S3 sync + CloudFront invalidation
- [ ] GitHub Actions すべて成功

---

### TC-4: production E2E 検証

#### 完了条件
- [ ] CloudFront URL でログイン
- [ ] 画面 4 / 画面 5 を E2E 確認 (T2-6 / T3-7 の項目を production 環境で再実施)
- [ ] 既存挙動 (画面 1-3 / 6) の回帰なし
- [ ] Slack で新規トピック投入 → events テーブル → 画面反映まで E2E

---

### TC-5: ナレッジ化判断

#### 完了条件
- [ ] 本 steering で得た学びを `obsidian/` ノート化するか判断
- [ ] 候補: 「issue カテゴリ分類を LLM 任せにする運用知見」「フェーズ分割で再発パッチ密集地点を回避するパターン」「shadcn Tabs 導入のバンドルコスト評価」
- [ ] 残す場合: `obsidian/2026-05-XX_*.md` 作成
- [ ] 残さない場合: 判断理由を本 tasklist 末尾に追記

---

### TC-6: 本 steering クローズ

#### 完了条件
- [ ] 本 tasklist の全 `[ ]` を `[x]` に更新
- [ ] 完了日と特記事項を末尾追記
- [ ] `memory/` への反映が必要な学びがあれば該当ファイル更新

---

## 受け入れ条件サマリ (requirements.md AC との対応)

| requirements.md AC | 対応する tasklist タスク |
|---|---|
| AC-1: 画面 4 (レビュー品質) Tier 2.2 完成 | T1-1, T1-2, T1-3, T2-1, T2-3, T2-4 |
| AC-2: 画面 5 (エラー & 健全性) タブ統合 | T3-1, T3-2, T3-4, T3-5 |
| AC-3: tasklist 真実性 | TC-2 (親 steering 引き継ぎ完了) |
| AC-4: テスト | T1-4, T2-2, T2-5, T3-3, T3-6 |
| AC-5: ドキュメント整合 | TC-1, TC-5 |

---

## ブロッカー / リスクログ

| リスク | 影響 | 緩和策 |
|---|---|---|
| `_run_review_loop` 周辺のパッチで既存テスト回帰 | T1-2 が進まない | T1-2 では emit 1 行追加に絞る、`_run_review_loop` の他ロジック禁則 |
| LLM が `issue_category` を出さないバラツキ | 集計が `"other"` 過多になる | T1-1 でヒューリスティクスを箇条書きで明示、T1-5 実機で割合を観測 |
| past data の欠損で UI が空表示になる | T2-6 で「データなし」状態が長期化 | T1-7 push 後 24h 待機を T2-1 着手前提に明記 |
| shadcn tabs のバンドル影響 | T3-1 で +50KB を超過 | T3-1 完了条件で +10KB 以下を必須化、超過時は radix-ui 直接使用に切替検討 |
| Codex 連続レビューが収束しない | T1-6 / T2-7 / T3-8 でループ | 3 回連続 P2 が出続けたら対症療法アンチパターン圏内とみなし設計層に戻る |

---

## 完了日 / 特記事項

(クロージング時に追記)
