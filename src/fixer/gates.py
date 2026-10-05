"""合否の検査（G-1〜G-10）と、対象外の場所の一覧。SDK には依存しない。

設計詳細: `.steering/20261004-execution-fixer/design.md` §6.2・§10

モデルの申告を信じず、コードが複製の差分と実際のテスト結果で確かめる。どちらの関数も、結果を返すだけで、
ファイルにもプロセスにも触れない。対象外の場所の一覧は、場所の再検証（handoff_input.py）・書き込みのツール
（workspace.py）・G-2 の 3 か所が、ここの同じものを使う。
"""

from __future__ import annotations

import bisect
import difflib
import functools
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from diagnosis.redaction import mask_secrets

from src.fixer.models import FixLimits, GateResult

if TYPE_CHECKING:
    from src.fixer.runners import TestReport
    from src.fixer.workspace import ChangeSet

# 前方一致で対象外にする場所。安全の仕組み（診断役・修正役）、認証と秘密に関わるもの、プロンプト、規律と CI
FORBIDDEN_PREFIXES = (
    "src/fixer/",
    "src/agent/diagnosis/",
    "src/agent/prompts/",
    "src/token_monitor/",
    "src/dashboard_api/authorizer/",
    "src/dashboard_api/auth_",  # auth_login・auth_callback・auth_logout・auth_me
    "tests/unit/fixer/",
    "tests/unit/agent/diagnosis/",
    ".github/",
    ".steering/",
    "docs/",
)
# 完全一致で対象外にするパス
FORBIDDEN_PATHS = (
    "src/agent/main.py",  # 認証情報の準備と書き戻し
    "src/trigger/slack_verify.py",
    "src/trigger/app.py",  # 署名検証を呼ぶ側
    "src/agent/Dockerfile",
    "src/agent/requirements.txt",
    "template.yaml",
    "CLAUDE.md",
    "pyproject.toml",
    ".gitleaks.toml",
)
# どのディレクトリにあっても対象外にするファイル名
FORBIDDEN_NAMES = ("conftest.py", "sitecustomize.py", "usercustomize.py", "Dockerfile")
# 依存の定義（requirements.txt、requirements-dev.txt など）
_REQUIREMENTS_NAME_PATTERN = re.compile(r"requirements[^/]*\.txt")

# import の乗っ取りになる名前（依存パッケージの最上位の名前と、起動時に自動で読まれる名前）。`src/` と、その直下の
# ディレクトリは、pytest の pythonpath や実行時の検索パスに入るので、その直下に同じ名前を足すと、差し替えになる。
# これに、基準のコミットの依存の定義（requirements*.txt）から作った名前を足して使う（`ChangeScope`）。配布物の名前と
# import の名前が違うもの（PyYAML → yaml など）は、依存の定義からは作れないので、ここに書く
SHADOWED_DEPENDENCY_NAMES = frozenset(
    {
        "aws_lambda_powertools",
        "boto3",
        "botocore",
        "slack_sdk",
        "requests",
        "urllib3",
        "certifi",
        "idna",
        "charset_normalizer",
        "claude_agent"
        + "_sdk",  # 名前を分けて書く（このモジュールが SDK に依存していないことを、文字列で確かめるテストがある）
        "mcp",
        "anyio",
        "jwt",
        "jsonschema",
        "pydantic",
        "pytest",
        "_pytest",
        "ruff",
        "yaml",
        "dateutil",
        "s3transfer",
        "jmespath",
        "six",
        "typing_extensions",
        "sitecustomize",
        "usercustomize",
    }
)
_STDLIB_MODULE_NAMES = frozenset(name.casefold() for name in sys.stdlib_module_names)
# 依存の定義の 1 行の、先頭にある配布物の名前（版の指定、extras の `[...]`、環境の条件は、この後ろに続く）
_REQUIREMENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_REQUIREMENT_COMMENT_PATTERN = re.compile(r"(?:^|\s)#.*")
# トークンを受け取るファイル。変更はできるが、下書きの冒頭に注意書きを入れる
CREDENTIAL_HANDLING_FILES = (
    "src/agent/storage/github_client.py",
    "src/agent/storage/notion_client.py",
    "src/agent/notify/slack_client.py",
)
# コミットに入れない文字: 双方向の制御文字、見えない文字、CR（見た目と実体が違うコードを作れる）
HIDDEN_CHARACTERS = frozenset(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\r"
)

