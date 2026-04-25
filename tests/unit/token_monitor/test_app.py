import json
import time
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest


# ---------------------------------------------------------------------------
# _needs_refresh
# ---------------------------------------------------------------------------


class TestNeedsRefresh:
    _BUFFER_MS = 60 * 60 * 1000  # 1h

    def test_returns_false_when_more_than_buffer_remaining(self):
        from handler import _needs_refresh

        now_ms = 1_000_000_000_000
        # 残り 2h
        expires_at_ms = now_ms + 2 * 60 * 60 * 1000
        assert _needs_refresh(expires_at_ms, self._BUFFER_MS, now_ms) is False

    def test_returns_true_when_within_buffer(self):
        from handler import _needs_refresh

        now_ms = 1_000_000_000_000
        # 残り 30min
        expires_at_ms = now_ms + 30 * 60 * 1000
        assert _needs_refresh(expires_at_ms, self._BUFFER_MS, now_ms) is True

    def test_returns_true_when_already_expired(self):
        from handler import _needs_refresh

        now_ms = 1_000_000_000_000
        # 1h 前に失効
        expires_at_ms = now_ms - 60 * 60 * 1000
        assert _needs_refresh(expires_at_ms, self._BUFFER_MS, now_ms) is True

    def test_boundary_at_exactly_buffer_returns_true(self):
        from handler import _needs_refresh

        now_ms = 1_000_000_000_000
        # ちょうど残り 1h
        expires_at_ms = now_ms + self._BUFFER_MS
        assert _needs_refresh(expires_at_ms, self._BUFFER_MS, now_ms) is True


# ---------------------------------------------------------------------------
# _call_refresh_endpoint
# ---------------------------------------------------------------------------


class TestCallRefreshEndpoint:
    def _mock_response(self, body: dict) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        return mock_resp

    def test_returns_parsed_json_on_success(self):
        from handler import _call_refresh_endpoint

        body = {"access_token": "new", "refresh_token": "rt2", "expires_in": 7200, "scope": "a b"}
        with patch("handler.urlopen", return_value=self._mock_response(body)):
            result = _call_refresh_endpoint("rt1")

        assert result == body

    def test_payload_contains_required_fields(self):
        from handler import CLIENT_ID, SCOPES, _call_refresh_endpoint

        captured: dict = {}

        def fake_urlopen(req, timeout):
            captured["req"] = req
            return self._mock_response({"access_token": "x", "expires_in": 7200})

        with patch("handler.urlopen", side_effect=fake_urlopen):
            _call_refresh_endpoint("my-rt")

        req = captured["req"]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["grant_type"] == "refresh_token"
        assert payload["refresh_token"] == "my-rt"
        assert payload["client_id"] == CLIENT_ID
        assert payload["scope"] == SCOPES
        assert req.headers["Content-type"] == "application/json"
        assert req.method == "POST"
        # User-Agent は必須: 既定の Python urllib UA は Cloudflare に Bot 判定される
        assert "claude-cli" in req.headers["User-agent"].lower()

    def test_propagates_http_401(self):
        from handler import _call_refresh_endpoint

        err = HTTPError(
            url="https://platform.claude.com/v1/oauth/token",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        with patch("handler.urlopen", side_effect=err):
            with pytest.raises(HTTPError) as exc_info:
                _call_refresh_endpoint("rt")
        assert exc_info.value.code == 401

    def test_propagates_http_500(self):
        from handler import _call_refresh_endpoint

        err = HTTPError(url="...", code=500, msg="ISE", hdrs=None, fp=None)
        with patch("handler.urlopen", side_effect=err):
            with pytest.raises(HTTPError) as exc_info:
                _call_refresh_endpoint("rt")
        assert exc_info.value.code == 500

    def test_propagates_url_error(self):
        from handler import _call_refresh_endpoint

        with patch("handler.urlopen", side_effect=URLError("dns fail")):
            with pytest.raises(URLError):
                _call_refresh_endpoint("rt")


# ---------------------------------------------------------------------------
# _build_updated_credentials
# ---------------------------------------------------------------------------


class TestBuildUpdatedCredentials:
    def test_calculates_expires_at_from_expires_in(self):
        from handler import _build_updated_credentials

        old = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "rt-old"}}
        result = {"access_token": "new", "expires_in": 7200}
        now_ms = 1_700_000_000_000

        new = _build_updated_credentials(old, result, now_ms)

        assert new["claudeAiOauth"]["expiresAt"] == now_ms + 7200 * 1000
        assert new["claudeAiOauth"]["accessToken"] == "new"

    def test_preserves_old_refresh_token_when_response_omits_it(self):
        from handler import _build_updated_credentials

        old = {"claudeAiOauth": {"refreshToken": "rt-keep"}}
        result = {"access_token": "new", "expires_in": 7200}

        new = _build_updated_credentials(old, result, 0)

        assert new["claudeAiOauth"]["refreshToken"] == "rt-keep"

    def test_uses_new_refresh_token_when_response_includes_it(self):
        from handler import _build_updated_credentials

        old = {"claudeAiOauth": {"refreshToken": "rt-old"}}
        result = {"access_token": "new", "refresh_token": "rt-new", "expires_in": 7200}

        new = _build_updated_credentials(old, result, 0)

        assert new["claudeAiOauth"]["refreshToken"] == "rt-new"

    def test_splits_scope_into_array(self):
        from handler import _build_updated_credentials

        old = {"claudeAiOauth": {}}
        result = {"access_token": "x", "scope": "user:profile user:inference"}

        new = _build_updated_credentials(old, result, 0)

        assert new["claudeAiOauth"]["scopes"] == ["user:profile", "user:inference"]

    def test_preserves_other_top_level_keys(self):
        from handler import _build_updated_credentials

        old = {
            "claudeAiOauth": {"accessToken": "old"},
            "oauthAccount": {"emailAddress": "test@example.com"},
        }
        result = {"access_token": "new", "expires_in": 7200}

        new = _build_updated_credentials(old, result, 0)

        assert new["oauthAccount"] == {"emailAddress": "test@example.com"}

    def test_uses_default_expires_in_when_missing(self):
        from handler import DEFAULT_EXPIRES_IN_SEC, _build_updated_credentials

        old = {"claudeAiOauth": {}}
        result = {"access_token": "x"}
        now_ms = 1_000_000

        new = _build_updated_credentials(old, result, now_ms)

        assert new["claudeAiOauth"]["expiresAt"] == now_ms + DEFAULT_EXPIRES_IN_SEC * 1000


