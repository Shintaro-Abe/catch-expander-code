# 設計: Cloudflare ブロック時の Slack 案内とリトライ誘導

## 設計方針

1. **検知**: `notion_client._request_with_retry` 内で 403 かつ body に Cloudflare シグネチャが含まれる場合に、専用例外 `NotionCloudflareBlockError` を送出する
2. **観測**: 同箇所で Cloudflare レスポンスヘッダ（`CF-Ray` / `cf-mitigated` / `cf-cache-status` / `Server`）と送信 `User-Agent` を `logger.warning` に `extra` として記録する（根本原因 (A)〜(E) 切り分け用）
3. **案内**: `main._notify_task_failure` を分岐し、最上位で捕捉した例外が `NotionCloudflareBlockError` の場合は専用の Slack メッセージを送信する（既存の汎用「OAuth 切れ」文言は使わない）
4. **責務分離**: 自動リトライ / IP 切替は**行わない**。 DynamoDB 更新は既存フロー（`_run_orchestrator` の `failed` 更新）をそのまま使う

呼び出し元（orchestrator / generator）は無変更。

## 現状調査の結果

| 項目 | 現状 |
|------|------|
| Notion 403 の発生箇所 | `src/agent/storage/notion_client.py:_request_with_retry` で `response.raise_for_status()` が `HTTPError` を送出 |
| 既存の 4xx ハンドリング | body 先頭 1000 char を `logger.error` に記録後、即 `raise`（リトライしない） |
| 最上位例外捕捉 | `src/agent/main.py:main()` の `except Exception` → `_notify_task_failure` で Slack へ汎用文言（「Claude OAuth トークンが期限切れ〜」）を送信 |
| 既存 Slack 汎用文言 | `main._notify_task_failure` に**ハードコード**されている。エラー種別で分岐する機構は無い |
| Slack クライアント | `notify/slack_client.SlackClient.post_error(channel, thread_ts, error_message)` が利用可能 |
| response.headers へのアクセス | `requests.HTTPError.response.headers` で取得可能（dict-like） |
| User-Agent | 未設定。`requests` デフォルト（`python-requests/X.Y.Z`） |
| 観測された body | `<!DOCTYPE html>... Attention Required! \| Cloudflare` |

## 影響範囲

| ファイル | 変更内容 | 必須/任意 |
|----------|----------|-----------|
| `src/agent/storage/notion_client.py` | `NotionCloudflareBlockError` 例外クラス定義 / Cloudflare シグネチャ定数 / `_request_with_retry` の 4xx 分岐に Cloudflare 判定とヘッダログを追加 | 必須 |
| `src/agent/main.py` | `_notify_task_failure` を `NotionCloudflareBlockError` で分岐し専用メッセージを送信 | 必須 |
| `tests/unit/agent/test_notion_client.py` | Cloudflare 検知 / 通常 403 非検知 / ヘッダログ出力のテスト追加 | 必須 |
| `tests/unit/agent/test_main.py`（無ければ新規）| `_notify_task_failure` の分岐テスト | 必須 |
| なし | orchestrator / generator / Slack クライアントは無変更 | - |

## 例外クラス定義

```python
# src/agent/storage/notion_client.py

class NotionCloudflareBlockError(Exception):
    """Notion 前段の Cloudflare がリクエストを 403 でブロックしたことを示す例外。

    IP reputation / User-Agent / JA3 fingerprint / Payload pattern 等の複合要因で
    Cloudflare が automation と判定したケースをまとめて扱う。Notion 本体の
    権限エラー（JSON で返る 403）はこの例外では扱わない。
    """

    def __init__(self, message: str, cf_ray: str | None = None) -> None:
        super().__init__(message)
        self.cf_ray = cf_ray  # サポート問い合わせ時に引用する識別子
```

## Cloudflare 検知ロジック

### 判定条件

以下を**すべて**満たす場合を Cloudflare ブロックと判定:

1. `response.status_code == 403`
2. 以下のいずれかの文字列が `response.text`（body）に含まれる:
   - `Attention Required! | Cloudflare`
   - `cdn-cgi/styles/cf.errors.css`（Cloudflare エラーページ共通アセット）

`Cloudflare` 単独では判定しない（Notion JSON にも「Cloudflare」が偶発的に含まれる可能性を排除）。上記 2 文字列は Cloudflare エラーページ HTML にほぼ確実に含まれる（観測済み body で両方存在）。

### 実装スケッチ

```python
_CLOUDFLARE_BLOCK_SIGNATURES = (
    "Attention Required! | Cloudflare",
    "cdn-cgi/styles/cf.errors.css",
)


def _is_cloudflare_block(status_code: int, body: str) -> bool:
    if status_code != 403:
        return False
    return any(sig in body for sig in _CLOUDFLARE_BLOCK_SIGNATURES)
```

### `_request_with_retry` への組み込み

既存の 4xx 分岐に**判定と例外差し替えとヘッダログ**を追加:

