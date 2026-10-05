"""診断結果の Slack 投稿文の組み立て。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §9.3

固定の見出しと、決まった値の日本語の対応表から組み立てる。自由記述（要約・対処の説明）は
モデルが書いたものなので、「未検証」の印を付け、マスク・URL の無効化・Slack 向けの無効化を
通してから入れる。根拠の note とツールの引数は入れない。
"""

from __future__ import annotations

import re
import unicodedata

from diagnosis.models import DiagnosisRun
from diagnosis.redaction import mask_secrets, neutralize_slack

_UNKNOWN_LABEL = "不明"

# 段階の表示は docs/glossary.md §4 の状態名に揃える
_FAILED_STAGE_LABELS = {
    "analyzing": "解析中",
    "planning": "計画中",
    "researching": "調査中",
    "generating": "生成中",
    "reviewing": "レビュー中",
    "storing": "格納中",
    "notifying": "通知中",
    "none": "該当なし（実行は失敗していない）",
    "unknown": _UNKNOWN_LABEL,
}
_CAUSE_CATEGORY_LABELS = {
    "rate_limit": "レート制限",
    "external_service_error": "外部サービスのエラー",
    "research_failed": "調査の失敗",
    "output_validation_failed": "出力の検証失敗",
    "review_not_passed": "レビュー不合格",
    "auth_or_environment": "認証・環境",
    "insufficient_records": "記録の不足",
    "unknown": _UNKNOWN_LABEL,
}
_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
_ACTION_TYPE_LABELS = {
    "retry_later": "時間を置いて再投入",
    "retry_now": "すぐ再投入",
    "review_config": "設定の見直し",
    "fix_code": "コードの修正",
    "no_action": "対処不要",
    "human_investigation": "人による調査",
}
_TOOL_LABELS = {
    "get_execution_overview": "概要",
    "list_steps": "段階",
    "list_events": "イベント",
    "get_prompt_record": "プロンプト記録",
    "submit_diagnosis": "提出",
}


# Slack が書式として解釈する記号を、見た目の近い全角の文字に置き換える（消さない。前後の語をつなげない）
_FORMATTING_SYMBOLS = str.maketrans(
    {"*": "＊", "_": "＿", "~": "～", "`": "｀", "&": "＆", "!": "！", "#": "＃"}
)
# Slack が自動でリンクにする書き方を崩す（元の大文字小文字は保つ）
_LINK_PATTERNS = (
    (re.compile(r"(?i)(www)\."), r"\1[.]"),
    (re.compile(r"(?i)(tel|mailto):"), r"\1[:]"),
)


def _free_text(text: str) -> str:
    """自由記述（モデルが書いた文）を 1 行にし、マスクと Slack 向けの無効化を通す。

    固定の行（見出し、決まった値の表示、execution_id）には使わない。
    """
    # 改行などの空白は 1 つの空白にし、書式・制御の文字（Unicode の Cf・Cc。不可視の文字、文字の向きの
    # 上書きなど）は取り除く。マスクより先に行う（不可視の文字で、トークンの形を崩されないようにする）
    text = " ".join(text.split())
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cc"))
    text = mask_secrets(text).replace("://", "[:]//")
    for pattern, replacement in _LINK_PATTERNS:
        text = pattern.sub(replacement, text)
    return neutralize_slack(text).translate(_FORMATTING_SYMBOLS)


def format_slack_message(run: DiagnosisRun) -> str:
    """診断結果を、失敗通知に続けて投稿する文面にする。"""
    diagnosis = run.diagnosis
    action = _ACTION_TYPE_LABELS.get(diagnosis.action_type, _UNKNOWN_LABEL)
    action_note = _free_text(diagnosis.action_note)
    if action_note:
        action = f"{action}（補足・未検証: {action_note}）"
    inspected = " → ".join(_TOOL_LABELS.get(call.tool, _UNKNOWN_LABEL) for call in run.trace)
    lines = [
        "🩺 実行の診断（自動・参考情報）",
        f"失敗した段階: {_FAILED_STAGE_LABELS.get(diagnosis.failed_stage, _UNKNOWN_LABEL)}",
        f"原因: {_CAUSE_CATEGORY_LABELS.get(diagnosis.cause_category, _UNKNOWN_LABEL)}"
        f"（確からしさ: {_CONFIDENCE_LABELS.get(diagnosis.confidence, _UNKNOWN_LABEL)}）",
        f"推奨する対処: {action}",
        f"要約（未検証）: {_free_text(diagnosis.summary)}",
        f"根拠: {len(diagnosis.evidence)} 件 ／ 調べた記録: {inspected}",
        f"execution_id: `{run.execution_id}`",
    ]
    return "\n".join(lines)
