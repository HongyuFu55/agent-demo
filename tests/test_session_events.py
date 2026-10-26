"""会话事件日志与投影的单元测试.

这些测试不连数据库：直接构造 EventRecord，验证"折叠"这个纯函数的行为。
事件溯源的好处之一就在这里 —— 核心逻辑可以脱离任何基础设施被测试。
"""

import asyncio
from datetime import (
    UTC,
    datetime,
)

import pytest

from app.services.events import (
    EVENT_SPECS,
    MESSAGES,
    SUMMARY,
    EventDraft,
    EventRecord,
    EventType,
    UnknownEventError,
)
from app.services.events.store import EventStore


def make_record(
    seq: int,
    event_type: EventType,
    data: dict | None = None,
    *,
    turn_id: str = "turn-1",
    ignorable: bool | None = None,
    type_name: str | None = None,
) -> EventRecord:
    """构造一条已持久化事件的测试替身."""
    spec = EVENT_SPECS[event_type.value]
    return EventRecord(
        id=seq,
        session_id="session-1",
        seq=seq,
        turn_id=turn_id,
        type=type_name or event_type.value,
        data=data or {},
        ignorable=spec.ignorable if ignorable is None else ignorable,
        schema_version=spec.schema_version,
        created_at=datetime.now(UTC),
    )


def test_every_event_type_is_registered() -> None:
    """枚举里的每个类型都必须在注册表中，否则写入时会被拒绝."""
    assert {event_type.value for event_type in EventType} == set(EVENT_SPECS)


def test_messages_projection_pairs_user_and_assistant() -> None:
    """一轮完整的对话折叠成两条消息."""
    records = [
        make_record(1, EventType.TURN_START, {"source": "chat"}),
        make_record(2, EventType.MESSAGE_USER, {"content": "你好"}),
        make_record(3, EventType.MESSAGE_ASSISTANT, {"content": "你好，有什么可以帮你？"}),
        make_record(4, EventType.TURN_END, {"status": "ok"}),
    ]

    state = EventStore.fold_records(records, MESSAGES)

    assert [message["role"] for message in state["messages"]] == ["user", "assistant"]
    assert [message["content"] for message in state["messages"]] == ["你好", "你好，有什么可以帮你？"]
    assert [message["seq"] for message in state["messages"]] == [2, 3]


def test_tool_events_do_not_change_messages() -> None:
    """工具事件是观测信息，不应污染用户可见的消息投影."""
    records = [
        make_record(1, EventType.TURN_START),
        make_record(2, EventType.MESSAGE_USER, {"content": "今天天气"}),
        make_record(3, EventType.TOOL_START, {"name": "get_weather"}),
        make_record(4, EventType.TOOL_END, {"name": "get_weather", "ok": True}),
        make_record(5, EventType.MESSAGE_ASSISTANT, {"content": "晴"}),
        make_record(6, EventType.TURN_END, {"status": "ok"}),
    ]

    state = EventStore.fold_records(records, MESSAGES)

    assert [message["role"] for message in state["messages"]] == ["user", "assistant"]


def test_session_cleared_resets_messages() -> None:
    """清空历史是一条事件，而不是删除数据：折叠到这里时消息列表归零."""
    records = [
        make_record(1, EventType.MESSAGE_USER, {"content": "旧问题"}),
        make_record(2, EventType.MESSAGE_ASSISTANT, {"content": "旧回答"}),
        make_record(3, EventType.SESSION_CLEARED, {}),
        make_record(4, EventType.MESSAGE_USER, {"content": "新问题"}),
    ]

    state = EventStore.fold_records(records, MESSAGES)

    assert len(records) == 4
    assert [message["content"] for message in state["messages"]] == ["新问题"]


def test_summary_counts_turns_and_messages() -> None:
    """摘要投影统计轮次、消息数、工具调用与异常数."""
    records = [
        make_record(1, EventType.SESSION_CREATED, {"name": "测试会话"}),
        make_record(2, EventType.TURN_START),
        make_record(3, EventType.MESSAGE_USER, {"content": "hi"}),
        make_record(4, EventType.TOOL_START, {"name": "search"}),
        make_record(5, EventType.TOOL_END, {"name": "search"}),
        make_record(6, EventType.MESSAGE_ASSISTANT, {"content": "hello"}),
        make_record(7, EventType.TURN_END, {"status": "ok"}),
        make_record(8, EventType.ERROR_RAISED, {"kind": "boom"}),
    ]

    state = EventStore.fold_records(records, SUMMARY)

    assert state["turn_count"] == 1
    assert state["message_count"] == 2
    assert state["tool_call_count"] == 1
    assert state["error_count"] == 1
    assert state["name"] == "测试会话"
    assert state["last_seq"] == 8


