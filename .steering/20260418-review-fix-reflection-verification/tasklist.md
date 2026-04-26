# タスクリスト: Review fix 反映の追検証

> **ステータス: 受動観測中 (2026-04-26 注記)**
>
> 本 steering は「fix loop の自然発火を待つ」受動観測タスクで、コード変更は不要。検証対象である `_run_review_loop` の M2 修正は `.steering/20260423-review-loop-code-files-loss/` の追加保護（`_PRESERVED_DELIVERABLE_FIELDS`）と組み合わさって `src/agent/orchestrator.py:802-826` に現存。
> なお、本 steering 起票後に code 成果物生成方式が刷新（`.steering/20260425-code-gen-redesign-filesystem/`）されたが、fix loop は text 成果物のみを扱うため検証手順自体は影響を受けない。
> 観測待機継続。

## 方針

- 受動観測タスク（fix loop 発火を待つ）。コード変更は原則ゼロ
- AC-1 トリガーが観測されたタイミングで AC-2 / AC-3 の検証ステップへ進む
- Step 4（補助ログ追加）は AC-2 が手元データだけで判定不能と確定したときのみ実施

## フェーズ 1: トリガー観測（受動 / AC-1）

### O1: fix loop 発火検知

- [ ] **O1-1** 直近 7 日の fix 発火を確認

```bash
aws logs filter-log-events \
  --log-group-name /ecs/catch-expander-agent \
  --filter-pattern '"Deliverables updated by review fix"' \
  --start-time $(date -u -d '7 days ago' +%s000) \
  --query 'events[*].[timestamp,message]' --output text
```

- [ ] **O1-2** 直近 30 日の fix 失敗（parse error）を確認

```bash
aws logs filter-log-events \
  --log-group-name /ecs/catch-expander-agent \
  --filter-pattern '"Fix attempt produced unparseable response"' \
  --start-time $(date -u -d '30 days ago' +%s000) \
  --query 'events[*].[timestamp,message]' --output text
```

- [ ] **O1-3** 検知ゼロが続く場合の判断基準を確認: 自然発火を待ち、Notion-block-length-limit 関連検証が一段落した後も 2 週間ゼロなら意図的トピック投入で 1 件取得（出典が乏しいニッチトピック / 最新の不安定情報）

### O2: AC-1 該当 execution の特定

- [ ] **O2-1** 検知済みログの `execution_id` を抽出（CloudWatch メッセージ extra 経由）
- [ ] **O2-2** 既知の発火事例を本 tasklist に追記（観測時の git SHA も記録）

#### 既観測（参考）

| 観測時刻 (UTC) | execution_id | 状態 | 備考 |
|---------------|--------------|------|------|
| 2026-04-18 08:51:11 | （要抽出）| Notion 400 で投入失敗 → AC-2 検証不可 | 当時 commit: 5e77dd3 |

## フェーズ 2: コンテキスト収集（AC-2 準備）

### C1: execution metadata と最終 deliverables 取得

- [ ] **C1-1** DynamoDB `catch-expander-workflow-executions` から topic / category / status / slack_thread_ts 取得

```bash
aws dynamodb get-item \
  --table-name catch-expander-workflow-executions \
  --key '{"execution_id":{"S":"<EXEC_ID>"}}' \
  --output json
```

- [ ] **C1-2** DynamoDB `catch-expander-deliverables` から content_blocks / code_files / summary 取得

```bash
aws dynamodb get-item \
  --table-name catch-expander-deliverables \
  --key '{"execution_id":{"S":"<EXEC_ID>"}}' \
  --output json > /tmp/<EXEC_ID>-deliverables.json
```

### C2: reviewer issues 本文の取得

- [ ] **C2-1** CloudWatch ログ近傍を時刻でフィルタし `Reviewing deliverables` / `Deliverables updated by review fix` 周辺を確認

```bash
aws logs filter-log-events \
  --log-group-name /ecs/catch-expander-agent \
  --filter-pattern '"<EXEC_ID>"' \
  --output text
```

- [ ] **C2-2** issues 本文がログに残っていないことを確認 → 残っていなければ Step 4（補助ログ）の追加要否を判断

### C3: Notion ページ取得

- [ ] **C3-1** Notion API でページブロックを取得（page_id は deliverables.notion_page_id 等から）

```bash
curl -s -H "Authorization: Bearer $NOTION_TOKEN" \
  -H "Notion-Version: 2022-06-28" \
  "https://api.notion.com/v1/blocks/<PAGE_ID>/children" \
  > /tmp/<EXEC_ID>-notion-blocks.json
```

### C4: GitHub コード取得（code_files が含まれる場合のみ）

- [ ] **C4-1** catch-expander-code リポジトリから該当 execution_id ディレクトリを取得

