"""投影（projection）：把事件流折叠成可读的会话状态.

一句话：事件是**账本**，投影是**余额**。

账本（session_event）只追加、永远完整；余额（投影）是临时算出来的视图，
可以随时丢掉重算。所以投影函数必须是**纯函数**：

    apply(state, event) -> new_state

不许碰数据库、不许读时间、不许发网络请求。只要守住这条，
「任何一个历史时刻的会话状态」都等于"把前 N 条事件按顺序折叠一遍"。

加一个新的投影 = 写一个 init 和一个 apply，然后注册进 PROJECTIONS。
不需要改表结构，也不需要回填历史数据。
"""

from dataclasses import dataclass
from typing import (
    Any,
    Callable,
)

from app.services.events.types import (
    EventRecord,
    EventType,
)


@dataclass(frozen=True)
class Projection:
    """一个可注册的投影定义.

    Attributes:
        key: 投影名，也是 `replay()` 返回字典的键。
        state_version: 状态结构版本；改了 init 的字段就 +1，方便识别历史缓存。
        init: 空状态的构造函数（必须返回全新对象，不能共享可变默认值）。
        apply: 折叠函数，必须是纯函数。
    """

    key: str
    state_version: int
    init: Callable[[], dict[str, Any]]
    apply: Callable[[dict[str, Any], EventRecord], dict[str, Any]]


MESSAGES_KEY = "messages"
SUMMARY_KEY = "summary"


def _init_messages() -> dict[str, Any]:
    """消息投影的初始状态：一个空的消息列表."""
    return {"messages": []}


def _apply_messages(state: dict[str, Any], event: EventRecord) -> dict[str, Any]:
    """把 message/* 事件追加到消息列表.

    只认两种事件：
        message/user       -> role=user
        message/assistant  -> role=assistant
    其余事件（工具、异常、turn 边界）对"用户看到的对话"没有影响，原样返回。
    session/cleared 是唯一会清空列表的事件。
    """
    if event.type == EventType.SESSION_CLEARED.value:
        return _init_messages()

    if event.type == EventType.MESSAGE_USER.value:
        role = "user"
    elif event.type == EventType.MESSAGE_ASSISTANT.value:
        role = "assistant"
    else:
        return state

    message = {
        "role": role,
        "content": event.data.get("content", ""),
        "seq": event.seq,
        "turn_id": event.turn_id,
    }
    return {**state, "messages": [*state["messages"], message]}


def _init_summary() -> dict[str, Any]:
    """会话摘要投影的初始状态."""
    return {
        "turn_count": 0,
        "message_count": 0,
        "tool_call_count": 0,
        "error_count": 0,
        "name": None,
        "last_seq": 0,
    }


def _apply_summary(state: dict[str, Any], event: EventRecord) -> dict[str, Any]:
    """累计会话级计数，用于会话列表 / 监控面板.

    这类投影的价值在于：它不需要"消息表"，也不需要 join，
    直接从日志算出来，永远不会和消息投影对不上。
    """
    next_state = {**state, "last_seq": event.seq}

    if event.type == EventType.TURN_END.value:
        next_state["turn_count"] = state["turn_count"] + 1
    elif event.type in (EventType.MESSAGE_USER.value, EventType.MESSAGE_ASSISTANT.value):
        next_state["message_count"] = state["message_count"] + 1
    elif event.type == EventType.TOOL_START.value:
        next_state["tool_call_count"] = state["tool_call_count"] + 1
    elif event.type == EventType.ERROR_RAISED.value:
        next_state["error_count"] = state["error_count"] + 1
    elif event.type in (EventType.SESSION_CREATED.value, EventType.SESSION_RENAMED.value):
        next_state["name"] = event.data.get("name")

    return next_state


MESSAGES = Projection(
    key=MESSAGES_KEY,
    state_version=1,
    init=_init_messages,
    apply=_apply_messages,
)

SUMMARY = Projection(
    key=SUMMARY_KEY,
    state_version=1,
    init=_init_summary,
    apply=_apply_summary,
)


PROJECTIONS: dict[str, Projection] = {projection.key: projection for projection in (MESSAGES, SUMMARY)}


def get_projection(key: str) -> Projection:
    """按名字取投影定义.

    参数：
        key: 投影名，如 "messages" / "summary"。

    返回：
        Projection: 投影定义。

    抛出：
        KeyError: 投影未注册。
    """
    if key not in PROJECTIONS:
        raise KeyError(f"未注册的投影: {key}，可选: {sorted(PROJECTIONS)}")
    return PROJECTIONS[key]
