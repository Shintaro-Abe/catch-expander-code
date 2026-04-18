# タスクリスト: Cloudflare ブロック時の Slack 案内とリトライ誘導

## 方針

- 単一 commit で実装＋テストを集約（例外クラス追加 / 検知 / ログ / Slack 分岐）
- push 後 1〜3 回の Slack 投入で AC-6 を確認（Cloudflare 403 が再現すれば AC-2・AC-5 も同時確認）
- 再現しない場合は「潜在問題として残置」扱いで完了、次回再現時に AC-5 のログから AC-7 の根本原因特定へ進む

## フェーズ 1: 実装

### T1: 例外クラスと判定ヘルパーの追加

- [ ] **T1-1** `src/agent/storage/notion_client.py` に `NotionCloudflareBlockError(Exception)` を定義（`cf_ray` 属性を保持）
- [ ] **T1-2** 定数 `_CLOUDFLARE_BLOCK_SIGNATURES = ("Attention Required! | Cloudflare", "cdn-cgi/styles/cf.errors.css")` を追加
- [ ] **T1-3** `_is_cloudflare_block(status_code: int, body: str) -> bool` をモジュールレベルに追加
- [ ] **T1-4** 定数 `_CF_HEADER_KEYS = ("CF-Ray", "cf-mitigated", "cf-cache-status", "Server")` / `_SENSITIVE_HEADER_KEYS = {"set-cookie", "authorization"}` を追加
- [ ] **T1-5** `_extract_cf_headers(headers) -> dict[str, str]` をモジュールレベルに追加（`CF-Ray` 等を個別キーで、機微ヘッダ除外した全体を `response_headers` として str 化 1000 char truncate で格納）

### T2: `_request_with_retry` への組み込み

- [ ] **T2-1** 既存の 4xx 分岐冒頭で `_is_cloudflare_block(e.response.status_code, response_body)` を判定
- [ ] **T2-2** true の場合、既存 `logger.error` は**出さず**、`logger.warning("Notion request blocked by Cloudflare | method=... url=... status=403", extra={**cf_headers, "user_agent_sent": ..., "body_snippet": response_body[:200]})` を出力
- [ ] **T2-3** `raise NotionCloudflareBlockError(f"... (CF-Ray={cf_ray})", cf_ray=cf_ray) from e` で送出
- [ ] **T2-4** false の場合（通常 403 / 401 等）は既存の `logger.error` + `raise` を維持
- [ ] **T2-5** 5xx 分岐（リトライ）は無変更であることを確認

### T3: `main._notify_task_failure` の分岐対応

- [ ] **T3-1** `src/agent/main.py` に `from storage.notion_client import NotionCloudflareBlockError` を追加
- [ ] **T3-2** `_notify_task_failure(slack_token: str, exc: BaseException | None = None)` にシグネチャ変更（`exc` は省略可能でデフォルト `None`）
- [ ] **T3-3** `isinstance(exc, NotionCloudflareBlockError)` の分岐で専用メッセージを組み立て（requirements.md AC-2 の文言例に従う）
- [ ] **T3-4** それ以外は既存の OAuth 文言を維持
- [ ] **T3-5** `main()` の `except Exception` を `except Exception as exc:` に変更し `_notify_task_failure(slack_token, exc)` で呼ぶ

## フェーズ 2: テスト

### T4: `notion_client` 側ユニットテスト追加

既存 `tests/unit/agent/test_notion_client.py` に `TestCloudflareBlockDetection` クラスとして追加。

- [ ] **T4-1** `test_cloudflare_block_raises_dedicated_exception` — 403 + body に `Attention Required! | Cloudflare` → `NotionCloudflareBlockError` 送出
- [ ] **T4-2** `test_cloudflare_css_signature_also_detected` — 403 + body に `cdn-cgi/styles/cf.errors.css` のみ → `NotionCloudflareBlockError` 送出
- [ ] **T4-3** `test_cloudflare_block_exposes_cf_ray` — 送出例外の `cf_ray` 属性にヘッダ値が入る
- [ ] **T4-4** `test_notion_json_403_is_not_cloudflare` — 403 + JSON body（`{"object":"error","status":403,...}`）→ `NotionCloudflareBlockError` ではなく `HTTPError` が送出される
- [ ] **T4-5** `test_401_is_not_cloudflare` — 401 → `HTTPError`
- [ ] **T4-6** `test_500_retries_as_before` — 500 で既存リトライ回数が変わらない（回帰チェック）
- [ ] **T4-7** `test_cloudflare_block_logs_cf_headers` — caplog で `cf_ray` / `cf_mitigated` / `user_agent_sent` が `extra` に含まれる
- [ ] **T4-8** `test_sensitive_headers_are_not_logged` — `Set-Cookie` / `Authorization` が `response_headers` ログに出ないことを確認

### T5: `main` 側ユニットテスト追加

`tests/unit/agent/test_main.py` が既存であれば追記、無ければ新規作成。

- [ ] **T5-1** 既存テストの有無を `ls tests/unit/agent/test_main.py` で確認
- [ ] **T5-2** `test_notify_task_failure_cloudflare_sends_retry_message` — `exc=NotionCloudflareBlockError("...", cf_ray="abc")` で `SlackClient.post_error` に渡される message が「Notion 前段（Cloudflare）」で始まることを確認（`execution_id` 含有もチェック）
- [ ] **T5-3** `test_notify_task_failure_generic_sends_oauth_message` — `exc=RuntimeError("boom")` で従来の OAuth 文言が送られる
- [ ] **T5-4** `test_notify_task_failure_no_channel_skips` — `SLACK_CHANNEL` 未設定で早期 return（回帰チェック）

