"""src/observability/prompt_recorder.py の単体テスト。"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest


class TestPromptRecorderWithBucket:
    """PROMPTS_BUCKET が設定されている前提のテスト群。"""

    @patch("boto3.client")
    def test_record_puts_object_with_correct_key(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("PROMPTS_BUCKET", "test-prompts")
        from src.observability.prompt_recorder import PromptRecorder

        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3

        recorder = PromptRecorder("exec-test-001")
        recorder.record("researcher", "step-001", "prompt text", "output text")

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-prompts"
        assert call_kwargs["Key"] == "prompts/exec-test-001/researcher_step-001.json"
        assert call_kwargs["ContentType"] == "application/json"

    @patch("boto3.client")
    def test_record_body_has_required_fields(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("PROMPTS_BUCKET", "test-prompts")
        from src.observability.prompt_recorder import PromptRecorder

        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3

        recorder = PromptRecorder("exec-test-002")
        recorder.record("generator", "0", "my prompt", "my output")

        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["subagent"] == "generator"
        assert body["index"] == "0"
        assert body["prompt"] == "my prompt"
        assert body["output"] == "my output"
        assert body["recorded_at"].endswith("Z")

    @patch("boto3.client")
    def test_record_key_format_for_all_subagent_types(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("PROMPTS_BUCKET", "test-prompts")
        from src.observability.prompt_recorder import PromptRecorder

        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3

        recorder = PromptRecorder("exec-test-003")
        cases = [
            ("researcher", "step-abc", "prompts/exec-test-003/researcher_step-abc.json"),
            ("generator", "0", "prompts/exec-test-003/generator_0.json"),
            ("reviewer_eval", "0", "prompts/exec-test-003/reviewer_eval_0.json"),
            ("reviewer_fix", "1", "prompts/exec-test-003/reviewer_fix_1.json"),
        ]
        for subagent, index, expected_key in cases:
            mock_s3.reset_mock()
            recorder.record(subagent, index, "p", "o")
            actual_key = mock_s3.put_object.call_args.kwargs["Key"]
            assert actual_key == expected_key, f"{subagent}: expected {expected_key}, got {actual_key}"

    @patch("boto3.client")
    def test_s3_failure_does_not_raise(self, mock_boto3, monkeypatch, caplog):
        monkeypatch.setenv("PROMPTS_BUCKET", "test-prompts")
        from src.observability.prompt_recorder import PromptRecorder

        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = RuntimeError("S3 unavailable")
        mock_boto3.return_value = mock_s3

        recorder = PromptRecorder("exec-test-fail")
        with caplog.at_level(logging.ERROR):
            recorder.record("researcher", "step-001", "p", "o")

        assert any("Failed to record prompt" in r.message for r in caplog.records)


class TestPromptRecorderWithoutBucket:
    """PROMPTS_BUCKET が未設定の場合の graceful skip テスト群。"""

    def test_record_is_noop_when_bucket_unset(self, monkeypatch):
        monkeypatch.delenv("PROMPTS_BUCKET", raising=False)
        from src.observability.prompt_recorder import PromptRecorder

        recorder = PromptRecorder("exec-test-no-env")
        # boto3 を mock せずに呼んでも例外なく完了する
        recorder.record("researcher", "step-001", "p", "o")

    def test_s3_client_not_created_when_bucket_unset(self, monkeypatch):
        monkeypatch.delenv("PROMPTS_BUCKET", raising=False)
        with patch("boto3.client") as mock_boto3:
            from src.observability.prompt_recorder import PromptRecorder

            PromptRecorder("exec-test-no-boto")
            mock_boto3.assert_not_called()

    def test_execution_id_is_stored(self, monkeypatch):
        monkeypatch.delenv("PROMPTS_BUCKET", raising=False)
        from src.observability.prompt_recorder import PromptRecorder

        recorder = PromptRecorder("exec-test-id")
        assert recorder.execution_id == "exec-test-id"