```python
except requests.HTTPError as e:
    last_error = e
    response_body = ""
    with contextlib.suppress(Exception):
        response_body = e.response.text[:1000]

    if e.response.status_code < 500:
        if _is_cloudflare_block(e.response.status_code, response_body):
            # Cloudflare ブロック専用の観測ログ（根本原因切り分け用）
            cf_headers = _extract_cf_headers(e.response.headers)
            cf_ray = cf_headers.get("cf_ray")
            logger.warning(
                "Notion request blocked by Cloudflare | method=%s url=%s status=403",
                method,
                url,
                extra={
                    **cf_headers,
                    "user_agent_sent": self.headers.get("User-Agent", "<default>"),
                    "body_snippet": response_body[:200],
                },
            )
            raise NotionCloudflareBlockError(
                f"Notion request blocked by Cloudflare (CF-Ray={cf_ray})",
                cf_ray=cf_ray,
            ) from e

        logger.error(
            "Notion API client error | method=%s url=%s status=%d response_body=%r",
            method, url, e.response.status_code, response_body,
        )
        raise
    # 5xx は既存ロジック継続
```

### ヘッダ抽出ヘルパー

```python
_CF_HEADER_KEYS = ("CF-Ray", "cf-mitigated", "cf-cache-status", "Server")
_SENSITIVE_HEADER_KEYS = {"set-cookie", "authorization"}


def _extract_cf_headers(headers) -> dict[str, str]:
    """Cloudflare 識別用ヘッダを dict で返す。機微ヘッダは除外する。"""
    result: dict[str, str] = {}
    for key in _CF_HEADER_KEYS:
        value = headers.get(key)
        if value is not None:
            result[key.lower().replace("-", "_")] = value
    # 全レスポンスヘッダのサマリ（機微ヘッダ除外）
    safe_headers = {
        k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADER_KEYS
    }
    result["response_headers"] = str(safe_headers)[:1000]
    return result
```

## 案内メッセージ送信

### 実装箇所

`src/agent/main.py:_notify_task_failure` を、捕捉した例外 `exc` を受け取るシグネチャに変更し、`NotionCloudflareBlockError` で分岐:

```python
def _notify_task_failure(slack_token: str, exc: BaseException | None = None) -> None:
    channel = os.environ.get("SLACK_CHANNEL", "")
    thread_ts = os.environ.get("SLACK_THREAD_TS", "")
    if not channel or not thread_ts:
        logger.warning("SLACK_CHANNEL or SLACK_THREAD_TS not set, skipping failure notification")
        return

    execution_id = os.environ.get("EXECUTION_ID", "<unknown>")

    if isinstance(exc, NotionCloudflareBlockError):
        message = (
            "Notion 前段（Cloudflare）でリクエストが拒否されたため、保存に失敗しました。\n"
            "数分〜数十分ほど時間を空けて再投入をお試しください。\n"
            "繰り返し失敗する場合はログを確認しますのでお知らせください。\n"
            f"execution_id: `{execution_id}`"
        )
    else:
        message = (
            "タスクの処理中にエラーが発生しました。\n"
            "Claude OAuthトークンが期限切れの場合は、開発環境で `claude` コマンドを実行して再ログインしてください。"
        )

    try:
        SlackClient(slack_token).post_error(channel, thread_ts, message)
    except Exception:
        logger.warning("Failed to send failure notification to Slack")
```

`main()` の呼び出しを修正:

```python
except Exception as exc:
    logger.exception("Task failed")
    _notify_task_failure(slack_token, exc)
    raise
```

### import

`main.py` で `from storage.notion_client import NotionCloudflareBlockError` を追加（既存の import スタイルに追従）。

### 例外チェーンによる判定

`orchestrator._run_orchestrator` は `raise` で例外を再送出しているため、`main()` の `except Exception as exc` で捕捉される `exc` は元の `NotionCloudflareBlockError` そのもの。`isinstance(exc, NotionCloudflareBlockError)` で直接判定可能。`raise ... from e` のチェーンを辿る必要は無い（最上位例外自体が本例外のため）。

## ユニットテスト計画

### `tests/unit/agent/test_notion_client.py`

| テスト名 | ケース |
|---------|--------|
| `test_cloudflare_block_raises_dedicated_exception` | status=403 + body に `Attention Required! \| Cloudflare` → `NotionCloudflareBlockError` 送出 |
| `test_cloudflare_css_signature_also_detected` | status=403 + body に `cdn-cgi/styles/cf.errors.css` のみ → `NotionCloudflareBlockError` 送出 |
| `test_cloudflare_block_exposes_cf_ray` | 送出された例外の `cf_ray` 属性にヘッダ値が入る |
| `test_notion_json_403_is_not_cloudflare` | status=403 + JSON body（`{"object":"error","status":403,"code":"restricted_resource"}`）→ 通常 `HTTPError` 送出、`NotionCloudflareBlockError` ではない |
| `test_401_is_not_cloudflare` | status=401 → 従来通り `HTTPError` |
| `test_500_is_not_cloudflare_and_retries` | status=500 → 既存リトライ動作継続（変更なし確認）|
| `test_cloudflare_block_logs_response_headers` | caplog で `cf_ray` / `cf_mitigated` / `user_agent_sent` が `extra` に含まれることを確認 |
| `test_sensitive_headers_are_not_logged` | `Set-Cookie` / `Authorization` がログに出ないことを確認 |

