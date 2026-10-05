"""修正役。受け渡し用の診断結果を 1 つ受け取り、再現するテストと修正を作って、作業用ブランチに提案する。

設計詳細: `.steering/20261004-execution-fixer/design.md`

本番のイメージには入れない（`src/agent/` と `src/observability/` の外に置く）。
"""

from src.fixer.models import FixLimits, FixRun
from src.fixer.runner import run_fix

__all__ = ["FixLimits", "FixRun", "run_fix"]
