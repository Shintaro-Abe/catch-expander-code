# Codex Review — 2026-05-17 commit c8f5e97 (value:null REMOVE fix)

## サマリ
- Critical: 0 / High: 0 / Medium: 0 / Low: 0 / Info: 0
- 真因根治: ✅
- 新規問題: 0 件
- 総合所感: 対応は完全。不要修正なし。`value: None` を REMOVE 対象に戻しつつ、block/action 欠落時の no-op 防御は維持されている。73c9a56 の「全 fields 欠落で全削除」懸念は再発していない。

## 指摘事項

なし。

確認ポイント:
- [src/trigger/app.py](/workspaces/Catch-Expander/src/trigger/app.py:415) で block 欠落は `continue` のまま。
- [src/trigger/app.py](/workspaces/Catch-Expander/src/trigger/app.py:418) で action 欠落も `continue` のまま。
- [src/trigger/app.py](/workspaces/Catch-Expander/src/trigger/app.py:425) 以降で `None` は `""` に正規化され、[src/trigger/app.py](/workspaces/Catch-Expander/src/trigger/app.py:227) の REMOVE 側に入る。
- 想定外型は no-op のままで、現状の `PROFILE_FIELDS` が全て `plain_text_input` である限り妥当。
- `long_keys` は strip 後の文字列だけを対象にしており、空文字は問題なし。
- 全 field が `None` の場合に全 REMOVE になる挙動は、現在の Modal が全 6 field を表示する設計では「明示的に全空欄保存」と解釈でき、意図的挙動として妥当。

Slack 公式 docs 確認:
- `plain_text_input` 要素定義ページ自体には、空欄 submit 時の `null` の明記は見当たりません。
- ただし Slack changelog “A full state on view submission and block actions” では、空 state について “send `null` as the value” と説明し、`plain_text_input` の `"value": null` 例を示しています。multi_select / checkboxes は `[]` 例外とも明記されています。  
  Source: https://docs.slack.dev/changelog/2020/09/01/full-state-on-view-submisson-and-block-actions/

検証:
- `uv run pytest -q tests/unit/trigger/test_app.py`
- 結果: `75 passed in 0.71s`