SOURCE_DIRECTORY = "src"
WRITABLE_TOP_DIRECTORIES = ("src", "tests")
TESTS_DIRECTORY = "tests"
PYTHON_SUFFIX = ".py"
TEST_FILE_PREFIX = "test_"
PACKAGE_INIT_NAME = "__init__.py"
# 変更してよいパスの形。各要素は英数字か下線で始まり、英数字・下線・ハイフンだけからなる。ピリオドは、
# 最後の要素の、末尾の `.py` だけ（pytest は、パスの `/` を `.` に変えてテストの ID を作る。ピリオドを含む要素が
# あると、別のファイルのテストと ID が同じになる）。検証は fullmatch で行う。コミットメッセージと下書きに入る
# パスを、この形に限る
CHANGE_PATH_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_][A-Za-z0-9_-]*)*(?:\.py)?")
MAX_CHANGE_PATH_CHARS = 200

OTHER_PATH = "<other>"
_DISPLAY_PATH_PATTERN = re.compile(r"[A-Za-z0-9_./-]{1,200}")
_MAX_LISTED_PATHS = 5

_STATIC_GATES = ("G1", "G2", "G3", "G4", "G9", "G10")
_FAILING_OUTCOMES = ("failed", "error")
# pytest が、テストを集めて走らせ終えたときの終了コード（0: すべて成功、1: 失敗あり）
_COMPLETED_EXIT_CODES = (0, 1)
# 一意な行で区切れない区間を、difflib で比べる大きさの上限（行数の積）。超えたら、区間ごと変更と数える
_MAX_DIFFLIB_CELLS = 250_000


def is_forbidden(rel: str) -> bool:
    """対象外の場所なら True。

    大文字と小文字を区別しないファイルシステムでは、別の綴りで同じファイルに届く（`src/agent/MAIN.py`）。
    綴りの違いですり抜けないよう、大文字と小文字を畳んで比べる。
    """
    folded = rel.casefold()
    name = folded.rsplit("/", 1)[-1]
    if any(folded.startswith(prefix.casefold()) for prefix in FORBIDDEN_PREFIXES):
        return True
    # 対象外のディレクトリそのもの（ディレクトリが、ファイルやシンボリックリンクに置き換えられた場合）
    if any(prefix.endswith("/") and folded == prefix[:-1].casefold() for prefix in FORBIDDEN_PREFIXES):
        return True
    if any(folded == path.casefold() for path in FORBIDDEN_PATHS):
        return True
    if any(name == forbidden.casefold() for forbidden in FORBIDDEN_NAMES):
        return True
    return _REQUIREMENTS_NAME_PATTERN.fullmatch(name) is not None


def has_hidden_characters(text: str) -> bool:
    """双方向の制御文字・見えない文字・CR を含めば True。"""
    return not HIDDEN_CHARACTERS.isdisjoint(text)


