"""src/dashboard_api/auth_callback/app.py の単体テスト。

カバー対象:
- 正常フロー: state 検証 → Slack token exchange → userInfo → JWT 発行 → 302 + Set-Cookie
- state 欠落 → 400
- state 不一致 (DDB に存在しない) → 400
- Slack token exchange 失敗 → 502
- Slack token exchange ネットワークエラー → 502
- workspace mismatch → 401
- userInfo 失敗 → 502
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import jwt
import pytest

_TEST_KEY = "test-jwt-signing-key-32-bytes-ok"
_SLACK_CONFIG = {
    "client_id": "TEST_CLIENT_ID",
    "client_secret": "TEST_CLIENT_SECRET",
    "workspace_id": "T_EXPECTED",
}
_USER_INFO = {
    "ok": True,
    "sub": "U01234567",
    "name": "Alice",
    "https://slack.com/team_id": "T_EXPECTED",
}
_TOKEN_RESP_OK = {
    "ok": True,
    "authed_user": {"access_token": "xoxp-test-token"},
}


def _event(code: str = "CODE", state: str = "STATE") -> dict:
    return {
        "queryStringParameters": {"code": code, "state": state},
        "headers": {"host": "dashboard.example.com"},
        "requestContext": {"domainName": "api-gw.example.com"},
    }


def _ddb_found(state: str = "STATE") -> MagicMock:
    mock = MagicMock()
    mock.get_item.return_value = {"Item": {"state": state, "ttl": int(time.time()) + 600}}
    return mock


def _ddb_not_found() -> MagicMock:
    mock = MagicMock()
    mock.get_item.return_value = {}
    return mock


@pytest.fixture(autouse=True)
def set_env_and_reset_cache(monkeypatch):
    """env var を設定してモジュールレベルキャッシュをリセット。"""
    monkeypatch.setenv("OAUTH_STATE_TABLE", "test-oauth-state")
    monkeypatch.setenv("SLACK_OAUTH_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-1:123:secret:slack")
    monkeypatch.setenv("JWT_KEY_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-1:123:secret:jwt")

    import src.dashboard_api.auth_callback.app as mod
    mod._slack_config = None
    mod._jwt_signing_key = None
    yield


@pytest.fixture()
def mock_secrets():
    """Secrets Manager をモック。"""
    client = MagicMock()
    client.get_secret_value.side_effect = lambda SecretId, **_: {
        "SecretString": (
            json.dumps(_SLACK_CONFIG)
            if "slack" in SecretId
            else _TEST_KEY
        )
    }
    with patch("src.dashboard_api.auth_callback.app._secrets_client", client):
        yield client


@pytest.fixture()
def mock_dynamodb():
    resource = MagicMock()
    table = _ddb_found()
    resource.Table.return_value = table
    with patch("src.dashboard_api.auth_callback.app._dynamodb", resource):
        yield table


@pytest.fixture()
def mock_slack(mock_dynamodb, mock_secrets):
    """Slack API 呼び出しをモック (正常系デフォルト)。"""
    with patch("src.dashboard_api.auth_callback.app._slack_post") as mock_post:
        mock_post.side_effect = [_TOKEN_RESP_OK, _USER_INFO]
        yield mock_post


class TestHappyPath:
    def test_returns_302_with_set_cookie(self, mock_slack):
        from src.dashboard_api.auth_callback.app import lambda_handler

        result = lambda_handler(_event(), None)

        assert result["statusCode"] == 302
        assert "Location" in result["headers"]
        assert "Set-Cookie" in result["headers"]

    def test_cookie_is_httponly_secure_samesite(self, mock_slack):
        from src.dashboard_api.auth_callback.app import lambda_handler

        result = lambda_handler(_event(), None)
        cookie = result["headers"]["Set-Cookie"]

        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Lax" in cookie

    def test_cookie_contains_valid_jwt(self, mock_slack):
        from src.dashboard_api.auth_callback.app import lambda_handler

        result = lambda_handler(_event(), None)
        cookie = result["headers"]["Set-Cookie"]
        # "session=<token>; HttpOnly; ..."
        token = cookie.split(";")[0].split("=", 1)[1]
        claims = jwt.decode(token, _TEST_KEY, algorithms=["HS256"])

        assert claims["sub"] == _USER_INFO["sub"]
        assert claims["name"] == _USER_INFO["name"]
        assert claims["exp"] > int(time.time())

    def test_redirects_to_host_root(self, mock_slack):
        from src.dashboard_api.auth_callback.app import lambda_handler

        result = lambda_handler(_event(), None)

        assert result["headers"]["Location"] == "https://dashboard.example.com/"

    def test_state_deleted_after_use(self, mock_slack, mock_dynamodb):
        from src.dashboard_api.auth_callback.app import lambda_handler

        lambda_handler(_event(state="STATE"), None)

        mock_dynamodb.delete_item.assert_called_once_with(Key={"state": "STATE"})


class TestInvalidState:
    def test_missing_code_returns_400(self, mock_secrets):
        from src.dashboard_api.auth_callback.app import lambda_handler

        event = {"queryStringParameters": {"state": "STATE"}, "headers": {}, "requestContext": {"domainName": "x"}}
        result = lambda_handler(event, None)

        assert result["statusCode"] == 400

    def test_missing_state_returns_400(self, mock_secrets):
        from src.dashboard_api.auth_callback.app import lambda_handler

        event = {"queryStringParameters": {"code": "CODE"}, "headers": {}, "requestContext": {"domainName": "x"}}
        result = lambda_handler(event, None)

        assert result["statusCode"] == 400

    def test_unknown_state_returns_400(self, mock_secrets):
        from src.dashboard_api.auth_callback.app import lambda_handler

        resource = MagicMock()
        resource.Table.return_value = _ddb_not_found()
        with patch("src.dashboard_api.auth_callback.app._dynamodb", resource):
            result = lambda_handler(_event(), None)

        assert result["statusCode"] == 400
        assert json.loads(result["body"])["error"] == "invalid_state"


class TestSlackErrors:
    def test_slack_token_exchange_failure_returns_502(self, mock_dynamodb, mock_secrets):
        from src.dashboard_api.auth_callback.app import lambda_handler

        with patch("src.dashboard_api.auth_callback.app._slack_post") as mock_post:
            mock_post.return_value = {"ok": False, "error": "invalid_code"}
            result = lambda_handler(_event(), None)

        assert result["statusCode"] == 502

    def test_slack_network_error_returns_502(self, mock_dynamodb, mock_secrets):
        from urllib.error import URLError

        from src.dashboard_api.auth_callback.app import lambda_handler

        with patch("src.dashboard_api.auth_callback.app._slack_post") as mock_post:
            mock_post.side_effect = URLError("connection refused")
            result = lambda_handler(_event(), None)

        assert result["statusCode"] == 502

    def test_workspace_mismatch_returns_401(self, mock_dynamodb, mock_secrets):
        from src.dashboard_api.auth_callback.app import lambda_handler

        wrong_workspace = {**_USER_INFO, "https://slack.com/team_id": "T_OTHER"}
        with patch("src.dashboard_api.auth_callback.app._slack_post") as mock_post:
            mock_post.side_effect = [_TOKEN_RESP_OK, wrong_workspace]
            result = lambda_handler(_event(), None)

        assert result["statusCode"] == 401
        assert json.loads(result["body"])["error"] == "workspace_mismatch"

    def test_userinfo_failure_returns_502(self, mock_dynamodb, mock_secrets):
        from src.dashboard_api.auth_callback.app import lambda_handler

        with patch("src.dashboard_api.auth_callback.app._slack_post") as mock_post:
            mock_post.side_effect = [_TOKEN_RESP_OK, {"ok": False, "error": "invalid_auth"}]
            result = lambda_handler(_event(), None)

        assert result["statusCode"] == 502
