# requirements.md — TokenMonitor still_valid 経路の events emit 追加

## 背景

`src/token_monitor/handler.py` は Claude OAuth トークンの定期リフレッシュ Lambda (EventBridge Schedule, 30 分周期想定)。
リフレッシュ完了 / 失敗時には `_emitter.emit()` で events テーブルに記録するが、**「リフレッシュ不要 (still_valid)」経路だけ emit が抜けている**。

memory `project_token_monitor_still_valid_no_emit.md` で確認済み:
- バグ箇所: `src/token_monitor/handler.py:194-197`
- dashboard `get_token_monitor_health` は `oauth_refresh_completed` / `oauth_refresh_failed` のみクエリし、`still_valid` は集計対象外
- 結果: ダッシュボード TokenMonitor タイムラインの `last_refresh_at` が古いまま更新されず、Lambda の生存確認ができない

## ユーザーストーリー

- **As a** 運用者（個人開発者）
- **I want** ダッシュボードの TokenMonitor セクションで「Lambda が直近 30 分以内にチェックを走らせているか」を視認できる
- **So that** リフレッシュ実行有無に依らず、TokenMonitor Lambda の死活を確認できる

## 受け入れ条件

### AC-1: handler 側で skip 経路を emit
- `_needs_refresh()` が False を返した時、`oauth_refresh_skipped` event_type で events テーブルに 1 件記録される
- payload に最低限 `reason` (= "still_valid") と `remaining_seconds` を含む
- `status_at_emit` は `"success"`（リフレッシュ不要は正常系）

### AC-2: dashboard API が 3 状態を集計
- `get_token_monitor_health` が `oauth_refresh_skipped` も含めて返す
- 新規フィールド `last_check_at` を追加（completed / skipped / failed の最新タイムスタンプの最大値）
- 既存フィールド `last_refresh_at` (= completed の最新) は後方互換のため維持

### AC-3: 既存 API 契約の後方互換
- `total_refresh_attempts` / `success_count` / `failure_count` / `success_rate` の意味は維持（skipped はこれらに加算しない）
- skipped は別途 `skip_count` フィールドとして提供

### AC-4: テスト
- `tests/unit/token_monitor/test_app.py` に新規 emit を検証するケース追加
- `tests/unit/dashboard_api/` に `last_check_at` / `skip_count` を検証するケース追加
- 既存テスト全件 pass

### AC-5: デプロイ後の動作確認
- sam deploy 後、TokenMonitor 手動 invoke で `oauth_refresh_skipped` イベントが DynamoDB に書き込まれることを確認
- ダッシュボード API を叩いて `last_check_at` が現在時刻に近いことを確認

## 制約事項

- **frontend 表示の変更は今回のスコープ外**: バックエンドのデータ提供 (API レスポンス) までで完了とする。UI 表示 (新フィールド `last_check_at` / `skip_count` の描画) は別 steering で対応可能。今回は API が正しいデータを返すところまで。
- **新イベント型のため events テーブルの GSI (event_type-timestamp) で自然にクエリ可能**: スキーマ変更や migration は不要。
- **コストインパクト ほぼゼロ**: DynamoDB events テーブルへの書き込みが月 1,440 件増（30 分 × 30 日 ÷ 30 分）。on-demand 想定で月 $0.001 未満。

## スコープ外

- frontend SPA の UI 変更
- 既存 emit 経路（`oauth_refresh_completed` / `oauth_refresh_failed`）の payload 変更
- CloudWatch Alarm との連携
- TokenMonitor の他の改善（DLQ 追加等）
