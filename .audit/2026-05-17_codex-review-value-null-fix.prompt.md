直前の commit `c8f5e97 fix(trigger): treat Modal value:null as explicit empty (REMOVE), not no-op` を独立 second-opinion でレビューしてほしい。

## 背景

これは前回 2 pass 是正 commit `73c9a56` で発生した実機リグレッションの fix。

- 実機検証で「Slack Modal の interests フィールドを空欄にして保存しても、DynamoDB から削除されない」と判明
- 真因: Slack の optional plain_text_input は空欄を `value: null` (Python `None`) で送る仕様だが、`73c9a56` の `isinstance(raw, str)` 防御が None を「Modal 欠落扱い」と同等に no-op 化してしまった
- Codex 2 nd pass の指摘文「block/action が存在する場合だけ更新対象にし...」を「value が null なら no-op」まで拡大解釈したのが間違いだった

## 修正内容 (c8f5e97)

### src/trigger/app.py `_handle_view_submission` 内

```python
for key, _label, _placeholder, _multiline in PROFILE_FIELDS:
    block = values.get(f"block_{key}")
    if not isinstance(block, dict):
        continue  # Modal block 自体が無い → no-op (フィールド追加直後の古い submit を防御)
    action = block.get(f"input_{key}")
    if not isinstance(action, dict):
        continue
    # Slack 仕様: optional な plain_text_input を空欄で submit すると
    # `value: null` (Python では None) で届く。空文字 `""` で届くとは限らない。
    # block / action が存在する以上ユーザーが Modal を submit した明示的意図なので、
    # None も「空欄保存 = REMOVE 対象」として扱う。想定外型 (int 等) のみ no-op。
    raw = action.get("value")
    if raw is None:
        new_fields[key] = ""
    elif isinstance(raw, str):
        new_fields[key] = raw.strip()
    else:
        continue
```

### テスト

- `test_view_submission_writes_set_and_remove`: 既存テストで `value: None` ケースを「REMOVE 対象」に戻した
- `test_view_submission_removes_field_when_slack_sends_value_null` (新規): 実機シナリオ「interests だけ空欄保存 → interests のみ REMOVE、他 5 フィールドは SET」

## レビュー観点

A. **真因の根治確認**
   - `value: None` を REMOVE 対象として扱う実装が Slack 仕様と整合しているか
   - block 構造欠落の no-op (Codex 元指摘の本質) が今も維持されているか
   - 想定外型 (int 等) を no-op にしたままにする判断は妥当か

B. **退行・新規問題**
   - 「`raw is None` を REMOVE 対象に戻した」変更で、73c9a56 以前にあった「全 fields 欠落で全削除」リグレッションが再発していないか (Medium #2 の元懸念)
   - 73c9a56 で入れた他の防御化 (isinstance 連鎖、try/except SlackApiError、interactive payload 400 化) との整合は保たれているか
   - 新規テスト 1 件が実際の Slack payload 形状を正しく模倣しているか (state.values 構造、value: null の表現)

C. **見落とし**
   - Slack の他の input element (例: `multi_static_select`, `checkboxes`, `datepicker`) を将来追加した場合、value の null 仕様は同じか、別 schema か
   - `value` が空 list `[]` や dict `{}` で来る可能性のある element と将来混在した場合の挙動
   - long_keys バリデーションは `len(v) > 500` で行うが、`new_fields` には strip 後の空文字も入る。empty string は long_keys に該当しないので問題ないが念のため確認
   - `if not new_fields: return 200` は変わらず存続するが、今回の修正で「全 None で来た場合」も `new_fields = {k: "" for k}` になるため、early return は発火しなくなる。これが意図的か (= 全削除を許可) を確認

## 出力フォーマット

```markdown
# Codex Review — 2026-05-17 commit c8f5e97 (value:null REMOVE fix)

## サマリ
- Critical: N / High: N / Medium: N / Low: N / Info: N
- 真因根治: ✅/❌
- 新規問題: N 件
- 総合所感 (5 行以内)

## 指摘事項

### [severity] [path:line] 短いタイトル
- 問題: ...
- 影響: ...
- 推奨修正: ...

(severity 順、severity は Critical / High / Medium / Low / Info)
```

注意:
- 「対応が完全 / 不要修正なし」も明示してほしい
- Slack 公式 docs (https://api.slack.com/reference/block-kit/block-elements#input) の `plain_text_input` の `value` 仕様について、null 扱いに言及があれば引用してほしい
