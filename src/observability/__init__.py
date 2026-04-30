"""観測ポイントの構造化イベント書き込みヘルパー。

`EventEmitter` を使って orchestrator / Lambda Trigger / token_monitor /
feedback_processor から events DDB テーブルに best-effort で書き込む。

詳細設計は `.steering/20260430-workflow-observability/design.md` 参照。
"""

from src.observability.event_emitter import EventEmitter

__all__ = ["EventEmitter"]
