import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
import requests

# ---------------------------------------------------------------------------
# _setup_claude_credentials
# ---------------------------------------------------------------------------


class TestSetupClaudeCredentials:
    def test_writes_credentials_to_dot_credentials_json(self, tmp_path):
        from main import _setup_claude_credentials

        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            _setup_claude_credentials('{"token": "test-value"}')

        creds_file = tmp_path / ".claude" / ".credentials.json"
        assert creds_file.exists()
        assert creds_file.read_text() == '{"token": "test-value"}'

    def test_creates_claude_dir_if_not_exists(self, tmp_path):
        from main import _setup_claude_credentials

        assert not (tmp_path / ".claude").exists()

        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            _setup_claude_credentials("{}")

        assert (tmp_path / ".claude").exists()

    def test_sets_file_permissions_to_0600(self, tmp_path):
        import stat

        from main import _setup_claude_credentials

        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            _setup_claude_credentials("{}")

        creds_file = tmp_path / ".claude" / ".credentials.json"
        assert stat.S_IMODE(creds_file.stat().st_mode) == 0o600

    def test_returns_sha256_hash_of_secret_value(self, tmp_path):
        import hashlib

        from main import _setup_claude_credentials

        secret = '{"token": "abc"}'
        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            returned = _setup_claude_credentials(secret)

        assert returned == hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# _setup_codex_credentials
# ---------------------------------------------------------------------------


class TestSetupCodexCredentials:
    def test_writes_auth_json(self, tmp_path):
        from main import _setup_codex_credentials

        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            _setup_codex_credentials('{"tokens": "test"}')

        auth_file = tmp_path / ".codex" / "auth.json"
        assert auth_file.exists()
        assert auth_file.read_text() == '{"tokens": "test"}'

    def test_creates_codex_dir_if_not_exists(self, tmp_path):
        from main import _setup_codex_credentials

        assert not (tmp_path / ".codex").exists()

        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            _setup_codex_credentials("{}")

        assert (tmp_path / ".codex").exists()

    def test_sets_file_permissions_to_0600(self, tmp_path):
        import stat

        from main import _setup_codex_credentials

        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            _setup_codex_credentials("{}")

        auth_file = tmp_path / ".codex" / "auth.json"
        assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600

    def test_returns_sha256_hash_of_secret_value(self, tmp_path):
        import hashlib

        from main import _setup_codex_credentials

        secret = '{"tokens": "abc"}'
        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            returned = _setup_codex_credentials(secret)

        assert returned == hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# _writeback_codex_credentials
# ---------------------------------------------------------------------------


class TestWritebackCodexCredentials:
    def _patch_home(self, tmp_path):
        return patch.object(
            __import__("main").Path,
            "home",
            staticmethod(lambda: tmp_path),
        )

    def _setup_auth_file(self, tmp_path, content: str):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir(exist_ok=True)
        (codex_dir / "auth.json").write_text(content)

    def test_skips_when_credentials_unchanged(self, tmp_path):
        from main import _hash_text, _writeback_codex_credentials

        content = '{"tokens": "same"}'
        self._setup_auth_file(tmp_path, content)
        initial_hash = _hash_text(content)

        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
            _writeback_codex_credentials("arn:codex", initial_hash)

        mock_client_factory.assert_not_called()

    def test_calls_put_when_credentials_changed(self, tmp_path):
        from main import _hash_text, _writeback_codex_credentials

        initial_content = '{"tokens": "old"}'
        self._setup_auth_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".codex" / "auth.json").write_text('{"tokens": "new"}')

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": initial_content}
        with self._patch_home(tmp_path), patch("main.boto3.client", return_value=mock_client):
            _writeback_codex_credentials("arn:codex", initial_hash)

        mock_client.put_secret_value.assert_called_once_with(
            SecretId="arn:codex",
            SecretString='{"tokens": "new"}',
        )

    def test_skips_when_remote_updated_concurrently(self, tmp_path):
        from main import _hash_text, _writeback_codex_credentials

        initial_content = '{"tokens": "old"}'
        self._setup_auth_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".codex" / "auth.json").write_text('{"tokens": "new"}')

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": '{"tokens": "concurrent"}'}
        with self._patch_home(tmp_path), patch("main.boto3.client", return_value=mock_client):
            _writeback_codex_credentials("arn:codex", initial_hash)

        mock_client.put_secret_value.assert_not_called()

    def test_handles_missing_auth_file(self, tmp_path):
        from main import _writeback_codex_credentials

        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
            _writeback_codex_credentials("arn:codex", "any-hash")

        mock_client_factory.assert_not_called()

    def test_swallows_put_exception(self, tmp_path):
        from main import _hash_text, _writeback_codex_credentials

        initial_content = '{"tokens": "old"}'
        self._setup_auth_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".codex" / "auth.json").write_text('{"tokens": "new"}')

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": initial_content}
        mock_client.put_secret_value.side_effect = RuntimeError("AWS down")

        with self._patch_home(tmp_path), patch("main.boto3.client", return_value=mock_client):
            _writeback_codex_credentials("arn:codex", initial_hash)


# ---------------------------------------------------------------------------
# _writeback_claude_credentials
# ---------------------------------------------------------------------------


def _claude_creds_json(access: str, refresh: str) -> str:
    """claudeAiOauth 形式のダミー credentials (T27 のログイン判定を通す/落とす用)。"""
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh}})


