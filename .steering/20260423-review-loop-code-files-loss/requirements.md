# 要求内容: レビュー修正ループでの code_files 欠落バグの修正

## 背景

2026-04-20 11:22 UTC（20:22 JST）に Slack 経由で投入された "AWSのCloud Front" トピック（`exec-20260420112201-21b9ba13`）において、`workflow_plan.storage_targets` に `"github"` を含み、`generate_steps` に `iac_code` を含む構成であったにもかかわらず、GitHub リポジトリ `catch-expander-code` へ IaC コードが push されなかった。

### CloudWatch Logs + DynamoDB による事実確認

| 時刻（UTC） | ログ / 状態 | 意味 |
|---|---|---|
| 2026-04-20 11:39:55 | `Code files generated` | `code_files` 生成成功（独立パイプライン） |
| 2026-04-20 11:52:48 | `Fix attempt produced unparseable response, keeping previous deliverables` | レビュー修正 1 回目は解析失敗で `current_deliverables` 温存 |
| 2026-04-20 12:01:43 | `Deliverables updated by review fix` | レビュー修正 2 回目が成功し `current_deliverables = parsed` で置換 |
| 2026-04-20 12:04:35 | `Review loop limit reached` | 上限到達で抜け |
| 2026-04-20 12:04:36 | `Storage decision`（`code_files = None` 状態） | GitHub push 条件分岐が False |
| 2026-04-20 12:04:37 | `Notion page created` | Notion のみ格納 |
| 2026-04-20 12:04:39 | `Workflow completed` | status=`completed`（DynamoDB 上も `storage=notion`） |

## 根本原因

`src/agent/orchestrator.py:737-738`

```python
else:
    current_deliverables = parsed
```

- 修正用 `fix_prompt` は generator プロンプトを再利用しており、返却 JSON は text 成果物のみ（`orchestrator.py:457` コメントで明文化）
- その結果を `current_deliverables` に丸ごと代入することで、独立パイプラインで生成された `code_files` キーが失われる
- commit `49d11a2 fix: persist review-fix deliverables from review loop (M2)` で修正後成果物を採用する挙動を導入した際に、code_files 保護ロジックが欠落していた（既存回帰）

## 要求

### R1. レビュー修正後も `code_files` を保持する
- レビュー修正が解析成功した場合でも、直前の `current_deliverables` に `code_files` が存在していれば、修正後 deliverables にも引き継がれること
- generator 修正プロンプトが返す text 成果物（summary / content_blocks など）は修正版を採用する
- 他の独立生成フィールドが将来追加される場合も同じ方針で保護する余地を残す

### R2. 回帰防止ユニットテスト
- `_run_review_loop` を対象に、レビュー修正成功パスで `code_files` が保持されることを確認するテストを追加
- 既存の 125+ テストが引き続きパスすること

### R3. 4/20 CloudFront 成果物のリカバリ
- 修正コードをデプロイ後、Slack から同トピックを再投入して再生成
- 再生成が成功すれば `catch-expander-code` に `aws-のcloud-front-20260423` 等のディレクトリが作成される想定

### R4. ドキュメント同期
- 永続ドキュメント（`docs/`）への影響を確認。基本設計変更ではないため更新は不要と判断するが、設計書における「レビュー修正ループ」の記述に矛盾が生じないか確認する

## 制約・非目標

- **非目標**: `code_files` 以外の独立生成フィールドの大規模リファクタ、`_run_review_loop` の全体再設計
- **制約**: 既存ユニットテスト（orchestrator 関連を含む）に破壊的変更を加えない
- **制約**: デプロイは SAM による再デプロイを伴う。本 steering では修正 + テスト + main push までをスコープとし、`sam deploy` はユーザー判断で実施する

## 受け入れ条件

- [ ] `_run_review_loop` 修正後、既存全テストがパス
- [ ] 新規回帰テスト: レビュー修正成功時に `code_files` が `current_deliverables` に残存していることを検証
- [ ] `git log` に修正コミットが追加され、main にマージ可能な状態
- [ ] （本スコープ外: デプロイ後に Slack 再投入で CloudFront IaC が `catch-expander-code` に push されることを手動確認）
