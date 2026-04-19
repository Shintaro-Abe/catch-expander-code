# タスクリスト: Cloudflare ブロック時の Slack 案内とリトライ誘導

## 方針

- 単一 commit で実装＋テストを集約（例外クラス追加 / 検知 / ログ / Slack 分岐）
- push 後 1〜3 回の Slack 投入で AC-6 を確認（Cloudflare 403 が再現すれば AC-2・AC-5 も同時確認）
- 再現しない場合は「潜在問題として残置」扱いで完了、次回再現時に AC-5 のログから AC-7 の根本原因特定へ進む

## フェーズ 1: 実装

### T1: 例外クラスと判定ヘルパーの追加

- [x] **T1-1** `src/agent/storage/notion_client.py` に `NotionCloudflareBlockError(Exception)` を定義（`cf_ray` 属性を保持）
- [x] **T1-2** 定数 `_CLOUDFLARE_BLOCK_SIGNATURES = ("Attention Required! | Cloudflare", "cdn-cgi/styles/cf.errors.css")` を追加
- [x] **T1-3** `_is_cloudflare_block(status_code: int, body: str) -> bool` をモジュールレベルに追加
- [x] **T1-4** 定数 `_CF_HEADER_KEYS = ("CF-Ray", "cf-mitigated", "cf-cache-status", "Server")` / `_SENSITIVE_HEADER_KEYS = {"set-cookie", "authorization"}` を追加
- [x] **T1-5** `_extract_cf_headers(headers) -> dict[str, str]` をモジュールレベルに追加（`CF-Ray` 等を個別キーで、機微ヘッダ除外した全体を `response_headers` として str 化 1000 char truncate で格納）

### T2: `_request_with_retry` への組み込み

- [x] **T2-1** 既存の 4xx 分岐冒頭で `_is_cloudflare_block(e.response.status_code, response_body)` を判定
- [x] **T2-2** true の場合、既存 `logger.error` は**出さず**、`logger.warning("Notion request blocked by Cloudflare | method=... url=... status=403", extra={**cf_headers, "user_agent_sent": ..., "body_snippet": response_body[:200]})` を出力
- [x] **T2-3** `raise NotionCloudflareBlockError(f"... (CF-Ray={cf_ray})", cf_ray=cf_ray) from e` で送出
- [x] **T2-4** false の場合（通常 403 / 401 等）は既存の `logger.error` + `raise` を維持
- [x] **T2-5** 5xx 分岐（リトライ）は無変更であることを確認

### T3: `main._notify_task_failure` の分岐対応

- [x] **T3-1** `src/agent/main.py` に `from storage.notion_client import NotionCloudflareBlockError` を追加
- [x] **T3-2** `_notify_task_failure(slack_token: str, exc: BaseException | None = None)` にシグネチャ変更（`exc` は省略可能でデフォルト `None`）
- [x] **T3-3** `isinstance(exc, NotionCloudflareBlockError)` の分岐で専用メッセージを組み立て（requirements.md AC-2 の文言例に従う）
- [x] **T3-4** それ以外は既存の OAuth 文言を維持
- [x] **T3-5** `main()` の `except Exception` を `except Exception as exc:` に変更し `_notify_task_failure(slack_token, exc)` で呼ぶ

## フェーズ 2: テスト

### T4: `notion_client` 側ユニットテスト追加

既存 `tests/unit/agent/test_notion_client.py` に `TestCloudflareBlockDetection` クラスとして追加。

- [x] **T4-1** `test_cloudflare_block_raises_dedicated_exception` — 403 + body に `Attention Required! | Cloudflare` → `NotionCloudflareBlockError` 送出
- [x] **T4-2** `test_cloudflare_css_signature_also_detected` — 403 + body に `cdn-cgi/styles/cf.errors.css` のみ → `NotionCloudflareBlockError` 送出
- [x] **T4-3** `test_cloudflare_block_exposes_cf_ray` — 送出例外の `cf_ray` 属性にヘッダ値が入る
- [x] **T4-4** `test_notion_json_403_is_not_cloudflare` — 403 + JSON body（`{"object":"error","status":403,...}`）→ `NotionCloudflareBlockError` ではなく `HTTPError` が送出される
- [x] **T4-5** `test_401_is_not_cloudflare` — 401 → `HTTPError`
- [x] **T4-6** `test_500_retries_as_before` — 500 で既存リトライ回数が変わらない（回帰チェック）
- [x] **T4-7** `test_cloudflare_block_logs_cf_headers` — caplog で `cf_ray` / `cf_mitigated` / `user_agent_sent` が `extra` に含まれる
- [x] **T4-8** `test_sensitive_headers_are_not_logged` — `Set-Cookie` / `Authorization` が `response_headers` ログに出ないことを確認