```bash
gh repo clone Shintaro-Abe/catch-expander-code /tmp/cec-clone || true
ls /tmp/cec-clone/<CATEGORY>/<EXEC_ID>/
```

## フェーズ 3: 反映確認（AC-2）

### V1: 3 系統一致確認

- [ ] **V1-1** DynamoDB の content_blocks に reviewer 指摘に対応する文言（出典追加、補足説明等）が含まれることを確認
- [ ] **V1-2** Notion ページ本文（C3 の JSON）が DynamoDB の content_blocks と一致することを確認（ブロック数 / type / 主要 text）
- [ ] **V1-3** GitHub の code_files が DynamoDB の code_files と一致することを確認（code_files が含まれる場合のみ）
- [ ] **V1-4** reviewer 指摘の少なくとも 1 件が修正版で解消されていることを確認

### V2: 結果の記録

- [ ] **V2-1** 観測結果を本 tasklist の「観測結果」セクションに追記（execution_id / git SHA / 確認結果 / 不一致があれば原因）

## フェーズ 4: パースエラー安全性確認（AC-3）

### P1: parse error 該当 execution の確認

- [ ] **P1-1** O1-2 で取得した parse error 該当 execution があれば execution_id を抽出
- [ ] **P1-2** DynamoDB `catch-expander-deliverables` の最終内容が fix 前 deliverables（rebind されていない）であることを reasoning で確認
- [ ] **P1-3** Notion ページ本文も fix 前内容と一致していることを確認
- [ ] **P1-4** ログメッセージ表記の整合確認: requirements.md は `Review fix returned parse error, keeping previous deliverables` だが実装は `Fix attempt produced unparseable response, keeping previous deliverables`。実装側の表記を正とし requirements.md は履歴として残す（更新不要）

## フェーズ 5: 補助ログ追加判断（任意 / Step 4）

### A1: 必要性の判定

- [ ] **A1-1** AC-2 検証 1 件回した結果、reviewer issues 本文 / 修正前後 deliverables 差分が手元に無くて検証が困難な場合のみ A2 へ進む
- [ ] **A1-2** 不要と判断した場合、本フェーズはスキップして本 steering 完了

### A2: 補助ログ実装（必要時のみ）

- [ ] **A2-1** `src/agent/orchestrator.py:_run_review_loop` の fix 成功時に `Review fix applied | execution_id=... loop=... issues_count=... issues_preview=... prev_summary=... new_summary=...` を `logger.info` で出力
- [ ] **A2-2** `tests/unit/agent/test_orchestrator.py` に caplog ベースのログ検証テストを 1 件追加
- [ ] **A2-3** `pytest tests/ -q` 全件パス
- [ ] **A2-4** 単一 commit + push（M2 ロジック非変更を git diff で再確認）
- [ ] **A2-5** デプロイ完了後、次の fix 発火を待ち AC-2 を機械的に再検証

## フェーズ 6: N1 効果測定（AC-4 / 5 件以上蓄積後）

### N1: 集計と判断

- [ ] **N1-1** 蓄積 execution が 5 件以上揃ったら CloudWatch Insights で fix loop 経過時間（`Generating` 〜 `Deliverables updated by review fix`）を集計
- [ ] **N1-2** result.usage 系のトークン消費を集計（取得可能なら）
- [ ] **N1-3** 平均 fix loop 時間 > 60s かつ tokens 大 → N1（fix_prompt 差分化）steering を新規起票
- [ ] **N1-4** それ以下 → 現状維持の判断を本 tasklist に記録

## 完了条件

- AC-1（O1〜O2）: fix loop 発火 execution を 1 件以上特定
- AC-2（V1〜V2）: 該当 execution で 3 系統反映を確認、reviewer 指摘の 1 件以上が解消
- AC-3（P1）: parse error 該当 execution があれば安全性確認、無ければ「観測待ち」として保留扱い
- AC-4（N1）: 5 件以上蓄積後の集計判断を記録、未達なら「蓄積待ち」として保留扱い

## 観測結果

（観測のたびに追記）

| 日時 (UTC) | execution_id | git SHA | AC | 結果 | 備考 |
|-----------|--------------|---------|----|------|------|
|  |  |  |  |  |  |

## リスクと対処

| リスク | 対処 |
|--------|------|
| fix loop が長期間発火しない | O1-3 のとおり 2 週間ゼロで意図的投入に切替 |
| AC-2 で 3 系統不一致が判明 | 別 steering `[YYYYMMDD]-review-fix-rebind-fix/` を起票し M2 を再検査 |
| C2 で issues 本文取得不可 | A2 の補助ログ追加を実施し次回発火を待つ |
| 補助ログ追加で M2 ロジックを誤って変更 | A2-1 は `logger.info(...)` 1 行のみ、git diff で確認 |
