"""テストデータの組み立て。

``diagnosis`` パッケージの import は関数の中で行う（実装が無くても、このファイルは import できる）。
"""

from __future__ import annotations

import atexit
import dataclasses
import json
import re
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

VALID_EXECUTION_ID = "exec-20261004120000-abcdef12"

FRAME_ID = re.compile(r"^FR-\d+$")
EVENT_ID = re.compile(r"^EV-\d+$")
STEP_ID = re.compile(r"^ST-\d+$")


def fake_tokens() -> dict[str, str]:
    """トークンの形をした文字列。断片の連結で作る（そのまま書くと ``.gitleaks.toml`` の検出に当たる）。"""
    return {
        "slack": "xo" + "xb-" + "1234567890-" + "abcdefghijkl",
        "github": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        "github_pat": "github" + "_pat_" + "11ABCDEFG0abcdefghijkl_mnop",
        "anthropic": "sk-" + "ant-" + "api03-abcdefghijklmnop",
        "notion_secret": "sec" + "ret_" + "abcdefghijklmnopqrstuvwx",
        "notion_ntn": "nt" + "n_" + "1234567890abcdefghijklmn",
        "aws_akia": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "aws_asia": "AS" + "IA" + "ABCDEFGHIJKLMNOP",
    }


def extra_secrets() -> dict[str, tuple[str, tuple[str, ...]]]:
    """修正仕様 L3 で足す、マスクの対象。``種別: (全体の文字列, マスク後に残ってはいけない部分)``。

    こちらも断片の連結で作る（そのまま書くと ``.gitleaks.toml`` の検出に当たる）。
    """
    xapp = "xa" + "pp-" + "1-A0" + "123456789-1234567890123-" + ("abcdef0" + "123456789") * 4
    hook_path = "T0123ABCD" + "/B0456EFGH/" + "abcdefghijklmnopqrstuvwx"
    hook = "https://hooks.sl" + "ack.com/services/" + hook_path
    jwt_parts = (
        "ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "ey" + "JzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    )
    jwt = ".".join(jwt_parts)
    google = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7"
    secrets = {
        "slack_app": (xapp, (xapp, xapp[10:40])),
        "slack_webhook": (hook, (hook, hook_path, hook_path[-24:])),
        "jwt": (jwt, (jwt, jwt_parts[1], jwt_parts[2])),
        "google_api_key": (google, (google, google[6:30])),
    }
    for label in ("RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY"):
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj" + label[:2] + "\n" + "Zm9vYmFyYmF6cXV4" * 4 + "=="
        pem = "-----BEGIN " + label + "-----\n" + body + "\n-----END " + label + "-----"
        must_not_remain = (body, "Zm9vYmFyYmF6cXV4", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj")
        secrets["pem_" + label.lower().replace(" ", "_")] = (pem, must_not_remain)
    return secrets


def more_secrets() -> dict[str, tuple[str, tuple[str, ...]]]:
    """再レビュー後の修正仕様 A5 で足す、マスクの対象。``種別: (文の中に置く文字列, マスク後に残ってはいけない部分)``。

    こちらも断片の連結で作る（そのまま書くと ``.gitleaks.toml`` の検出に当たる）。
    """
    token = "abcdefghij" + "klmnop.qrstuv-wx"
    signature = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    header = "ey" + "JhbGciOiJIUzI1NiJ9"
    alnum = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    basic = "dXNlcm5hbWU6" + "cGFzc3dvcmQxMjM0NTY="
    password = "S3cr3t" + "Passw0rdValue"
    aws_secret = "wJalrXUtnFEMI" + "K7MDENGbPxRfiCY" + "EXAMPLEKEY12"
    secrets: dict[str, tuple[str, tuple[str, ...]]] = {
        "bearer_lower": ("bea" + "rer " + token, (token,)),
        "bearer_upper": ("BEA" + "RER " + token, (token,)),
        "bearer_mixed": ("BeA" + "rEr  " + token, (token,)),
        "jwt_minimal_payload": (f"{header}.e30.{signature}", (signature, f"{header}.e30")),
        "sk_plain": ("s" + "k-" + alnum + "abcdefghijkl", (alnum,)),
        "sk_proj": ("s" + "k-" + "proj-" + alnum + "_" + alnum, (alnum,)),
        "sk_ant": ("s" + "k-" + "ant-" + "api03-" + alnum, (alnum,)),
        "glpat": ("gl" + "pat-" + "A1b2C3d4E5f6G7h8I9j0", ("A1b2C3d4E5f6G7h8I9j0",)),
        "basic_auth": ("Authorization: Bas" + "ic " + basic, (basic, basic[:20])),
        "url_credentials": ("postgres://admin:" + password + "@db.example.com:5432/app", (password,)),
        "url_credentials_https": ("https://deploy:" + password + "@git.example.com/repo.git", (password,)),
        "aws_secret_equals": ("aws_secret_" + "access_key = " + aws_secret, (aws_secret,)),
        "aws_secret_upper": ("AWS_SECRET_" + "ACCESS_KEY=" + aws_secret, (aws_secret,)),
        "aws_secret_colon": ("aws_secret_" + "access_key: " + aws_secret, (aws_secret,)),
    }
    for prefix in ("b", "p", "a", "c", "s", "r"):
        body = "1234567890-0" + "987654321-" + "abcdefghijklmnopqrstuvwx"
        secrets[f"slack_xox{prefix}"] = ("xo" + f"x{prefix}-" + body, (body,))
    for prefix in ("p", "o", "u", "s", "r"):
        body = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        secrets[f"github_gh{prefix}"] = ("gh" + f"{prefix}_" + body, (body,))
    pat = "11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz0" + "123456789"
    secrets["github_pat"] = ("github" + "_pat_" + pat, (pat,))
    return secrets


def adversarial_inputs() -> dict:
    """マスクの正規表現を最も遅くする形の入力（A1）。``種別: 大きさ n を受け取って、n 字の入力を作る関数``。

    接頭辞の繰り返し、区切りの無い長い並び、途中で途切れる並び。
    """

    def repeat(unit: str):
        return lambda size: (unit * (size // len(unit) + 1))[:size]

    def after(prefix: str, filler: str):
        return lambda size: prefix + filler * size

    jwt = "ey" + "J"
    bearer = "Bea" + "rer"
    aws = "aws_secret_" + "access_key"
    return {
        "jwt_prefix_repeated": repeat(jwt),
        "jwt_prefix_then_long_run": after(jwt, "a"),
        "jwt_brief_dotted_repeated": repeat(jwt + "a.b."),
        "jwt_two_parts_repeated": repeat(jwt + "a" * 12 + "."),
        "jwt_header_then_dots": after(jwt + "abc", "."),
        "bearer_repeated": repeat(bearer + " "),
        "bearer_lower_repeated": repeat(bearer.lower() + " "),
        "bearer_then_spaces": after(bearer, " "),
        "bearer_short_tokens": repeat(bearer + " abc "),
        "slack_prefix_repeated": repeat("xo" + "xb-"),
        "slack_app_prefix_repeated": repeat("xa" + "pp-"),
        "sk_prefix_repeated": repeat("s" + "k-"),
        "sk_short_keys": repeat("s" + "k-" + "a" * 10 + " "),
        "sk_then_hyphens": after("s" + "k-", "-"),
        "github_prefix_repeated": repeat("gh" + "p_"),
        "github_pat_prefix_repeated": repeat("github" + "_pat_"),
        "gitlab_prefix_repeated": repeat("gl" + "pat-"),
        "google_prefix_repeated": repeat("AI" + "za"),
        "aws_key_prefix_repeated": repeat("AK" + "IA"),
        "notion_prefix_repeated": repeat("sec" + "ret_"),
        "webhook_prefix_repeated": repeat("hooks.sl" + "ack.com/services/"),
        "url_scheme_repeated": repeat("https://"),
        "url_user_repeated": repeat("https://user:"),
        "url_password_without_at": after("https://user:", "p"),
        "url_colon_at_repeated": repeat("a:b@"),
        "url_scheme_letters_only": after("", "a"),
        "colons": after("", ":"),
        "slashes": after("", "/"),
        "at_signs": after("", "@"),
        "dots": after("", "."),
        "hyphens": after("", "-"),
        "underscores": after("", "_"),
        "spaces": after("", " "),
        "newlines": after("", "\n"),
        "equals_signs": after("", "="),
        "pem_begin_repeated": repeat("-----BEGIN " + "PRIVATE KEY-----"),
        "pem_begin_cut_repeated": repeat("-----BEGIN "),
        "pem_begin_long_label": after("-----BEGIN ", "A"),
        "pem_dashes": repeat("-----"),
        "basic_auth_prefix_repeated": repeat("Authorization: Bas" + "ic "),
        "basic_auth_then_spaces": after("Authorization:", " "),
        "basic_word_repeated": repeat("Bas" + "ic "),
        "base64_run": repeat("QUJD"),
        "aws_secret_name_repeated": repeat(aws),
        "aws_secret_equals_repeated": repeat(aws + "="),
        "aws_secret_then_spaces": after(aws, " "),
        "japanese_with_prefixes": repeat("日本語の文 " + jwt + " " + bearer + " "),
    }


def bearer_value() -> str:
    return "abcdefghij" + "klmnop.qrstuv-wx"


def valid_submission(**overrides: Any) -> dict:
    """形の正しい ``submit_diagnosis`` の入力（design.md §5.4）。"""
    submission: dict[str, Any] = {
        "failed_stage": "researching",
        "cause_category": "rate_limit",
        "cause_nature": "transient",
        "confidence": "high",
        "evidence": [],
        "action_type": "retry_later",
        "action_note": "時間を置いて再投入してください",
        "summary": "レート制限に達して失敗しました",
    }
    submission.update(overrides)
    return submission


def parse(result: Any) -> dict:
    """``ToolResult.to_text()`` を辞書に戻す。"""
    return json.loads(result.to_text())


def frame_ids(event: dict) -> list[str]:
    """イベントの ``frames`` から ``FR-<n>`` の ID を取り出す（項目名は決め打ちしない）。"""
    found = []
    for frame in event.get("frames") or []:
        values = frame.values() if isinstance(frame, dict) else [frame]
        found.extend(v for v in values if isinstance(v, str) and FRAME_ID.match(v))
    return found


Frame = tuple[str, int, str]  # (パス, 行番号, 関数名)
MODULE_LEVEL = "<module>"
_CODE_ROOTS: dict[tuple, str] = {}
CLASSES_FILE = "error_types.py"  # テスト用のコードの根で、クラス定義を置くファイル
# 筋書きに出てくる、このシステムのコードが定義する例外クラス（実物は src/agent/orchestrator.py にある）
SCENARIO_CLASSES = ("NonDictGeneratorResponse", "ClaudeInvocationError")


def write_code_root(root: Path, frames: Iterable[Frame]) -> Path:
    """``frames`` がすべて「本物」になる小さなコードを ``root`` の下に作る（修正仕様 S1）。

    ``/app/x/y.py`` は ``root/x/y.py`` に置く。関数は、渡された行番号をすべて含む範囲で定義する
    （``<module>`` は、その行までファイルがあるようにするだけ）。``/app/`` で始まらないものは無視する。
    """
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, line, function in frames:
        if path.startswith("/app/"):
            by_file.setdefault(path[len("/app/") :], []).append((line, function))
    for relative, entries in by_file.items():
        ranges: dict[str, tuple[int, int]] = {}
        length = max(line for line, _ in entries)
        for line, function in entries:
            if function == MODULE_LEVEL:
                continue
            start, end = ranges.get(function, (line, line))
            ranges[function] = (min(start, line), max(end, line))
        lines = [""] * length
        occupied: set[int] = set()
        for function, (start, end) in sorted(ranges.items(), key=lambda item: item[1]):
            span = set(range(start, end + 1))
            assert not span & occupied, f"overlapping functions in test code root: {relative}:{function}"
            occupied |= span
            if start == end:
                lines[start - 1] = f"def {function}(): pass"
                continue
            lines[start - 1] = f"def {function}():"
            for number in range(start + 1, end + 1):
                lines[number - 1] = "    pass"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def new_code_root() -> Path:
    """空の「コードの根」を一時ディレクトリに作る（プロセスの終了時に消す）。"""
    root = tempfile.mkdtemp(prefix="diag-code-root-")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    return Path(root)


def write_classes(root: Path, classes: Iterable[str], *, file_name: str = CLASSES_FILE) -> Path:
    """``classes`` の名前のクラス定義を、コードの根に置く（修正仕様 C1: 例外の型の許可一覧に入る）。"""
    lines = [f"class {name}(Exception):\n    pass\n" for name in classes]
    target = root / file_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def code_root_for(frames: Iterable[Frame], classes: Iterable[str] = ()) -> str:
    """``frames`` がすべて本物になり、``classes`` が定義されている「コードの根」のパスを返す。

    同じ引数なら使い回す。
    """
    frame_key = tuple(sorted({frame for frame in frames if frame[0].startswith("/app/")}))
    class_key = tuple(sorted(set(classes)))
    key = (frame_key, class_key)
    if key not in _CODE_ROOTS:
        root = write_code_root(new_code_root(), frame_key)
        if class_key:
            write_classes(root, class_key)
        _CODE_ROOTS[key] = str(root)
    return _CODE_ROOTS[key]


def default_code_root() -> str:
    """5 つの筋書き（``scenarios.py``）のフレームが、すべて本物になる「コードの根」。"""
    from .scenarios import ALL_FRAMES

    return code_root_for(ALL_FRAMES, SCENARIO_CLASSES)


def _diagnosis_from(fields: dict[str, Any]) -> Any:
    """``Diagnosis`` を組み立てる。

    修正仕様 M6 で足す項目（``exception_evidence_id``・``code_location_evidence_ids``）が、まだ型に無い間は
    渡さない（M6 と関係のないテストまで落とさないため）。項目があること自体は ``test_handoff.py`` が確かめる。
    """
    from diagnosis.models import Diagnosis

    known = {f.name for f in dataclasses.fields(Diagnosis)}
    return Diagnosis(**{name: value for name, value in fields.items() if name in known})


def make_diagnosis(**overrides: Any) -> Any:
    from diagnosis.models import CodeLocation, EvidenceRef

    fields: dict[str, Any] = {
        "failed_stage": "generating",
        "cause_category": "output_validation_failed",
        "cause_nature": "code_defect",
        "confidence": "high",
        "evidence": [EvidenceRef(id="EV-7", note="生成の失敗")],
        "invalid_evidence_count": 0,
        "action_type": "fix_code",
        "action_note": "検証の条件を見直す",
        "summary": "出力の検証に失敗しました",
        "exception_type": "NonDictGeneratorResponse",
        "code_locations": [
            CodeLocation(path="/app/orchestrator.py", function="_run_text_generator_with_retries", line=2211)
        ],
        # M6: 例外の型を取ったイベントと、各場所（code_locations と同じ並び）が由来するイベントの根拠 ID
        "exception_evidence_id": "EV-7",
        "code_location_evidence_ids": ["EV-7"],
    }
    fields.update(overrides)
    return _diagnosis_from(fields)


def make_run(*, status: str = "completed", stop_detail: str = "submitted", **diagnosis_overrides: Any) -> Any:
    """``DiagnosisRun`` を直接組み立てる（Slack の投稿文と ``main.py`` のテスト用）。"""
    from diagnosis.handoff import build_handoff
    from diagnosis.models import DiagnosisRun, ToolCall

    fields: dict[str, Any] = {
        "failed_stage": "researching",
        "cause_category": "rate_limit",
        "cause_nature": "transient",
        "confidence": "high",
        "action_type": "retry_later",
        "action_note": "30 分ほど置いてください",
        "summary": "レート制限に 3 回当たって失敗しました",
        "exception_type": None,
        "code_locations": [],
        "exception_evidence_id": None,
        "code_location_evidence_ids": [],
    }
    fields.update(diagnosis_overrides)
    if "evidence" not in fields:
        from diagnosis.models import EvidenceRef

        fields["evidence"] = [EvidenceRef(id=f"EV-{n}", note="レート制限") for n in (2, 3, 4)]
    diagnosis = make_diagnosis(**fields)
    trace = [
        ToolCall(seq=1, tool="get_execution_overview", args={}, outcome="ok", evidence_count=1, duration_ms=3),
        ToolCall(seq=2, tool="list_events", args={}, outcome="ok", evidence_count=7, duration_ms=5),
        ToolCall(seq=3, tool="submit_diagnosis", args={}, outcome="ok", evidence_count=0, duration_ms=1),
    ]
    return DiagnosisRun(
        execution_id=VALID_EXECUTION_ID,
        status=status,
        stop_detail=stop_detail,
        diagnosis=diagnosis,
        handoff=build_handoff(VALID_EXECUTION_ID, diagnosis),
        trace=trace,
        observed_tool_uses=[f"mcp__diag__{call.tool}" for call in trace],
        num_turns=4,
        duration_ms=4200,
        usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 165},
        total_cost_usd=0.0123,
    )
