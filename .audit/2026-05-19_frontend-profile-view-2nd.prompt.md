# Codex レビュー依頼 (2 回目): Frontend Profile View 是正

## 役割

あなたは Catch-Expander プロジェクトのシニアレビュアーです。
**前回 (Pass 1) のレビュー (`.audit/2026-05-19_frontend-profile-view.prompt.md`) で挙がった指摘に対する是正コミット** (`067e53c fix(dashboard): normalize learned_preferences + tighten user_sub validation`) を独立 second-pass でレビューしてください。

`memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` の経験則に従い、
**前 pass の修正で剥がれた次層のミス**がないかを確認してください。

## 前回レビュー結果と対応

### Critical / High — 1 件

- **[High] `learned_preferences` の実保存形式と API / frontend 型が不一致** [`src/dashboard_api/get_my_profile/app.py:64`]
  - Pass 1 指摘要約: 既存 `feedback_processor.py:199` は `{"text": str, "created_at": str}` の dict 配列で書き込む。本 PR の API は item をそのまま返し、frontend は `string[]` を期待 → 学習履歴 1 件以上で `Objects are not valid as a React child` runtime error 発生 → `/profile` 表示不能
  - **対応**: `_serialize_learned_preferences(raw) -> list[str]` ヘルパを新規追加し、API 出口で正規化:
    - dict 要素は `text` フィールドを抽出 (空文字 / whitespace-only / 非 str はスキップ)
    - 旧 string 要素はそのまま採用
    - その他型 (int / None / nested list / 非 list raw) は捨てて `[]`
    - frontend には `string[]` のまま渡る (API contract 維持、frontend 無変更)
  - テスト fixture を本番形式 `{"text": ..., "created_at": ...}` に更新

### Medium — 0 件

### Low — 2 件

1. **`user_sub` の形式検証がなく仕様 drift を静かに placeholder 化** [`src/dashboard_api/get_my_profile/app.py:31`]
   - Pass 1 指摘要約: `_extract_slack_user_id()` は空文字以外なら `split("-", 1)[0]` をそのまま採用 → 壊れた sub (`"abc"` 等) でも DDB key 照会して未存在 placeholder を返す → cookie 破損時の原因特定が遅れる
   - **対応**: `_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")` を追加し、split 後の candidate を `fullmatch` で検証。不一致なら None → 401。新テスト `test_malformed_user_sub_returns_401` (3 ケース: lowercase / 数字始まり / 特殊文字) を追加

2. **6 軸定義が 3 箇所分散で drift 検知が弱い** [`src/dashboard_api/get_my_profile/app.py:20`]
   - Pass 1 指摘要約: `_PROFILE_KEYS` (backend) / `AXIS_FIELDS` (frontend) / `PROFILE_FIELDS` (`src/trigger/app.py`) が手動同期。Codex 推奨は trigger.app から直接 import するテスト
   - **対応**: Codex 推奨案ではなく **ハードコード比較案** に変更。`test_profile_keys_are_stable` を新規追加し、`_PROFILE_KEYS == ("role", "interests", ...)` を tuple 比較。trigger 側との同期は trigger テストの責務として境界を保つ (`src.trigger.app` を dashboard test に import すると ECS client 初期化等の副作用が dashboard test suite に侵入するリスクを回避)。意図的な逸脱なので妥当性を再評価してほしい

### Info — 4 件 (全て対応不要、Codex 自身が明示)

- ✅ `/api/v1/profile/me` 固定 + IAM `dynamodb:GetItem` のみは設計意図どおり
- ✅ env 不在の `KeyError` は IaC 信頼で許容
- ✅ DynamoDB 例外の `except Exception` 広め捕捉は既存 dashboard API と整合
- ✅ 401 redirect は `frontend/src/api/client.ts` の interceptor で共通処理済 (B5 懸念は既存設計で解消)

## レビュー対象 (Pass 2)

### 変更 1: `_extract_slack_user_id` に regex 検証追加 (`src/dashboard_api/get_my_profile/app.py:31-50`)

