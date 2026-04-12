"""
trigger ユニットテスト用 conftest。

app.py はモジュールレベルで boto3.client() / boto3.resource() を呼び出すため、
devcontainer 環境で login_session プロファイルが設定されている場合に
botocore[crt] 依存エラーが発生する。
app.py を最初にインポートする前に boto3 をモックに差し替えることで回避する。
"""

import sys
from unittest.mock import MagicMock

if "app" not in sys.modules:
    _real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = MagicMock()
    try:
        import app  # noqa: F401
    finally:
        if _real_boto3 is not None:
            sys.modules["boto3"] = _real_boto3
        else:
            sys.modules.pop("boto3", None)
