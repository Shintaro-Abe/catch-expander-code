"""サブエージェントのプロンプト・出力を S3 に記録する best-effort recorder。

設計詳細: `.steering/20260505-prompt-output-recording/design.md`

使い方:
    from src.observability.prompt_recorder import PromptRecorder

    recorder = PromptRecorder(execution_id)
    recorder.record("researcher", step_id, prompt, raw_output)

設計上の不変条件:
- 書き込み失敗は logging.error で記録するのみ。例外は呼び出し元へ伝播させない
- PROMPTS_BUCKET 環境変数が未設定でも import エラーにならない (graceful skip)
- S3 キー形式: prompts/{execution_id}/{subagent}_{index}.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3

logger = logging.getLogger(__name__)


class PromptRecorder:
    """1 つの execution_id に紐づくサブエージェントの入出力を S3 に記録する recorder。

    Attributes:
        execution_id: 記録対象の execution_id。
    """

    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        self._bucket = os.environ.get("PROMPTS_BUCKET", "")
        self._s3 = boto3.client("s3") if self._bucket else None

    def record(
        self,
        subagent: str,
        index: str,
        prompt: str,
        output: str,
    ) -> None:
        """サブエージェントのプロンプトと出力を S3 に書き込む (best-effort)。

        Args:
            subagent: サブエージェント種別。
                "researcher" / "generator" / "reviewer_eval" / "reviewer_fix"
            index: レコードの識別子。
                researcher: step_id, generator: "0", reviewer_*: ループ番号文字列
            prompt: サブエージェントに渡したプロンプト全文。
            output: サブエージェントの出力全文（Claude CLI の stdout）。

        Returns:
            None。書き込み成功/失敗は呼出元には伝えない (best-effort)。
        """
        if not self._bucket or self._s3 is None:
            return

        key = f"prompts/{self.execution_id}/{subagent}_{index}.json"
        body: dict[str, Any] = {
            "subagent": subagent,
            "index": index,
            "prompt": prompt,
            "output": output,
            "recorded_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(body, ensure_ascii=False),
                ContentType="application/json",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Failed to record prompt for %s/%s_%s: %s",
                self.execution_id,
                subagent,
                index,
                e,
            )
