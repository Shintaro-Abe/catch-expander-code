"""DashboardGetMyProfileFunction 単体テスト。

design.md §5.1 の 6 ケース + Codex Pass 1 是正で追加した 4 ケース:
1. 正常: 全フィールド設定済み (dict 配列の learned_preferences を string[] へ正規化)
2. 正常: レコード未存在 (新規ユーザー)
3. 正常: 一部フィールド REMOVE 済み
4. 正常: sub 形式が <user>-<team> (防御的 split + regex 検証)
5. 異常: user_sub 欠落 → 401
6. 異常: sub が空文字 → 401
7. 異常: sub が Slack user_id 形式に合わない → 401 (Codex L1)
8. 異常: DDB raise → 500
9. ヘルパ: _serialize_learned_preferences の混在入力吸収 (Codex H1)
10. drift: _PROFILE_KEYS の順序・名称が固定されている (Codex L2)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

_USER_SUB = "U04JBJU88A0"


def _make_event(user_sub: str | None = _USER_SUB) -> dict:
    """authorizer 経由の HttpApi event を模擬する。"""
    auth_lambda: dict = {}
    if user_sub is not None:
        auth_lambda["user_sub"] = user_sub
    return {
        "requestContext": {
            "authorizer": {"lambda": auth_lambda},
        },
    }


def _run(table_mock: MagicMock, event: dict) -> dict:
    resource = MagicMock()
    resource.Table.return_value = table_mock
    with patch("src.dashboard_api.get_my_profile.app._dynamodb", resource):
        from src.dashboard_api.get_my_profile.app import lambda_handler

        return lambda_handler(event, None)


class TestGetMyProfile:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("USER_PROFILES_TABLE", "test-user-profiles")

    # ------------------------------------------------------------------
    # 正常系
    # ------------------------------------------------------------------

    def test_returns_full_profile(self):
        item = {
            "user_id": _USER_SUB,
            "role": "クラウドエンジニア",
            "interests": "AI / 料理",
            "expertise": "Python / SQL",
            "learning_goals": "副業案件獲得",
            "background": "30代後半 / 子育て中",
            "output_preferences": "箇条書き重視",
            # 本番形式: feedback_processor.py が書き込む {"text", "created_at", "scope"} dict
            # (scope なしは 20260706-preference-scope 移行前の旧形式)
            "learned_preferences": [
                {"text": "長めのサマリを好む", "created_at": "2026-05-10T00:00:00.000Z"},
                {
                    "text": "コード例を望む",
                    "created_at": "2026-05-12T00:00:00.000Z",
                    "scope": {"categories": ["技術"], "deliverables": ["code"]},
                },
            ],
            "updated_at": "2026-05-18T10:00:00.000Z",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": item}

        result = _run(table, _make_event())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])["data"]
        assert body["user_id"] == _USER_SUB
        assert body["role"] == "クラウドエンジニア"
        assert body["background"] == "30代後半 / 子育て中"
        # backend が dict 配列を {text, scope}[] に正規化する (scope 欠損 = 汎用)
        assert body["learned_preferences"] == [
            {"text": "長めのサマリを好む", "scope": {"categories": [], "deliverables": []}},
            {"text": "コード例を望む", "scope": {"categories": ["技術"], "deliverables": ["code"]}},
        ]
        assert body["updated_at"] == "2026-05-18T10:00:00.000Z"
        # GetItem は本人 PK のみで呼ばれる
        table.get_item.assert_called_once_with(Key={"user_id": _USER_SUB})

    def test_returns_placeholder_when_record_missing(self):
        table = MagicMock()
        table.get_item.return_value = {}  # 新規ユーザー

        result = _run(table, _make_event())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])["data"]
        assert body["user_id"] == _USER_SUB
        # 6 軸全部 null、learned_preferences は空配列、updated_at は null
        for key in (
            "role",
            "interests",
            "expertise",
            "learning_goals",
            "background",
            "output_preferences",
        ):
            assert body[key] is None, f"{key} should be None"
        assert body["learned_preferences"] == []
        assert body["updated_at"] is None

    def test_partial_fields_missing(self):
        """Slack Modal で空欄保存された軸は DDB から REMOVE され、GetItem 後に該当キーが存在しない。"""
        item = {
            "user_id": _USER_SUB,
            "role": "エンジニア",
            # interests / expertise / learning_goals / output_preferences は REMOVE 済み
            "background": "リモートワーカー",
            "updated_at": "2026-05-18T11:00:00.000Z",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": item}

        result = _run(table, _make_event())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])["data"]
        assert body["role"] == "エンジニア"
        assert body["background"] == "リモートワーカー"
        assert body["interests"] is None
        assert body["expertise"] is None
        assert body["learning_goals"] is None
        assert body["output_preferences"] is None
        assert body["learned_preferences"] == []

    def test_strips_team_suffix_from_sub_defensive(self):
        """将来 Slack OIDC が "<user_id>-<team_id>" 形式を返した場合の防御挙動。"""
        table = MagicMock()
        table.get_item.return_value = {"Item": {"user_id": "U04JBJU88A0", "role": "Eng"}}

        result = _run(table, _make_event(user_sub="U04JBJU88A0-T0123456789"))  # gitleaks:allow

        assert result["statusCode"] == 200
        body = json.loads(result["body"])["data"]
        assert body["user_id"] == "U04JBJU88A0"
        # GetItem は "-" 抽出後の pure user_id で呼ばれる
        table.get_item.assert_called_once_with(Key={"user_id": "U04JBJU88A0"})

    # ------------------------------------------------------------------
    # 異常系
    # ------------------------------------------------------------------

    def test_missing_user_sub_returns_401(self):
        table = MagicMock()
        result = _run(table, _make_event(user_sub=None))

        assert result["statusCode"] == 401
        body = json.loads(result["body"])
        assert body["error"]["code"] == "UNAUTHORIZED"
        table.get_item.assert_not_called()

    def test_empty_user_sub_returns_401(self):
        table = MagicMock()
        result = _run(table, _make_event(user_sub=""))

        assert result["statusCode"] == 401
        body = json.loads(result["body"])
        assert body["error"]["code"] == "UNAUTHORIZED"
        table.get_item.assert_not_called()

    def test_malformed_user_sub_returns_401(self):
        """Codex L1: sub が Slack user_id 形式 (^[UW][A-Z0-9]+$) に合わなければ早期 401。

        従来は黙って placeholder 表示になり、cookie 破損時の原因特定が遅れていた。
        regex 追加で形状破壊を即 fail させる。
        """
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
        # GetItem は 3 回とも呼ばれていない (regex 段階で fail)
        table.get_item.assert_not_called()

    def test_ddb_failure_returns_500(self):
        table = MagicMock()
        table.get_item.side_effect = RuntimeError("simulated DDB outage")

        result = _run(table, _make_event())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"]["code"] == "INTERNAL_ERROR"

    # ------------------------------------------------------------------
    # ヘルパ単体テスト (Codex H1: dict / str / 想定外型の吸収)
    # ------------------------------------------------------------------

    def test_serialize_learned_preferences_handles_mixed_input(self):
        from src.dashboard_api.get_my_profile.app import _serialize_learned_preferences

        _general = {"categories": [], "deliverables": []}

        # 本番形式 (dict with text + created_at + scope)
        assert _serialize_learned_preferences(
            [
                {
                    "text": "好み A",
                    "created_at": "2026-01-01T00:00:00Z",
                    "scope": {"categories": ["時事"], "deliverables": ["research_report"]},
                }
            ]
        ) == [{"text": "好み A", "scope": {"categories": ["時事"], "deliverables": ["research_report"]}}]

        # 移行前の旧形式 (scope なし) は汎用扱い
        assert _serialize_learned_preferences([{"text": "好み B", "created_at": "2026-01-01T00:00:00Z"}]) == [
            {"text": "好み B", "scope": _general}
        ]

        # 旧データ互換 (素の文字列)
        assert _serialize_learned_preferences(["legacy_pref"]) == [{"text": "legacy_pref", "scope": _general}]

        # 混在
        mixed = _serialize_learned_preferences([{"text": "A"}, "B", {"text": "C", "created_at": "x"}])
        assert [p["text"] for p in mixed] == ["A", "B", "C"]

        # 空文字 / whitespace-only / None text はスキップ
        assert _serialize_learned_preferences([{"text": ""}, {"text": "   "}, {"text": None}, {}]) == []

        # 想定外型 (int / None / list of list 等) はスキップ
        assert _serialize_learned_preferences([123, None, ["nested"], "valid"]) == [
            {"text": "valid", "scope": _general}
        ]

        # 非 list は空配列
        assert _serialize_learned_preferences(None) == []
        assert _serialize_learned_preferences("not-a-list") == []
        assert _serialize_learned_preferences({}) == []
        assert _serialize_learned_preferences(42) == []

    def test_serialize_scope_drops_invalid_enum_values(self):
        from src.dashboard_api.get_my_profile.app import _serialize_scope

        # enum 外の値・型不正は表示側では汎用側に倒す（縮退フォールバックは書き込み側の責務）
        assert _serialize_scope({"categories": ["技術", "でたらめ"], "deliverables": ["code", "iac_code"]}) == {
            "categories": ["技術"],
            "deliverables": ["code"],
        }
        assert _serialize_scope({"categories": "技術", "deliverables": None}) == {
            "categories": [],
            "deliverables": [],
        }
        assert _serialize_scope(None) == {"categories": [], "deliverables": []}
        assert _serialize_scope("junk") == {"categories": [], "deliverables": []}

    # ------------------------------------------------------------------
    # drift 検知 (Codex L2: _PROFILE_KEYS の意図せぬ改変を防ぐ)
    # ------------------------------------------------------------------

    def test_profile_keys_are_stable(self):
        """6 軸キーの順序・名称が固定されていることを検証する。

        変更時は **src/trigger/app.py:PROFILE_FIELDS も同期更新すること**。
        本テストは dashboard_api 側の意図せぬ改変 (リファクタ事故 / typo / 順序入れ替え) を
        検知する。trigger 側との同期は trigger テストの責務。
        """
        from src.dashboard_api.get_my_profile.app import _PROFILE_KEYS

        assert _PROFILE_KEYS == (
            "role",
            "interests",
            "expertise",
            "learning_goals",
            "background",
            "output_preferences",
        )
