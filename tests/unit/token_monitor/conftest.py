"""
token_monitor ユニットテスト用 conftest。

handler.py はモジュールレベルで boto3.client() を呼び出すため、
devcontainer 環境で AWS プロファイルが設定されている場合にエラーが発生する。
handler.py を最初にインポートする前に boto3 をモックに差し替えることで回避する。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

_TOKEN_MONITOR_SRC = str(Path(__file__).parents[3] / "src" / "token_monitor")

if "handler" not in sys.modules:
    _real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = MagicMock()
    sys.path.insert(0, _TOKEN_MONITOR_SRC)
    try:
        import handler  # noqa: F401
    finally:
        if _real_boto3 is not None:
            sys.modules["boto3"] = _real_boto3
        else:
            sys.modules.pop("boto3", None)
        if sys.path and sys.path[0] == _TOKEN_MONITOR_SRC:
            sys.path.pop(0)
