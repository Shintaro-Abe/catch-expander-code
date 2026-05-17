# Codex Review 2nd pass — 2026-05-16 session commit 97b033e

## サマリ
- Critical: 0 / High: 0 / Medium: 0 / Low: 2 / Info: 0
- 前回指摘の解消確認: ✅ 4/5 解消、❌ 1/5 残存
- Medium #1/#2、Low #4/#5 は解消済み。
- Low #3 は通常の壊れた JSON は 400 化済みだが、valid JSON の非 object payload などで 500 経路が残る。
- `chat_postEphemeral(user=user_id)` は Slack 署名済み payload の actor ID なので、追加の本人確認は不要と判断。
- 空 Modal no-op、`trigger_id` 欠落 no-op、`set_keys`/`remove_keys` 分岐、追加テストの assert は概ね妥当。
- 検証: `pytest` が環境に無く、テスト実行は未実施。

## 指摘事項 (新規発生分 + 残存分)

### [Low] [src/trigger/app.py:488] valid JSON だが object でない interactive payload が 500 になる
- 区分: 残存
- 問題: `json.JSONDecodeError` は捕捉されていますが、`payload` が `[]` や `"x"` のような valid JSON の非 dict の場合、`_handle_interactive(payload, ...)` 内の `payload.get("type")` で `AttributeError` になります。同様に `actions[0]`、`view`、`state.values`、各 `block/action` が dict でない場合も `.get()` 前提で落ち得ます。
- 影響: 前回 Low #3 の「JSON/必須キー不正を 400/200 化」は一部残存です。署名検証があるため外部攻撃面は限定的ですが、Slack 側仕様差分・テスト/設定ミス・内部再送で Lambda 500 になります。
- 推奨修正: `json.loads` 後に `isinstance(payload, dict)` を検証し、非 dict は 400。`actions[0]`、`view`、`state`、`values`、`block`、`action` も dict/list 型を確認して 200 no-op または 400 に寄せてください。

### [Low] [src/trigger/app.py:433] 保存後の ephemeral 通知失敗で view_submission 全体が 500 になる
- 区分: 新規
- 問題: `_update_user_profile_fields()` 完了後、`_post_profile_saved()` の `chat_postEphemeral` が未捕捉です。`channel_not_found`、`user_not_in_channel`、`not_in_channel`、rate limit などの `SlackApiError` が発生すると Lambda が 500 を返します。
- 影響: プロファイル保存自体は完了しているのに Slack Modal 側では失敗扱いになり、ユーザーの再送や Slack retry による重複更新が起き得ます。ephemeral 化で発生しやすい失敗条件が増えたため、今回の修正で浮上した経路です。
- 推奨修正: 保存後通知だけを `try/except SlackApiError` で囲み、ログを出して `200` を返してください。DDB 更新失敗とは分け、通知失敗で保存成功を巻き戻さない扱いが妥当です。
