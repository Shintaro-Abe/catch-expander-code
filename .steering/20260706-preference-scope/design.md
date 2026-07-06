# 設計書：F8 学習済み好みの適用スコープ導入

requirements.md の決定 1〜8 を実装に落とす。

## 1. データ構造

### 1.1 `user-profiles.learned_preferences` の新スキーマ

```
learned_preferences: [
  {
    "text": string,          # 命令形の好み文章（既存）
    "created_at": string,    # ISO 8601 UTC（既存）
    "scope": {               # 新規
      "categories":   list[string],  # ⊆ {技術, 時事, ビジネス, 学術, カルチャー}
      "deliverables": list[string]   # ⊆ {code, research_report, architecture_design,
                                     #    comparison_table, cost_estimate, procedure_guide}
    }
  },
  ...   # 最大 20 件（MAX_TOTAL_PREFERENCES 10 → 20）
]
```

- 両リスト空 = **汎用好み**（全プロンプト注入対象）
- `scope` キー欠損 = 汎用として扱う（防御的読み取り、受け入れ条件 8）

### 1.2 スコープ用 enum と展開規則（新規定数）

好みスコープの成果物区分（6 値）はワークフローの `deliverable_type`（7 値）とは**別レイヤーの語彙**。
対応はコード側の展開マップで固定する：

```python
SCOPE_CATEGORIES = {"技術", "時事", "ビジネス", "学術", "カルチャー"}

SCOPE_DELIVERABLES = {
    "code", "research_report", "architecture_design",
    "comparison_table", "cost_estimate", "procedure_guide",
}

# scope 区分 → workflow deliverable_type 集合
SCOPE_DELIVERABLE_EXPANSION = {
    "code": {"iac_code", "program_code"},
    # 他 5 値は自分自身に対応
}

SCOPE_DELIVERABLE_LABELS_JA = {
    "code": "コード", "research_report": "調査レポート",
    "architecture_design": "アーキテクチャ設計書", "comparison_table": "比較表",
    "cost_estimate": "料金見積もり", "procedure_guide": "手順書",
}
```

配置: `src/agent/feedback/` と `orchestrator.py` の両方から参照するため、`src/agent/feedback/scope.py`（新規）に定数 + 判定関数を置き、orchestrator からは `from feedback.scope import ...` で参照する（ECS image は `src/agent/` 直下が PYTHONPATH。feedback_processor.py の既存 import 形式 `from orchestrator import ...` と同じ流儀）。

## 2. 変更するコンポーネント

| ファイル | 種別 | 変更内容 |
|---------|------|---------|
| `src/agent/feedback/scope.py` | 新規 | enum 定数・展開マップ・`preference_applies()`・`format_scope_label()`・`validate_scope()` |
| `src/agent/feedback/feedback_processor.py` | 変更 | 抽出プロンプトに scope 出力を追加、バリデーション + 非対称フォールバック、上限 20、イベント payload 拡張、Slack 通知にスコープ渡し |
| `src/agent/orchestrator.py` | 変更 | `profile_text` 一括構築をやめ、①②③ 各段階でフィルタ済み好みセクションを構築 |
| `src/agent/notify/slack_client.py` | 変更 | `post_feedback_result()` にスコープラベル表示 |
| `src/dashboard_api/get_my_profile/app.py` | 変更 | `_serialize_learned_preferences()` を `string[]` → `{text, scope}[]` に変更 |
| `frontend/src/api/types.ts` | 変更 | `LearnedPreference` 型追加、`MyProfile.learned_preferences` の型変更 |
| `frontend/src/routes/MyProfile.tsx` | 変更 | 学習履歴の各行にスコープバッジ表示 |
| `scripts/migrate_preference_scopes.py` | 新規 | 既存好みの一括再分類（dry-run → 目視確認 → `--apply`） |
| `docs/glossary.md` | 変更 | 適用スコープ / 汎用好み / 段階的絞り込み / 成果物区分と deliverable_type の対応表 |
| `docs/adr/0002-preference-scope-extraction-time-classification.md` | 新規 | 「関連性判定は抽出時 1 回・実行時は決定的フィルタ」の ADR |
| `docs/functional-design.md` | 変更 | F8 データモデル・適用フローの記述更新 |
| `tests/unit/agent/test_feedback_processor.py` | 変更 | scope 抽出・バリデーション・フォールバック・上限 20 のテスト追加 |
| `tests/unit/agent/test_orchestrator.py` | 変更 | 段階別フィルタのテスト追加 |
| `tests/unit/agent/test_slack_client.py` | 変更 | スコープラベル表示テスト |
| `tests/unit/dashboard_api/` (get_my_profile) | 変更 | 新シリアライズ形状のテスト |

**変更なし:** `template.yaml`、`src/trigger/app.py`、`dynamodb_client.py`、`get_feedback_aggregation`、`FeedbackAnalysis.tsx`、`orchestrator.md`（成果物タイプ 7 種・選択基準は不変）

## 3. 詳細設計

### 3.1 判定関数（`scope.py`）

