"""修正役の手動実行の入口。

使い方（リポジトリの根を作業ディレクトリにして実行する）:
    PYTHONPATH=src/agent python -m src.fixer --handoff <file> --out-dir <dir> [--base <commit>] [--repo <path>]

手順と前提（本番の認証情報がない環境で動かすこと）は `docs/execution-fixer.md` を参照。
pytest の既定実行では走らない（実際の LLM を使うため）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.fixer.draft import strip_control
from src.fixer.models import FixRun
from src.fixer.runner import run_fix

EXIT_PROPOSED = 0
EXIT_NOT_PROPOSED = 1
EXIT_USAGE = 2  # argparse が、引数の誤りで使う終了コード


def _text_lines(run: FixRun, out_dir: str) -> list[str]:
    lines = [
        f"status: {run.status} ({run.stop_detail})",
        f"execution_id: {run.execution_id}",
        f"base_commit: {run.base_commit}",
    ]
    if run.branch is not None:
        lines.extend([f"branch: {run.branch}", f"commit: {run.commit}"])
    lines.extend(f"changed_file: {path}" for path in run.changed_files)
    lines.append("gates:")
    lines.extend(f"  {gate.gate}: {'passed' if gate.passed else 'FAILED'} {gate.detail}" for gate in run.gates)
    lines.append(f"tool_calls: {len(run.trace)} / num_turns: {run.num_turns} / duration_ms: {run.duration_ms}")
    lines.append(f"out_dir: {out_dir}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.fixer",
        description="受け渡し用の診断結果を 1 つ渡して、修正案を手元の作業用ブランチに作る（プッシュはしない）",
    )
    parser.add_argument("--handoff", required=True, metavar="FILE", help="受け渡し用の診断結果（JSON）のファイル")
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="結果・足取り・下書きの出力先。リポジトリの外で、無いか、空のディレクトリであること",
    )
    parser.add_argument("--base", default="HEAD", metavar="COMMIT", help="基準のコミット（既定は HEAD）")
    parser.add_argument("--repo", default=".", metavar="PATH", help="リポジトリの根（既定はカレントディレクトリ）")
    parser.add_argument(
        "--allow-unisolated",
        action="store_true",
        help=(
            "ホームに認証情報らしいもの（~/.aws・~/.ssh など）がある環境、検査に使う Python の環境に"
            "書き込める環境でも起動する（既定では断る）"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    run = run_fix(
        Path(args.handoff),
        repo=Path(args.repo),
        out_dir=Path(args.out_dir),
        base=args.base,
        allow_unisolated=args.allow_unisolated,
    )

    # 結果には、モデルが書いた文字列に由来するものが入りうる。端末のエスケープシーケンスをそのまま出さない
    print(strip_control("\n".join(_text_lines(run, args.out_dir))))
    if getattr(run, "repository_changed", False) is True:
        print(
            "WARNING: 元のリポジトリの .git/hooks または .git/config が、実行の間に変わりました。"
            "次の git の操作の前に、確かめてください。",
            file=sys.stderr,
        )
    return EXIT_PROPOSED if run.status == "proposed" else EXIT_NOT_PROPOSED


if __name__ == "__main__":
    sys.exit(main())
