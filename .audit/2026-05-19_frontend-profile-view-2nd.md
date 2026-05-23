# Codex Review Result: frontend-profile-view (Pass 2)

- Pass: 2
- Date: 2026-05-19
- Model: gpt-5.5 (Codex v0.125.0)
- Prompt: `.audit/2026-05-19_frontend-profile-view-2nd.prompt.md`
- 対象 commit: `067e53c fix(dashboard): normalize learned_preferences + tighten user_sub validation`
- Pass 1 結果: `.audit/2026-05-19_frontend-profile-view.md`
- Raw output: `.audit/2026-05-19_frontend-profile-view-2nd.raw.txt` (125 KB)

---

# Codex Review Pass 2 — 2026-05-19 frontend-profile-view

## サマリ
- Critical: 0 / High: 0 / Medium: 0 / Low: 1 / Info: 1
- 前回指摘の解消確認: ✅ 2/3 解消、⚠️ 1/3 部分
- 多層ミス検出: 1 件
- 総合所感: H1 の runtime error は API 境界で正しく収束。`learned_preferences` は内部 dict / API string[] の境界が成立しています。`user_sub` の 401 化も妥当。残るのは 6 軸 drift 検知が dashboard 側だけで、trigger/frontend 側変更を捕まえない点です。

## 指摘事項 (新規発生分 + 残存分)

### [Low] [tests/unit/dashboard_api/test_get_my_profile.py:230] drift 検知が dashboard 側の固定化に留まっている
- 区分: 残存
- 問題: `test_profile_keys_are_stable` は `_PROFILE_KEYS` 自体の typo / 順序変更は検知できますが、[src/trigger/app.py:34](/workspaces/Catch-Expander/src/trigger/app.py:34) の `PROFILE_FIELDS` が変わった場合は dashboard test では検知できません。Pass 1 の「3 箇所分散 drift」への対策としては半分だけです。
- 影響: trigger 側で 6 軸が追加・改名されても、dashboard API / frontend 表示が追随しないまま通る可能性があります。
- 推奨修正: dashboard test から `src.trigger.app` を import しない判断は妥当です。代わりに trigger test suite 側へ対称テストを追加してください。

```python
def test_profile_fields_keys_are_stable(_env_vars):
    from app import PROFILE_FIELDS

    assert tuple(key for key, *_ in PROFILE_FIELDS) == (
        "role",
        "interests",
        "expertise",
        "learning_goals",
        "background",
        "output_preferences",
    )
```

### [Info] [src/dashboard_api/get_my_profile/app.py:31] Slack user_id コメントは公式仕様としては少し強い
- 区分: 新規
- 問題: Slack 公式は user ID が `U` または `W` で始まることは明記していますが、文字集合を固定前提にしないよう案内しています。Slack changelog も “business logic shouldn’t contain assumptions about the specific characters composing an ID” としています。参考: Slack changelog「User ID format changes」および SCIM API reference。  
  https://docs.slack.dev/changelog/2016/08/11/user-id-format-changes/  
  https://docs.slack.dev/reference/scim-api/
- 影響: 現在の実機 cookie 形式を前提にした防御としては許容範囲です。ただしコメントの「uppercase alphanumeric のみで hyphen を含まない仕様」は公式仕様断定としてはやや強いです。
- 推奨修正: 必須ではありませんが、コメントを「現行の観測形式 / 保存キーとして許容する形式」に寄せるとよいです。長さ制約追加は推奨しません。Slack は ID 長や文字集合を固定契約として扱わないほうが安全です。

## 前回指摘の解消状況
- High #1 (`learned_preferences`): 解消 ✅  
  [src/dashboard_api/get_my_profile/app.py:53](/workspaces/Catch-Expander/src/dashboard_api/get_my_profile/app.py:53) で dict / str / 混在 / 空 text / 想定外型を吸収し、[app.py:97](/workspaces/Catch-Expander/src/dashboard_api/get_my_profile/app.py:97) で API 出口のみ `string[]` 化。frontend の React child error は解消。内部の [src/agent/orchestrator.py:1265](/workspaces/Catch-Expander/src/agent/orchestrator.py:1265) は dict 直アクセスのままで境界も正しいです。
- Low #1 (`user_sub` regex): 解消 ✅  
  malformed sub を 401 に倒す修正は妥当です。`"U"` 単体が通る点は理論上ありますが、実 DDB key として存在しないため placeholder になるだけで、現実的な認証 bypass ではありません。長さ制約は Slack ID の将来変化に弱くなるため不要です。
- Low #2 (drift 検知 案 C 採用): 部分解消 ⚠️ / 判断自体は妥当  
  `trigger.app` 直 import を避ける判断は妥当です。top-level で `boto3.client/resource` と `ecs_client` が初期化されるため、dashboard test に副作用を持ち込まないほうがよいです。ただし案 C を完結させるには trigger 側の対称テストが必要です。

## 補足評価
- `created_at` / `replaces_index` を API で落とす情報損失: 現行 frontend contract が `string[]` なので妥当。将来表示するなら `learned_preferences_v2` 等で別 contract 化が自然です。
- whitespace-only text のスキップ: 妥当。保存側も trim / empty 排除の意図があり、表示 API で空項目を出さないほうが安全です。
- regex 失敗時の error message 共通化: 許容範囲。401 の利用者向けレスポンスで詳細分岐する必要は薄いです。
- 6 軸本体の dict 混入リスク: 現状の主要書き込み経路は Slack Modal の string 保存なので H1 と同型の高リスクは見当たりません。

## 結論
- 収束判定: 収束 (新規 Critical/High ゼロ)
- マージ可否: 可。Low の trigger 対称テストは後続または同 PR で追加推奨です。
- 確認: `uv run pytest tests/unit/dashboard_api/test_get_my_profile.py -v` は 10 passed。
