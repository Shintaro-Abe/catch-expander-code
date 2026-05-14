# design.md — TokenMonitor still_valid 経路の events emit 追加

## 実装アプローチ

### 全体方針

- 既存の `EventEmitter.emit()` パターンを踏襲（後方互換、最小差分）
- 新イベント型 `oauth_refresh_skipped` を追加（既存 `oauth_refresh_completed` / `oauth_refresh_failed` と並列）
- dashboard API に新フィールドを追加（既存フィールド維持）
- events テーブルのスキーマ・GSI 変更なし

## 1. handler.py の変更

### 変更箇所
`src/token_monitor/handler.py:194-197` の still_valid 早期 return 直前に emit を追加。

### Before
```python
if not _needs_refresh(expires_at_ms, REFRESH_BUFFER_MS, now_ms):
    remaining_min = (expires_at_ms - now_ms) // 60_000
    logger.info("Token still valid", extra={"remaining_min": remaining_min})
    return {"refreshed": False, "reason": "still_valid"}
```

### After
```python
if not _needs_refresh(expires_at_ms, REFRESH_BUFFER_MS, now_ms):
    remaining_min = (expires_at_ms - now_ms) // 60_000
    logger.info("Token still valid", extra={"remaining_min": remaining_min})
    if _emitter is not None:
        _emitter.emit(
            "oauth_refresh_skipped",
            {
                "reason": "still_valid",
                "remaining_seconds": (expires_at_ms - now_ms) // 1000,
            },
            status_at_emit="success",
        )
    return {"refreshed": False, "reason": "still_valid"}
```

### 設計判断
- **`status_at_emit="success"`**: リフレッシュ不要は正常系。`failed` ではない
- **payload に `remaining_seconds`**: 将来 dashboard で「次回リフレッシュまでの残り時間」を可視化したい場合に使える
- **`_emitter is not None` ガード**: 既存 4 箇所の emit と同じパターン（EventEmitter import 失敗時の no-op を保証）

## 2. dashboard_api の変更

### ファイル
`src/dashboard_api/get_token_monitor_health/app.py`

### 変更後の構造

```python
def lambda_handler(event, context):
    # ... (request_id, period, ts_range 取得は既存通り)

    try:
        successes = query_event_type(table, "oauth_refresh_completed", from_ts, to_ts)
        failures = query_event_type(table, "oauth_refresh_failed", from_ts, to_ts)
        skipped = query_event_type(table, "oauth_refresh_skipped", from_ts, to_ts)  # NEW
    except Exception as e:
        # ... 既存通り
        return error_response(500, "INTERNAL_ERROR", "Database query failed", request_id)

    success_count = len(successes)
    failure_count = len(failures)
    skip_count = len(skipped)  # NEW
    total = success_count + failure_count  # skipped は加算しない（API 契約維持）

    last_refresh_at = successes[-1]["timestamp"] if successes else None
    last_failure_at = failures[-1]["timestamp"] if failures else None
    last_skip_at = skipped[-1]["timestamp"] if skipped else None  # NEW
    last_check_at = max(  # NEW: 3 種の最新タイムスタンプの最大値
        ts for ts in [last_refresh_at, last_failure_at, last_skip_at] if ts is not None
    ) if any([last_refresh_at, last_failure_at, last_skip_at]) else None

    last_failure_reason = None
    if failures:
        last_failure_reason = (failures[-1].get("payload") or {}).get("error_message")

    return json_response(200, {
        "data": {
            "period": period,
            "total_refresh_attempts": total,
            "success_count": success_count,
            "failure_count": failure_count,
            "skip_count": skip_count,                              # NEW
            "success_rate": round(success_count / total, 4) if total > 0 else None,
            "last_refresh_at": last_refresh_at,
            "last_failure_at": last_failure_at,
            "last_skip_at": last_skip_at,                          # NEW
            "last_check_at": last_check_at,                        # NEW (3 種の max)
            "last_failure_reason": last_failure_reason,
        },
    })
```

### 設計判断
- **`last_check_at` は ISO 文字列の max** で算出: ISO 8601 文字列は辞書順 = 時刻順で比較可能（events テーブル GSI のソートキーもそうなっている前提）
- **`total_refresh_attempts` には skipped を含めない**: ユーザーストーリー上「リフレッシュ試行回数」と「チェック実行回数」は別概念。後方互換のため既存定義を維持
- **`skip_count` 追加**: 「リフレッシュ不要回数」を別カウントとして提供
- **`success_rate` は (success / (success + failure))**: skipped を分母に含めない（同じ理由）

## 3. データモデル

