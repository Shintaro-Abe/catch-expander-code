"""DashboardGetMyProfileFunction 単体テスト。

design.md §5.1 の 6 ケースを網羅する:
1. 正常: 全フィールド設定済み
2. 正常: レコード未存在 (新規ユーザー)
3. 正常: 一部フィールド REMOVE 済み
4. 異常: user_sub 欠落 → 401
5. 異常: sub が空文字 → 401
6. 異常: DDB raise → 500
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
            "learned_preferences": ["長めのサマリを好む", "コード例を望む"],
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
        assert body["learned_preferences"] == ["長めのサマリを好む", "コード例を望む"]
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

    def test_ddb_failure_returns_500(self):
        table = MagicMock()
        table.get_item.side_effect = RuntimeError("simulated DDB outage")

        result = _run(table, _make_event())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"]["code"] == "INTERNAL_ERROR"