class ChangeScope:
    """変更してよい場所かどうかの判定。場所の再検証・書き込みのツール・G-2 の 3 か所が、これを使う。

    対象外の一覧（`is_forbidden`）に加えて、基準のコミットのパスの一覧から決まる規則を見る:
    `src/` の下の `__init__.py` と、import の乗っ取りになる名前。
    """

    def __init__(self, base_paths: Iterable[str]) -> None:
        """
        Args:
            base_paths: 基準のコミットのパスの一覧。基準の一覧（相対パス → 記録の `Mapping`）を渡すと、
                依存の定義（`requirements*.txt`。どの階層でも）の中身から、依存の名前も読む。
        """
        self._dependency_names = SHADOWED_DEPENDENCY_NAMES | _required_names(base_paths)
        folded = [path.casefold() for path in base_paths]
        self._base = frozenset(folded)
        self._base_directories = frozenset(
            "/".join(path.split("/")[:depth]) for path in folded for depth in range(1, path.count("/") + 1)
        )
        # `src/` の直下と、その直下の各ディレクトリの直下にある、モジュールとパッケージの名前
        names: set[str] = set()
        for path in folded:
            parts = path.split("/")
            if parts[0] != SOURCE_DIRECTORY or len(parts) < 2:
                continue
            names.add(_module_name(parts[1]) if len(parts) == 2 else parts[1])
            if len(parts) >= 3:
                names.add(_module_name(parts[2]) if len(parts) == 3 else parts[2])
        names.discard(_module_name(PACKAGE_INIT_NAME))
        self._module_names = frozenset(names)

    def forbids(self, rel: str, content: bytes | None) -> bool:
        """そのパスへの変更を認めないなら True。

        Args:
            rel: 複製の根からの相対パス。
            content: 変更後の中身。分からないとき（削除、通常のファイルでないもの）は None。
        """
        if is_forbidden(rel):
            return True
        parts = rel.casefold().split("/")
        if parts[0] != SOURCE_DIRECTORY:
            return False
        exists = "/".join(parts) in self._base
        if parts[-1] == PACKAGE_INIT_NAME and (exists or content != b""):
            # 既にある __init__.py は変えられない。新しく足せるのは、空のものだけ
            return True
        if exists:
            return False
        introduced = self._introduced_name(parts)
        return introduced is not None and (
            introduced in _STDLIB_MODULE_NAMES
            or introduced in self._dependency_names
            or introduced in self._module_names
        )

    def _introduced_name(self, parts: list[str]) -> str | None:
        """新しいパスが、import の根の直下に足す名前（モジュールかパッケージ）。足さないなら None。"""
        if len(parts) == 2:
            name = _module_name(parts[1])  # src/<名前>.py
        elif "/".join(parts[:2]) not in self._base_directories:
            name = parts[1]  # src/ の直下の、新しいディレクトリ
        elif len(parts) == 3:
            name = _module_name(parts[2])  # 根の直下の、新しいファイル
        elif "/".join(parts[:3]) not in self._base_directories:
            name = parts[2]  # 根の直下の、新しいディレクトリ
        else:
            return None  # 直下ではない
        return None if name == _module_name(PACKAGE_INIT_NAME) else name


def _required_names(baseline: object) -> frozenset[str]:
    """基準の一覧の中の依存の定義から、import の名前を作る。一覧が `Mapping` でなければ、空。

    配布物の名前を小文字にし、`-` と `.` を `_` にする。中身を持っていない定義（大きすぎるもの）は、読めない。
    """
    if not isinstance(baseline, Mapping):
        return frozenset()
    names: set[str] = set()
    for rel, entry in baseline.items():
        content = getattr(entry, "content", None)
        if not isinstance(rel, str) or not isinstance(content, bytes):
            continue
        if _REQUIREMENTS_NAME_PATTERN.fullmatch(rel.casefold().rsplit("/", 1)[-1]) is None:
            continue
        for raw in content.decode("utf-8", errors="replace").splitlines():
            line = _REQUIREMENT_COMMENT_PATTERN.sub("", raw).strip()
            # `-r`・`-e`・`--no-binary` などのオプションの行からは、名前を作らない
            if not line or line.startswith("-"):
                continue
            matched = _REQUIREMENT_NAME_PATTERN.match(line)
            if matched is not None:
                names.add(matched.group().casefold().replace("-", "_").replace(".", "_"))
    return frozenset(names)


def _module_name(file_name: str) -> str:
    """ファイル名から、import するときの名前を取る（`json.py`・`json.pyi` は、どちらも `json`）。"""
    return file_name.split(".", 1)[0]


def is_under(rel: str, directory: str) -> bool:
    return rel.startswith(directory + "/")


def is_test_file_name(name: str) -> bool:
    return name.startswith(TEST_FILE_PREFIX) and name.endswith(PYTHON_SUFFIX)