```python
def preference_applies(
    pref: dict,
    category: str | None = None,
    deliverable_type: str | None = None,
) -> bool:
    """決定的フィルタ本体。
    - scope 欠損 / 両リスト空 → 常に True（汎用）
    - categories 非空: category が None → False / category ∉ categories → False
    - deliverables 非空: deliverable_type が None → False /
      deliverable_type ∉ ∪ expand(d) → False
    """
```

段階ごとの使い分け（呼び出し側 orchestrator）:

| 段階 | 注入対象 | 命令文言 |
|---|---|---|
| ① トピック解析 | 汎用のみ（`scope` 欠損 or 両リスト空） | 「以下の好みを必ず反映してください」（現行維持） |
| ② ワークフロー設計 | 汎用 + カテゴリ一致。うち deliverables 非空のものは**別小節**でスコープラベル付き提示 | 汎用・カテゴリ一致分は「反映してください」、成果物スコープ分は「**該当する成果物を選ぶ場合はこの好みを考慮してください**」 |
| ③ 各生成ステップ | `preference_applies(pref, category, step.deliverable_type)` の完全フィルタ | 「以下の好みを必ず反映してください」（フィルタ済みなので強い命令を維持） |

追加規則:
- ② で `analysis.get("category")` が enum 外 / 欠損の場合はカテゴリ不明として**汎用のみ**注入（保守的）
- ③ も同様に category 不明なら categories 制約付き好みは落とす（`preference_applies` の None 規則で自然に実現）

### 3.2 orchestrator.py の変更

現在 `run()` 冒頭で `profile_text` を 1 回構築し 3 箇所で使い回している（design 20260412 §3.5 の方式）。これを廃止し：

```python
profile_text_base = json.dumps(profile, ...) or "プロファイル未登録"
learned_prefs = profile.get("learned_preferences", [])

# ① 解析: 汎用のみ
analysis_prompt = ... + _render_profile(profile_text_base, _general_only(learned_prefs))
# ② 設計: category 確定後に構築
wf_prompt = ... + _render_profile_for_workflow(profile_text_base, learned_prefs, category)
# ③ 生成: ステップごとに構築
gen_prompt = ... + _render_profile_for_generation(profile_text_base, learned_prefs, category, deliverable_type)
```

セクション見出し「## ユーザーの蓄積された好み（学習済み）」は維持。該当好みが 0 件の段階ではセクション自体を出さない（現行の `learned_prefs` 空のときと同じ挙動）。

### 3.3 抽出プロンプトの変更（`_build_extraction_prompt`）

追加する要素:

1. **スコープ選択の指示と enum 全定義**（カテゴリ 5 値の判断基準は orchestrator.md と同一の説明を転記、成果物区分 6 値は日本語ラベルで提示）
2. **元実行のコンテキスト**: topic / category（既存）に加え、実行レコードの `deliverable_types` を成果物区分に逆マップして提示（iac_code, program_code → コード）
3. **既存好み一覧にスコープラベルを併記**（置換判定の材料。例: `0: [コード] Terraform…`）
4. **出力 JSON に scope を追加**:

```json
{
  "preferences": [
    {
      "text": "命令形の好み文章",
      "scope": {"categories": [], "deliverables": []},
      "replaces_index": null
    }
  ]
}
```

5. **スコープ判断ルールの文言**:
   - 「この好みが将来どの範囲のトピック・成果物に適用されるべきかを選ぶ」
   - 「トピック固有でなく全成果物に当てはまる嗜好（文体・構成・簡潔さ等）は両方を空リストにする」
   - 良い例の文言を「コードはmodule分割してディレクトリ構造で管理する」（scope: deliverables=["code"]）に変更

6. **スコープ訂正フィードバックへの対応**: 既存好みへの「それは〜全般に適用して」型のフィードバックは、同じ text で scope だけ変えた preference + replaces_index として表現できることを明記

### 3.4 バリデーションと非対称フォールバック（`validate_scope()`）

入力: LLM 出力の `scope`（任意の型）、元実行の `category`、元実行の `deliverable_types`。

```
1. scope が dict でない / キー欠損 → 「検証失敗」扱い（3 へ）
2. 各次元: enum 外の値を破棄
   - 破棄前に非空 & 破棄後に空 → その次元は「検証失敗」（3 へ）
   - もともと空リスト → 意図的な汎用。そのまま採用
3. 検証失敗した次元のフォールバック（迷ったら狭く）:
   - categories → [元実行 category]（元実行 category が enum 外なら空リスト）
   - deliverables → 元実行 deliverable_types を成果物区分に逆マップした集合
     （iac_code / program_code → code。両方あっても {code} に収束）
```

`replaces_index` の扱いは現行 `_merge_preferences` のまま。置換時は text / scope / created_at をすべて新しい値で上書き。`MAX_TOTAL_PREFERENCES = 20` に変更、FIFO 削除は現行維持。

### 3.5 Slack 通知（`post_feedback_result`）

```
✅ フィードバックを記録しました。次回から以下の好みを反映します：
• [コード] コードはmodule分割してディレクトリ構造で管理する
• [時事・調査レポート] 複数の政治的立場の出典を併記する
• [汎用] 結論を先に簡潔に書く
（現在 12 件の好みが登録されています）
```

