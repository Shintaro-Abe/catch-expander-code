"""マスク・識別子の除去・切り詰め・Slack 向けの無効化。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §6

順序は「識別子の除去 → マスク → 切り詰め」。マスクを先にするのは、切り詰めでトークンが
途中で切れて形に合わなくなるのを防ぐため。
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[MASKED]"

_DROPPED_KEYS = frozenset(
    {
        "user_id",
        "user_id_hash",
        "slack_channel",
        "slack_thread_ts",
        "channel_id",
        "thread_ts",
        "ttl",
        "gsi_pk",
    }
)

# マスクする形。どの式も、入力の長さに対してほぼ線形の時間で終わるように書く:
# - 長い並びの後ろに別の要素が続く式（JWT、URL の認証情報）は、並びの途中から試し直さないよう、
#   手前の文字を確かめ（否定の後読み）、量指定子を所有的（++ や {m,n}+）にするか、上限を付ける。
# - 末尾が「1 つの文字クラスの繰り返し」で終わる式は、失敗しても後戻りが起きない。
# (?P<keepN>…) の部分は、マスクせずに残す（何の値か、どこへの接続かは、診断の材料になる）。
_SECRET_PATTERN = re.compile(
    "|".join(
        (
            r"xox[a-z]-[A-Za-z0-9-]{10,}",  # Slack のトークン（xoxb・xoxp・xoxa など）
            r"xapp-[A-Za-z0-9-]{10,}",  # Slack のアプリレベルトークン
            r"(?:https?://)?hooks\.slack\.com/services/[A-Za-z0-9_/-]+",  # Slack の Webhook
            r"gh[pousr]_[A-Za-z0-9]{20,}",  # GitHub
            r"github_pat_[A-Za-z0-9_]{20,}",
            r"glpat-[A-Za-z0-9_-]{20,}",  # GitLab
            # sk- で始まるキー（sk-ant-…、sk-proj-… を含む）。語の途中の sk-（mask-secrets… など）は残す
            r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}",
            r"secret_[A-Za-z0-9]{20,}",  # Notion
            r"ntn_[A-Za-z0-9]{20,}",
            r"(?:AKIA|ASIA)[A-Z0-9]{16}",  # AWS のアクセスキー
            # AWS のシークレットキー（= か : の後ろの値）
            r"(?P<keep1>(?i:aws_secret_access_key)[ \t]{0,8}[=:][ \t]{0,8}[\"']?)[A-Za-z0-9/+=]{16,}",
            r"(?i:bearer)\s{1,16}[A-Za-z0-9._~+/=-]{16,}",
            r"(?P<keep2>(?i:authorization:[ \t]{0,8}basic)[ \t]{1,8})[A-Za-z0-9+/=]{8,}",
            # URL の中の認証情報（scheme://user:password@host の password）
            r"(?P<keep3>://[^\s/:@]{0,64}:)[^\s/@]{1,256}(?=@)",
            # JWT（eyJ で始まる 3 区切り）。3 つ目の区切りが切れていても、先頭の 2 つでマスクする
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}+\.[A-Za-z0-9_-]{2,}+(?:\.[A-Za-z0-9_-]*+)?",
            r"AIza[A-Za-z0-9_-]{30,}",  # Google の API キー
            # 秘密鍵の PEM ブロック。END が無ければ、文字列の終わりまでをマスクする
            r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|\Z)",
        )
    )
)
_KEPT_GROUPS = ("keep1", "keep2", "keep3")

_SLACK_NEUTRALIZE = str.maketrans({"<": "＜", ">": "＞", "@": "＠"})


def drop_identifiers(obj: Any) -> Any:
    """辞書を再帰的にたどり、Slack の識別子などのキーを落とす。"""
    if isinstance(obj, dict):
        return {key: drop_identifiers(value) for key, value in obj.items() if key not in _DROPPED_KEYS}
    if isinstance(obj, list):
        return [drop_identifiers(value) for value in obj]
    return obj


def mask_secrets(text: str) -> str:
    """トークン・キーの形をした部分を置き換える。"""
    return _SECRET_PATTERN.sub(_masked, text)


def _masked(match: re.Match[str]) -> str:
    for name in _KEPT_GROUPS:
        kept = match.group(name)
        if kept is not None:
            return kept + MASK
    return MASK


def _mask_strings(obj: Any) -> Any:
    if isinstance(obj, str):
        return mask_secrets(obj)
    if isinstance(obj, dict):
        # 辞書のキーの文字列もマスクする
        return {
            (mask_secrets(key) if isinstance(key, str) else key): _mask_strings(value) for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_strings(value) for value in obj]
    return obj


def redact(obj: Any) -> Any:
    """識別子を落としたあと、すべての文字列（辞書のキーを含む）をマスクする。"""
    return _mask_strings(drop_identifiers(obj))


def truncate_middle(text: str, head: int, tail: int) -> tuple[str, bool]:
    """先頭 head 字と末尾 tail 字を残して中を省く。省いたかどうかを併せて返す。"""
    if len(text) <= head + tail:
        return text, False
    omitted = len(text) - head - tail
    return f"{text[:head]}…[{omitted} 字省略]…{text[len(text) - tail :]}", True


def truncate_strings(obj: Any, max_chars: int) -> Any:
    """max_chars を超える文字列を、末尾を長めに残して切り詰める（スタックトレースは末尾が大事なため）。"""
    if isinstance(obj, str):
        head = max_chars * 3 // 8
        return truncate_middle(obj, head, max_chars - head)[0]
    if isinstance(obj, dict):
        return {key: truncate_strings(value, max_chars) for key, value in obj.items()}
    if isinstance(obj, list):
        return [truncate_strings(value, max_chars) for value in obj]
    return obj


def neutralize_slack(text: str) -> str:
    """メンションとリンクの記法が効かないよう、記号を全角に置き換える。"""
    return text.translate(_SLACK_NEUTRALIZE)
