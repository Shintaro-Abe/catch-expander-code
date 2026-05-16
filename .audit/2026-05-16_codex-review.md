# Codex Review — 2026-05-16 session commits

## サマリ
- Critical: 0 / High: 0 / Medium: 2 / Low: 3 / Info: 2
- 総合所感: 署名検証は interactive payload にも適用されており、`GetItem` + `UpdateItem` の IAM 判断も妥当です。
- `learned_preferences` は UpdateExpression レベルで触っておらず、AttributeName injection も現経路では成立しにくいです。
- 主な懸念は Slack channel へのプロファイル内容再掲と、Modal state 欠落を削除扱いにする更新ロジックです。
- dashboard/API/frontend の型と意味は概ね揃っていますが、`success_rate` の skipped 除外は docs/UI 表示に補足余地があります。
- 検証: `pytest` が環境に無く、対象テストは実行できませんでした。

## 指摘事項

### [Medium] [src/trigger/app.py:343] プロファイル内容がチャンネルに再掲される
- 問題: `view_submission` 後に `_post_profile_saved` が、登録された各フィールドの先頭 80 文字を `chat_postMessage` で元チャンネルへ投稿します。
- 影響: `background` や `learning_goals` など、個人属性・職務状況・嗜好を含む情報が public/private channel の参加者へ露出します。Slack Modal で個人入力した内容としては期待より公開範囲が広いです。
- 推奨修正: 保存完了通知は本文を含めず「保存しました」だけにするか、DM/ephemeral に限定してください。少なくとも public channel では値の preview を出さない方が安全です。

### [Medium] [src/trigger/app.py:389] 欠落した Modal state がフィールド削除として扱われる
- 問題: `PROFILE_FIELDS` 全件を走査し、`values.get(...).get(...).get("value") or ""` で欠落を空文字に変換しています。そのため、存在しない block/action も明示的な空欄保存と同じ `REMOVE` 対象になります。
- 影響: フィールド追加直後に古い Modal が submit された場合や、Slack payload 形状差分・部分欠落が起きた場合、ユーザーが触っていないフィールドまで削除され得ます。
- 推奨修正: block/action が存在する場合だけ更新対象にし、存在しないフィールドは no-op にしてください。空欄削除は `value == ""` が明示的に届いた場合に限定すると、将来追加にも強くなります。

### [Low] [src/trigger/app.py:456] interactive payload の JSON/必須キー不正が 500 になり得る
- 問題: `json.loads(payload_str)`、`payload["actions"][0]`、`payload["view"]`、`json.loads(private_metadata)` などが未捕捉です。
- 影響: Slack 署名済みでない攻撃は弾けますが、設定ミス・Slack 側仕様差分・壊れた payload で Lambda が例外終了し、Slack 側には不安定な応答になります。
- 推奨修正: interactive 分岐では JSON decode と必須キー検証を行い、400 または 200 no-op を明示返却してください。`private_metadata` も decode 失敗時は `{}` 扱いが安全です。

### [Low] [docs/credential-setup.md:37] `im:read` / `im:write` の必要性が現行コードから説明できない
- 問題: 現行実装で DM 受信には `im:history`、投稿には通常 `chat:write` が中心で、`im:read` / `im:write` を直接要求するコードパスは見当たりません。
- 影響: Slack Bot token の権限が過剰になり、ワークスペース内 DM 関連操作のリスク面積が広がります。
- 推奨修正: 実際に conversations API で DM open/read が必要な機能がないなら削除してください。将来用途なら docs に「未使用だが予定」と明記せず、必要になった時点で追加する方が最小権限です。

### [Low] [docs/functional-design.md:1118] `success_rate` の skipped 除外が API docs に明示されていない
- 問題: コードでは `success_count / (success_count + failure_count)` で skipped を除外していますが、API 表の説明は `skip_count` と timestamp 追加に留まり、`success_rate` の分母を説明していません。
- 影響: frontend の「成功率」表示が、チェック全体に対する成功率なのか、実リフレッシュ試行だけの成功率なのか読み手に曖昧です。
- 推奨修正: docs と UI tooltip/ラベルで「skipped は分母から除外」「更新試行成功率」などを明記してください。

### [Info] [src/trigger/app.py:437] Slack 署名検証は interactive payload にも適用済み
- 問題: なし。署名検証は Content-Type 分岐より前に raw body で実行されています。
- 影響: `application/x-www-form-urlencoded` の `payload=` 形式でも、Slack の署名前提は維持されています。
- 推奨修正: 現状維持でよいです。追加するなら invalid interactive signature の専用テストがあると回帰防止になります。

### [Info] [src/trigger/app.py:219] `UpdateItem` の SET+REMOVE と AttributeName alias は妥当
- 問題: 重大な injection リスクは見当たりません。更新キーは `PROFILE_FIELDS` 由来で、ExpressionAttributeNames の alias 経由です。
- 影響: `learned_preferences` も UpdateExpression に含まれないため、この経路では保持されます。`PutItem` なしでも `UpdateItem` は新規 item 作成可能なので IAM も `GetItem` + `UpdateItem` で足ります。
- 推奨修正: 将来の保守性として、4-tuple は `NamedTuple` か frozen dataclass にすると `key/label/placeholder/multiline` の取り違えを減らせます。
