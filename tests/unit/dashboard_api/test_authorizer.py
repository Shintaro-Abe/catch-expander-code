"""src/dashboard_api/authorizer/app.py の単体テスト。

T1-8 完了条件:
- 有効 JWT → isAuthorized: True + context
- 期限切れ JWT → isAuthorized: False
- 不正署名 JWT → isAuthorized: False
- cookie なし (cookies リスト空) → isAuthorized: False
- cookie あるが session キーなし → isAuthorized: False
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest

_TEST_KEY = "test-signing-key-for-unit-tests-only"
_USER_SUB = "U01234567"
_USER_NAME = "tester"


def _make_token(
    sub: str = _USER_SUB,
    name: str = _USER_NAME,
    exp_offset: int = 3600,
    key: str = _TEST_KEY,
) -> str:
    payload = {"sub": sub, "name": name, "exp": int(time.time()) + exp_offset}
    return jwt.encode(payload, key, algorithm="HS256")


def _event(cookies: list[str]) -> dict:
    return {"cookies": cookies}


@pytest.fixture(autouse=True)
def patch_secrets(monkeypatch):
    """Secrets Manager 呼び出しをモックし、テスト用署名キーを返す。"""
    monkeypatch.setenv("JWT_KEY_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:test")

    mock_client = MagicMock()
    mock_client.get_secret_value.return_value = {"SecretString": _TEST_KEY}

    # モジュールレベルキャッシュをリセット
    with (
        patch("src.dashboard_api.authorizer.app.secrets_client", mock_client),
        patch("src.dashboard_api.authorizer.app._jwt_signing_key", None),
    ):
        yield mock_client


class TestValidToken:
    def test_valid_jwt_returns_authorized(self, patch_secrets):
        from src.dashboard_api.authorizer.app import lambda_handler

        result = lambda_handler(_event([f"session={_make_token()}"]), None)

        assert result["isAuthorized"] is True
        assert result["context"]["user_sub"] == _USER_SUB
        assert result["context"]["user_name"] == _USER_NAME

    def test_valid_jwt_mixed_with_other_cookies(self, patch_secrets):
        from src.dashboard_api.authorizer.app import lambda_handler

        cookies = ["theme=dark", f"session={_make_token()}", "lang=ja"]
        result = lambda_handler(_event(cookies), None)

        assert result["isAuthorized"] is True

    def test_signing_key_fetched_once_per_env(self, patch_secrets):
        from src.dashboard_api.authorizer import app

        # _jwt_signing_key をリセットしてから 2 回呼ぶ
        app._jwt_signing_key = None
        app.lambda_handler(_event([f"session={_make_token()}"]), None)
        app.lambda_handler(_event([f"session={_make_token()}"]), None)

        # Secrets Manager は 1 回のみ呼ばれる
        assert patch_secrets.get_secret_value.call_count == 1


class TestInvalidToken:
    def test_expired_jwt_returns_unauthorized(self, patch_secrets):
        from src.dashboard_api.authorizer.app import lambda_handler

        token = _make_token(exp_offset=-1)  # 既に失効
        result = lambda_handler(_event([f"session={token}"]), None)

        assert result == {"isAuthorized": False}

    def test_wrong_signature_returns_unauthorized(self, patch_secrets):
        from src.dashboard_api.authorizer.app import lambda_handler

        token = _make_token(key="wrong-key")
        result = lambda_handler(_event([f"session={token}"]), None)

        assert result == {"isAuthorized": False}

    def test_malformed_token_returns_unauthorized(self, patch_secrets):
        from src.dashboard_api.authorizer.app import lambda_handler

        result = lambda_handler(_event(["session=not.a.valid.jwt.string"]), None)

        assert result == {"isAuthorized": False}


class TestMissingCookie:
    def test_empty_cookies_returns_unauthorized(self, patch_secrets):
        from src.dashboard_api.authorizer.app import lambda_handler

        result = lambda_handler(_event([]), None)

        assert result == {"isAuthorized": False}

    def test_no_cookies_key_in_event_returns_unauthorized(self, patch_secrets):
        from src.dashboard_api.authorizer.app import lambda_handler

        result = lambda_handler({}, None)

        assert result == {"isAuthorized": False}

    def test_cookies_without_session_key_returns_unauthorized(self, patch_secrets):
        from src.dashboard_api.authorizer.app import lambda_handler

        result = lambda_handler(_event(["theme=dark", "lang=ja"]), None)

        assert result == {"isAuthorized": False}
