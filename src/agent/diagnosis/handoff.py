"""受け渡し用の診断結果の組み立てと「修正役に渡せる」の判定。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §11

受け渡しに入る文字列は、決まった値・形を検証済みの実行 ID・コードが記録から取り出した
例外の型とコードの場所だけ。自由記述（要約・説明・根拠の note）は入れない。
"""

from __future__ import annotations

from diagnosis.models import HANDOFF_SCHEMA_VERSION, Diagnosis, DiagnosisHandoff

_CATEGORIES_NEVER_ELIGIBLE = ("unknown", "insufficient_records")


def is_fixer_eligible(d: Diagnosis) -> bool:
    """修正役に渡せるかを判定する。判定はコードが行い、モデルには決めさせない。

    原因・対処・確からしさの条件に加えて、例外の型を取ったイベントが、選ばれた場所の少なくとも
    1 件の由来であり、有効な根拠にも含まれていることを求める（例外・場所・根拠が同じイベントでつながる）。
    """
    return (
        d.cause_nature == "code_defect"
        and d.confidence == "high"
        and d.action_type == "fix_code"
        and d.cause_category not in _CATEGORIES_NEVER_ELIGIBLE
        and len(d.evidence) >= 1
        and len(d.code_locations) >= 1
        and d.exception_evidence_id is not None
        and d.exception_evidence_id in d.code_location_evidence_ids
        and any(ref.id == d.exception_evidence_id for ref in d.evidence)
    )


def build_handoff(execution_id: str, diagnosis: Diagnosis) -> DiagnosisHandoff:
    """診断結果から、固定項目だけを取り出す。"""
    return DiagnosisHandoff(
        schema_version=HANDOFF_SCHEMA_VERSION,
        execution_id=execution_id,
        failed_stage=diagnosis.failed_stage,
        cause_category=diagnosis.cause_category,
        cause_nature=diagnosis.cause_nature,
        confidence=diagnosis.confidence,
        exception_type=diagnosis.exception_type,
        code_locations=tuple(diagnosis.code_locations),
        action_type=diagnosis.action_type,
        valid_evidence_count=len(diagnosis.evidence),
        fixer_eligible=is_fixer_eligible(diagnosis),
    )