### events テーブル (変更なし)
| 属性 | 値（新規エントリ） |
|---|---|
| PK `execution_id` | `system-token-refresh-{epoch}` (既存と同じ合成 ID) |
| SK `sk` | `{timestamp}#oauth_refresh_skipped#{ulid}` |
| `event_type` | `oauth_refresh_skipped` |
| `status_at_emit` | `"success"` |
| `payload` | `{"reason": "still_valid", "remaining_seconds": N}` |
| `timestamp` | ISO 8601 |
| GSI `gsi_event_type_timestamp` | PK=`oauth_refresh_skipped` / SK=timestamp |

→ 既存 GSI で自然にクエリ可能。スキーマ・migration 不要。

## 4. テスト計画

### 4.1 handler テスト追加 (`tests/unit/token_monitor/test_app.py`)

```python
def test_oauth_refresh_skipped_emitted_on_still_valid(self, monkeypatch):
    # _needs_refresh=False となる条件を作る (expiresAt が REFRESH_BUFFER 以上先)
    # emitter.emit が "oauth_refresh_skipped" で呼ばれることを assert
    # payload に reason="still_valid" / remaining_seconds が含まれることを assert
    # status_at_emit="success" を assert
```

既存 5 ケース（`test_oauth_refresh_completed_emitted_on_success`、
`test_oauth_refresh_failed_emitted_on_no_expires_at`、
`test_oauth_refresh_failed_emitted_on_http_error`、
`test_oauth_refresh_failed_emitted_on_url_error`、
かつ no_refresh_token 経路）に追加で **1 ケース** 追加。

### 4.2 dashboard API テスト追加 (`tests/unit/dashboard_api/test_extended_metrics.py`)

既存 `_make_event("oauth_refresh_completed", ...)` パターンに `oauth_refresh_skipped` を含むケースを追加し:

- `skip_count` が正しいか
- `last_skip_at` が最新の skipped イベントの timestamp か
- `last_check_at` が 3 種の最大値か
- `success_rate` が skipped を除いた値か（後方互換）

新規ケース **2〜3 件**:
1. skipped のみで completed/failed なし → `success_rate=None`, `skip_count > 0`, `last_check_at` 取得可
2. completed + failed + skipped 混在 → 全カウント正しい
3. （オプション）skipped が最新 → `last_check_at == last_skip_at`

### 4.3 既存テストの非後方互換確認

`test_extended_metrics.py:252` 周辺の既存ケース（completed のみ）は **既存フィールド維持** なので変更不要。新フィールド `skip_count`/`last_skip_at`/`last_check_at` が None / 0 で返ることだけ追加 assert（任意）。

## 5. 影響範囲

| レイヤ | ファイル | 変更 |
|---|---|---|
| Lambda handler | `src/token_monitor/handler.py` | +9 行（emit 呼び出し）|
| Dashboard API | `src/dashboard_api/get_token_monitor_health/app.py` | +6 行（query + フィールド追加）|
| handler test | `tests/unit/token_monitor/test_app.py` | +1 ケース（〜25 行）|
| API test | `tests/unit/dashboard_api/test_extended_metrics.py` | +2〜3 ケース（〜80 行）|
| events テーブル | （変更なし）| スキーマ不変 |
| GSI | （変更なし）| `gsi_event_type_timestamp` で自然クエリ |
| Frontend SPA | （変更なし、別 steering）| API レスポンスに新フィールドが追加されるが既存フィールドを描画している限り影響なし |

## 6. 検証手順

1. ユニットテスト全件 pass を確認
2. `sam deploy` で Lambda + API GW を更新（events テーブル変更なしのため大きな影響なし）
3. CloudWatch Logs で TokenMonitor の次回実行（EventBridge Schedule）を確認
4. AWS Console で events テーブルを `event_type=oauth_refresh_skipped` でクエリし、新規エントリを確認
5. `curl https://<dashboard-api>/api/v1/metrics/token-monitor/health?period=1d` を叩き、`skip_count` / `last_check_at` が返ることを確認

## 7. ロールバック計画

万一バグが見つかった場合:
- handler.py: emit 追加部分を revert すれば既存挙動に戻る（events テーブルへの不要書き込みが止まる）
- dashboard_api: `oauth_refresh_skipped` query を削除すれば既存 API レスポンスに戻る
- 既に書き込まれた `oauth_refresh_skipped` イベントは TTL (5 年) で自然消滅 or 手動削除

## 8. 関連メモリ・ドキュメント

- memory: `project_token_monitor_still_valid_no_emit.md` (本バグの調査記録)
- `.steering/20260430-workflow-observability/requirements.md:264-269` (Tier 1.4 受け入れ条件、本件で完全充足)
- `docs/architecture.md:495` (events テーブル GSI 定義)
