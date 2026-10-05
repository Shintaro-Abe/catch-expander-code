"""例外の型の許可一覧（修正役の側の再検証）。SDK には依存しない。

設計詳細: `.steering/20261004-execution-fixer/design.md` §4.3

受け渡しのファイルは人が運ぶので、「識別子の形をしている」だけでは通さない。例外の型は、
組み込みの例外クラスの名前、複製の `src/` の下のコードで定義されたクラスの名前、依存ライブラリの
決まった一覧のどれかにある名前だけを通す。

依存ライブラリの一覧は、第 1 段階の定数（`LIBRARY_EXCEPTION_TYPES`）をそのまま使う（一覧が 2 か所で
ずれないようにする）。複製のコードの走査は、根と返す形が違うので、こちらに持つ。
"""

from __future__ import annotations

import ast
import builtins
import os
import re
from pathlib import Path

from diagnosis.exception_types import LIBRARY_EXCEPTION_TYPES

MAX_SCANNED_FILES = 2_000
MAX_SOURCE_BYTES = 1_000_000
SOURCE_DIRECTORY = "src"

_BUILTIN_EXCEPTION_TYPES = frozenset(
    name for name, value in vars(builtins).items() if isinstance(value, type) and issubclass(value, BaseException)
)
# 識別子の形で、80 字まで。検証は fullmatch で行う（前後の空白や改行のある値を通さない）
_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
# 走査しないディレクトリ（隠しディレクトリは、名前の先頭で見分ける）
_SKIPPED_DIRECTORIES = frozenset({"__pycache__", "node_modules"})


def allowed_exception_types(workspace_root: Path) -> frozenset[str]:
    """例外の型として通す名前の一覧を返す。例外を出さない。"""
    return _BUILTIN_EXCEPTION_TYPES | LIBRARY_EXCEPTION_TYPES | _code_class_names(workspace_root)


def is_known_exception_type(name: object, workspace_root: Path) -> bool:
    """許可一覧にある名前なら True。None、文字列でない値、形に合わない値は False。"""
    if not isinstance(name, str) or _NAME_PATTERN.fullmatch(name) is None:
        return False
    if name in _BUILTIN_EXCEPTION_TYPES or name in LIBRARY_EXCEPTION_TYPES:
        return True
    return name in _code_class_names(workspace_root)


def _code_class_names(workspace_root: Path) -> frozenset[str]:
    """複製の `src/` の下の `.py` を解析して、クラス定義の名前を集める。上限を超えたぶんは飛ばす。"""
    names: set[str] = set()
    try:
        root = Path(workspace_root, SOURCE_DIRECTORY).resolve()
        # `src` が複製の外を指すシンボリックリンクなら、読まない
        if not root.is_relative_to(Path(workspace_root).resolve()):
            return frozenset()
    except (OSError, RuntimeError, TypeError):
        return frozenset()
    scanned = 0
    try:
        # 走査の順を決めておく（上限に当たったとき、どのファイルが入るかが環境で変わらないようにする）
        for directory, subdirectories, files in os.walk(root):
            subdirectories[:] = sorted(
                name for name in subdirectories if not name.startswith(".") and name not in _SKIPPED_DIRECTORIES
            )
            for file_name in sorted(files):
                if not file_name.endswith(".py"):
                    continue
                if scanned >= MAX_SCANNED_FILES:
                    return frozenset(names)
                scanned += 1
                names.update(_class_names(Path(directory, file_name), root))
    except OSError:
        return frozenset(names)
    return frozenset(names)


def _class_names(path: Path, root: Path) -> list[str]:
    """ファイル 1 つのクラス定義の名前を返す。解析できないファイル、根の外を指すものは空。"""
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return []
        with resolved.open("rb") as file:
            raw = file.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            return []
        tree = ast.parse(raw.decode("utf-8"))
    except (OSError, ValueError, SyntaxError, RecursionError, MemoryError, RuntimeError):
        return []
    return [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