ラベルは `format_scope_label(scope)`: 両空 → `汎用`、それ以外 → categories + 成果物区分の日本語ラベルを `・` 連結。
訂正方法の 1 行（「スコープが違う場合はこのスレッドに返信で訂正できます」）を末尾に追加。

### 3.6 イベント payload（`feedback_received`）

既存フィールドは不変。追加のみ:

```
"new_general_count": int,   # 今回抽出分のうち汎用の件数
"new_scoped_count": int,    # 今回抽出分のうちスコープ付きの件数
```

`get_feedback_aggregation` は未知フィールドを無視する実装のため変更不要。

### 3.7 /profile API とフロントエンド

`get_my_profile/app.py` の `_serialize_learned_preferences`:

```python
# 変更後の返却形状（string[] → object[]）
[{"text": str, "scope": {"categories": [str], "deliverables": [str]}}]
# scope 欠損レコードは {"categories": [], "deliverables": []} に正規化
```

`frontend/src/api/types.ts`:

```ts
export interface LearnedPreference {
  text: string
  scope: { categories: string[]; deliverables: string[] }
}
// MyProfile.learned_preferences: string[] → LearnedPreference[]
```

`MyProfile.tsx`: 各行の先頭にバッジ表示（両空 → `汎用` をニュートラル色、カテゴリ / 成果物区分はそれぞれ別トーン）。成果物区分の表示名は `SCOPE_DELIVERABLE_LABELS_JA` と同じ対応表を frontend 側に定数として持つ。read-only は維持。

**注意（デプロイ順序）**: API とフロントは形状変更で結合するため、backend deploy → frontend deploy の順（`feedback_frontend_deploy_separate_from_sam` の手順: npm run build → s3 sync → CloudFront invalidation）。旧フロント + 新 API の間は学習履歴の表示が崩れる可能性があるが、個人利用のため許容（デプロイを同セッションで連続実行）。

### 3.8 移行スクリプト（`scripts/migrate_preference_scopes.py`）

既存好み（≤10 件・単一ユーザー）の一括再分類。検証の基準（CLAUDE.md §1）に従い書き戻し前に目視確認を挟む 2 段構成:

```
usage: python scripts/migrate_preference_scopes.py            # dry-run（デフォルト）
       python scripts/migrate_preference_scopes.py --apply    # 確認済み結果を書き戻し
```

1. **dry-run**: user-profiles を scan → scope 未付与の好みを列挙 → §3.3 と同じ判断基準の分類プロンプトで Claude に分類させ、`現在の text / 提案 scope` の対照表を stdout に出力し、提案 JSON を `scripts/.migration_proposal.json` に保存
2. **ユーザーが対照表を目視確認**（誤分類はファイルを手で修正可能）
3. **--apply**: proposal JSON を enum バリデーション（§3.4 と同一関数を import）してから `put_user_profile` で書き戻し。書き戻し前の値を stdout に出力（手動ロールバック用）

LLM 呼び出しはローカルの `claude -p`（headless）を使用。AWS 認証（`aws login`）が前提。

## 4. 影響範囲の分析

- **既存ユーザーへの影響**: 移行完了までの間、scope 欠損 = 汎用扱いなので**現行と同一挙動**（悪化しない）。移行後に本障害が解消
- **`_parse_claude_response` 型契約**（メモリ: 6 call sites 中 5 つが同型バグ）: 抽出結果の `parsed.get("preferences")` は現行も型ガードが弱い。scope 追加に伴い `validate_scope()` が非 dict 入力を握るため、この call site は型契約違反に対して現行より頑健になる
- **pre-existing failures**: unit 23 + integration 3 件（2026-05-14 時点メモ）。本変更のテストはこれらと独立
- **トークン量**: プロンプト注入は現行「全件」→「フィルタ済み」なので減少方向。保存上限 20 への引き上げは注入量に影響しない

## 5. テスト設計（主要ケース）

| 対象 | ケース |
|---|---|
| `preference_applies` | 汎用 / scope 欠損 / カテゴリ一致・不一致 / code 展開で iac_code・program_code 両方に適用 / category=None で categories 制約付きが落ちる |
| `validate_scope` | 意図的空 = 汎用維持 / enum 外破棄 / 破棄で空化 → 元実行値に縮退 / scope 非 dict → 縮退 / 元実行 category も不正 → 空 |
| orchestrator ① | 汎用のみ注入・スコープ付きが混入しない |
| orchestrator ② | カテゴリ一致分と成果物スコープ分が別小節・別文言 / category 不明 → 汎用のみ |
| orchestrator ③ | 完全フィルタ / 時事 research_report に code スコープが混入しない（受け入れ条件 1 の再現テスト） |
| merge | 上限 20 / 置換で scope も上書き |
| slack | ラベル整形（汎用 / 複合スコープ） |
| get_my_profile | 新形状 / scope 欠損の正規化 |

※ orchestrator のテストは `call_codex` / `call_claude` の両方を patch する（メモリ: `feedback_test_patches_call_codex_and_claude`）。
