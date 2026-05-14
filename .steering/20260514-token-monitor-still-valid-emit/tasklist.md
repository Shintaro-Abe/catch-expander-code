# tasklist.md — TokenMonitor still_valid 経路の events emit 追加

design.md の実装計画を具体的なタスクに分解した一覧。

## タスク一覧

| ID | 内容 | 完了条件 | 状態 |
|---|---|---|---|
| T1 | `src/token_monitor/handler.py:194-197` の still_valid 経路に `oauth_refresh_skipped` emit を追加 | emit 呼び出しが含まれ、`_emitter is not None` ガードがある | pending |
| T2 | `src/dashboard_api/get_token_monitor_health/app.py` に `oauth_refresh_skipped` クエリと `skip_count` / `last_skip_at` / `last_check_at` フィールドを追加 | API レスポンスに 3 新フィールド、`success_rate` は skipped 除外で後方互換 | pending |
| T3 | `tests/unit/token_monitor/test_app.py` に `test_oauth_refresh_skipped_emitted_on_still_valid` ケースを 1 件追加 | ケース pass、emit が `oauth_refresh_skipped` / payload / status_at_emit を正しく検証 | pending |
| T4 | `tests/unit/dashboard_api/test_extended_metrics.py` に skipped 関連 2〜3 ケースを追加 | 全ケース pass、`skip_count`/`last_check_at`/後方互換を検証 | pending |
| T5 | `pytest tests/unit/token_monitor tests/unit/dashboard_api -v` で対象テストグリーン | 新規 + 既存全 pass | pending |
| T6 | `pytest` 全件実行で回帰確認 | pre-existing 23 failures から増えないこと | pending |
| T7 | pre-commit-secret-scan Skill 起動 → diff 範囲に新規 leak が無いことを確認 | exit code 0 または既知の japanese-phone 誤検知のみ | pending |
| T8 | commit (handler + dashboard + tests を 1〜2 commit) → push | working tree clean、main 反映 | pending |
| T9 | GitHub Actions CI 完了確認 → `sam deploy` で Lambda + API GW を更新 | UPDATE_COMPLETE、新 Task Definition revision | pending |
| T10 | TokenMonitor を AWS Console から手動 invoke → events テーブルで `event_type=oauth_refresh_skipped` のレコード存在を確認 | 1 件以上のエントリ確認 | pending |
| T11 | `curl https://<dashboard-api>/api/v1/metrics/token-monitor/health?period=1d` で `skip_count > 0` と `last_check_at` が現在時刻に近いことを確認 | レスポンスに新フィールドが含まれ、正しい値を返す | pending |
| T12 | memory 更新（`project_token_monitor_still_valid_no_emit.md` を「修正済み」マーキング、または削除）| MEMORY.md エントリが「resolved」表記または削除 | pending |

## タスク依存関係

```
T1 ──┐
     ├──→ T3 ──┐
T2 ──┤         ├──→ T5 ──→ T6 ──→ T7 ──→ T8 ──→ T9 ──→ T10 ──→ T11 ──→ T12
     └──→ T4 ──┘
```

- T1, T2 は独立（別ファイル）。並列着手可能だが 1 セッションで連続編集の方が自然
- T3 は T1 完了後、T4 は T2 完了後（実装に対するテスト）
- T5 (対象テスト) → T6 (全件回帰) の順で 2 段階
- T7 (secret scan) は commit 直前必須（memory: feedback_pre_commit_secret_scan_skill.md）
- T9 は CI 完了後（memory: feedback_deploy_after_ci_completion.md）
- T10, T11 は実環境検証のためデプロイ後のみ

## 各タスクの実装メモ

### T1: handler.py の emit 追加

挿入位置: `handler.py:196` の `logger.info(...)` 直後、`return` 直前。

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

### T2: dashboard_api の query 追加とレスポンス拡張

`get_token_monitor_health/app.py` の lambda_handler を全面差し替え。
- L24-25 の successes/failures クエリの後に `skipped = query_event_type(table, "oauth_refresh_skipped", from_ts, to_ts)` を追加
- カウント・タイムスタンプ計算ロジックを拡張
- レスポンス JSON に 3 フィールド追加

### T3: handler 新規テストケース

既存の `test_oauth_refresh_completed_emitted_on_success` をテンプレに、`_needs_refresh` をモックで False を返すよう変更したケース。具体的には:
- `creds["claudeAiOauth"]["expiresAt"]` を `now_ms + REFRESH_BUFFER_MS + 1_000_000` に設定（バッファより十分先）
- 期待: `emitter.emit` の 1 回目呼び出しが `("oauth_refresh_skipped", {"reason": "still_valid", "remaining_seconds": <int>}, status_at_emit="success")` 形式

### T4: dashboard API 新規テストケース

`test_extended_metrics.py` の既存パターン参照（L237 付近）:
- Case A: skipped のみ → success_count=0, failure_count=0, skip_count>0, success_rate=None, last_check_at=last_skip_at
- Case B: completed + failed + skipped 混在、最新が skipped → last_check_at=last_skip_at, success_rate=skipped 除外
- Case C: completed のみ → 既存挙動、skip_count=0, last_skip_at=None, last_check_at=last_refresh_at

### T8: commit 戦略

design.md / memory の方針に則り、機能単位で 1 commit:
```
feat(token-monitor): emit oauth_refresh_skipped on still_valid path

- handler.py: add events emit for skip path (was missing, dashboard timeline stale)
- dashboard_api: add skip_count / last_skip_at / last_check_at fields
- tests: add 1 handler case + 3 dashboard cases
```

または steering 通り 2 commit に分けても可（handler / dashboard_api 別ファイル）。
1 commit のほうが「skip 経路の観測性追加」という 1 件の意図として理解しやすい。

### T11: dashboard API 動作確認

dashboard URL は `docs/architecture.md` または AWS Console (API Gateway) から取得。
認証は Slack OAuth Cookie が必要なので、ブラウザで dashboard SPA を開いて DevTools の Network タブから直接 token-monitor/health のレスポンスを確認するのが現実的。

curl で叩く場合は `Cookie: session=<value>` を付与する必要があるが、検証目的なら SPA 経由のほうが楽。

### T12: memory 更新方針

修正完了後、`project_token_monitor_still_valid_no_emit.md` の冒頭に「## 修正済み (2026-05-14)」セクションを追加し、修正 commit ハッシュと検証結果を記録。MEMORY.md のエントリは「修正済み」を匂わせる説明文に更新（または resolved 記号付与）。

完全削除はしない: 「なぜこの修正が必要だったか」のコンテキストは将来の同種バグ予防に有用。

## 受け入れ条件との対応

| AC | 対応タスク |
|---|---|
| AC-1 (handler emit) | T1 + T3 |
| AC-2 (dashboard 3 状態集計) | T2 + T4 |
| AC-3 (API 後方互換) | T2 + T4 (Case C) |
| AC-4 (テスト) | T3 + T4 + T5 + T6 |
| AC-5 (デプロイ後動作確認) | T9 + T10 + T11 |