`requests` は既存テストと同じく `unittest.mock.patch` で `requests.request` をモック。`response.text` / `response.headers` / `response.status_code` / `response.raise_for_status()` を仕込む。

### `tests/unit/agent/test_main.py`（新規または既存追記）

| テスト名 | ケース |
|---------|--------|
| `test_notify_task_failure_cloudflare_sends_retry_message` | `exc=NotionCloudflareBlockError(...)` で呼び出し、`SlackClient.post_error` に渡される message が「Notion 前段（Cloudflare）」で始まることを確認 |
| `test_notify_task_failure_generic_sends_oauth_message` | `exc=RuntimeError(...)` で呼び出し、従来の OAuth 文言が送られることを確認 |
| `test_notify_task_failure_no_channel_skips` | `SLACK_CHANNEL` 未設定で早期 return（既存挙動の回帰チェック）|

## 検証フロー

### Step 1: ユニットテスト全件パス

```bash
pytest tests/ -q
```

既存 200 passed + 新規 11 → 計 211 passed を目標。

### Step 2: push / deploy

- 単一 commit で push（例外クラス + 検知 + 分岐 + テスト）
- GitHub Actions `build-agent.yml` の success を確認

### Step 3: 実機検証（AC-6）

- Slack 投入を 1〜3 回実施
- **期待 1**: 通常成功（Cloudflare 非再現）→ 既存の完了通知が届く
- **期待 2**: Cloudflare 403 再現 → 新 Slack 文言が届き、CloudWatch に `Notion request blocked by Cloudflare | ...` warning と `cf_ray` 等の `extra` が残る
- 非再現の場合は AC-6 の通り「潜在問題として残置」扱いで完了

### Step 4: 根本原因特定（AC-7、観測蓄積後）

- CloudWatch Insights クエリ例:

  ```
  fields @timestamp, @message, cf_ray, cf_mitigated, user_agent_sent, body_snippet
  | filter @message like /blocked by Cloudflare/
  | sort @timestamp desc
  ```

- `cf_mitigated` の値 → `challenge` / `block` / 空 で仮説絞り込み
- `user_agent_sent` が `python-requests/*` だった場合は仮説 (A) の線を強化 / 別の UA を試す検証へ

## 非機能要件

- **後方互換**: `NotionClient` の公開メソッドシグネチャ無変更。既存呼び出し元への影響なし
- **パフォーマンス**: 正常系（200）では追加処理ゼロ。403 時のみヘッダ走査 + 文字列 `in` 2 回（無視可能）
- **メモリ**: レスポンスヘッダ dict の shallow copy のみ
- **ログ量**: 403 時に 1 件追加（既存 `error` が `warning` + `error` の 2 行にならない設計 = Cloudflare 時は `error` を出さず `warning` だけに絞る）
- **機微情報**: `Set-Cookie` / `Authorization` を `_SENSITIVE_HEADER_KEYS` で除外
- **冪等性**: 正常系は無変更

## リスクと対処

| リスク | 対処 |
|--------|------|
| Cloudflare ブロックページ文言が変わる | `_CLOUDFLARE_BLOCK_SIGNATURES` タプルに新文言追加で対応。AC-4 テスト更新 |
| Notion が独自の 403 HTML を返すようになる | 現状観測例なし。発生時は signature 条件を厳格化（例: HTML かつ Cloudflare 以外のワードを除外）|
| `response.headers` に想定外の値 | `_extract_cf_headers` が存在チェック後に取り出すため KeyError なし |
| `main._notify_task_failure` のシグネチャ変更が他所に影響 | 既存呼び出し元は `main.main()` の 1 箇所のみ（grep で確認済み）。省略可能引数 `exc=None` で後方互換 |
| テストで `response.text` / `response.headers` のモック構造が既存と違う | 既存 `test_notion_client.py` の `_request_with_retry` テストパターンに揃える |
| レスポンスヘッダログが巨大化 | `response_headers` は str 化後 1000 char で truncate |

## スコープ外（再掲）

- 出口 IP の固定化（NAT Gateway / NAT Instance / Cloudflare Worker 経由 / 外部 VPS 経由 等）
- 自動リトライ機構（タスク再起動を含む）
- User-Agent カスタマイズ（AC-7 の結果次第で別 steering として起票）
- Notion 以外の API（GitHub / Slack）の同種対策
- Slack 汎用エラー文言の全面見直し