# ---------------------------------------------------------------------------
# _post_slack_failure
# ---------------------------------------------------------------------------


class TestPostSlackFailure:
    def test_posts_message_with_reason_and_expiry(self):
        from handler import _post_slack_failure

        mock_client = MagicMock()
        with patch("handler.WebClient", return_value=mock_client):
            past_ms = int(time.time() * 1000) - 60 * 60 * 1000
            _post_slack_failure("slack-token", "C123", "http_401", past_ms)

        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "🚨" in text
        assert "http_401" in text
        assert "再認証" in text

    def test_posts_unknown_when_expires_at_none(self):
        from handler import _post_slack_failure

        mock_client = MagicMock()
        with patch("handler.WebClient", return_value=mock_client):
            _post_slack_failure("slack-token", "C123", "no_refresh_token", None)

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "不明" in text

    def test_swallows_slack_error(self):
        from slack_sdk.errors import SlackApiError

        from handler import _post_slack_failure

        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = SlackApiError(
            message="channel_not_found", response={"error": "channel_not_found"}
        )
        with patch("handler.WebClient", return_value=mock_client):
            # 例外が伝播しないこと（最終手段の通知のため）
            _post_slack_failure("slack-token", "C_INVALID", "http_500", None)


# ---------------------------------------------------------------------------
# lambda_handler — 統合フロー
# ---------------------------------------------------------------------------


