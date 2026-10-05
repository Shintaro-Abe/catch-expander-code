"""プルリクエスト本文の下書き。SDK には依存しない。

設計詳細: `.steering/20261004-execution-fixer/design.md` §12

下書きに入るのは、固定の文面、検証済みの固定項目、コードが出した値（差分の要約、検査の結果、足取り）。
モデルが書いた文字列は「修正役の説明」の節にだけ、未検証と明示し、囲みのコードブロックに入れる（見出しや
固定の文面を偽造できず、リンク・画像・メンション・HTML も効かない）。
"""

from __future__ import annotations

import re
import unicodedata

from diagnosis.redaction import mask_secrets

from src.fixer.gates import CREDENTIAL_HANDLING_FILES
from src.fixer.models import FixLimits, FixRun, FixToolCall, HandoffInput

MAX_EXPLANATION_CHARS = FixLimits().max_explanation_chars
MAX_TRACE_LINES = 200
MAX_CHANGE_LINES = 50

_NO_VALUE = "（なし）"
_NO_COMMIT = "コミットなし"
_REMOVED_CATEGORIES = ("Cc", "Cs", "Zl", "Zp")
# モデルの説明から取り除く文字の分類: 制御文字、書式文字（双方向の制御文字・見えない文字）、単独のサロゲート、
# 行・段落の区切り
_REMOVED_FROM_EXPLANATION = ("Cc", "Cf", "Cs", "Zl", "Zp")
_BACKTICK_RUN = re.compile(r"`+")
_MIN_FENCE_LENGTH = 3
_CREDENTIAL_NOTE = """\
**この変更は、認証情報を扱うファイルに触れています。** トークンの受け渡し・送信先・ログへの出力が
変わっていないかを、差分で必ず確かめてください。
"""

_OPENING = """\
## 取り込む前に人が行うこと

1. 変更の内容を確認する。
2. CLAUDE.md の作業規律どおり、steering を起こして承認を得る。
3. プッシュの後、Codex レビューを通す。
4. main への取り込みは人が行う。

この修正案は、自動の診断と自動の修正によるものです。自動の検査が確かめたのは、新しいテストがこの変更で
落ちる状態から通る状態になったこと、既存のテストの結果が変わらないことだけです。
**再現テストが、実際に起きた失敗と同じ原因を突いているかは確かめていません。**
**最後の確認は、このプルリクエストを読む人が行ってください。**
"""

_NOT_VERIFIED = """\
## 確かめていないこと

- 実機での確認は未実施
- 診断の場所が正しいかは未確認（手がかりとして使った）
- 再現テストが、実際に起きた失敗と同じ原因を突いているかは未確認
- 既存のテストに無い経路への影響は未確認
"""


def strip_control(text: str) -> str:
    """制御文字（C0・DEL・C1）、行・段落の区切りの文字（U+2028・U+2029）、単独のサロゲートを取り除く。

    改行とタブは残す。行の区切りを LF だけにして、引用の外に「行頭」を作れないようにする。
    単独のサロゲートは、UTF-8 にできず、ファイルに書けない。
    """
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in _REMOVED_CATEGORIES)


def clean_explanation(text: str, max_chars: int = MAX_EXPLANATION_CHARS) -> str:
    """モデルの説明を、結果に入れられる形にする: 見えない文字を除く → マスク → 切り詰め。

    除くのは、制御文字（改行は残す）、書式文字（双方向の制御文字・幅の無い文字）、単独のサロゲート、
    行・段落の区切りの文字。マスクを先にするのは、切り詰めでトークンが途中で切れて形に合わなくなるのを
    防ぐため（第 1 段階と同じ順）。
    """
    kept = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in _REMOVED_FROM_EXPLANATION)
    return mask_secrets(kept)[:max_chars]


def _fenced_explanation(explanation: str) -> list[str]:
    """説明を、囲みのコードブロックに、そのまま入れる。

    フェンスは、説明の中のどのバッククォートの連続よりも長くする（中からコードブロックを閉じられない）。
    """
    cleaned = clean_explanation(explanation).strip("\n")
    if not cleaned.strip():
        return [_NO_VALUE]
    longest = max((len(run) for run in _BACKTICK_RUN.findall(cleaned)), default=0)
    fence = "`" * max(_MIN_FENCE_LENGTH, longest + 1)
    return [fence, *cleaned.split("\n"), fence]


def _trace_line(call: FixToolCall) -> str:
    line = f"{call.seq}. {call.tool}"
    if call.path is not None:
        line += f" `{call.path}`"
    line += f" → {call.outcome}"
    if call.tests_passed is not None or call.tests_failed is not None:
        line += f"（成功 {call.tests_passed} 件、失敗 {call.tests_failed} 件）"
    return line


def render_pr_draft(run: FixRun, handoff: HandoffInput, diff_stat: list[tuple[str, int, int]]) -> str:
    """プルリクエスト本文の下書きを組み立てる。節の順序は固定。

    Args:
        run: 修正の結果（検査の結果、足取り、モデルの説明）。
        handoff: 検証を通った受け渡し（固定項目）。
        diff_stat: 変更したファイルごとの（パス, 足した行数, 消した行数）。コードが数えたもの。
    """
    lines = ["# 修正案の下書き", "", _OPENING]
    if any(path in CREDENTIAL_HANDLING_FILES for path in run.changed_files):
        lines.extend([_CREDENTIAL_NOTE])
    lines.extend(["## 診断", ""])
    lines.extend(
        [
            f"- 実行 ID: {handoff.execution_id}",
            f"- 失敗した段階: {handoff.failed_stage}",
            f"- 原因の分類: {handoff.cause_category}",
            f"- 例外の型: {handoff.exception_type or _NO_VALUE}",
            "- 手がかりの場所:",
        ]
    )
    lines.extend(f"  - `{place.path}` の {place.function}（{place.line} 行目の付近）" for place in run.locations)

    lines.extend(["", "## 変更点", ""])
    if run.commit is None or run.branch is None:
        lines.append(f"- {_NO_COMMIT}（合否の検査に通らなかったため、変更は破棄した）")
    else:
        lines.extend([f"- ブランチ: `{run.branch}`", f"- コミット: `{run.commit}`"])
    lines.append(f"- 基準のコミット: `{run.base_commit}`")
    lines.extend(f"- `{path}`: +{added} / -{removed}" for path, added, removed in diff_stat[:MAX_CHANGE_LINES])
    if len(diff_stat) > MAX_CHANGE_LINES:
        lines.append(f"- ほか {len(diff_stat) - MAX_CHANGE_LINES} 件")

    lines.extend(["", "## 検査の結果", "", "コードが実際に実行した結果。"])
    lines.extend(f"- {gate.gate}: {'合格' if gate.passed else '不合格'} — {gate.detail}" for gate in run.gates)

    lines.extend(["", "## 修正役の説明（モデルの説明・未検証）", "", f"- 修正の種類の申告: {run.patch_kind}", ""])
    lines.extend(_fenced_explanation(run.explanation))

    lines.extend(["", _NOT_VERIFIED, "## 足取り", ""])
    lines.extend(_trace_line(call) for call in run.trace[:MAX_TRACE_LINES])
    if len(run.trace) > MAX_TRACE_LINES:
        lines.append(f"ほか {len(run.trace) - MAX_TRACE_LINES} 件")
    # 端末や画面を乱す文字を、どの節にも残さない
    return strip_control("\n".join(lines) + "\n")