class TestWritebackClaudeCredentials:
    def _patch_home(self, tmp_path):
        return patch.object(
            __import__("main").Path,
            "home",
            staticmethod(lambda: tmp_path),
        )

    def _setup_creds_file(self, tmp_path, content: str):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / ".credentials.json").write_text(content)

    def test_skips_when_credentials_unchanged(self, tmp_path):
        from main import _hash_text, _writeback_claude_credentials

        content = _claude_creds_json("same-at", "same-rt")
        self._setup_creds_file(tmp_path, content)
        initial_hash = _hash_text(content)

        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
            _writeback_claude_credentials("arn:claude", initial_hash)

        mock_client_factory.assert_not_called()

    def test_calls_put_when_credentials_changed(self, tmp_path):
        from main import _hash_text, _writeback_claude_credentials

        initial_content = _claude_creds_json("old-at", "old-rt")
        new_content = _claude_creds_json("new-at", "new-rt")
        self._setup_creds_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".claude" / ".credentials.json").write_text(new_content)

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": initial_content}
        with self._patch_home(tmp_path), patch("main.boto3.client", return_value=mock_client):
            _writeback_claude_credentials("arn:claude", initial_hash)

        mock_client.put_secret_value.assert_called_once_with(
            SecretId="arn:claude",
            SecretString=new_content,
        )

    def test_skips_when_remote_updated_concurrently(self, tmp_path):
        from main import _hash_text, _writeback_claude_credentials

        initial_content = _claude_creds_json("old-at", "old-rt")
        self._setup_creds_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".claude" / ".credentials.json").write_text(
            _claude_creds_json("new-at", "new-rt")
        )

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": _claude_creds_json("concurrent-at", "concurrent-rt")
        }
        with self._patch_home(tmp_path), patch("main.boto3.client", return_value=mock_client):
            _writeback_claude_credentials("arn:claude", initial_hash)

        mock_client.put_secret_value.assert_not_called()

    def test_handles_missing_credentials_file(self, tmp_path):
        from main import _writeback_claude_credentials

        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
            _writeback_claude_credentials("arn:claude", "any-hash")

        mock_client_factory.assert_not_called()

    def test_skips_writeback_when_credentials_logged_out(self, tmp_path):
        """T27 (exec-20260712003302): 認証失敗で CLI がログアウトし空になった credentials を
        書き戻すとシークレット側の正常値まで破壊するため、writeback をスキップする。"""
        from main import _hash_text, _writeback_claude_credentials

        initial_content = _claude_creds_json("old-at", "old-rt")
        self._setup_creds_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".claude" / ".credentials.json").write_text(
            json.dumps(
                {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}
            )
        )

        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
            _writeback_claude_credentials("arn:claude", initial_hash)

        mock_client_factory.assert_not_called()

    def test_skips_writeback_when_credentials_unparseable(self, tmp_path):
        """T27: JSON として読めない credentials も writeback しない (fail-closed)。"""
        from main import _hash_text, _writeback_claude_credentials

        initial_content = _claude_creds_json("old-at", "old-rt")
        self._setup_creds_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".claude" / ".credentials.json").write_text("not-json")

        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
            _writeback_claude_credentials("arn:claude", initial_hash)

        mock_client_factory.assert_not_called()

    def test_skips_writeback_when_oauth_section_is_not_dict(self, tmp_path):
        """T27 (Codex Pass 1 補足): claudeAiOauth が null 等でも例外ではなく skip。"""
        from main import _hash_text, _writeback_claude_credentials

        initial_content = _claude_creds_json("old-at", "old-rt")
        self._setup_creds_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": null}')

        with self._patch_home(tmp_path), patch("main.boto3.client") as mock_client_factory:
            _writeback_claude_credentials("arn:claude", initial_hash)

        mock_client_factory.assert_not_called()

    def test_swallows_put_exception(self, tmp_path):
        from main import _hash_text, _writeback_claude_credentials

        initial_content = _claude_creds_json("old-at", "old-rt")
        self._setup_creds_file(tmp_path, initial_content)
        initial_hash = _hash_text(initial_content)
        (tmp_path / ".claude" / ".credentials.json").write_text(
            _claude_creds_json("new-at", "new-rt")
        )

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": initial_content}
        mock_client.put_secret_value.side_effect = RuntimeError("AWS down")

        with self._patch_home(tmp_path), patch("main.boto3.client", return_value=mock_client):
            _writeback_claude_credentials("arn:claude", initial_hash)


# ---------------------------------------------------------------------------
# _run_feedback
# ---------------------------------------------------------------------------


class TestRunFeedback:
    def _set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USER_ID", "U123")
        monkeypatch.setenv("FEEDBACK_TEXT", "コードを短くして")
        monkeypatch.setenv("EXECUTION_ID", "exec-001")
        monkeypatch.setenv("SLACK_CHANNEL", "C456")
        monkeypatch.setenv("SLACK_THREAD_TS", "1234567890.123456")

    def test_calls_processor_with_env_vars(self, monkeypatch):
        self._set_env(monkeypatch)
        mock_processor = MagicMock()
        mock_slack = MagicMock()
        mock_db = MagicMock()

        with patch("main.FeedbackProcessor", return_value=mock_processor) as mock_cls:
            from main import _run_feedback

            _run_feedback(mock_slack, mock_db)

        mock_cls.assert_called_once_with(mock_slack, mock_db)
        mock_processor.process.assert_called_once_with(
            "U123", "コードを短くして", "exec-001", "C456", "1234567890.123456"
        )

    def test_does_not_update_execution_status(self, monkeypatch):
        self._set_env(monkeypatch)
        mock_db = MagicMock()

        with patch("main.FeedbackProcessor"):
            from main import _run_feedback

            _run_feedback(MagicMock(), mock_db)

        mock_db.update_execution_status.assert_not_called()


# ---------------------------------------------------------------------------
# _run_orchestrator
# ---------------------------------------------------------------------------


class TestRunOrchestrator:
    def _set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXECUTION_ID", "exec-999")
        monkeypatch.setenv("USER_ID", "U789")
        monkeypatch.setenv("TOPIC", "Terraform入門")
        monkeypatch.setenv("SLACK_CHANNEL", "C111")
        monkeypatch.setenv("SLACK_THREAD_TS", "9999999999.000001")
        monkeypatch.setenv("NOTION_DATABASE_ID", "notion-db-id")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")

    def test_calls_orchestrator_run_with_correct_args(self, monkeypatch):
        self._set_env(monkeypatch)
        mock_orchestrator = MagicMock()
        mock_db = MagicMock()
        mock_slack = MagicMock()

        with patch("main.Orchestrator", return_value=mock_orchestrator) as mock_cls:
            from main import _run_orchestrator

            _run_orchestrator(mock_slack, mock_db, "notion-token", "github-token")

        mock_cls.assert_called_once_with(
            mock_slack, mock_db, "notion-token", "notion-db-id", "github-token", "owner/repo"
        )
        mock_orchestrator.run.assert_called_once_with("exec-999", "U789", "Terraform入門", "C111", "9999999999.000001")

    def test_sets_completed_on_success(self, monkeypatch):
        self._set_env(monkeypatch)
        mock_db = MagicMock()

        with patch("main.Orchestrator"):
            from main import _run_orchestrator

            _run_orchestrator(MagicMock(), mock_db, "t", "g")

        mock_db.update_execution_status.assert_called_once_with("exec-999", "completed")

    def test_sets_failed_and_reraises_on_exception(self, monkeypatch):
        self._set_env(monkeypatch)
        mock_db = MagicMock()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run.side_effect = RuntimeError("something went wrong")

        with patch("main.Orchestrator", return_value=mock_orchestrator):
            from main import _run_orchestrator

            with pytest.raises(RuntimeError, match="something went wrong"):
                _run_orchestrator(MagicMock(), mock_db, "t", "g")

        mock_db.update_execution_status.assert_called_once_with("exec-999", "failed")


