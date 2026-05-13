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


class TestPromptRecorderWithOutputFiles:
    """2026-05-13 改修: output_files 引数とキー分離のテスト群。

    `.steering/20260512-parse-claude-response-dict-contract/`
    """

    @patch("boto3.client")
    def test_record_stores_output_files(self, mock_boto3, monkeypatch):
        """output_files を渡したら S3 body に保存される (workspace モード対応)。"""
        monkeypatch.setenv("PROMPTS_BUCKET", "test-prompts")
        from src.observability.prompt_recorder import PromptRecorder

        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3

        recorder = PromptRecorder("exec-test-ws")
        recorder.record(
            "generator_text",
            "0",
            "prompt body",
            "Wrote: deliverable.json",
            output_files={"deliverable.json": '{"summary": "ok"}'},
        )

        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["subagent"] == "generator_text"
        assert body["output"] == "Wrote: deliverable.json"
        assert body["output_files"] == {"deliverable.json": '{"summary": "ok"}'}

    @patch("boto3.client")
    def test_record_omits_output_files_when_none(self, mock_boto3, monkeypatch):
        """output_files が None なら S3 body に含まれない (旧 record との後方互換)。"""
        monkeypatch.setenv("PROMPTS_BUCKET", "test-prompts")
        from src.observability.prompt_recorder import PromptRecorder

        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3

        recorder = PromptRecorder("exec-test-no-ws")
        recorder.record("researcher", "step-001", "prompt body", "stdout output")

        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert "output_files" not in body

    @patch("boto3.client")
    def test_record_separates_generator_text_and_code_keys(self, mock_boto3, monkeypatch):
        """text/code generator が別 S3 キーで分離される (派生 2 統合: 同キー上書きバグ解消)。"""
        monkeypatch.setenv("PROMPTS_BUCKET", "test-prompts")
        from src.observability.prompt_recorder import PromptRecorder

        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3

        recorder = PromptRecorder("exec-test-split")

        recorder.record("generator_text", "0", "p1", "out1")
        text_key = mock_s3.put_object.call_args.kwargs["Key"]

        mock_s3.reset_mock()
        recorder.record("generator_code", "iac_code", "p2", "out2")
        code_key = mock_s3.put_object.call_args.kwargs["Key"]

        # キーが完全に分離されている
        assert text_key == "prompts/exec-test-split/generator_text_0.json"
        assert code_key == "prompts/exec-test-split/generator_code_iac_code.json"
        assert text_key != code_key


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
