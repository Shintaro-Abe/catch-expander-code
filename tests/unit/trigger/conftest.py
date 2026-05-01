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
    # boto3.dynamodb.conditions はパッケージ階層として登録しないと from import が失敗する
    sys.modules["boto3.dynamodb"] = MagicMock()
    sys.modules["boto3.dynamodb.conditions"] = MagicMock()
    try:
        import app  # noqa: F401
    finally:
        if _real_boto3 is not None:
            sys.modules["boto3"] = _real_boto3
        else:
            sys.modules.pop("boto3", None)
        sys.modules.pop("boto3.dynamodb", None)
        sys.modules.pop("boto3.dynamodb.conditions", None)
        # T1-3 で追加した `from src.observability import EventEmitter` の連鎖で、
        # event_emitter モジュール内の `boto3` 参照がモック化された MagicMock に固着する。
        # 後続の tests/unit/observability/ で実 boto3 ベースの mock が効かなくなるため
        # ここで sys.modules から落として、observability テストで新規 import させる。
        sys.modules.pop("src.observability.event_emitter", None)
        sys.modules.pop("src.observability", None)