def is_change_path(rel: str) -> bool:
    """変更してよいパスの形（`src/`・`tests/` の下で、決まった文字だけ）なら True。"""
    return (
        len(rel) <= MAX_CHANGE_PATH_CHARS
        and CHANGE_PATH_PATTERN.fullmatch(rel) is not None
        and any(is_under(rel, top) for top in WRITABLE_TOP_DIRECTORIES)
    )


def display_path(rel: str) -> str:
    """結果・下書きに出すパス。決まった文字だけの、秘密情報らしい部分のないパスだけを、そのまま出す。"""
    if _DISPLAY_PATH_PATTERN.fullmatch(rel) is None or mask_secrets(rel) != rel:
        return OTHER_PATH
    return rel


def count_changed_lines(old: bytes | None, new: bytes | None) -> tuple[int, int]:
    """（足した行数, 消した行数）を返す。`old` が None なら新しいファイル、`new` が None なら削除。

    同じ入力の結果は覚えておく（G-9 と、下書きの差分の要約とで、同じ計算を 2 回しない）。
    """
    return _count_changed_lines(old or b"", new or b"")


@functools.lru_cache(maxsize=32)
def _count_changed_lines(old: bytes, new: bytes) -> tuple[int, int]:
    """行の差分を数える。入力の大きさに対して、ほぼ線形の時間で終わる。

    先頭と末尾の同じ行を除き、残りを「両方に 1 回ずつだけ現れる行」で区切って、区間ごとに数える。
    区切れない小さな区間は difflib で比べ、大きな区間は、区間ごと変更として数える（多めに数える側に倒す）。
    """
    old_lines, new_lines = _lines(old), _lines(new)
    added = removed = 0
    pending = [(0, len(old_lines), 0, len(new_lines))]
    while pending:
        old_start, old_end, new_start, new_end = pending.pop()
        while old_start < old_end and new_start < new_end and old_lines[old_start] == new_lines[new_start]:
            old_start, new_start = old_start + 1, new_start + 1
        while old_start < old_end and new_start < new_end and old_lines[old_end - 1] == new_lines[new_end - 1]:
            old_end, new_end = old_end - 1, new_end - 1
        if old_start == old_end or new_start == new_end:
            removed, added = removed + old_end - old_start, added + new_end - new_start
            continue
        anchors = _unique_common_lines(old_lines, old_start, old_end, new_lines, new_start, new_end)
        if anchors:
            for old_index, new_index in anchors:
                pending.append((old_start, old_index, new_start, new_index))
                old_start, new_start = old_index + 1, new_index + 1
            pending.append((old_start, old_end, new_start, new_end))
        elif (old_end - old_start) * (new_end - new_start) <= _MAX_DIFFLIB_CELLS:
            matcher = difflib.SequenceMatcher(
                None, old_lines[old_start:old_end], new_lines[new_start:new_end], autojunk=False
            )
            same = sum(block.size for block in matcher.get_matching_blocks())
            removed, added = removed + old_end - old_start - same, added + new_end - new_start - same
        else:
            removed, added = removed + old_end - old_start, added + new_end - new_start
    return added, removed


def _unique_common_lines(
    old: list[str], old_start: int, old_end: int, new: list[str], new_start: int, new_end: int
) -> list[tuple[int, int]]:
    """両方の区間に 1 回ずつだけ現れる行のうち、順序が保たれる最長の並び（位置の組）を返す。"""
    old_counts = Counter(old[old_start:old_end])
    new_counts = Counter(new[new_start:new_end])
    new_positions = {
        line: index for index, line in enumerate(new[new_start:new_end], start=new_start) if new_counts[line] == 1
    }
    pairs = [
        (index, new_positions[line])
        for index, line in enumerate(old[old_start:old_end], start=old_start)
        if old_counts[line] == 1 and line in new_positions
    ]
    # 新しい側の位置について、最長の増加列を取る
    tails: list[int] = []  # 長さごとの、末尾の位置の最小値
    tail_pairs: list[int] = []  # その末尾を持つ組の番号
    previous: list[int] = [-1] * len(pairs)
    for number, (_old_index, new_index) in enumerate(pairs):
        slot = bisect.bisect_left(tails, new_index)
        if slot == len(tails):
            tails.append(new_index)
            tail_pairs.append(number)
        else:
            tails[slot], tail_pairs[slot] = new_index, number
        previous[number] = tail_pairs[slot - 1] if slot else -1
    chain = []
    number = tail_pairs[-1] if tail_pairs else -1
    while number >= 0:
        chain.append(pairs[number])
        number = previous[number]
    return chain[::-1]


