# 要求定義: Cloudflare ブロック時の Slack 案内とリトライ誘導

## 問題

Notion API 前段の Cloudflare が、Fargate タスクからのリクエストに対して `HTTP 403 Forbidden` を HTML（`Attention Required! | Cloudflare`）で返すことがある。

実機観測:

- execution 開始: 2026-04-18 10:22:58 UTC
- 失敗時刻: 2026-04-18 10:56:01 UTC
- Notion API レスポンス: `403 Client Error: Forbidden for url: https://api.notion.com/v1/pages`
- レスポンス body: Cloudflare の `Attention Required!` HTML
- 同じ時間帯にローカル端末から同一トークン / `curl` で `POST /v1/pages` を実行した場合は 200 応答

本件は Notion-block-length-limit の修正（`_split_long_rich_text`）投入後に初めて顕在化した別事象であり、2000 char 制限とは独立した問題である。

### 根本原因は未確定（仮説の並置）

ローカル成功 / Fargate 失敗の差分は IP だけでなく、User-Agent（`curl` vs `python-requests`）・TLS fingerprint（OpenSSL vs urllib3）・payload サイズ・ヘッダセット等が**同時に**変わっているため、以下のいずれも現状の観測データからは排除できない:

- **(E) IP reputation**: Fargate 共有 public IP プールの当該 IP が Cloudflare で汚染スコア
- **(A) User-Agent**: `python-requests/*` が Cloudflare の managed bot rule に該当
- **(B) JA3 fingerprint**: urllib3 の TLS handshake が automation 判定
- **(C) AWS IP range challenge**: Notion 側 WAF が AWS IP 範囲に challenge 設定
- **(D) Payload pattern**: ~300KB の POST が "atypical request" 判定

したがって本 steering では根本原因を断定せず、「Cloudflare 起因の 403」として扱い、**次回再現時に切り分けられる観測情報を残しつつ**、ユーザーが状況を把握できる Slack 案内を出すことを目的とする。

## 影響

- ユーザーが Slack 投入したリクエストが、最終段（Notion 投入）で 403 によって失敗する
- 既存の例外ハンドリングでは汎用エラーとして処理され、Slack に「OAuth 切れの可能性」を案内する汎用文言が送られるため、ユーザーから見ると根本原因が誤認される
- 再実行すれば別の public IP に割り当て直され通る可能性が高いが、ユーザーはその判断材料を持たない

## 目的

Cloudflare 起因の 403 ブロックを検知した場合、**(1) ユーザーが状況を理解してリトライ判断できる Slack 案内を送る** と **(2) 次回再現時に根本原因を特定できる観測情報（レスポンスヘッダ等）をログに残す** の 2 点を同時に満たす。コードや infra の自動リトライ / IP 切替は行わない（コスト・複雑度の観点で別 steering として分離）。

## ユーザーストーリー

- Slack 投入ユーザーとして、Notion 投入が Cloudflare で拒否された場合、その旨と「しばらく時間を空けて再投入してみる」程度の行動指針をスレッドで受け取りたい。現状の汎用エラー（OAuth 切れ案内）だと原因を誤解してトークン更新の無駄作業をしてしまうため
- 運用者として、Cloudflare 起因 403 の再発 1〜2 件で根本原因（IP / UA / JA3 / Payload 等）を切り分けられるよう、Cloudflare レスポンスヘッダをログに残したい

## 受け入れ条件

### AC-1（Must）Cloudflare 403 の検知

`notion_client._request_with_retry` が Notion API から以下の条件を満たすレスポンスを受け取った場合、これを「Cloudflare IP ブロック」として識別する:

- HTTP status が `403`
- response body（HTML / text）に `Cloudflare` または `Attention Required` のいずれかが含まれる

識別時は、既存の汎用 `HTTPError` ではなく本件専用の例外（例: `NotionCloudflareBlockError`）を送出する。通常の Notion 403（権限不足 = JSON body）は影響を受けず従来通り処理する。

### AC-2（Must）Slack 案内メッセージの送信

orchestrator / main で AC-1 の専用例外を捕捉し、該当 execution の `slack_thread_ts` 宛に以下の要件を満たすメッセージを送信する:

- **言語**: 日本語
- **トーン**: ユーザー向け平易表現、**断言を避ける**（根本原因が未確定のため）
- **必須要素**:
  - Notion 前段（Cloudflare）でリクエストが拒否された旨
  - 数分〜数十分空けての再投入を試してほしい旨（「成功する可能性が高い」等の断言はしない）
  - 繰り返し失敗する場合は連絡してほしい旨
  - 実行 ID（デバッグ用）
- **文言例**: 「Notion 前段（Cloudflare）でリクエストが拒否されたため、保存に失敗しました。数分〜数十分ほど時間を空けて再投入をお試しください。繰り返し失敗する場合はログを確認しますのでお知らせください。execution_id: `xxx`」

送信後、execution は従来通り `failed` として DynamoDB に記録する（自動リトライは行わない）。

### AC-3（Must）通常エラーとの区別