```python
# Slack user_id は U / W プレフィックス + uppercase alphanumeric。
# 形状破壊時に「placeholder 表示」で隠れず即 401 で原因特定を早めるためのガード。
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


def _extract_slack_user_id(user_sub: str | None) -> str | None:
    """Slack OIDC sub から Slack user_id を取り出す。

    本番 Slack OIDC は pure user_id 形式 (例: "U0XXXXXXXX") を返すことを
    実機 cookie デコードで確認済 (.steering/20260518-frontend-profile-view/tasklist.md T0-1)。
    将来 "<user_id>-<team_id>" 形式に変わっても壊れないよう split("-")[0] で防御。
    Slack user_id は uppercase alphanumeric のみで hyphen を含まない仕様。
    抽出後に Slack user_id らしさを regex 検証し、形状破壊を 401 で早期 fail させる。
    """
    if not user_sub:
        return None
    candidate = user_sub.split("-", 1)[0]
    if not _SLACK_USER_ID_RE.fullmatch(candidate):
        return None
    return candidate
```

### 変更 2: `_serialize_learned_preferences` ヘルパ新規追加 (`src/dashboard_api/get_my_profile/app.py:53-74`)

```python
def _serialize_learned_preferences(raw: object) -> list[str]:
    """UserProfilesTable の learned_preferences を frontend 用 string[] に正規化する。

    実保存形式は `{"text": str, "created_at": str}` の dict 配列 (feedback_processor.py:199)。
    将来の互換性のため、文字列要素 / 旧形式 / 想定外型も吸収する:
    - dict: "text" フィールドを抽出 (空文字や whitespace-only はスキップ)
    - str: そのまま採用
    - その他: スキップ
    """
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for pref in raw:
        if isinstance(pref, str):
            text: object = pref
        elif isinstance(pref, dict):
            text = pref.get("text")
        else:
            continue
        if isinstance(text, str) and text.strip():
            result.append(text)
    return result
```

### 変更 3: ハンドラ内で正規化ヘルパを呼び出し (`src/dashboard_api/get_my_profile/app.py:97`)

```python
    item = result.get("Item") or {}
    body = {
        "user_id": user_id,
        **{k: item.get(k) for k in _PROFILE_KEYS},
        "learned_preferences": _serialize_learned_preferences(item.get("learned_preferences")),
        "updated_at": item.get("updated_at"),
    }
    return json_response(200, {"data": body})
```

### テスト (前回からの差分のみ)

`tests/unit/dashboard_api/test_get_my_profile.py`:

**既存テスト更新**:
- `test_returns_full_profile`: fixture を本番形式 `[{"text": str, "created_at": str}]` 配列に変更。期待値は変わらず `["長めのサマリを好む", "コード例を望む"]` (backend で正規化されるため)

**新規追加 3 件**:

1. `test_malformed_user_sub_returns_401` — regex 不一致 3 ケース (lowercase / 数字始まり / 特殊文字)、いずれも 401 + `get_item.assert_not_called`

```python
def test_malformed_user_sub_returns_401(self):
    table = MagicMock()
    # ケース 1: lowercase
    result = _run(table, _make_event(user_sub="u04jbju88a0"))
    assert result["statusCode"] == 401
    # ケース 2: 数字始まり
    result = _run(table, _make_event(user_sub="0123456789"))  # gitleaks:allow
    assert result["statusCode"] == 401
    # ケース 3: 特殊文字含む
    result = _run(table, _make_event(user_sub="U04*ABC"))
    assert result["statusCode"] == 401
    table.get_item.assert_not_called()
```

2. `test_serialize_learned_preferences_handles_mixed_input` — 6 系統 (dict / str / 混在 / 空 text / 想定外型 / 非 list)

```python
def test_serialize_learned_preferences_handles_mixed_input(self):
    from src.dashboard_api.get_my_profile.app import _serialize_learned_preferences

    assert _serialize_learned_preferences(
        [{"text": "好み A", "created_at": "2026-01-01T00:00:00Z"}]
    ) == ["好み A"]
    assert _serialize_learned_preferences(["legacy_pref"]) == ["legacy_pref"]
    assert _serialize_learned_preferences(
        [{"text": "A"}, "B", {"text": "C", "created_at": "x"}]
    ) == ["A", "B", "C"]
    assert _serialize_learned_preferences(
        [{"text": ""}, {"text": "   "}, {"text": None}, {}]
    ) == []
    assert _serialize_learned_preferences([123, None, ["nested"], "valid"]) == ["valid"]
    assert _serialize_learned_preferences(None) == []
    assert _serialize_learned_preferences("not-a-list") == []
    assert _serialize_learned_preferences({}) == []
    assert _serialize_learned_preferences(42) == []
```