# ---------------------------------------------------------------------------
# _notify_task_failure
# ---------------------------------------------------------------------------


class TestNotifyTaskFailure:
    def test_posts_error_to_slack_thread(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        mock_slack = MagicMock()

        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token")

        mock_slack.post_error.assert_called_once()
        args = mock_slack.post_error.call_args
        assert args[0][0] == "C123"
        assert args[0][1] == "111.222"

    def test_does_nothing_when_channel_missing(self, monkeypatch):
        monkeypatch.delenv("SLACK_CHANNEL", raising=False)
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        mock_slack = MagicMock()

        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token")

        mock_slack.post_error.assert_not_called()

    def test_silently_ignores_slack_error(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        mock_slack = MagicMock()
        mock_slack.post_error.side_effect = Exception("Slack down")

        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token")

    def test_cloudflare_exception_sends_retry_message(self, monkeypatch):
        from storage.notion_client import NotionCloudflareBlockError

        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-cf-1")
        mock_slack = MagicMock()

        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            exc = NotionCloudflareBlockError("blocked", cf_ray="r-1")
            _notify_task_failure("slack-token", exc)

        mock_slack.post_error.assert_called_once()
        message = mock_slack.post_error.call_args[0][2]
        assert "Notion 前段（Cloudflare）" in message
        assert "再投入" in message
        assert "exec-cf-1" in message
        assert "Claude OAuth" not in message

    def test_generic_exception_sends_unknown_error_message(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-gen-1")
        mock_slack = MagicMock()

        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token", RuntimeError("boom"))

        mock_slack.post_error.assert_called_once()
        message = mock_slack.post_error.call_args[0][2]
        assert "RuntimeError" in message
        assert "exec-gen-1" in message
        assert "Claude OAuth" not in message
        assert "Cloudflare" not in message

    def test_codex_rate_limit_error_sends_rate_limit_message(self, monkeypatch):
        """Codex CLI (CalledProcessError) の stderr 429 判定は従来どおり動く"""
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-rl-1")
        mock_slack = MagicMock()

        exc = subprocess.CalledProcessError(1, ["codex", "exec"], stderr="Error: rate limit exceeded")
        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token", exc)

        message = mock_slack.post_error.call_args[0][2]
        assert "レート制限" in message
        assert "再投入" in message
        assert "exec-rl-1" in message

    def test_claude_rate_limit_error_sends_rate_limit_message(self, monkeypatch):
        """20260706 Agent SDK 移行: Claude の rate limit は ClaudeInvocationError.rate_limited で分岐"""
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-rl-2")
        mock_slack = MagicMock()

        from orchestrator import ClaudeInvocationError

        exc = ClaudeInvocationError("claude agent sdk call failed after retries", rate_limited=True)
        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token", exc)

        message = mock_slack.post_error.call_args[0][2]
        assert "レート制限" in message
        assert "再投入" in message
        assert "exec-rl-2" in message

    def test_claude_sdk_failure_sends_claude_message(self, monkeypatch):
        """20260706 Agent SDK 移行: Claude 呼び出し失敗は ClaudeInvocationError で届く"""
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-cli-1")
        mock_slack = MagicMock()

        from orchestrator import ClaudeInvocationError

        exc = ClaudeInvocationError(
            "claude agent sdk call failed after retries", exit_code=1, stderr="some other error"
        )
        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token", exc)

        message = mock_slack.post_error.call_args[0][2]
        assert "Claude Agent SDK" in message
        assert "exec-cli-1" in message
        assert "レート制限" not in message

    def test_codex_cli_failure_sends_codex_message(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-codex-1")
        mock_slack = MagicMock()

        exc = subprocess.CalledProcessError(1, ["codex", "exec", "--model", "gpt-5.5"], stderr="exit 1")
        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token", exc)

        message = mock_slack.post_error.call_args[0][2]
        assert "Codex CLI" in message
        assert "exec-codex-1" in message

    def test_research_failed_sends_research_message(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-research-1")
        mock_slack = MagicMock()

        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token", RuntimeError("All research steps failed"))

        message = mock_slack.post_error.call_args[0][2]
        assert "リサーチ" in message
        assert "exec-research-1" in message

    def test_http_error_sends_api_error_message(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-http-1")
        mock_slack = MagicMock()

        mock_response = MagicMock()
        mock_response.status_code = 400
        exc = requests.HTTPError(response=mock_response)
        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token", exc)

        message = mock_slack.post_error.call_args[0][2]
        assert "HTTP 400" in message
        assert "exec-http-1" in message

    def test_file_not_found_sends_missing_command_message(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL", "C123")
        monkeypatch.setenv("SLACK_THREAD_TS", "111.222")
        monkeypatch.setenv("EXECUTION_ID", "exec-fnf-1")
        mock_slack = MagicMock()

        exc = FileNotFoundError(2, "No such file or directory", "codex")
        with patch("main.SlackClient", return_value=mock_slack):
            from main import _notify_task_failure

            _notify_task_failure("slack-token", exc)

        message = mock_slack.post_error.call_args[0][2]
        assert "codex" in message
        assert "exec-fnf-1" in message


# ---------------------------------------------------------------------------
# main() — TASK_TYPE ルーティング
# ---------------------------------------------------------------------------


class TestMain:
    _COMMON_ENV = {
        "SLACK_BOT_TOKEN_SECRET_ARN": "arn:slack",
        "NOTION_TOKEN_SECRET_ARN": "arn:notion",
        "GITHUB_TOKEN_SECRET_ARN": "arn:github",
        "CLAUDE_OAUTH_SECRET_ARN": "arn:claude",
        "CODEX_AUTH_SECRET_ARN": "arn:codex",
        "DYNAMODB_TABLE_PREFIX": "catch-expander",
    }

    def _set_common_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in self._COMMON_ENV.items():
            monkeypatch.setenv(k, v)

    def test_routes_to_feedback_when_task_type_is_feedback(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.setenv("TASK_TYPE", "feedback")

        with (
            patch("main._get_secret", return_value="secret"),
            patch("main._setup_claude_credentials"),
            patch("main._writeback_claude_credentials"),
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_feedback") as mock_feedback,
            patch("main._run_orchestrator") as mock_orch,
        ):
            from main import main

            main()

        mock_feedback.assert_called_once()
        mock_orch.assert_not_called()

    def test_feedback_does_not_setup_codex_credentials(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.setenv("TASK_TYPE", "feedback")

        with (
            patch("main._get_secret", return_value="secret"),
            patch("main._setup_claude_credentials"),
            patch("main._writeback_claude_credentials"),
            patch("main._setup_codex_credentials") as mock_codex_setup,
            patch("main._writeback_codex_credentials") as mock_codex_wb,
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_feedback"),
        ):
            from main import main

            main()

        mock_codex_setup.assert_not_called()
        mock_codex_wb.assert_not_called()

    def test_feedback_fetches_four_secrets(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.setenv("TASK_TYPE", "feedback")

        with (
            patch("main._get_secret", return_value="secret") as mock_get,
            patch("main._setup_claude_credentials"),
            patch("main._writeback_claude_credentials"),
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_feedback"),
        ):
            from main import main

            main()

        called_arns = {c.args[0] for c in mock_get.call_args_list}
        assert called_arns == {"arn:slack", "arn:notion", "arn:github", "arn:claude"}
        assert "arn:codex" not in called_arns

    def test_routes_to_orchestrator_when_no_task_type(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.delenv("TASK_TYPE", raising=False)

        with (
            patch("main._get_secret", return_value="secret"),
            patch("main._setup_claude_credentials"),
            patch("main._writeback_claude_credentials"),
            patch("main._setup_codex_credentials"),
            patch("main._writeback_codex_credentials"),
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_feedback") as mock_feedback,
            patch("main._run_orchestrator") as mock_orch,
        ):
            from main import main

            main()

        mock_orch.assert_called_once()
        mock_feedback.assert_not_called()

    def test_notifies_slack_and_reraises_on_failure(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.setenv("SLACK_CHANNEL", "C999")
        monkeypatch.setenv("SLACK_THREAD_TS", "000.001")
        monkeypatch.delenv("TASK_TYPE", raising=False)

        with (
            patch("main._get_secret", return_value="secret"),
            patch("main._setup_claude_credentials"),
            patch("main._writeback_claude_credentials"),
            patch("main._setup_codex_credentials"),
            patch("main._writeback_codex_credentials"),
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_orchestrator", side_effect=RuntimeError("boom")),
            patch("main._notify_task_failure") as mock_notify,
        ):
            from main import main

            with pytest.raises(RuntimeError, match="boom"):
                main()

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert call_args[0][0] == "secret"
        assert isinstance(call_args[0][1], RuntimeError)

    def test_fetches_all_five_secrets(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.delenv("TASK_TYPE", raising=False)

        with (
            patch("main._get_secret", return_value="secret") as mock_get,
            patch("main._setup_claude_credentials"),
            patch("main._writeback_claude_credentials"),
            patch("main._setup_codex_credentials"),
            patch("main._writeback_codex_credentials"),
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_orchestrator"),
        ):
            from main import main

            main()

        called_arns = {c.args[0] for c in mock_get.call_args_list}
        assert called_arns == {"arn:slack", "arn:notion", "arn:github", "arn:claude", "arn:codex"}

    def test_writeback_called_in_finally_on_success(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.delenv("TASK_TYPE", raising=False)

        with (
            patch("main._get_secret", return_value="secret"),
            patch("main._setup_claude_credentials", return_value="claude-hash"),
            patch("main._writeback_claude_credentials") as mock_claude_wb,
            patch("main._setup_codex_credentials", return_value="codex-hash"),
            patch("main._writeback_codex_credentials") as mock_codex_wb,
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_orchestrator"),
        ):
            from main import main

            main()

        mock_claude_wb.assert_called_once_with("arn:claude", "claude-hash")
        mock_codex_wb.assert_called_once_with("arn:codex", "codex-hash")

    def test_writeback_called_in_finally_on_failure(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.delenv("TASK_TYPE", raising=False)

        with (
            patch("main._get_secret", return_value="secret"),
            patch("main._setup_claude_credentials", return_value="claude-hash"),
            patch("main._writeback_claude_credentials") as mock_claude_wb,
            patch("main._setup_codex_credentials", return_value="codex-hash"),
            patch("main._writeback_codex_credentials") as mock_codex_wb,
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_orchestrator", side_effect=RuntimeError("boom")),
            patch("main._notify_task_failure"),
        ):
            from main import main

            with pytest.raises(RuntimeError):
                main()

        mock_claude_wb.assert_called_once_with("arn:claude", "claude-hash")
        mock_codex_wb.assert_called_once_with("arn:codex", "codex-hash")


# ---------------------------------------------------------------------------
# 実行診断役の自動起動（.steering/20261004-execution-diagnostician、AC-20〜AC-23）
# ---------------------------------------------------------------------------

_DIAG_EXECUTION_ID = "exec-20261004120000-abcdef12"
_DIAG_CHANNEL = "C999"
_DIAG_THREAD_TS = "000.001"
_DIAG_MENTIONS = "<@U0123ABCDE> <!channel> @here"


def _diagnosis_run(status: str = "completed", stop_detail: str = "submitted", **overrides):
    from tests.unit.agent.diagnosis.factories import make_run

    return make_run(status=status, stop_detail=stop_detail, **overrides)


def _run_failing_main(
    monkeypatch,
    *,
    flag: str | None,
    exc: Exception | None = None,
    task_type: str | None = None,
    execution_id: str | None = _DIAG_EXECUTION_ID,
    diagnose_result=None,
    diagnose_error: BaseException | None = None,
    progress_error: Exception | None = None,
    fail_secret_number: int | None = None,
    patch_diagnosis: bool = True,
):
    """main() を失敗させ、失敗通知・診断・2 通目の投稿・書き戻しの呼ばれ方を集める。"""
    import contextlib
    from types import SimpleNamespace

    for key, value in TestMain._COMMON_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SLACK_CHANNEL", _DIAG_CHANNEL)
    monkeypatch.setenv("SLACK_THREAD_TS", _DIAG_THREAD_TS)
    for name, value in (("EXECUTION_DIAGNOSIS", flag), ("EXECUTION_ID", execution_id), ("TASK_TYPE", task_type)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    exc = exc if exc is not None else RuntimeError("boom")
    order: list[str] = []
    mock_slack = MagicMock()
    mock_slack.post_error.side_effect = lambda *a, **k: order.append("post_error")

    def _post_progress(*_args, **_kwargs):
        order.append("post_progress")
        if progress_error is not None:
            raise progress_error

    def _diagnose(*_args, **_kwargs):
        order.append("diagnose")
        if diagnose_error is not None:
            raise diagnose_error
        return diagnose_result

    mock_slack.post_progress.side_effect = _post_progress
    mock_diagnose = MagicMock(side_effect=_diagnose)
    secret_calls = {"n": 0}

    def _secret(_arn):
        secret_calls["n"] += 1
        if fail_secret_number is not None and secret_calls["n"] == fail_secret_number:
            raise exc
        return "secret"

    raised: BaseException | None = None
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("main._get_secret", side_effect=_secret))
        stack.enter_context(patch("main._setup_claude_credentials", return_value="claude-hash"))
        claude_wb = stack.enter_context(patch("main._writeback_claude_credentials"))
        stack.enter_context(patch("main._setup_codex_credentials", return_value="codex-hash"))
        codex_wb = stack.enter_context(patch("main._writeback_codex_credentials"))
        stack.enter_context(patch("main.SlackClient", return_value=mock_slack))
        stack.enter_context(patch("main.DynamoDbClient"))
        stack.enter_context(patch("main._run_orchestrator", side_effect=exc))
        stack.enter_context(patch("main._run_feedback", side_effect=exc))
        if patch_diagnosis:
            stack.enter_context(patch("diagnosis.diagnose_execution", mock_diagnose))
        from main import main

        try:
            main()
        except Exception as e:  # noqa: BLE001
            raised = e

    return SimpleNamespace(
        exc=exc,
        raised=raised,
        order=order,
        slack=mock_slack,
        diagnose=mock_diagnose,
        claude_writeback=claude_wb,
        codex_writeback=codex_wb,
    )


def _expected_generic_failure_message(execution_id: str = _DIAG_EXECUTION_ID) -> str:
    """既存の失敗通知の文面（RuntimeError のとき）。診断役の追加で変わってはいけない。"""
    return (
        "タスクの処理中に予期しないエラーが発生しました（RuntimeError）。\n"
        "ログを確認しますのでお知らせください。\n"
        f"execution_id: `{execution_id}`"
    )


@pytest.fixture
def _diagnosis_no_network(monkeypatch):
    """AC-25: 自動起動のテストも、通信と認証情報を使えない状態で通す。

    ``tests/unit/agent/diagnosis/conftest.py`` の自動適用フィクスチャは、このファイルには効かないので、
    同じ縛りをここでかける（既存のテストには適用しない）。
    """
    import socket

    def _blocked(*_args, **_kwargs):
        raise AssertionError("network access is forbidden in diagnosis tests (AC-25)")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


class TestShouldRunDiagnosis:
    def test_returns_false_when_env_is_not_set(self, monkeypatch):
        monkeypatch.delenv("EXECUTION_DIAGNOSIS", raising=False)
        from main import _should_run_diagnosis

        assert _should_run_diagnosis() is False

    @pytest.mark.parametrize("value", ["false", "False", "", "0", "1", "yes", "on", " true"])
    def test_returns_false_when_env_is_not_true(self, monkeypatch, value):
        monkeypatch.setenv("EXECUTION_DIAGNOSIS", value)
        from main import _should_run_diagnosis

        assert _should_run_diagnosis() is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE"])
    def test_returns_true_when_env_is_true(self, monkeypatch, value):
        monkeypatch.setenv("EXECUTION_DIAGNOSIS", value)
        from main import _should_run_diagnosis

        assert _should_run_diagnosis() is True


@pytest.mark.usefixtures("_diagnosis_no_network")
class TestMainDiagnosisFlagOff:
    """AC-20: フラグが未設定または false のとき、診断役は呼ばれず、既存の挙動は変わらない。"""

    @pytest.mark.parametrize("flag", [None, "false"])
    def test_flag_off_does_not_call_diagnosis_and_keeps_failure_notification(self, monkeypatch, flag):
        result = _run_failing_main(monkeypatch, flag=flag)

        result.diagnose.assert_not_called()
        result.slack.post_error.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, _expected_generic_failure_message()
        )
        result.slack.post_progress.assert_not_called()
        assert result.order == ["post_error"]
        assert result.raised is result.exc
        result.claude_writeback.assert_called_once_with("arn:claude", "claude-hash")
        result.codex_writeback.assert_called_once_with("arn:codex", "codex-hash")

    @pytest.mark.parametrize("flag", [None, "false"])
    def test_flag_off_leaves_diagnosis_package_unloaded(self, monkeypatch, flag):
        """R22: オフのとき、診断役のコードは 1 行も実行されない（import もされない）。"""
        import sys

        with patch.dict(sys.modules):
            for name in [n for n in sys.modules if n == "diagnosis" or n.startswith("diagnosis.")]:
                del sys.modules[name]

            result = _run_failing_main(monkeypatch, flag=flag, patch_diagnosis=False)

            assert "diagnosis" not in sys.modules
        assert result.raised is result.exc
        assert result.order == ["post_error"]

    def test_flag_helper_exists_and_is_off_by_default(self, monkeypatch):
        monkeypatch.delenv("EXECUTION_DIAGNOSIS", raising=False)
        import main

        assert main._should_run_diagnosis() is False
        assert callable(main._maybe_post_diagnosis)


@pytest.mark.usefixtures("_diagnosis_no_network")
class TestMainDiagnosisFlagOn:
    """AC-21: フラグがオンのとき、失敗通知 → 診断 → 2 通目の順。例外と書き戻しはそのまま。"""

    def test_flag_on_notifies_first_then_diagnoses_then_posts_second_message(self, monkeypatch):
        run = _diagnosis_run()

        result = _run_failing_main(monkeypatch, flag="true", diagnose_result=run)

        assert result.order == ["post_error", "diagnose", "post_progress"]
        result.slack.post_error.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, _expected_generic_failure_message()
        )
        result.diagnose.assert_called_once_with(_DIAG_EXECUTION_ID, trigger="auto")

    def test_flag_on_posts_formatted_diagnosis_to_same_thread(self, monkeypatch):
        from diagnosis.notify import format_slack_message

        run = _diagnosis_run()

        result = _run_failing_main(monkeypatch, flag="true", diagnose_result=run)

        result.slack.post_progress.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, format_slack_message(run)
        )
        assert "レート制限" in result.slack.post_progress.call_args[0][2]

    def test_flag_on_neutralizes_mentions_in_posted_message(self, monkeypatch):
        run = _diagnosis_run(summary=f"要約 {_DIAG_MENTIONS}", action_note=f"対処 {_DIAG_MENTIONS}")

        result = _run_failing_main(monkeypatch, flag="true", diagnose_result=run)

        text = result.slack.post_progress.call_args[0][2]
        for pattern in ("<@", "<!", "@here"):
            assert pattern not in text

    def test_flag_on_reraises_original_exception_and_writes_back_credentials(self, monkeypatch):
        result = _run_failing_main(monkeypatch, flag="true", diagnose_result=_diagnosis_run())

        assert result.raised is result.exc
        result.claude_writeback.assert_called_once_with("arn:claude", "claude-hash")
        result.codex_writeback.assert_called_once_with("arn:codex", "codex-hash")

    def test_flag_on_is_case_insensitive(self, monkeypatch):
        result = _run_failing_main(monkeypatch, flag="TRUE", diagnose_result=_diagnosis_run())

        result.diagnose.assert_called_once()

    def test_flag_on_runs_diagnosis_for_claude_failure_that_is_not_rate_limit(self, monkeypatch):
        from orchestrator import ClaudeInvocationError

        exc = ClaudeInvocationError("claude agent sdk call failed after retries", exit_code=1, stderr="other")

        result = _run_failing_main(monkeypatch, flag="true", exc=exc, diagnose_result=_diagnosis_run())

        result.diagnose.assert_called_once_with(_DIAG_EXECUTION_ID, trigger="auto")
        assert result.raised is exc


@pytest.mark.usefixtures("_diagnosis_no_network")
class TestMainDiagnosisNotStarted:
    """AC-22: フラグがオンでも起動しない場合。既存の失敗通知は今までどおり。"""

    def test_claude_rate_limit_failure_does_not_start_diagnosis(self, monkeypatch):
        from orchestrator import ClaudeInvocationError

        exc = ClaudeInvocationError("claude agent sdk call failed after retries", rate_limited=True)

        result = _run_failing_main(monkeypatch, flag="true", exc=exc)

        result.diagnose.assert_not_called()
        result.slack.post_error.assert_called_once()
        assert "レート制限" in result.slack.post_error.call_args[0][2]
        result.slack.post_progress.assert_not_called()
        assert result.raised is exc

    def test_feedback_failure_does_not_start_diagnosis(self, monkeypatch):
        result = _run_failing_main(monkeypatch, flag="true", task_type="feedback")

        result.diagnose.assert_not_called()
        result.slack.post_error.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, _expected_generic_failure_message()
        )
        result.slack.post_progress.assert_not_called()
        assert result.raised is result.exc

    def test_missing_execution_id_does_not_start_diagnosis(self, monkeypatch):
        result = _run_failing_main(monkeypatch, flag="true", execution_id=None)

        result.diagnose.assert_not_called()
        result.slack.post_error.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, _expected_generic_failure_message("<unknown>")
        )
        result.slack.post_progress.assert_not_called()
        assert result.raised is result.exc

    def test_failure_before_credentials_are_ready_does_not_start_diagnosis(self, monkeypatch):
        # 2 つ目のシークレット（Notion）の取得で失敗する。Claude の認証情報はまだ置かれていない
        result = _run_failing_main(monkeypatch, flag="true", fail_secret_number=2)

        result.diagnose.assert_not_called()
        result.slack.post_error.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, _expected_generic_failure_message()
        )
        result.slack.post_progress.assert_not_called()
        result.claude_writeback.assert_not_called()
        assert result.raised is result.exc

    def test_maybe_post_diagnosis_with_credentials_not_ready_does_nothing(self, monkeypatch):
        monkeypatch.setenv("EXECUTION_DIAGNOSIS", "true")
        monkeypatch.setenv("EXECUTION_ID", _DIAG_EXECUTION_ID)
        monkeypatch.setenv("SLACK_CHANNEL", _DIAG_CHANNEL)
        monkeypatch.setenv("SLACK_THREAD_TS", _DIAG_THREAD_TS)
        monkeypatch.delenv("TASK_TYPE", raising=False)
        mock_slack = MagicMock()

        with (
            patch("main.SlackClient", return_value=mock_slack),
            patch("diagnosis.diagnose_execution") as mock_diagnose,
        ):
            from main import _maybe_post_diagnosis

            _maybe_post_diagnosis("slack-token", RuntimeError("boom"), credentials_ready=False)

        mock_diagnose.assert_not_called()
        mock_slack.post_progress.assert_not_called()

    @pytest.mark.parametrize("missing", ["SLACK_CHANNEL", "SLACK_THREAD_TS"])
    def test_maybe_post_diagnosis_without_slack_destination_does_nothing(self, monkeypatch, missing):
        monkeypatch.setenv("EXECUTION_DIAGNOSIS", "true")
        monkeypatch.setenv("EXECUTION_ID", _DIAG_EXECUTION_ID)
        monkeypatch.setenv("SLACK_CHANNEL", _DIAG_CHANNEL)
        monkeypatch.setenv("SLACK_THREAD_TS", _DIAG_THREAD_TS)
        monkeypatch.delenv("TASK_TYPE", raising=False)
        monkeypatch.delenv(missing)
        mock_slack = MagicMock()

        with (
            patch("main.SlackClient", return_value=mock_slack),
            patch("diagnosis.diagnose_execution") as mock_diagnose,
        ):
            from main import _maybe_post_diagnosis

            _maybe_post_diagnosis("slack-token", RuntimeError("boom"), credentials_ready=True)

        mock_diagnose.assert_not_called()
        mock_slack.post_progress.assert_not_called()


@pytest.mark.usefixtures("_diagnosis_no_network")
class TestMainDiagnosisFailureIsolated:
    """AC-23: 診断役や 2 通目の投稿が失敗しても、元の例外・失敗通知・書き戻しに影響しない。"""

    def _assert_existing_behaviour_unchanged(self, result) -> None:
        assert result.raised is result.exc
        result.slack.post_error.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, _expected_generic_failure_message()
        )
        assert result.order[0] == "post_error"
        result.claude_writeback.assert_called_once_with("arn:claude", "claude-hash")
        result.codex_writeback.assert_called_once_with("arn:codex", "codex-hash")

    @pytest.mark.parametrize(
        ("status", "stop_detail"),
        [("failed", "sdk_error"), ("incomplete", "max_turns"), ("incomplete", "timeout")],
    )
    def test_diagnosis_not_completed_does_not_post_and_changes_nothing_else(self, monkeypatch, status, stop_detail):
        run = _diagnosis_run(status=status, stop_detail=stop_detail)

        result = _run_failing_main(monkeypatch, flag="true", diagnose_result=run)

        result.diagnose.assert_called_once()
        result.slack.post_progress.assert_not_called()
        self._assert_existing_behaviour_unchanged(result)

    def test_diagnosis_raising_changes_nothing_else(self, monkeypatch):
        result = _run_failing_main(monkeypatch, flag="true", diagnose_error=RuntimeError("diagnosis exploded"))

        result.diagnose.assert_called_once()
        result.slack.post_progress.assert_not_called()
        self._assert_existing_behaviour_unchanged(result)

    def test_second_post_raising_changes_nothing_else(self, monkeypatch):
        result = _run_failing_main(
            monkeypatch,
            flag="true",
            diagnose_result=_diagnosis_run(),
            progress_error=RuntimeError("Slack down"),
        )

        assert result.order == ["post_error", "diagnose", "post_progress"]
        self._assert_existing_behaviour_unchanged(result)

    def test_maybe_post_diagnosis_swallows_diagnosis_exception(self, monkeypatch):
        monkeypatch.setenv("EXECUTION_DIAGNOSIS", "true")
        monkeypatch.setenv("EXECUTION_ID", _DIAG_EXECUTION_ID)
        monkeypatch.setenv("SLACK_CHANNEL", _DIAG_CHANNEL)
        monkeypatch.setenv("SLACK_THREAD_TS", _DIAG_THREAD_TS)
        monkeypatch.delenv("TASK_TYPE", raising=False)

        with (
            patch("main.SlackClient"),
            patch("diagnosis.diagnose_execution", side_effect=RuntimeError("diagnosis exploded")) as mock_diagnose,
        ):
            from main import _maybe_post_diagnosis

            _maybe_post_diagnosis("slack-token", RuntimeError("boom"), credentials_ready=True)

        mock_diagnose.assert_called_once_with(_DIAG_EXECUTION_ID, trigger="auto")


@pytest.mark.usefixtures("_diagnosis_no_network")
class TestMainDiagnosisEndToEnd:
    """AC-21: main.py から本物の ``diagnose_execution`` までを通す（差し替えるのは SDK・AWS・Slack だけ）。

    ほかの ``TestMainDiagnosis*`` は ``diagnose_execution`` を丸ごと偽物にしているので、main.py の
    呼び方（引数）と、本物の関数の受け方が食い違っていても気づけない。ここで 1 本、つないで確かめる。
    """

    def _run(self, monkeypatch, script, *, scenario=None):
        from tests.unit.agent.diagnosis import fakes, scenarios
        from tests.unit.agent.diagnosis.sdk_fake import FakeSdk

        scenario = scenario or scenarios.rate_limited()
        bundle = fakes.build_reader(scenario)
        monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", fakes.TABLE_PREFIX)
        monkeypatch.setenv("EVENTS_TABLE", fakes.EVENTS_TABLE)
        monkeypatch.setenv("PROMPTS_BUCKET", fakes.PROMPTS_BUCKET)
        monkeypatch.delenv("DIAGNOSIS_MODEL", raising=False)
        sdk = FakeSdk(script)
        with (
            patch("diagnosis.runner.boto3") as boto3_mock,
            patch("diagnosis.runner._invoke_sdk", sdk),
            patch("diagnosis.runner._EventEmitter") as emitter_class,
        ):
            boto3_mock.resource.return_value = bundle.dynamodb
            boto3_mock.client.return_value = bundle.s3
            result = _run_failing_main(
                monkeypatch, flag="true", execution_id=scenario.execution_id, patch_diagnosis=False
            )
        return result, scenario, bundle, sdk, emitter_class

    def test_real_diagnosis_runs_once_and_posts_its_result_after_failure_notification(self, monkeypatch):
        async def script(call) -> None:
            events = (await call("list_events", {"event_type": "rate_limit_hit"})).data["events"]
            await call(
                "submit_diagnosis",
                {
                    "failed_stage": "researching",
                    "cause_category": "rate_limit",
                    "cause_nature": "transient",
                    "confidence": "high",
                    "evidence": [{"id": event["evidence_id"], "note": "429"} for event in events],
                    "action_type": "retry_later",
                    "action_note": f"しばらく待つ {_DIAG_MENTIONS}",
                    "summary": "レート制限で失敗しました",
                },
            )

        result, scenario, bundle, sdk, emitter_class = self._run(monkeypatch, script)

        assert result.raised is result.exc
        assert result.order == ["post_error", "post_progress"]
        result.slack.post_error.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, _expected_generic_failure_message(scenario.execution_id)
        )
        assert sdk.invocation_count == 1
        channel, thread_ts, text = result.slack.post_progress.call_args[0]
        assert (channel, thread_ts) == (_DIAG_CHANNEL, _DIAG_THREAD_TS)
        lines = text.split("\n")
        assert "失敗した段階: 調査中" in lines
        assert "原因: レート制限（確からしさ: 高）" in lines
        assert "根拠: 3 件 ／ 調べた記録: イベント → 提出" in lines
        assert lines[-1] == f"execution_id: `{scenario.execution_id}`"
        # 修正仕様 M2: モデルが書いた自由記述には「未検証」の旨が付く
        summary_lines = [line for line in lines if "レート制限で失敗しました" in line]
        assert len(summary_lines) == 1
        assert "未検証" in summary_lines[0]
        for pattern in ("<@", "<!", "@here"):
            assert pattern not in text
        # 自動起動のイベントは、trigger=auto・status_at_emit=failed で 1 件
        emitter_class.assert_called_once_with(scenario.execution_id)
        emit = emitter_class.return_value.emit
        emit.assert_called_once()
        assert emit.call_args.args[0] == "diagnosis_completed"
        assert emit.call_args.args[1]["trigger"] == "auto"
        assert emit.call_args.args[1]["status"] == "completed"
        assert emit.call_args.kwargs == {"status_at_emit": "failed"}
        # 記録は読み取りだけ。Slack の識別子は、記録にあっても投稿文に出ない
        assert {op.name for op in bundle.all_operations()} <= fakes_read_operations()
        result.claude_writeback.assert_called_once_with("arn:claude", "claude-hash")
        result.codex_writeback.assert_called_once_with("arn:codex", "codex-hash")

    def test_real_diagnosis_without_submission_does_not_post_and_changes_nothing_else(self, monkeypatch):
        async def script(call) -> None:
            await call("get_execution_overview")

        result, scenario, _, sdk, emitter_class = self._run(monkeypatch, script)

        assert result.raised is result.exc
        assert result.order == ["post_error"]
        result.slack.post_progress.assert_not_called()
        assert sdk.invocation_count == 1
        assert emitter_class.return_value.emit.call_args.args[1]["status"] == "incomplete"
        result.claude_writeback.assert_called_once_with("arn:claude", "claude-hash")

    def test_real_diagnosis_with_tool_isolation_failure_does_not_post_and_changes_nothing_else(self, monkeypatch):
        """修正仕様 M3: 自作の 5 個以外のツールが見えたら中断する。2 通目は投稿しない。"""
        from tests.unit.agent.diagnosis import fakes, scenarios
        from tests.unit.agent.diagnosis.sdk_fake import FakeSdk, full_name

        async def script(call) -> None:
            await call("get_execution_overview")

        scenario = scenarios.rate_limited()
        bundle = fakes.build_reader(scenario)
        monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", fakes.TABLE_PREFIX)
        sdk = FakeSdk(script, init_tools=[full_name("list_events"), "Bash"])
        with (
            patch("diagnosis.runner.boto3") as boto3_mock,
            patch("diagnosis.runner._invoke_sdk", sdk),
            patch("diagnosis.runner._EventEmitter") as emitter_class,
        ):
            boto3_mock.resource.return_value = bundle.dynamodb
            boto3_mock.client.return_value = bundle.s3
            result = _run_failing_main(
                monkeypatch, flag="true", execution_id=scenario.execution_id, patch_diagnosis=False
            )

        assert result.raised is result.exc
        assert result.order == ["post_error"]
        result.slack.post_progress.assert_not_called()
        assert sdk.script_started is False
        assert bundle.all_operations() == []
        payload = emitter_class.return_value.emit.call_args.args[1]
        assert (payload["status"], payload["stop_detail"]) == ("failed", "tool_isolation_failed")
        result.claude_writeback.assert_called_once_with("arn:claude", "claude-hash")

    def test_real_diagnosis_with_malformed_execution_id_does_not_post_or_touch_records(self, monkeypatch):
        from tests.unit.agent.diagnosis import fakes, scenarios
        from tests.unit.agent.diagnosis.sdk_fake import FakeSdk

        bundle = fakes.build_reader(scenarios.rate_limited())
        sdk = FakeSdk()
        with (
            patch("diagnosis.runner.boto3") as boto3_mock,
            patch("diagnosis.runner._invoke_sdk", sdk),
            patch("diagnosis.runner._EventEmitter") as emitter_class,
        ):
            result = _run_failing_main(
                monkeypatch, flag="true", execution_id="exec-123/../other", patch_diagnosis=False
            )

        assert result.raised is result.exc
        assert result.order == ["post_error"]
        boto3_mock.resource.assert_not_called()
        emitter_class.assert_not_called()
        assert sdk.invocation_count == 0
        assert bundle.all_operations() == []


def fakes_read_operations() -> frozenset:
    from tests.unit.agent.diagnosis import fakes

    return fakes.READ_OPERATIONS


@pytest.mark.usefixtures("_diagnosis_no_network")
class TestMainDiagnosisFailureIsolatedMore:
    """AC-23 の補足: 投稿文の組み立て・Slack クライアントの生成が失敗しても、既存の挙動は変わらない。"""

    def test_message_formatting_failure_changes_nothing_else(self, monkeypatch, capsys):
        with patch("diagnosis.notify.format_slack_message", side_effect=RuntimeError("format exploded")):
            result = _run_failing_main(monkeypatch, flag="true", diagnose_result=_diagnosis_run())

        assert result.raised is result.exc
        assert result.order == ["post_error", "diagnose"]
        result.slack.post_error.assert_called_once_with(
            _DIAG_CHANNEL, _DIAG_THREAD_TS, _expected_generic_failure_message()
        )
        result.claude_writeback.assert_called_once_with("arn:claude", "claude-hash")
        result.codex_writeback.assert_called_once_with("arn:codex", "codex-hash")
        # main() がログの出力先を標準出力に設定するので、ログは標準出力で確かめる
        assert "WARNING Execution diagnosis failed (non-fatal)" in capsys.readouterr().out

    def test_diagnosis_result_of_unexpected_type_changes_nothing_else(self, monkeypatch):
        result = _run_failing_main(monkeypatch, flag="true", diagnose_result=None)

        assert result.raised is result.exc
        assert result.order == ["post_error", "diagnose"]
        result.slack.post_progress.assert_not_called()
        result.claude_writeback.assert_called_once_with("arn:claude", "claude-hash")