def _lines(data: bytes | None) -> list[str]:
    if not data:
        return []
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def _listed(paths: Iterable[str]) -> str:
    """検査の結果に出すパスの並び。件数を絞り、決まった形のパスだけを出す。"""
    ordered = sorted(set(paths))
    shown = ", ".join(display_path(path) for path in ordered[:_MAX_LISTED_PATHS])
    rest = len(ordered) - _MAX_LISTED_PATHS
    return f"{shown}、ほか {rest} 件" if rest > 0 else shown


def _baseline_content(baseline: Mapping[str, object], rel: str) -> bytes | None:
    content = getattr(baseline.get(rel), "content", None)
    return content if isinstance(content, bytes) else None


def _is_empty_test_init(rel: str, changes: ChangeSet) -> bool:
    """`tests/` の下に新しく足した、空の `__init__.py`。"""
    return (
        is_under(rel, TESTS_DIRECTORY) and rel.rsplit("/", 1)[-1] == PACKAGE_INIT_NAME and changes.added.get(rel) == b""
    )


def check_static(changes: ChangeSet, baseline: Mapping[str, object], limits: FixLimits) -> list[GateResult]:
    """静的な検査（G-1〜G-4、G-9、G-10）。1 つ落ちても、6 つすべての結果を返す。

    Args:
        changes: 複製から取り出した変更。
        baseline: 基準の一覧（相対パス → 元のファイルの記録。`content` と `mode` を持つ）。
        limits: 上限（ファイルの数と行数）。
    """
    contents = {**changes.added, **changes.modified}
    changed = sorted({*contents, *changes.deleted, *changes.irregular, *changes.oversized})
    results = {
        "G1": _check_scope(changed, changes),
        "G2": _check_forbidden(changed, contents, ChangeScope(baseline)),
        "G3": _check_existing_tests(changed, changes, baseline),
        "G4": _check_new_test(changes),
        "G9": _check_size(changed, changes, baseline, limits),
        "G10": _check_content(contents, changes, baseline),
    }
    return [results[gate] for gate in _STATIC_GATES]


def _check_scope(changed: list[str], changes: ChangeSet) -> GateResult:
    outside = [rel for rel in changed if not is_change_path(rel)]
    if outside or changes.deleted:
        problems = sorted({*outside, *changes.deleted})
        return GateResult("G1", False, f"範囲の外の変更、または削除が {len(problems)} 件: {_listed(problems)}")
    return GateResult("G1", True, f"変更 {len(changed)} 件は、すべて src/ または tests/ の下")


def _check_forbidden(changed: list[str], contents: Mapping[str, bytes], scope: ChangeScope) -> GateResult:
    forbidden = [rel for rel in changed if scope.forbids(rel, contents.get(rel))]
    if forbidden:
        return GateResult("G2", False, f"禁止の場所への変更が {len(forbidden)} 件: {_listed(forbidden)}")
    return GateResult("G2", True, "禁止の場所への変更なし")


def _check_existing_tests(changed: list[str], changes: ChangeSet, baseline: Mapping[str, object]) -> GateResult:
    problems = []
    for rel in changed:
        if not is_under(rel, TESTS_DIRECTORY):
            continue
        if rel in baseline:
            problems.append(rel)  # 既存のファイル（conftest.py・__init__.py を含む）の変更・削除
        elif not (is_test_file_name(rel.rsplit("/", 1)[-1]) or _is_empty_test_init(rel, changes)):
            problems.append(rel)  # 足せるのは、新しいテストファイルと、空の __init__.py だけ
    if problems:
        return GateResult(
            "G3", False, f"既存のテストの変更、または足せない種類のファイルが {len(problems)} 件: {_listed(problems)}"
        )
    return GateResult("G3", True, "tests/ の既存のファイルに変更なし")


