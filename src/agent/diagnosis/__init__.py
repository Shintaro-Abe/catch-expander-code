"""実行診断役。実行 ID を 1 つ受け取り、その実行の記録を調べて診断結果を返す。

設計詳細: `.steering/20261004-execution-diagnostician/design.md`
"""

from diagnosis.models import DiagnosisLimits, DiagnosisRun
from diagnosis.runner import diagnose_execution

__all__ = ["DiagnosisLimits", "DiagnosisRun", "diagnose_execution"]
