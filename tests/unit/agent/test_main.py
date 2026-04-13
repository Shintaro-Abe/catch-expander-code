from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _setup_claude_credentials
# ---------------------------------------------------------------------------


class TestSetupClaudeCredentials:
    def test_writes_credentials_to_dot_credentials_json(self, tmp_path):
        from main import _setup_claude_credentials

        with patch("main.Path") as mock_path_cls:
            # Path.home() → tmp_path
            mock_path_cls.home.return_value = tmp_path
            # Path(...) / ".claude" は実際の Path オブジェクトを返す
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            _setup_claude_credentials('{"token": "test-value"}')

        creds_file = tmp_path / ".claude" / ".credentials.json"
        assert creds_file.exists()
        assert creds_file.read_text() == '{"token": "test-value"}'

    def test_creates_claude_dir_if_not_exists(self, tmp_path):
        from main import _setup_claude_credentials

        claude_dir = tmp_path / ".claude"
        assert not claude_dir.exists()

        with patch("main.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = lambda *a, **kw: __import__("pathlib").Path(*a, **kw)

            _setup_claude_credentials("{}")

        assert claude_dir.exists()


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
# main() — TASK_TYPE ルーティング
# ---------------------------------------------------------------------------


class TestMain:
    _COMMON_ENV = {
        "SLACK_BOT_TOKEN_SECRET_ARN": "arn:slack",
        "NOTION_TOKEN_SECRET_ARN": "arn:notion",
        "GITHUB_TOKEN_SECRET_ARN": "arn:github",
        "CLAUDE_OAUTH_SECRET_ARN": "arn:claude",
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
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_feedback") as mock_feedback,
            patch("main._run_orchestrator") as mock_orch,
        ):
            from main import main

            main()

        mock_feedback.assert_called_once()
        mock_orch.assert_not_called()

    def test_routes_to_orchestrator_when_no_task_type(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.delenv("TASK_TYPE", raising=False)

        with (
            patch("main._get_secret", return_value="secret"),
            patch("main._setup_claude_credentials"),
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_feedback") as mock_feedback,
            patch("main._run_orchestrator") as mock_orch,
        ):
            from main import main

            main()

        mock_orch.assert_called_once()
        mock_feedback.assert_not_called()

    def test_fetches_all_four_secrets(self, monkeypatch):
        self._set_common_env(monkeypatch)
        monkeypatch.delenv("TASK_TYPE", raising=False)

        with (
            patch("main._get_secret", return_value="secret") as mock_get,
            patch("main._setup_claude_credentials"),
            patch("main.SlackClient"),
            patch("main.DynamoDbClient"),
            patch("main._run_orchestrator"),
        ):
            from main import main

            main()

        called_arns = {c.args[0] for c in mock_get.call_args_list}
        assert called_arns == {"arn:slack", "arn:notion", "arn:github", "arn:claude"}