class TestLambdaHandlerFlow:
    _COMMON_ENV = {
        "CLAUDE_OAUTH_SECRET_ARN": "arn:claude",
        "SLACK_BOT_TOKEN_SECRET_ARN": "arn:slack",
        "SLACK_NOTIFICATION_CHANNEL_ID": "C_MONITOR",
    }

    def _set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in self._COMMON_ENV.items():
            monkeypatch.setenv(k, v)

    def _creds_json(self, expires_at_ms: int, refresh_token: str | None = "rt") -> str:
        oauth = {"expiresAt": expires_at_ms}
        if refresh_token is not None:
            oauth["refreshToken"] = refresh_token
        return json.dumps({"claudeAiOauth": oauth})

    def test_still_valid_returns_no_op(self, monkeypatch):
        self._set_env(monkeypatch)
        future_ms = int(time.time() * 1000) + 2 * 60 * 60 * 1000  # 残り 2h

        with (
            patch("handler._get_secret", return_value=self._creds_json(future_ms)),
            patch("handler._put_secret") as mock_put,
            patch("handler._call_refresh_endpoint") as mock_refresh,
            patch("handler._post_slack_failure") as mock_notify,
        ):
            from handler import lambda_handler

            result = lambda_handler({}, None)

        assert result == {"refreshed": False, "reason": "still_valid"}
        mock_put.assert_not_called()
        mock_refresh.assert_not_called()
        mock_notify.assert_not_called()

    def test_no_refresh_token_notifies_slack(self, monkeypatch):
        self._set_env(monkeypatch)
        past_ms = int(time.time() * 1000) - 60 * 60 * 1000

        with (
            patch(
                "handler._get_secret",
                side_effect=[self._creds_json(past_ms, refresh_token=None), "slack-token"],
            ),
            patch("handler._put_secret") as mock_put,
            patch("handler._call_refresh_endpoint") as mock_refresh,
            patch("handler._post_slack_failure") as mock_notify,
        ):
            from handler import lambda_handler

            result = lambda_handler({}, None)

        assert result == {"refreshed": False, "reason": "no_refresh_token"}
        mock_put.assert_not_called()
        mock_refresh.assert_not_called()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.args[2] == "no_refresh_token"

    def test_http_401_notifies_slack(self, monkeypatch):
        self._set_env(monkeypatch)
        past_ms = int(time.time() * 1000) - 60 * 60 * 1000
        err = HTTPError(url="...", code=401, msg="Unauthorized", hdrs=None, fp=None)

        with (
            patch(
                "handler._get_secret",
                side_effect=[self._creds_json(past_ms), "slack-token"],
            ),
            patch("handler._put_secret") as mock_put,
            patch("handler._call_refresh_endpoint", side_effect=err),
            patch("handler._post_slack_failure") as mock_notify,
        ):
            from handler import lambda_handler

            result = lambda_handler({}, None)

        assert result == {"refreshed": False, "reason": "http_401"}
        mock_put.assert_not_called()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.args[2] == "http_401"

    def test_url_error_notifies_slack(self, monkeypatch):
        self._set_env(monkeypatch)
        past_ms = int(time.time() * 1000) - 60 * 60 * 1000

        with (
            patch(
                "handler._get_secret",
                side_effect=[self._creds_json(past_ms), "slack-token"],
            ),
            patch("handler._put_secret") as mock_put,
            patch("handler._call_refresh_endpoint", side_effect=URLError("dns")),
            patch("handler._post_slack_failure") as mock_notify,
        ):
            from handler import lambda_handler

            result = lambda_handler({}, None)

        assert result == {"refreshed": False, "reason": "url_error"}
        mock_put.assert_not_called()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.args[2] == "url_error"

    def test_success_updates_secret(self, monkeypatch):
        self._set_env(monkeypatch)
        past_ms = int(time.time() * 1000) - 60 * 60 * 1000
        refresh_response = {"access_token": "new", "refresh_token": "rt2", "expires_in": 7200}

        with (
            patch("handler._get_secret", return_value=self._creds_json(past_ms)),
            patch("handler._put_secret") as mock_put,
            patch("handler._call_refresh_endpoint", return_value=refresh_response),
            patch("handler._post_slack_failure") as mock_notify,
        ):
            from handler import lambda_handler

            result = lambda_handler({}, None)

        assert result["refreshed"] is True
        assert "new_expires_at_ms" in result
        mock_put.assert_called_once()
        # Secrets Manager に書かれた JSON を検証
        put_value = json.loads(mock_put.call_args.args[1])
        assert put_value["claudeAiOauth"]["accessToken"] == "new"
        assert put_value["claudeAiOauth"]["refreshToken"] == "rt2"
        mock_notify.assert_not_called()

    def test_missing_expires_at_notifies_slack(self, monkeypatch):
        self._set_env(monkeypatch)
        # claudeAiOauth に expiresAt が無い
        creds = json.dumps({"claudeAiOauth": {"refreshToken": "rt"}})

        with (
            patch("handler._get_secret", side_effect=[creds, "slack-token"]),
            patch("handler._put_secret") as mock_put,
            patch("handler._call_refresh_endpoint") as mock_refresh,
            patch("handler._post_slack_failure") as mock_notify,
        ):
            from handler import lambda_handler

            result = lambda_handler({}, None)

        assert result == {"refreshed": False, "reason": "no_expires_at"}
        mock_put.assert_not_called()
        mock_refresh.assert_not_called()
        mock_notify.assert_called_once()