以下のケースは本件扱いとせず、既存の汎用エラーハンドリングに委ねる:

- 403 かつ body に Cloudflare 文字列が含まれない（= Notion の権限不足 / integration 未共有）
- 401 / 429 / 500 系
- 接続エラー（DNS / TLS / timeout）

### AC-4（Must）ユニットテスト

以下のテストケースを `tests/unit/agent/test_notion_client.py` に追加する:

- 403 + Cloudflare HTML body → `NotionCloudflareBlockError` 送出
- 403 + `Attention Required` を含む body → `NotionCloudflareBlockError` 送出
- 403 + JSON body（Cloudflare 文字列なし）→ 従来通り `HTTPError` 送出
- 401 / 500 → 従来通り `HTTPError` 送出（本件例外にはならない）

Slack 送信側（orchestrator 層）の単体テストは design 段階で既存テスト資産を確認したうえで判断する。

### AC-5（Must）ログ出力（根本原因切り分け用）

AC-1 検知時に `logger.warning` で以下を出力する（**これが根本原因仮説 (A)〜(E) の切り分けに直結するため Must 扱い**）:

- メッセージ: `Notion request blocked by Cloudflare | execution_id=... status=403`
- extra フィールド（または message 連結）で以下を記録:
  - `cf_ray`: `CF-Ray` ヘッダ値（Cloudflare のリクエスト ID、サポート問い合わせに必須）
  - `cf_mitigated`: `cf-mitigated` ヘッダ値（存在すれば `challenge` / `block` 等が入る）
  - `cf_cache_status`: `cf-cache-status` ヘッダ値
  - `server`: `Server` ヘッダ値（`cloudflare` であることの確証）
  - `response_headers`: 取得可能なレスポンスヘッダ全体（dict → str で可）
  - `body_snippet`: 先頭 200 char 程度
  - `user_agent_sent`: 送信した `User-Agent`（python-requests のデフォルトか上書きか）

この情報があれば、次回再現 1 件で (A)〜(E) のどれが支配的かをほぼ確定できる。

### AC-6（Must）実機検証

実装 push 後、以下で検証する:

- Slack 投入を 1〜3 回実施
- いずれかで Cloudflare 403 が再現した場合、Slack に想定メッセージが届き execution が `failed` で記録されていることを確認
- 再現しない場合、本件は「現時点では潜在問題として残置」として完了扱いとし、次回再現時の観測を tasklist に記録する形とする

### AC-7（Must）次回再現時の根本原因特定

AC-5 で追加したログが有効な形で 1 件以上蓄積された時点で、以下を実施する:

- `cf_ray` / `cf_mitigated` / `response_headers` / `user_agent_sent` を確認
- 仮説 (A)〜(E) のうちどれが最も妥当かを tasklist に記録
- 根本原因が (A)/(B)/(D)（IP 以外）と判明した場合、Slack 案内文言および fix 方針を再検討する（別 steering 起票も選択肢）

## 制約事項

- `notion_client.py` の公開 API シグネチャは変更しない
- 例外クラスは `notion_client` モジュール内に定義し orchestrator から import する形とする
- Slack 送信は既存の Slack 通知経路（`slack_client` など）を再利用し、新しいチャネルは作らない
- 自動リトライは行わない（別 IP 割り当てを期待する場合でも、Fargate タスクの再起動が必要なため単純な再試行では効果が薄い）

## 対象外

- 出口 IP の固定化（NAT Gateway / NAT Instance / Cloudflare Worker 経由 / 外部 VPS 経由 等）
- 自動リトライ機構（タスク再起動を含む）
- Notion 以外の API（GitHub / Slack）の同種対策
- Slack 汎用エラー文言の全面見直し（別 steering）
- Cloudflare 側への IP 登録申請

## トリガー条件

本 steering は即時実装対象（観測済みのため）。

## リスクと対処

| リスク | 対処 |
|--------|------|
| Cloudflare ブロック判定の誤検知（通常の Notion 403 を Cloudflare 扱い） | body 文字列の二重条件（`Cloudflare` AND HTML フォーマット的な特徴）で厳格化。AC-4 のテストで担保 |
| 実機検証時に Cloudflare 403 が再現しない | AC-6 のとおり「潜在問題として残置」扱いとし、次回再現時に観測結果を追記する運用とする |
| Slack メッセージが連投される（複数 execution が同時にブロックされた場合） | 各 execution で 1 通のみ送信する設計（重複抑止ロジックは持たない = シンプルさ優先） |
| 将来 Cloudflare のブロックページ文言が変わる | 判定条件を定数化し、変更時は AC-4 テスト追加で検知 |
| 根本原因が IP 以外（UA / JA3 / Payload）で、リトライしても解消しない | Slack 文言を断言しない表現に抑える（AC-2）。AC-5/7 で根本原因を特定後、fix 方針を再検討 |
| レスポンスヘッダに機微情報が含まれる（`Set-Cookie` 等） | ログ出力時に `Set-Cookie` / `Authorization` を除外する処理を AC-5 実装時に入れる |