3. `test_profile_keys_are_stable` — `_PROFILE_KEYS` の順序・名称をハードコード比較

```python
def test_profile_keys_are_stable(self):
    from src.dashboard_api.get_my_profile.app import _PROFILE_KEYS

    assert _PROFILE_KEYS == (
        "role",
        "interests",
        "expertise",
        "learning_goals",
        "background",
        "output_preferences",
    )
```

**テスト結果**: 7 件 → 10 件全 pass

```
$ uv run pytest tests/unit/dashboard_api/test_get_my_profile.py -v
10 passed in 0.57s
```

ruff check / format クリーン、gitleaks 検出件数 96 (ベースライン維持)。

### 変更ファイル統計

```
src/dashboard_api/get_my_profile/app.py         | +37 行 (regex import + RE + 検証分岐 + 新ヘルパ + 呼び出し変更)
tests/unit/dashboard_api/test_get_my_profile.py | +90 行 / -8 行 (fixture 更新 + 新テスト 3 件 + docstring 更新)
合計: +127 行 / -8 行
```

## Pass 2 レビュー観点

### 1. 前回指摘の解消確認

前回 `.audit/2026-05-19_frontend-profile-view.md` で指摘された 3 件 (High 1 + Low 2) が完全に解消されているか:

- **High #1 (learned_preferences 形状不一致)**: 是正実装で `_serialize_learned_preferences` を導入し API 出口で正規化。frontend 型契約 `string[]` を維持。学習履歴あり dict 配列でも React render error にならないか? 旧 string 形式 / dict + str 混在 / 空 text / 想定外型すべて吸収しているか? **特に: `_run_review_loop` パターンの「内部消費者は dict のまま、API 経由は string[]」の境界が正しく機能しているか** (orchestrator.py:1265 は dict 直アクセスを維持)
- **Low #1 (regex 検証なし)**: `^[UW][A-Z0-9]+$` で防御。pure `U...` / `<U...>-<T...>` どちらも match、malformed は弾く。挙動変更 (placeholder → 401) は意図的だが、本物のユーザーが弾かれる誤検出リスクはないか? regex の `+` 量化子で 1 文字以上を許容している (例: `"U"` 単体も通る) が、これは現実的に問題か?
- **Low #2 (drift 検知)**: Codex 推奨の **trigger.app import 案を採用せず、ハードコード比較案に変更**。dashboard test の境界保護を理由としたが、これにより trigger 側の `PROFILE_FIELDS` 変更を dashboard test で検知できなくなった。この**意図的逸脱**は妥当か? trigger 側で対称テスト (`PROFILE_FIELDS` の key 部分が `("role", "interests", ...)` であること) を別途追加すべきか?

### 2. 是正により浮上した新規問題

修正コードによって新たに混入した可能性のある問題:

- **a. `_serialize_learned_preferences` の正規化で発生する情報損失**: `created_at` / `replaces_index` 等のメタ情報が API レスポンスから消える。将来 frontend で「いつ学習したか」を表示したくなった場合、API 仕様変更が必要 (現状 `string[]` を期待しているコードは frontend のみで影響範囲は限定的) — この情報損失を許容する設計判断は妥当か?
- **b. `_SLACK_USER_ID_RE` の規約**: Slack 公式仕様で user_id 文字種は uppercase alphanumeric のみと明記されている前提で書いている。`+` 量化子で 1 文字以上を許容しているため `"U"` 1 文字 sub でも通過する。本来は最低 7-9 文字程度の長さ制約を入れるべきか? あるいは `+` で十分か?
- **c. `_serialize_learned_preferences` で whitespace-only text をスキップする挙動**: feedback_processor.py:194 が `text = pref.get("text", "").strip()` で empty 排除済だが、過去データに strip 前の whitespace-only が残っているリスク。スキップ判定 (`text.strip()`) は API としての見え方を変えるが、これは妥当か?
- **d. error_response の context**: regex 失敗時も既存の "Missing or invalid user_sub" メッセージを再利用 (specific code を新設していない)。debug 時に「regex 不一致」と「sub 欠落」を区別できないが、CloudWatch Logs で request_id を辿れば判別可能。これは許容範囲か?