def new_test_files(changes: ChangeSet) -> list[str]:
    """新しいテストファイル（`tests/` の下に足した `test_*.py`）。"""
    return sorted(
        rel for rel in changes.added if is_under(rel, TESTS_DIRECTORY) and is_test_file_name(rel.rsplit("/", 1)[-1])
    )


def _check_new_test(changes: ChangeSet) -> GateResult:
    count = len(new_test_files(changes))
    if count == 0:
        return GateResult("G4", False, "新しいテストファイル（tests/ の下の test_*.py）が無い")
    return GateResult("G4", True, f"新しいテストファイル {count} 件")


def _check_size(
    changed: list[str], changes: ChangeSet, baseline: Mapping[str, object], limits: FixLimits
) -> GateResult:
    file_count = sum(1 for rel in changed if not _is_empty_test_init(rel, changes))
    line_count = 0
    uncountable = []
    for content in changes.added.values():
        line_count += sum(count_changed_lines(None, content))
    for rel, content in changes.modified.items():
        original = _baseline_content(baseline, rel)
        if original is None:
            uncountable.append(rel)  # 元の中身を持っていない（大きすぎる）ファイルは、行数を数えられない
            continue
        line_count += sum(count_changed_lines(original, content))
    for rel in changes.deleted:
        line_count += sum(count_changed_lines(_baseline_content(baseline, rel), None))
    detail = (
        f"変更したファイル {file_count} 件（上限 {limits.max_changed_files}）、"
        f"変更した行 {line_count} 行（上限 {limits.max_changed_lines}）"
    )
    if changes.oversized:
        detail += f"、大きさの上限を超えるファイル {len(changes.oversized)} 件"
    if uncountable:
        detail += f"、行数を数えられないファイル {len(uncountable)} 件"
    passed = (
        file_count <= limits.max_changed_files
        and line_count <= limits.max_changed_lines
        and not changes.oversized
        and not uncountable
    )
    return GateResult("G9", passed, detail)


def _check_content(contents: Mapping[str, bytes], changes: ChangeSet, baseline: Mapping[str, object]) -> GateResult:
    problems: dict[str, list[str]] = {}

    def add(kind: str, rel: str) -> None:
        problems.setdefault(kind, []).append(rel)

    for rel in changes.irregular:
        add("シンボリックリンク・モードの変化・通常のファイルでないもの", rel)
    for rel, content in contents.items():
        if not rel.endswith(PYTHON_SUFFIX):
            add(".py でないファイル", rel)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            add("UTF-8 で読めないファイル", rel)
            continue
        if "\x00" in text:
            add("NUL を含むファイル", rel)
        # 見つけた文字列そのものは、結果に入れない
        if mask_secrets(text) != text:
            add("秘密情報らしい文字列を含むファイル", rel)
        if has_hidden_characters(text):
            add("双方向の制御文字・見えない文字・CR を含むファイル", rel)
    for rel in changes.modified:
        # コミットは、常に 100644 で作る。実行権限のあるファイルを変えると、権限が落ちる
        mode = getattr(baseline.get(rel), "mode", 0)
        if isinstance(mode, int) and mode & 0o111:
            add("実行権限のあるファイルの変更", rel)
    if problems:
        detail = "; ".join(f"{kind} {len(paths)} 件: {_listed(paths)}" for kind, paths in problems.items())
        return GateResult("G10", False, detail)
    return GateResult("G10", True, "シンボリックリンク・モードの変化・バイナリ・秘密情報らしい文字列なし")


def _counts(report: TestReport) -> tuple[int, int, int]:
    """（テストの件数, 通った件数, 落ちた件数）。"""
    values = list(report.outcomes.values())
    return len(values), values.count("passed"), sum(values.count(outcome) for outcome in _FAILING_OUTCOMES)


def _exit_text(report: TestReport) -> str:
    if report.timed_out:
        return "時間切れ"
    return "終了コードなし" if report.exit_code is None else f"終了コード {report.exit_code}"


