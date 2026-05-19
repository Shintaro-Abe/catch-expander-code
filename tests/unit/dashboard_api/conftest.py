"""dashboard_api Lambda モジュールの事前インポート。

unittest.mock.patch() はターゲットモジュールが sys.modules に存在しないと
AttributeError になる。各 Lambda の app.py を事前インポートすることで解決する。
"""
import importlib

_LAMBDA_APP_MODULES = [
    "src.dashboard_api.get_cost_summary.app",
    "src.dashboard_api.get_api_health.app",
    "src.dashboard_api.get_token_monitor_health.app",
    "src.dashboard_api.get_feedback_aggregation.app",
    "src.dashboard_api.get_metrics_summary.app",
    "src.dashboard_api.get_review_quality.app",
    "src.dashboard_api.get_errors.app",
    "src.dashboard_api.list_executions.app",
    "src.dashboard_api.get_my_profile.app",
]

for _mod in _LAMBDA_APP_MODULES:
    importlib.import_module(_mod)
