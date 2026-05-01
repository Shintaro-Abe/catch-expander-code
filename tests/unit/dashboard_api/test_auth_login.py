"""src/dashboard_api/auth_login/app.py の単体テスト。"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

_SLACK_CONFIG = {
    "client_id": "TEST_CLIENT_ID",
    "client_secret": "TEST_CLIENT_SECRET",
    "workspace_id": "T_EXPECTED",
}


def _event(ip: str = "1.2.3.4", ua: str = "Mozilla/5.0") -> dict:
    return {
        "headers": {"host": "dashboard.example.com", "user-agent": ua},
        "requestContext": {
            "domainName": "api-gw.example.com",
            "http": {"sourceIp": ip},
        },
    }


@pytest.fixture(autouse=True)
def set_env_and_reset_cache(monkeypatch):
    monkeypatch.setenv("OAUTH_STATE_TABLE", "test-oauth-state")
    monkeypatch.setenv("SLACK_OAUTH_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-1:123:secret:slack")

    import src.dashboard_api.auth_login.app as mod
    mod._slack_config = None
    yield


@pytest.fixture()
def mock_secrets():
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(_SLACK_CONFIG)}
    with patch("src.dashboard_api.auth_login.app._secrets_client", client):
        yield client


@pytest.fixture()
def mock_dynamodb():
    resource = MagicMock()
    table = MagicMock()
    resource.Table.return_value = table
    with patch("src.dashboard_api.auth_login.app._dynamodb", resource):
        yield table


class TestLoginRedirect:
    def test_returns_302_to_slack(self, mock_secrets, mock_dynamodb):
        from src.dashboard_api.auth_login.app import lambda_handler

        result = lambda_handler(_event(), None)

        assert result["statusCode"] == 302
        assert result["headers"]["Location"].startswith("https://slack.com/oauth/v2/authorize")

    def test_redirect_contains_client_id(self, mock_secrets, mock_dynamodb):
        from src.dashboard_api.auth_login.app import lambda_handler

        result = lambda_handler(_event(), None)

        assert "client_id=TEST_CLIENT_ID" in result["headers"]["Location"]

    def test_redirect_contains_state(self, mock_secrets, mock_dynamodb):
        from src.dashboard_api.auth_login.app import lambda_handler

        result = lambda_handler(_event(), None)

        assert "state=" in result["headers"]["Location"]


class TestFingerprint:
    def test_fingerprint_stored_in_dynamodb(self, mock_secrets, mock_dynamodb):
        from src.dashboard_api.auth_login.app import lambda_handler

        ip, ua = "1.2.3.4", "Mozilla/5.0"
        lambda_handler(_event(ip=ip, ua=ua), None)

        call_kwargs = mock_dynamodb.put_item.call_args
        item = call_kwargs[1]["Item"] if call_kwargs[1] else call_kwargs[0][0]["Item"]
        expected_fp = hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()

        assert item.get("fingerprint") == expected_fp

    def test_fingerprint_changes_with_different_ip(self, mock_secrets, mock_dynamodb):
        from src.dashboard_api.auth_login.app import lambda_handler

        lambda_handler(_event(ip="10.0.0.1"), None)
        lambda_handler(_event(ip="10.0.0.2"), None)

        calls = mock_dynamodb.put_item.call_args_list
        fp1 = calls[0][1]["Item"]["fingerprint"]
        fp2 = calls[1][1]["Item"]["fingerprint"]

        assert fp1 != fp2