def check_dynamic(
    *,
    pre_fix_new: TestReport,
    post_fix_new: TestReport,
    base_existing: TestReport,
    post_fix_existing: TestReport,
    lint_before: int | None,
    lint_after: int | None,
) -> list[GateResult]:
    """動的な検査（G-5〜G-8）。1 つ落ちても、4 つすべての結果を返す。

    Args:
        pre_fix_new: 基準＋新しいテストファイルだけの複製で、新しいテストを走らせた結果（実行 B）。
        post_fix_new: 修正を足した複製で、新しいテストを走らせた結果（実行 D）。
        base_existing: 基準の複製で、既存のテストを走らせた結果（実行 A）。
        post_fix_existing: 修正を足した複製で、既存のテストだけを走らせた結果（実行 C）。
        lint_before: 基準の複製の、リントの指摘の数。数えられなければ None。
        lint_after: 修正を足した複製の、リントの指摘の数。数えられなければ None。
    """
    return [
        _check_fails_before(pre_fix_new),
        _check_passes_after(pre_fix_new, post_fix_new),
        _check_nothing_else_broken(base_existing, post_fix_existing),
        _check_lint(lint_before, lint_after),
    ]


def _failing_ids(report: TestReport) -> set[str]:
    return {test_id for test_id, outcome in report.outcomes.items() if outcome in _FAILING_OUTCOMES}


def _check_fails_before(report: TestReport) -> GateResult:
    total, _passed, failing = _counts(report)
    detail = f"修正前: 新しいテスト {total} 件のうち {failing} 件が失敗（{_exit_text(report)}）"
    # 集めるときのエラー（終了コード 2。まだ無いものを import するだけのテストなど）は、「落ちた」と数えない
    passed = not report.timed_out and report.exit_code == 1 and failing >= 1
    return GateResult("G5", passed, detail)


def _check_passes_after(before: TestReport, after: TestReport) -> GateResult:
    total, passed_count, _failing = _counts(after)
    passed_ids = {test_id for test_id, outcome in after.outcomes.items() if outcome == "passed"}
    # 修正前の実行が、テストの失敗として終わったとき（終了コード 1）の、落ちたテストを見る。集めるときのエラーの
    # 項目は、テストの ID ではないので、比べない（その場合は、G-5 が不合格になる）
    failed_before = _failing_ids(before) if before.exit_code == 1 else set()
    still_not_passing = len(failed_before - passed_ids)
    detail = f"修正後: 新しいテスト {total} 件のうち {passed_count} 件が成功（{_exit_text(after)}）"
    if still_not_passing:
        detail += f"、修正前に落ちたテストのうち、通っていないもの {still_not_passing} 件"
    passed = (
        not after.timed_out
        and after.exit_code == 0
        and total >= 1
        and passed_count == total
        # 修正前に落ちたテストそのものが、通っていること（別のテストが通っただけでは足りない）
        and still_not_passing == 0
    )
    return GateResult("G6", passed, detail)


def _check_nothing_else_broken(before: TestReport, after: TestReport) -> GateResult:
    names = set(before.outcomes) | set(after.outcomes)
    differing = sum(1 for name in names if before.outcomes.get(name) != after.outcomes.get(name))
    detail = (
        f"既存のテスト: 基準 {len(before.outcomes)} 件（{_exit_text(before)}）、"
        f"修正後 {len(after.outcomes)} 件（{_exit_text(after)}）、成否の違い {differing} 件"
    )
    # 成否を確かめられなかった実行（時間切れ、結果なし、集めるときのエラーなど）どうしを「同じ」とは扱わない
    passed = (
        not before.timed_out
        and not after.timed_out
        and before.exit_code in _COMPLETED_EXIT_CODES
        and before.exit_code == after.exit_code
        and len(before.outcomes) >= 1
        and before.outcomes == after.outcomes
    )
    return GateResult("G7", passed, detail)


def _check_lint(before: int | None, after: int | None) -> GateResult:
    if before is None or after is None:
        return GateResult("G8", False, "リントの指摘の数を数えられなかった")
    return GateResult("G8", after <= before, f"リントの指摘: 基準 {before} 件、修正後 {after} 件")