### T6: テスト全件パス確認

- [ ] **T6-1** `pytest tests/ -q` で全件パス（既存 200 + 新規 11 前後 → 計 211 前後）

## フェーズ 3: コミット & デプロイ

- [ ] **C1** 単一 commit を作成（T1〜T6 の成果物を包含）
- [ ] **C2** `git push origin main` で GitHub Actions `build-agent.yml` 経由デプロイ
- [ ] **C3** デプロイ完了確認（Actions run の success、所要時間は過去実績 3〜4 分）

## フェーズ 4: 実機検証（AC-6）

### V1: Slack 投入と結果判定

- [ ] **V1-1** Slack でトピックを 1 件投入（通常の投入で問題ない。Cloudflare 再現は確率的）
- [ ] **V1-2** CloudWatch で `Notion page created` が出れば → Cloudflare 非再現。V2 へ
- [ ] **V1-3** CloudWatch で `Notion request blocked by Cloudflare` warning が出れば → V3 へ
- [ ] **V1-4** いずれも出ず他エラーの場合 → 本 steering 対象外として記録し終了

### V2: Cloudflare 非再現時

- [ ] **V2-1** 本 steering は「潜在問題として残置」扱いで完了。次回再現時に AC-5 ログから AC-7 を実施
- [ ] **V2-2** 必要であれば追加で 1〜2 回投入し、一定期間（例: 1 週間）ゼロなら正式に完了扱い

### V3: Cloudflare 再現時

- [ ] **V3-1** Slack スレッドに「Notion 前段（Cloudflare）で拒否〜」メッセージが届いていることを確認（AC-2 達成）
- [ ] **V3-2** DynamoDB `catch-expander-workflow-executions` の status=`failed`
- [ ] **V3-3** CloudWatch ログに `cf_ray` / `cf_mitigated` / `user_agent_sent` / `response_headers` が `extra` として残っていることを確認（AC-5 達成）
- [ ] **V3-4** ユーザーに「時間を空けて再投入してください」旨が伝わる形になっているか目視で最終確認

## フェーズ 5: 根本原因特定（AC-7）

再現ログが 1 件以上蓄積された時点で実施。

### N1: ログ解析

- [ ] **N1-1** CloudWatch Insights で以下クエリを実行

  ```
  fields @timestamp, @message, cf_ray, cf_mitigated, cf_cache_status, user_agent_sent, body_snippet
  | filter @message like /blocked by Cloudflare/
  | sort @timestamp desc
  ```

- [ ] **N1-2** `cf_mitigated` 値の確認
  - `challenge` / `block` → WAF / Bot management ルール該当
  - 空 / `""` → IP reputation もしくは managed rule
- [ ] **N1-3** `user_agent_sent` の確認
  - `python-requests/*` デフォルト → 仮説 (A) の線強化
- [ ] **N1-4** 可能であれば Fargate task の public IP を VPC Flow Log から特定し、AbuseIPDB / Cloudflare Radar で IP 評判を照会（仮説 (E) 検証）

### N2: 仮説判定と次アクション記録

- [ ] **N2-1** 仮説 (A)〜(E) のうちどれが支配的かを本 tasklist「観測結果」セクションに追記
- [ ] **N2-2** (E)（IP 評判）が支配的 → 現 fix は妥当。継続
- [ ] **N2-3** (A)（User-Agent）が支配的 → 別 steering `[YYYYMMDD]-notion-user-agent-override/` を起票検討
- [ ] **N2-4** (B)/(D) が支配的 → fix 方針再検討。Slack 文言の更なる修正が必要か判断

## 完了条件

- T1〜T6 全達成
- C1〜C3 全達成
- V1 まで実施済み（V2 または V3 のいずれかに分岐完了）
- AC-1 / AC-3 / AC-4 / AC-5 は実装 + テストで全充足
- AC-2 は V3 到達時のみ機械的確認。V2 止まりの場合は「実装上は達成、実機再現待ち」として閉じる
- AC-6 は V1 以降で完了
- AC-7 は N1/N2 まで到達後。再現蓄積まで保留扱い可

## リスクと対処

| リスク | 対処 |
|--------|------|
| Cloudflare 403 が V1 で再現しない | V2 の通り潜在問題として残置し、次回再現時に AC-7 を実施 |
| Cloudflare ブロックページ文言が変わり検知漏れ | T4-1/T4-2 テストが FAIL することで気付く。`_CLOUDFLARE_BLOCK_SIGNATURES` に新文言追加で対応 |
| Notion JSON 403 を誤検知 | T4-4 テストで担保。誤検知時は signature を厳格化（HTML 特有の他ワード追加）|
| `_notify_task_failure` のシグネチャ変更で他所が壊れる | T3-2 で `exc=None` デフォルト維持 + grep で呼び出し元確認（現状 `main.py` 内 1 箇所のみ） |
| テストで `response.headers` のモック構造差異 | 既存 `test_notion_client.py` の `_request_with_retry` テストパターンに揃える。必要なら `MagicMock` で `headers.items()` / `headers.get(key)` の両対応 |
| AC-7 N1-4 の VPC Flow Log が未有効 | 有効化は infra 変更のため本 steering のスコープ外。Fargate task の `networkInterfaces[].attachment.details` から IP が取れれば代替可 |

## 観測結果

（再現観測のたびに追記）

| 日時 (UTC) | execution_id | git SHA | cf_ray | cf_mitigated | user_agent_sent | 仮説判定 | 備考 |
|-----------|--------------|---------|--------|--------------|-----------------|---------|------|
|  |  |  |  |  |  |  |  |