def test_balanced_drops_uncommitted_tail() -> None:
    """最后一轮没有 turn/end 时，balanced 折叠应丢弃这一整段尾巴."""
    records = [
        make_record(1, EventType.TURN_START),
        make_record(2, EventType.MESSAGE_USER, {"content": "第一问"}),
        make_record(3, EventType.MESSAGE_ASSISTANT, {"content": "第一答"}),
        make_record(4, EventType.TURN_END, {"status": "ok"}),
        # 第二轮写到一半就"崩溃"了：只有 start 和 user，没有 end
        make_record(5, EventType.TURN_START),
        make_record(6, EventType.MESSAGE_USER, {"content": "第二问（没答完）"}),
    ]

    unbalanced = EventStore.fold_records(records, MESSAGES)
    balanced = EventStore.fold_records(records, MESSAGES, balanced=True)

    assert len(unbalanced["messages"]) == 3
    assert [message["content"] for message in balanced["messages"]] == ["第一问", "第一答"]


def test_balanced_keeps_complete_turns() -> None:
    """所有轮次都完整时，balanced 不应改动任何事件."""
    records = [
        make_record(1, EventType.TURN_START),
        make_record(2, EventType.MESSAGE_USER, {"content": "问"}),
        make_record(3, EventType.MESSAGE_ASSISTANT, {"content": "答"}),
        make_record(4, EventType.TURN_END, {"status": "ok"}),
    ]

    state = EventStore.fold_records(records, MESSAGES, balanced=True)

    assert [message["content"] for message in state["messages"]] == ["问", "答"]


def test_unknown_ignorable_event_is_skipped() -> None:
    """老代码遇到标记为可跳过的新事件：跳过它，其余事件照常折叠."""
    records = [
        make_record(1, EventType.MESSAGE_USER, {"content": "问"}),
        make_record(2, EventType.ERROR_RAISED, {"kind": "future"}, type_name="future/telemetry", ignorable=True),
        make_record(3, EventType.MESSAGE_ASSISTANT, {"content": "答"}),
    ]

    state = EventStore.fold_records(records, MESSAGES)

    assert [message["content"] for message in state["messages"]] == ["问", "答"]


def test_unknown_non_ignorable_event_raises() -> None:
    """不认识又不可跳过的事件必须让回放失败，而不是悄悄少一条语义."""
    records = [
        make_record(1, EventType.MESSAGE_USER, {"content": "问"}),
        make_record(2, EventType.ERROR_RAISED, {"kind": "future"}, type_name="future/critical", ignorable=False),
        make_record(3, EventType.MESSAGE_ASSISTANT, {"content": "答"}),
    ]

    with pytest.raises(UnknownEventError):
        EventStore.fold_records(records, MESSAGES)


def test_fold_is_pure() -> None:
    """投影是纯函数：折叠两次结果相同，且不会修改输入事件."""
    records = [
        make_record(1, EventType.MESSAGE_USER, {"content": "问"}),
        make_record(2, EventType.MESSAGE_ASSISTANT, {"content": "答"}),
    ]
    snapshot = [record.data.copy() for record in records]

    first = EventStore.fold_records(records, MESSAGES)
    second = EventStore.fold_records(records, MESSAGES)

    assert first == second
    assert [record.data for record in records] == snapshot
    # 两次调用返回的对象不能是同一个（否则会互相污染）
    assert first is not second
    assert first["messages"] is not second["messages"]


def test_append_rejects_unregistered_type() -> None:
    """写入未登记的事件类型应当在触库之前就失败（fail fast）."""
    store = EventStore()

    with pytest.raises(UnknownEventError):
        asyncio.run(store.append("session-1", [EventDraft("not/a/real/type")]))  # type: ignore[arg-type]
