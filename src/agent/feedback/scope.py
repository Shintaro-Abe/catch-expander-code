"""学習済み好みの適用スコープ (.steering/20260706-preference-scope/design.md §1, §3.1, §3.4, §3.5)。

好みスコープの成果物区分（6 値）はワークフロー内部の deliverable_type（7 値）とは
別レイヤーの語彙。「コード」は iac_code / program_code の両方に展開される。
両次元とも空リスト（または scope キー欠損）= 汎用好みとして全プロンプト注入対象。
"""

# orchestrator.md のトピック解析出力 "category" と同一の値集合
SCOPE_CATEGORIES = frozenset({"技術", "時事", "ビジネス", "学術", "カルチャー"})

SCOPE_DELIVERABLES = frozenset(
    {
        "code",
        "research_report",
        "architecture_design",
        "comparison_table",
        "cost_estimate",
        "procedure_guide",
    }
)

# scope 区分 → workflow deliverable_type 集合。未掲載の区分は自分自身に対応
SCOPE_DELIVERABLE_EXPANSION: dict[str, frozenset[str]] = {
    "code": frozenset({"iac_code", "program_code"}),
}

SCOPE_DELIVERABLE_LABELS_JA = {
    "code": "コード",
    "research_report": "調査レポート",
    "architecture_design": "アーキテクチャ設計書",
    "comparison_table": "比較表",
    "cost_estimate": "料金見積もり",
    "procedure_guide": "手順書",
}

# workflow deliverable_type → scope 区分（validate_scope のフォールバックと
# 抽出プロンプトの元実行コンテキスト提示で使う逆マップ）
WORKFLOW_TYPE_TO_SCOPE = {
    "iac_code": "code",
    "program_code": "code",
    "research_report": "research_report",
    "architecture_design": "architecture_design",
    "comparison_table": "comparison_table",
    "cost_estimate": "cost_estimate",
    "procedure_guide": "procedure_guide",
}

_EMPTY_SCOPE: dict = {"categories": [], "deliverables": []}


def _scope_of(pref: dict) -> dict:
    """pref から scope を防御的に取り出す。欠損 / 非 dict は汎用扱い。"""
    scope = pref.get("scope")
    if not isinstance(scope, dict):
        return _EMPTY_SCOPE
    categories = scope.get("categories")
    deliverables = scope.get("deliverables")
    return {
        "categories": categories if isinstance(categories, list) else [],
        "deliverables": deliverables if isinstance(deliverables, list) else [],
    }


def is_general(pref: dict) -> bool:
    """汎用好み（スコープ制約なし）か。"""
    scope = _scope_of(pref)
    return not scope["categories"] and not scope["deliverables"]


def category_matches(pref: dict, category: str | None) -> bool:
    """カテゴリ次元のみの一致判定（②ワークフロー設計用: 成果物次元は無視する）。"""
    scope = _scope_of(pref)
    if not scope["categories"]:
        return True
    return category is not None and category in scope["categories"]


def has_deliverable_constraint(pref: dict) -> bool:
    """成果物区分の制約を持つか（②で「該当成果物を選ぶ場合に考慮」小節へ分ける判定）。"""
    return bool(_scope_of(pref)["deliverables"])


def expand_scope_deliverables(scope_deliverables: list) -> set[str]:
    """scope 区分のリストを workflow deliverable_type の集合へ展開する。"""
    expanded: set[str] = set()
    for kind in scope_deliverables:
        expanded |= SCOPE_DELIVERABLE_EXPANSION.get(kind, frozenset({kind}))
    return expanded


def preference_applies(
    pref: dict,
    category: str | None = None,
    deliverable_type: str | None = None,
) -> bool:
    """決定的フィルタ本体 (design §3.1)。

    - scope 欠損 / 両リスト空 → 常に True（汎用）
    - categories 非空: category が None または ∉ categories → False
    - deliverables 非空: deliverable_type が None または展開後集合に ∉ → False
    """
    scope = _scope_of(pref)
    if scope["categories"]:
        if category is None or category not in scope["categories"]:
            return False
    if scope["deliverables"]:
        if deliverable_type is None:
            return False
        return deliverable_type in expand_scope_deliverables(scope["deliverables"])
    return True


def validate_scope(
    raw_scope: object,
    source_category: str | None,
    source_deliverable_types: list | None,
) -> dict:
    """抽出 LLM が出力した scope を検証し、非対称フォールバックを適用する (design §3.4)。

    - 意図的な空リスト = 汎用としてそのまま採用
    - dict でない / キー欠損 / enum 検証で空化した次元 = 「検証失敗」として
      元実行の値に縮退（迷ったら狭く: 過剰適用より過少適用の方が害が小さい）
    """
    fallback_categories = [source_category] if source_category in SCOPE_CATEGORIES else []
    fallback_deliverables = sorted(
        {
            WORKFLOW_TYPE_TO_SCOPE[t]
            for t in (source_deliverable_types or [])
            if t in WORKFLOW_TYPE_TO_SCOPE
        }
    )

    if not isinstance(raw_scope, dict):
        return {"categories": fallback_categories, "deliverables": fallback_deliverables}

    def _validate_dimension(key: str, enum: frozenset, fallback: list) -> list:
        raw = raw_scope.get(key)
        if not isinstance(raw, list):
            return fallback  # キー欠損・型不正 = 検証失敗
        if not raw:
            return []  # 意図的な空 = 汎用
        valid = [v for v in raw if isinstance(v, str) and v in enum]
        if not valid:
            return fallback  # 全滅 = 検証失敗
        # 重複除去（順序維持）
        return list(dict.fromkeys(valid))

    return {
        "categories": _validate_dimension("categories", SCOPE_CATEGORIES, fallback_categories),
        "deliverables": _validate_dimension("deliverables", SCOPE_DELIVERABLES, fallback_deliverables),
    }


def format_scope_label(pref: dict) -> str:
    """Slack 通知・抽出プロンプトの既存好み一覧で使うスコープラベル (design §3.5)。

    両空 → "汎用"、それ以外 → カテゴリ + 成果物区分の日本語ラベルを「・」連結。
    """
    scope = _scope_of(pref)
    parts = list(scope["categories"])
    parts += [SCOPE_DELIVERABLE_LABELS_JA.get(d, d) for d in scope["deliverables"]]
    return "・".join(parts) if parts else "汎用"
