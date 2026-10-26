"""会话事件溯源（event sourcing）相关服务.

对外只暴露三样东西：
    event_store  —— 追加 / 读取 / 投影的唯一入口
    record_turn  —— 写一轮对话的上下文管理器
    PROJECTIONS  —— 投影注册表
"""

from app.services.events.projections import (
    MESSAGES,
    PROJECTIONS,
    SUMMARY,
    Projection,
    get_projection,
)
from app.services.events.store import (
    EventStore,
    VerificationResult,
    event_store,
)
from app.services.events.turn import (
    TurnHandle,
    record_turn,
)
from app.services.events.types import (
    EVENT_SPECS,
    EventDraft,
    EventRecord,
    EventSpec,
    EventType,
    TurnStatus,
    UnknownEventError,
)

__all__ = [
    "EVENT_SPECS",
    "EventDraft",
    "EventRecord",
    "EventSpec",
    "EventStore",
    "EventType",
    "MESSAGES",
    "PROJECTIONS",
    "Projection",
    "SUMMARY",
    "TurnHandle",
    "TurnStatus",
    "UnknownEventError",
    "VerificationResult",
    "event_store",
    "get_projection",
    "record_turn",
]
