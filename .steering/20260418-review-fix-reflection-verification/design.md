# 設計: Review fix 反映の追検証

## 設計方針

本 steering は **観測のみ**。コード変更は原則ゼロで、CloudWatch / DynamoDB / Notion API / GitHub から既存ログ・データを集めて反映確認するレシピを定義する。

ただし、現状では「修正前 deliverables」がどこにも保存されておらず before/after 差分が取れないため、**最低限の補助ログを 1 か所追加**する選択肢も用意する（M2 ロジック非変更）。

## 現状調査の結果

| 項目 | 現状 |
|------|------|
| fix loop 発火検知 | CloudWatch ログ `Deliverables updated by review fix` (extra: loop, issues_count) で検知可能 |
| fix loop 失敗検知 | CloudWatch ログ `Fix attempt produced unparseable response, keeping previous deliverables` で検知可能（requirements.md AC-3 のメッセージ名は実装と異なるため tasklist で整合させる）|
| 修正前 deliverables の保存 | **未保存**。`_run_review_loop` 内のローカル変数のみ |
| 修正後 deliverables の保存 | DynamoDB `catch-expander-deliverables` に最終版のみ保存（fix 後）|
| reviewer 指摘内容の保存 | `_run_review_loop` のローカル `review_result["issues"]` のみ。ログにも本文が残らない |
| Notion ページ本体 | Notion API 経由で取得可能 |
| GitHub コード | catch-expander-code リポジトリから取得可能 |

→ 「fix 前後の deliverables 差分」を直接比較する手段は現時点で無い。間接的に「reviewer 指摘の内容 → 最終成果物で解消されているか」を観察するアプローチが現実的。

## 影響範囲

| ファイル | 変更内容 | 必須/任意 |
|----------|----------|-----------|
| なし | 観測のみで AC-1〜AC-3 を完了する場合は変更なし | デフォルト |
| `src/agent/orchestrator.py` | （任意）`_run_review_loop` の発火時に reviewer issues 本文と修正後 summary を CloudWatch に diagnostics 形式で出力 | AC-2 を機械的に検証したい場合のみ |
| `tests/unit/agent/test_orchestrator.py` | （任意）上記ログ追加に伴うテスト | 上記が必須化された場合のみ |

`docs/` への追記は無し（観測手順は本 steering 内に閉じる）。

## 検証フロー

### Step 1: fix loop 発火 execution の捕捉（AC-1）

```bash
# 過去 N 日の発火を一括検出
aws logs filter-log-events \
  --log-group-name /ecs/catch-expander-agent \
  --filter-pattern '"Deliverables updated by review fix"' \
  --start-time $(date -d '7 days ago' +%s000) \
  --output json
```

該当が無い場合:

- 1 週間に一度自動実行する CloudWatch Insights クエリを設定（任意）
- もしくは reviewer が指摘を返しやすそうなトピック（出典が乏しい / 古い情報）を意図的に Slack 投入

### Step 2: 該当 execution のコンテキスト収集（AC-2 準備）

| データ | 取得元 | 内容 |
|--------|--------|------|
| execution metadata | DynamoDB `catch-expander-workflow-executions` | topic, category, status, slack_thread_ts |
| 最終 deliverables | DynamoDB `catch-expander-deliverables` | content_blocks, code_files, summary |
| reviewer issues | CloudWatch ログ近傍 | 現状ログ本文に出ない。要 (Step 4) 補助ログ |
| Notion ページ | Notion API（`pages.children`）| 本文ブロック |
| GitHub ファイル | catch-expander-code リポ | 該当 execution_id ディレクトリ |

### Step 3: 反映確認（AC-2）

3 系統で「修正後の状態」が一致していることを確認:

1. **DynamoDB**: deliverables.content_blocks の最新版が修正後である（reviewer issues に対応する文言が含まれる）
2. **Notion**: ページ本文が DynamoDB の content_blocks と同等
3. **GitHub**: code_files が含まれる場合、リポジトリ内ファイルが DynamoDB の files と同等

reviewer issues に「○○の出典が無い」とあれば、最終 content_blocks に該当の出典が追加されているか確認。