### T5: `main` 側ユニットテスト追加

`tests/unit/agent/test_main.py` が既存であれば追記、無ければ新規作成。

- [x] **T5-1** 既存テストの有無を確認（既存あり）
- [x] **T5-2** `test_cloudflare_exception_sends_retry_message` — `exc=NotionCloudflareBlockError("...", cf_ray="r-1")` で `SlackClient.post_error` に渡される message が「Notion 前段（Cloudflare）」含有 + `execution_id` 含有を確認
- [x] **T5-3** `test_generic_exception_sends_oauth_message` — `exc=RuntimeError("boom")` で従来の OAuth 文言が送られる
- [x] **T5-4** 既存 `test_notifies_slack_and_reraises_on_failure` のシグネチャ変更対応（回帰チェック）

### T6: テスト全件パス確認

- [x] **T6-1** `pytest tests/ -q` で全件パス（計 **210 passed**: 既存 200 + 新規 10）

## フェーズ 3: コミット & デプロイ

- [x] **C1** 単一 commit を作成（`e148df1`: "feat: detect Cloudflare block on Notion 403 and guide slack retry"、7 files / +855 -8）
- [x] **C2** `git push origin main`（2e62d25..e148df1）
- [x] **C3** GitHub Actions run `24607025977` success（3m32s / 14:43:12〜14:46:44 UTC）

## フェーズ 4: 実機検証（AC-6）

### V1: Slack 投入と結果判定

- [x] **V1-1** Slack 投入（23:48 JST = 14:48:43 UTC `exec-20260418144822-33a2a5ab` / 00:09 JST = 15:09:11 UTC `exec-20260418150911-13b8268d` の計 2 件）
- [x] **V1-2** CloudWatch で `Notion page created` 確認（両 execution ともに出力）→ Cloudflare 非再現、V2 へ分岐
- [x] **V1-3** `Notion request blocked by Cloudflare` warning は**出力されず**
- [x] **V1-4** 他の致命的エラーも無し

### V2: Cloudflare 非再現時

- [x] **V2-1** 本 steering は「潜在問題として残置」扱いで完了。次回再現時に AC-5 ログから AC-7 を実施
- [x] **V2-2** 2 件で非再現確認済み。追加投入は不要と判断

### V3: Cloudflare 再現時（今回対象外）

今回は非再現のため本フェーズは未実施。次回再現観測時に実施する。

- [ ] **V3-1** Slack スレッドに「Notion 前段（Cloudflare）で拒否〜」メッセージが届いていることを確認
- [ ] **V3-2** DynamoDB `catch-expander-workflow-executions` の status=`failed`
- [ ] **V3-3** CloudWatch ログに `cf_ray` / `cf_mitigated` / `user_agent_sent` / `response_headers` が `extra` として残っていることを確認
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

### 実機検証（非再現）

| 日時 (UTC) | execution_id | git SHA | status | storage | 備考 |
|-----------|--------------|---------|--------|---------|------|
| 2026-04-18 14:48:43 | exec-20260418144822-33a2a5ab | e148df1 | completed | notion+github | Cloudflare 非再現、15m44s で完了 |
| 2026-04-18 15:09:11 | exec-20260418150911-13b8268d | e148df1 | completed | notion | Cloudflare 非再現、コード生成なしトピック |

### Cloudflare 再現観測

（再現したらここへ追記）

| 日時 (UTC) | execution_id | git SHA | cf_ray | cf_mitigated | user_agent_sent | 仮説判定 | 備考 |
|-----------|--------------|---------|--------|--------------|-----------------|---------|------|
|  |  |  |  |  |  |  |  |