### 3. 前パスで見落としていた次層

前 pass では Info 扱い or 指摘なしだった次層:

- **e. `_serialize_learned_preferences` の同型バグ**: 6 軸 (`role` / `interests` 等) 本体も「string 期待だが実は dict や他型が混入」リスクはないか? `feedback_processor.py` を読む限り 6 軸自体は Slack Modal が string でしか書き込まないが、Modal 経由でない書き込みパス (移行スクリプト等) で型契約が破れる可能性は? `**{k: item.get(k) for k in _PROFILE_KEYS}` で何でもそのまま返している現状は H1 と対称的な脆弱性か?
- **f. テストカバレッジの非対称**: `test_serialize_learned_preferences_handles_mixed_input` は手厚いが、`test_extract_slack_user_id` を分離した unit test がない (lambda_handler 経由でしかテストしていない)。分離テストを追加すべきか?
- **g. `test_returns_full_profile` の fixture 変更**: 既存テスト 1 件で fixture 型変更を行った。これは「以前は string 配列を返していた挙動が事実上の bug だった」ことを暗黙に認める変更。テスト変更が事実認定として明示的か? (テスト追加した方が安全だったか?)
- **h. ハードコード比較案の見落とし**: trigger.app 側に対称テストを追加していない。`tests/unit/trigger/test_app.py` (存在するなら) に `PROFILE_FIELDS` の key 部分を ハードコード比較する対称テストを追加するのが完全な drift 検知になる。Pass 2 で追加すべきか後続 PR で良いか?
- **i. ECS client 初期化リスクの再考**: 「dashboard test に trigger.app を import すると ECS client が初期化される」を回避理由とした。だが現状でも他経路 (例: 統合テスト) で import される可能性。「テスト隔離違反」の判断基準は妥当か? それとも案 C (ハードコード比較) は過剰な防御で、Codex 推奨案 (trigger.app 直 import) でも問題なかったか?

## 出力フォーマット

```markdown
# Codex Review Pass 2 — 2026-05-19 frontend-profile-view

## サマリ
- Critical: N / High: N / Medium: N / Low: N / Info: N
- 前回指摘の解消確認: ✅ N/3 解消、⚠️ N/3 部分、❌ N/3 残存
- 多層ミス検出: N 件
- 総合所感 (5 行以内)

## 指摘事項 (新規発生分 + 残存分)

### [severity] [path:line] 短いタイトル
- 区分: 新規 / 残存 / リグレッション
- 問題: ...
- 影響: ...
- 推奨修正: ...

(severity 順、severity は Critical / High / Medium / Low / Info)

## 前回指摘の解消状況
- High #1 (learned_preferences): 解消 ✅ / 部分解消 ⚠️ / 未解消 ❌ / リグレッション 🔴
- Low #1 (user_sub regex): 解消 ✅ / ...
- Low #2 (drift 検知 案 C 採用): 解消 ✅ / 妥当 / ⚠️ Codex 推奨と異なる判断の妥当性評価

## 結論
- 収束判定: 収束 (新規 Critical/High ゼロ) / Pass 3 要 (新規指摘あり) / 不可
- マージ可否: 可 / 条件付き可 (指摘対応後) / 不可
```

注意:
- 前回 Info 扱いだった項目で今回も未着手のものは、別 issue としてスコープ外扱いで OK (再指摘可)
- 「対応が完全 / 不要修正なし」も明示してほしい
- 公式仕様への引用があれば URL 付きで (例: Slack OIDC user_id 文字種仕様)
- 推奨修正は具体的なコードスニペットで示してほしい
- **Codex 推奨案 (trigger.app 直 import) からの逸脱 (ハードコード比較案採用) の妥当性を明示的に評価してほしい**
