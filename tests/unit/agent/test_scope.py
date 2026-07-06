from feedback.scope import (
    expand_scope_deliverables,
    format_scope_label,
    is_general,
    preference_applies,
    validate_scope,
)


def _pref(categories=None, deliverables=None, **extra):
    pref = {"text": "t", "created_at": "2026-07-06T00:00:00+00:00", **extra}
    if categories is not None or deliverables is not None:
        pref["scope"] = {
            "categories": categories or [],
            "deliverables": deliverables or [],
        }
    return pref


class TestIsGeneral:
    def test_missing_scope_is_general(self):
        assert is_general(_pref())

    def test_empty_lists_are_general(self):
        assert is_general(_pref(categories=[], deliverables=[]))

    def test_scope_not_dict_is_general(self):
        assert is_general({"text": "t", "scope": "技術"})

    def test_category_scoped_is_not_general(self):
        assert not is_general(_pref(categories=["技術"]))

    def test_deliverable_scoped_is_not_general(self):
        assert not is_general(_pref(deliverables=["code"]))


class TestPreferenceApplies:
    def test_general_applies_everywhere(self):
        assert preference_applies(_pref(), category=None, deliverable_type=None)
        assert preference_applies(_pref(), category="時事", deliverable_type="research_report")

    def test_category_match(self):
        pref = _pref(categories=["技術"])
        assert preference_applies(pref, category="技術")
        assert not preference_applies(pref, category="時事")

    def test_category_constraint_drops_when_category_unknown(self):
        assert not preference_applies(_pref(categories=["技術"]), category=None)

    def test_code_scope_expands_to_both_workflow_types(self):
        pref = _pref(deliverables=["code"])
        assert preference_applies(pref, category="技術", deliverable_type="iac_code")
        assert preference_applies(pref, category="技術", deliverable_type="program_code")
        assert not preference_applies(pref, category="技術", deliverable_type="research_report")

    def test_deliverable_constraint_drops_when_type_unknown(self):
        assert not preference_applies(_pref(deliverables=["code"]), category="技術", deliverable_type=None)

    def test_non_code_scope_maps_to_itself(self):
        pref = _pref(deliverables=["research_report"])
        assert preference_applies(pref, category="時事", deliverable_type="research_report")
        assert not preference_applies(pref, category="時事", deliverable_type="iac_code")

    def test_both_dimensions_are_and(self):
        pref = _pref(categories=["技術"], deliverables=["code"])
        assert preference_applies(pref, category="技術", deliverable_type="iac_code")
        assert not preference_applies(pref, category="時事", deliverable_type="iac_code")
        assert not preference_applies(pref, category="技術", deliverable_type="research_report")

    def test_scope_with_non_list_values_treated_as_general(self):
        pref = {"text": "t", "scope": {"categories": "技術", "deliverables": None}}
        assert preference_applies(pref, category="時事", deliverable_type="research_report")


class TestExpandScopeDeliverables:
    def test_code_expands(self):
        assert expand_scope_deliverables(["code"]) == {"iac_code", "program_code"}

    def test_mixed(self):
        assert expand_scope_deliverables(["code", "cost_estimate"]) == {
            "iac_code",
            "program_code",
            "cost_estimate",
        }


class TestValidateScope:
    SOURCE_CATEGORY = "技術"
    SOURCE_TYPES = ["research_report", "iac_code"]

    def test_intentional_empty_stays_general(self):
        scope = validate_scope(
            {"categories": [], "deliverables": []},
            self.SOURCE_CATEGORY,
            self.SOURCE_TYPES,
        )
        assert scope == {"categories": [], "deliverables": []}

    def test_valid_values_pass_through(self):
        scope = validate_scope(
            {"categories": ["時事"], "deliverables": ["code"]},
            self.SOURCE_CATEGORY,
            self.SOURCE_TYPES,
        )
        assert scope == {"categories": ["時事"], "deliverables": ["code"]}

    def test_invalid_values_dropped_valid_kept(self):
        scope = validate_scope(
            {"categories": ["時事", "でたらめ"], "deliverables": ["code", "iac_code"]},
            self.SOURCE_CATEGORY,
            self.SOURCE_TYPES,
        )
        # iac_code は workflow の語彙であって scope の語彙ではないので破棄される
        assert scope == {"categories": ["時事"], "deliverables": ["code"]}

    def test_all_invalid_falls_back_to_source(self):
        scope = validate_scope(
            {"categories": ["でたらめ"], "deliverables": ["iac_code"]},
            self.SOURCE_CATEGORY,
            self.SOURCE_TYPES,
        )
        assert scope["categories"] == ["技術"]
        # 元実行の deliverable_types が scope 区分に逆マップされる（iac_code → code）
        assert scope["deliverables"] == ["code", "research_report"]

    def test_scope_not_dict_falls_back_to_source(self):
        scope = validate_scope("技術", self.SOURCE_CATEGORY, self.SOURCE_TYPES)
        assert scope["categories"] == ["技術"]
        assert scope["deliverables"] == ["code", "research_report"]

    def test_missing_keys_fall_back_to_source(self):
        scope = validate_scope({}, self.SOURCE_CATEGORY, self.SOURCE_TYPES)
        assert scope["categories"] == ["技術"]
        assert scope["deliverables"] == ["code", "research_report"]

    def test_fallback_with_invalid_source_category_is_empty(self):
        scope = validate_scope("junk", "不明", ["iac_code"])
        assert scope["categories"] == []
        assert scope["deliverables"] == ["code"]

    def test_fallback_with_no_source_types_is_empty(self):
        scope = validate_scope("junk", "技術", None)
        assert scope == {"categories": ["技術"], "deliverables": []}

    def test_duplicates_removed_order_kept(self):
        scope = validate_scope(
            {"categories": ["時事", "時事", "技術"], "deliverables": []},
            self.SOURCE_CATEGORY,
            self.SOURCE_TYPES,
        )
        assert scope["categories"] == ["時事", "技術"]


class TestFormatScopeLabel:
    def test_general(self):
        assert format_scope_label(_pref()) == "汎用"

    def test_category_only(self):
        assert format_scope_label(_pref(categories=["時事"])) == "時事"

    def test_deliverable_uses_ja_label(self):
        assert format_scope_label(_pref(deliverables=["code"])) == "コード"

    def test_combined(self):
        label = format_scope_label(_pref(categories=["時事"], deliverables=["research_report"]))
        assert label == "時事・調査レポート"
