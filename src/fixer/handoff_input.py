"""受け渡しの読み込み・形の検証・場所の再検証・条件の再計算。SDK には依存しない。

設計詳細: `.steering/20261004-execution-fixer/design.md` §4

受け渡しのファイルは人が運ぶので、修正役は信頼しない。形が合わないものはすべて拒否し、場所は
複製のコードと突き合わせ直し、「修正役に渡せる」も項目から計算し直す。第 1 段階の受け渡しの
組み立ての側は使わない（第 1 段階の形が変わったときに、黙って追随しないため）。
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from diagnosis.frames import FrameVerifier
from diagnosis.models import (
    ACTION_TYPES,
    CAUSE_CATEGORIES,
    CAUSE_NATURES,
    CONFIDENCES,
    FAILED_STAGES,
    HANDOFF_SCHEMA_VERSION,
    CodeLocation,
)

from src.fixer.gates import ChangeScope
from src.fixer.models import HandoffInput, VerifiedLocation

MAX_HANDOFF_BYTES = 100_000
MAX_CODE_LOCATIONS = 5
MAX_EVIDENCE_COUNT = 10
MAX_PATH_CHARS = 120
MAX_LINE = 9_999_999
APP_PREFIX = "/app/"
SOURCE_PREFIX = "src/"
AGENT_ROOT = ("src", "agent")  # `/app/` に当たるディレクトリ（複製の根からの相対）

_FIELDS = frozenset(
    {
        "schema_version",
        "execution_id",
        "failed_stage",
        "cause_category",
        "cause_nature",
        "confidence",
        "exception_type",
        "code_locations",
        "action_type",
        "valid_evidence_count",
        "fixer_eligible",
    }
)
_LOCATION_FIELDS = frozenset({"path", "function", "line"})
_ENUM_FIELDS = (
    ("failed_stage", FAILED_STAGES),
    ("cause_category", CAUSE_CATEGORIES),
    ("cause_nature", CAUSE_NATURES),
    ("confidence", CONFIDENCES),
    ("action_type", ACTION_TYPES),
)
# 数字は [0-9] と書く（\d は全角の数字にも一致するため）。検証は必ず fullmatch で行う。
_EXECUTION_ID_PATTERN = re.compile(r"exec-[0-9]{14}-[0-9a-f]{8}")
_EXCEPTION_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
_PATH_PATTERN = re.compile(r"/app/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*\.py")
_FUNCTION_PATTERN = re.compile(r"[A-Za-z_<][A-Za-z0-9_<>]{0,79}")
_CATEGORIES_NEVER_ELIGIBLE = ("unknown", "insufficient_records")
_READ_CHUNK_BYTES = 1 << 16


class HandoffError(Exception):
    """受け渡しの形が合わない。文面は決まった形だけ（拒否した値を入れない）。"""


class _DuplicateKeyError(ValueError):
    """JSON のオブジェクトに、同じ名前の項目が 2 つある。"""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _DuplicateKeyError
    return result


def _read_limited(path: Path) -> bytes:
    """ファイルを上限まで読む。通常のファイルでない、上限を超える、読めない → HandoffError。"""
    try:
        # O_NONBLOCK: FIFO などを指されても、開いたまま待たない
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, TypeError, ValueError) as e:
        raise HandoffError("handoff file cannot be opened") from e
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HandoffError("handoff is not a regular file")
        data = bytearray()
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            data.extend(chunk)
            if len(data) > MAX_HANDOFF_BYTES:
                raise HandoffError("handoff file is too large")
        return bytes(data)
    except OSError as e:
        raise HandoffError("handoff file cannot be read") from e
    finally:
        os.close(descriptor)


def _is_int(value: object, minimum: int, maximum: int) -> bool:
    # 真偽値（int の派生）と小数は通さない
    return type(value) is int and minimum <= value <= maximum


def _matches(pattern: re.Pattern[str], value: object, max_chars: int | None = None) -> bool:
    if not isinstance(value, str) or (max_chars is not None and len(value) > max_chars):
        return False
    return pattern.fullmatch(value) is not None


def _parse_locations(value: object) -> tuple[CodeLocation, ...]:
    if not isinstance(value, list) or len(value) > MAX_CODE_LOCATIONS:
        raise HandoffError("handoff field has an invalid value: code_locations")
    locations = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _LOCATION_FIELDS:
            raise HandoffError("handoff location has undefined or missing fields")
        if not _matches(_PATH_PATTERN, item["path"], MAX_PATH_CHARS):
            raise HandoffError("handoff location has an invalid value: path")
        if not _matches(_FUNCTION_PATTERN, item["function"]):
            raise HandoffError("handoff location has an invalid value: function")
        if not _is_int(item["line"], 1, MAX_LINE):
            raise HandoffError("handoff location has an invalid value: line")
        locations.append(CodeLocation(item["path"], item["function"], item["line"]))
    return tuple(locations)


def load_handoff(path: Path) -> HandoffInput:
    """受け渡しのファイルを読み、形を検証する。

    Raises:
        HandoffError: ファイルが読めない、大きすぎる、形が合わない（項目の過不足、決まった値以外、
            識別子の形でない値、範囲の外の数など）。
    """
    raw = _read_limited(path)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError):
        # 元の例外はつながない（UnicodeDecodeError は、ファイルの中身を引数に持つ）
        raise HandoffError("handoff is not valid JSON") from None
    if not isinstance(data, dict):
        raise HandoffError("handoff is not a JSON object")
    # 足りない・余る、のどちらも通さない（項目の名前は、文面に入れない）
    if set(data) != _FIELDS:
        raise HandoffError("handoff has undefined or missing fields")
    if not _is_int(data["schema_version"], HANDOFF_SCHEMA_VERSION, HANDOFF_SCHEMA_VERSION):
        raise HandoffError("handoff field has an invalid value: schema_version")
    if not _matches(_EXECUTION_ID_PATTERN, data["execution_id"]):
        raise HandoffError("handoff field has an invalid value: execution_id")
    for name, values in _ENUM_FIELDS:
        if not (isinstance(data[name], str) and data[name] in values):
            raise HandoffError(f"handoff field has an invalid value: {name}")
    exception_type = data["exception_type"]
    if exception_type is not None and not _matches(_EXCEPTION_TYPE_PATTERN, exception_type):
        raise HandoffError("handoff field has an invalid value: exception_type")
    code_locations = _parse_locations(data["code_locations"])
    if not _is_int(data["valid_evidence_count"], 0, MAX_EVIDENCE_COUNT):
        raise HandoffError("handoff field has an invalid value: valid_evidence_count")
    if not isinstance(data["fixer_eligible"], bool):
        raise HandoffError("handoff field has an invalid value: fixer_eligible")
    return HandoffInput(
        execution_id=data["execution_id"],
        failed_stage=data["failed_stage"],
        cause_category=data["cause_category"],
        cause_nature=data["cause_nature"],
        confidence=data["confidence"],
        exception_type=exception_type,
        code_locations=code_locations,
        action_type=data["action_type"],
        valid_evidence_count=data["valid_evidence_count"],
        fixer_eligible=data["fixer_eligible"],
    )


def _spelled_as_on_disk(root: Path, relative: str) -> bool:
    """相対パスの各要素が、その綴りのまま親ディレクトリの一覧にあるか（大文字と小文字の違いを通さない）。"""
    current = root
    for part in relative.split("/"):
        try:
            if part not in os.listdir(current):
                return False
        except OSError:
            return False
        current = current / part
    return True


def _verify_one(
    location: object, verifiers: tuple[tuple[FrameVerifier, str], ...], root: Path
) -> VerifiedLocation | None:
    """場所 1 件を突き合わせる。通らなければ None。"""
    path = getattr(location, "path", None)
    function = getattr(location, "function", None)
    line = getattr(location, "line", None)
    # 形の検証を通さずに作られた受け渡しでも、ここで落とす
    if not _matches(_PATH_PATTERN, path, MAX_PATH_CHARS) or not _matches(_FUNCTION_PATTERN, function):
        return None
    if not _is_int(line, 1, MAX_LINE):
        return None
    for verifier, prefix in verifiers:
        # 検証の根や、途中の要素がシンボリックリンクなら、その場所は使わない（対象外の場所が、別のパスとして
        # 通るのを防ぐ。FrameVerifier は、リンクをたどった先で突き合わせる）
        if _goes_through_link(root, prefix + path[len(APP_PREFIX) :]):
            return None
        verified = verifier.verify(path, line, function)
        if verified is None:
            continue
        relative = prefix + verified.path[len(APP_PREFIX) :]
        # 直せる場所（src/ の下で、対象外でない）だけを残す。tests/ と、安全の仕組み・認証まわりは捨てる。
        # 判定は、書き込みのツール・G-2 と同じもの（場所は既にあるファイルなので、そのパスを基準の一覧として渡す）
        if not relative.startswith(SOURCE_PREFIX) or ChangeScope([relative]).forbids(relative, None):
            return None
        if not _spelled_as_on_disk(root, relative):
            return None
        return VerifiedLocation(path=relative, function=verified.function, line=verified.line)
    return None


def _goes_through_link(root: Path, relative: str) -> bool:
    """複製の根から、相対パスの各要素をたどる途中（末尾を含む）に、シンボリックリンクがあれば True。"""
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            return True
    return False


def verify_locations(handoff: HandoffInput, workspace_root: Path) -> list[VerifiedLocation]:
    """受け渡しの場所を、複製の中のコードと突き合わせる。通ったものだけを、組み立て直して返す。例外を出さない。

    `/app/x.py` は、まず `<複製>/src/agent/x.py` と、次に `<複製>/x.py`（`/app/src/observability/...` のため）と
    突き合わせる。ファイルが実在し、その名前の関数定義があり、行番号がその範囲に入るものだけを通す。
    同じ（パス・関数名）は 1 件にまとめる。
    """
    root = Path(workspace_root)
    verifiers = (
        (FrameVerifier(code_root=root.joinpath(*AGENT_ROOT)), "/".join(AGENT_ROOT) + "/"),
        (FrameVerifier(code_root=root), ""),
    )
    kept: dict[tuple[str, str], VerifiedLocation] = {}
    for location in handoff.code_locations:
        try:
            verified = _verify_one(location, verifiers, root)
        except (OSError, RuntimeError, ValueError, TypeError):
            verified = None
        if verified is not None:
            kept.setdefault((verified.path, verified.function), verified)
    return list(kept.values())


def is_eligible(handoff: HandoffInput, locations: list[VerifiedLocation]) -> bool:
    """「修正役に渡せる」を、項目から計算し直す。ファイルの判定が真であることも求める。

    例外の型は、ここでは「ある」ことだけを確かめる。許可一覧との照合は、複製のコードが要るので、
    `exception_types.is_known_exception_type` で行う（`run_fix` が、両方を確かめる）。
    真であることは、修正役を起動してよいことを意味するだけで、不具合があることの証拠ではない。
    """
    return (
        handoff.exception_type is not None
        and handoff.cause_nature == "code_defect"
        and handoff.confidence == "high"
        and handoff.action_type == "fix_code"
        and handoff.cause_category not in _CATEGORIES_NEVER_ELIGIBLE
        and handoff.valid_evidence_count >= 1
        and len(locations) >= 1
        and handoff.fixer_eligible is True
    )
