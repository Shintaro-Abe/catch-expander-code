"""実行診断役の手動実行の入口。

使い方（src/agent/ を作業ディレクトリにして実行する）:
    python -m diagnosis <execution_id> [--handoff-out <path>] [--json]

手順と必要な環境変数・権限は `docs/execution-diagnostician.md` を参照。
pytest の既定実行では走らない（実際の LLM と AWS の記録を使うため）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any

from diagnosis import DiagnosisRun, diagnose_execution

EXIT_COMPLETED = 0
EXIT_NOT_COMPLETED = 1
EXIT_USAGE = 2


def _strip_control(text: str) -> str:
    """制御文字（C0・DEL・C1）を取り除く。改行とタブは残す。

    診断結果には、記録に由来する文字列やモデルが書いた文字列が入る。端末のエスケープシーケンスを
    そのまま出さないため。
    """
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")


def _strip_control_deep(value: Any) -> Any:
    if isinstance(value, str):
        return _strip_control(value)
    if isinstance(value, dict):
        return {_strip_control_deep(key): _strip_control_deep(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_strip_control_deep(item) for item in value]
    return value


def _to_dict(run: DiagnosisRun) -> dict:
    data = asdict(run)
    data["handoff"] = run.handoff.to_dict()
    return data


def _text_lines(run: DiagnosisRun) -> list[str]:
    diagnosis = run.diagnosis
    lines = [
        f"execution_id: {run.execution_id}",
        f"status: {run.status} ({run.stop_detail})",
        f"failed_stage: {diagnosis.failed_stage}",
        f"cause_category: {diagnosis.cause_category}",
        f"cause_nature: {diagnosis.cause_nature}",
        f"confidence: {diagnosis.confidence}",
        f"action: {diagnosis.action_type} {diagnosis.action_note}",
        f"summary: {diagnosis.summary}",
        f"exception_type: {diagnosis.exception_type}",
    ]
    lines.extend(
        f"code_location: {location.path}:{location.line} in {location.function}"
        for location in diagnosis.code_locations
    )
    lines.append(f"evidence: {len(diagnosis.evidence)} valid / {diagnosis.invalid_evidence_count} invalid")
    lines.extend(f"  {ref.id}: {ref.note}" for ref in diagnosis.evidence)
    lines.append(f"fixer_eligible: {run.handoff.fixer_eligible}")
    lines.append("trace:")
    lines.extend(
        f"  {call.seq}. {call.tool} {json.dumps(call.args, ensure_ascii=False)} -> {call.outcome} "
        f"(evidence={call.evidence_count}, {call.duration_ms}ms)"
        for call in run.trace
    )
    lines.append(f"num_turns: {run.num_turns} / duration_ms: {run.duration_ms}")
    lines.append(f"tokens: {run.usage} / total_cost_usd: {run.total_cost_usd}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m diagnosis", description="実行 ID を 1 つ渡して診断する")
    parser.add_argument("execution_id", help="診断する実行の ID（exec-<14 桁>-<16 進 8 桁>）")
    parser.add_argument(
        "--handoff-out",
        metavar="PATH",
        help="受け渡し用の診断結果（JSON）の書き出し先。既にファイルなどがある場合は、診断せずに終了する",
    )
    parser.add_argument("--json", action="store_true", help="結果を JSON で出力する")
    args = parser.parse_args(argv)

    # 古い受け渡しを修正役が拾わないよう、書き出し先に何かがあれば、診断を始める前に終わる。
    # lexists は、先の無いシンボリックリンクも「ある」と見る（リンクをたどって別の場所に書かない）
    if args.handoff_out and os.path.lexists(args.handoff_out):
        print(f"handoff target already exists, not diagnosing: {_strip_control(args.handoff_out)}", file=sys.stderr)
        return EXIT_USAGE

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    run = diagnose_execution(args.execution_id, trigger="manual")

    if args.json:
        print(json.dumps(_strip_control_deep(_to_dict(run)), ensure_ascii=False, indent=2))
    else:
        print(_strip_control("\n".join(_text_lines(run))))

    if args.handoff_out:
        if run.status == "completed":
            handoff = json.dumps(run.handoff.to_dict(), ensure_ascii=False, indent=2)
            # "x" で開く: 診断の間に書き出し先が作られていたら、上書きせずに失敗する
            with Path(args.handoff_out).open("x", encoding="utf-8") as file:
                file.write(handoff + "\n")
            print(f"handoff written: {_strip_control(args.handoff_out)}", file=sys.stderr)
        else:
            print("handoff not written: the diagnosis did not complete", file=sys.stderr)
    return EXIT_COMPLETED if run.status == "completed" else EXIT_NOT_COMPLETED


if __name__ == "__main__":
    sys.exit(main())