### Step 4: 補助ログ追加の判断（任意）

Step 1〜3 を 1 件回した結果、reviewer issues 本文 / 修正前後 deliverables 差分 が手元に無くて検証が難しい場合のみ、以下を追加:

```python
# orchestrator.py:_run_review_loop 内、fix 成功時
logger.info(
    "Review fix applied | execution_id=%s loop=%d issues_count=%d "
    "issues_preview=%r prev_summary=%r new_summary=%r",
    execution_id, loop, len(errors),
    json.dumps(errors)[:500],
    str(current_deliverables.get("summary", ""))[:200] if isinstance(current_deliverables, dict) else "",
    str(parsed.get("summary", ""))[:200],
)
```

- Phase A と同じ「メッセージ本文埋込」パターン
- summary は既に短いため preview でも要点が拾える
- issues は JSON serialize して 500 文字に切る
- 影響範囲: `_run_review_loop` 1 か所のみ

ロジック変更ではないため M2 動作には影響しない。テストは `caplog` で 1 件追加すれば十分。

### Step 5: パースエラー安全性確認（AC-3）

```bash
aws logs filter-log-events \
  --log-group-name /ecs/catch-expander-agent \
  --filter-pattern '"Fix attempt produced unparseable response"' \
  --start-time $(date -d '30 days ago' +%s000)
```

該当 execution があれば:

- DynamoDB `catch-expander-deliverables` の最終内容 = fix 前 deliverables（rebind されていない）であることを reasoning で確認
- Notion ページ本文も fix 前内容と一致していることを確認

### Step 6: N1 効果測定の集計（AC-4）

複数件蓄積後（目安 5 件以上）:

```bash
# fix loop 経過時間（INFO Generating 〜 Deliverables updated）と
# token 数（CloudWatch Insights で result.usage.* を集計）
```

集計結果から:

- 平均 fix loop 時間 > 60s かつ tokens 大 → N1（fix_prompt 差分化）の価値あり
- それ以下 → N1 不要、現状維持

## 検証フローの 1 サイクル所要時間

| ステップ | 想定 |
|---------|------|
| Step 1 検知 | ~5 分（aws logs filter-log-events 1 回）|
| Step 2 コンテキスト収集 | ~10 分 |
| Step 3 反映確認 | ~15 分（手作業比較）|
| Step 4 補助ログ追加（任意）| ~30 分（実装＋テスト）|
| Step 5 / Step 6 | 該当 execution が出るたび追加実施 |

## トリガー再開の判断基準

以下のいずれかが満たされた時点で本 steering を再開する:

- requirements.md「トリガー条件」を満たす execution が観測された
- 6 ヶ月以上トリガーが無い場合、意図的トピック投入で 1 件取得し検証する

## 非機能要件

- **後方互換**: M2 ロジックを変更しない。Step 4 補助ログ追加時もロジックには影響させない
- **観測コスト**: CloudWatch クエリ追加分のみ。Insights 月額への影響は無視可能範囲
- **セキュリティ**: reviewer issues / summary に外部入力が含まれる可能性があるため、補助ログでも 500 / 200 文字制限で漏洩リスクを抑制

## リスクと対処

| リスク | 対処 |
|--------|------|
| Step 1 検知後も Step 2 で reviewer issues 本文が手元に無い | Step 4 の補助ログを追加してから次回発火を待つ（2 回目検知時に検証可能になる）|
| Step 3 で「修正反映されていない」が判明 | 別 steering `[YYYYMMDD]-review-fix-rebind-fix/` を起票して M2 を見直す |
| 補助ログ追加で M2 ロジックを誤って変更 | 追加は `logger.info(...)` 1 行のみ。テストは caplog で挙動非変化を担保 |
| 6 ヶ月以上トリガーが無い | 意図的トピック投入時の手順を本 steering の追記として残す |

## スコープ外（再掲）

- M2 ロジックの再設計
- N1（fix_prompt 差分化）の実装
- 新しい reviewer 評価軸の追加
- deliverables history テーブルの新設（必要なら別 steering）
