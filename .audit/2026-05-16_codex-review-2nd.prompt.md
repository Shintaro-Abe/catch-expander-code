本セッション 1 回目の Codex レビュー (`.audit/2026-05-16_codex-review.md`) で挙がった指摘に対する是正コミット (`97b033e fix(trigger): address Codex review findings on F6 profile modal`) を独立 second-pass でレビューしてほしい。

memory 知見によると「独立基盤 LLM を 2〜3 回連続で回すと前段対応で初めて見える次層が剥がれる」ため、本パスでは以下の 3 軸でチェックしてほしい。

## レビュー対象 commit
- `97b033e` 1 件のみ (Medium 2 + Low 3 への是正)

## レビュー観点

### 1. 前回指摘の解消確認 (退行なし)

前回 `.audit/2026-05-16_codex-review.md` で指摘された 5 件が完全に解消されているか:

- **Medium #1** [src/trigger/app.py:343 旧]: プロファイル内容のチャンネル再掲 → ephemeral 化されているか
- **Medium #2** [src/trigger/app.py:389 旧]: 欠落 Modal state がフィールド削除扱い → 明示的 `value == ""` のみ REMOVE になっているか
- **Low #3** [src/trigger/app.py:456 旧]: interactive payload の JSON/必須キー不正 → try/except + defensive `.get()` で 400/200 化されているか
- **Low #4** [docs/credential-setup.md:37]: `im:read` / `im:write` 削除されているか
- **Low #5** [docs/functional-design.md:1118]: `success_rate` の分母が docs に明示されているか

### 2. 是正により浮上した新規問題

修正コードによって新たに混入した可能性のある問題を探してほしい:

- `chat_postEphemeral` の `user` 引数: Slack ユーザー ID として渡している `user_id` が本人かの検証は signature 検証以外で必要か
- `_handle_view_submission` で `view.get("state", {}).get("values", {})` のような defensive get の連鎖が他の必須キー扱いと不整合になっていないか
- `if not new_fields: return {"statusCode": 200, "body": ""}` を入れたことで、悪意ある submitter が空 Modal を連発しても問題ないか (rate limit / DoS 視点)
- `_handle_block_actions` で `trigger_id` 不在時に no-op で 200 返却するが、Slack 仕様上それは attack surface にならないか
- `json.JSONDecodeError` のみ捕捉しているが、他の `Exception` 型 (UnicodeDecodeError 等) で 500 になり得る経路はないか
- ephemeral 通知が失敗 (channel 不在等) した時、Lambda 全体が 500 にならないか (try/except 漏れ)

### 3. 前パスで見落としていた次層

1 回目では PROFILE_FIELDS の 4-tuple → NamedTuple/dataclass リファクタは Info 扱いだったが、今回の修正で:

- 同じ defensive 化を `_handle_block_actions` にも入れた整合性
- `_post_profile_open_button` や `_build_profile_modal` にも同様の防御が必要か
- `_get_user_profile` の `response.get("Item")` の戻り値型は Modal に流す `existing.get(key)` で安全か
- `_update_user_profile_fields` の `set_keys`/`remove_keys` への分岐ロジックで、修正後 (空 dict 経路をスキップする) のエッジケースが想定通り動くか
- テスト追加 3 ケースが指摘の本質を捉えているか (`test_view_submission_uses_ephemeral_for_save_confirmation` で `chat_postMessage.assert_not_called` までやっているか、`test_view_submission_skips_missing_blocks` で `update_item` が完全に呼ばれないことを assert しているか等)

## 主要な変更ファイル (97b033e の diff)

- `src/trigger/app.py`:
  - L336-358 `_post_profile_saved` を `chat_postEphemeral` に変更、`user=user_id` 指定
  - L361-381 `_handle_block_actions` に `actions = payload.get("actions") or []`、`trigger_id/user_id` 不在時 no-op
  - L384-432 `_handle_view_submission` に payload defensive get、`private_metadata` JSON decode 例外捕捉、Modal block 欠落の continue、`new_fields` 空時の no-op
  - L470-487 lambda_handler interactive entry に `json.JSONDecodeError` 捕捉
- `tests/unit/trigger/test_app.py`: 既存 `test_view_submission_writes_set_and_remove` 期待値修正 + 新規 3 ケース
- `docs/credential-setup.md`: `im:read` / `im:write` 削除 + 注釈
- `docs/functional-design.md`: `success_rate` 分母説明追加

## 出力フォーマット

```markdown
# Codex Review 2nd pass — 2026-05-16 session commit 97b033e

## サマリ
- Critical: N / High: N / Medium: N / Low: N / Info: N
- 前回指摘の解消確認: ✅ N/5 解消、❌ N/5 残存
- 総合所感 (5 行以内)

## 指摘事項 (新規発生分 + 残存分)

### [severity] [path:line] 短いタイトル
- 区分: 新規 / 残存 / リグレッション
- 問題: ...
- 影響: ...
- 推奨修正: ...

(severity 順、severity は Critical / High / Medium / Low / Info)
```

注意:
- 前回 Info 扱いだった「PROFILE_FIELDS の NamedTuple 化」は今回の修正でも未着手だが、これは別 issue としてスコープ外扱いで OK (再度指摘しても可)
- 「対応が完全 / 不要修正なし」も明示してほしい
