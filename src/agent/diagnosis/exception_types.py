"""例外の型の許可一覧。SDK には依存しない。

設計詳細: `.steering/20261004-execution-diagnostician/design.md` §18 の C1

イベントの `payload.error_type` は記録の中身（信頼できない入力）で、識別子の形をした任意の
文字列を入れられる。形だけを確かめて受け渡しに入れると、自由文字列の通路になる。例外の型は、
コード側の許可一覧にある名前だけを通す。
"""

from __future__ import annotations

import ast
import builtins
import os
import re
from pathlib import Path

MAX_SCANNED_FILES = 2_000
MAX_SOURCE_BYTES = 1_000_000

# 依存ライブラリの例外の、固定の一覧（botocore・slack_sdk・claude_agent_sdk・json・requests・subprocess）
LIBRARY_EXCEPTION_TYPES = frozenset(
    {
        "ClientError",
        "BotoCoreError",
        "EndpointConnectionError",
        "NoCredentialsError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "SlackApiError",
        "ClaudeSDKError",
        "CLINotFoundError",
        "CLIConnectionError",
        "ProcessError",
        "CLIJSONDecodeError",
        "JSONDecodeError",
        "HTTPError",
        "RequestException",
        "ConnectionError",
        "Timeout",
        "ReadTimeout",
        "ConnectTimeout",
        "SSLError",
        "TooManyRedirects",
        "CalledProcessError",
        "TimeoutExpired",
    }
)

_BUILTIN_EXCEPTION_TYPES = frozenset(
    name for name, value in vars(builtins).items() if isinstance(value, type) and issubclass(value, BaseException)
)
# 識別子の形で、80 字まで。検証は fullmatch で行う（前後の空白や改行のある値を通さない）
_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
# 走査しないディレクトリ（隠しディレクトリは、名前の先頭で見分ける）
_SKIPPED_DIRECTORIES = frozenset({"__pycache__", "node_modules"})


class ExceptionTypeAllowlist:
    """例外の型として受け渡しに入れてよい名前かを判定する。

    許可するのは、(1) 組み込みの例外クラスの名前、(2) 「コードの根」の下の `.py` に定義された
    クラスの名前（入れ子・サブディレクトリを含む。例外クラスかどうかは問わない）、
    (3) LIBRARY_EXCEPTION_TYPES。コードの根の走査は、必要になったときに 1 回だけ行い、結果を
    インスタンスの中で使い回す（診断 1 回につき 1 つ作る）。どのメソッドも例外を出さない。
    """

    def __init__(self, code_root: str | os.PathLike[str] | None = None) -> None:
        # 既定は diagnosis パッケージの親ディレクトリ（フレームの突き合わせと同じ根）
        root = Path(code_root) if code_root is not None else Path(__file__).resolve().parent.parent
        try:
            self._root: Path | None = root.resolve()
        except (OSError, RuntimeError):
            self._root = None
        self._code_classes: frozenset[str] | None = None

    def allows(self, name: object) -> bool:
        """許可一覧にある名前なら True。文字列でない値、形に合わない値は False。"""
        if not isinstance(name, str) or _NAME_PATTERN.fullmatch(name) is None:
            return False
        if name in _BUILTIN_EXCEPTION_TYPES or name in LIBRARY_EXCEPTION_TYPES:
            return True
        if self._code_classes is None:
            self._code_classes = frozenset(self._scan())
        return name in self._code_classes

    def _scan(self) -> set[str]:
        """コードの根の下の `.py` を解析して、クラス定義の名前を集める。上限を超えたぶんは飛ばす。"""
        names: set[str] = set()
        if self._root is None:
            return names
        scanned = 0
        try:
            # 走査の順を決めておく（上限に当たったとき、どのファイルが入るかが環境で変わらないようにする）
            for directory, subdirectories, files in os.walk(self._root):
                subdirectories[:] = sorted(
                    name for name in subdirectories if not name.startswith(".") and name not in _SKIPPED_DIRECTORIES
                )
                for file_name in sorted(files):
                    if not file_name.endswith(".py"):
                        continue
                    if scanned >= MAX_SCANNED_FILES:
                        return names
                    scanned += 1
                    names.update(self._class_names(Path(directory, file_name)))
        except OSError:
            return names
        return names

    def _class_names(self, path: Path) -> list[str]:
        """ファイル 1 つのクラス定義の名前を返す。解析できないファイル、根の外を指すものは空。"""
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self._root) or not resolved.is_file():
                return []
            with resolved.open("rb") as file:
                raw = file.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES:
                return []
            tree = ast.parse(raw.decode("utf-8"))
        except (OSError, ValueError, SyntaxError, RecursionError, MemoryError, RuntimeError):
            return []
        return [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
