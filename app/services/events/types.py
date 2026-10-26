"""会话事件的类型字典与领域对象.

这个文件是事件契约的"唯一事实来源"：写入方（chatbot / auth）和读取方
（投影 / replay 脚本）都从这里取类型与 schema 版本，避免两边各写一份字符串。

事件字典（v1）
---------------
会话级：
    session/created     会话创建      {user_id, name, username}
    session/renamed     会话改名      {name, previous_name, source}
    session/cleared     清空历史      {}
轮次级（一对 turn/start … turn/end 构成一个"已提交事务"）：
    turn/start          一轮开始      {source}
    turn/end            一轮结束      {status, duration_ms}
消息级：
    message/user        用户消息      {content}
    message/assistant   助手消息      {content, model}
工具级：
    tool/start          工具开始      {tool_call_id, name, args}
    tool/end            工具结束      {tool_call_id, name, ok, duration_ms, result_preview}
异常级：
    error/raised        运行异常      {kind, message}

为什么 turn/start 和 turn/end 要单独存在？
    因为"已提交"的边界必须可判定。进程在第 37 条事件后崩溃，第 37 条可能只写了
    半轮对话。回放时先找到最后一条 turn/end，只认它之前的事件（见
    EventStore.fold(balanced=True)），这就是"丢弃崩溃尾巴"。
"""

from dataclasses import (
    dataclass,
    field,
)
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Optional,
)


class EventType(str, Enum):
    """v1 事件类型枚举.

    值用 "域/动作" 的形式命名：斜杠前是聚合根（session / turn / message / tool），
    斜杠后是在该聚合根上发生的事实。
    """

    SESSION_CREATED = "session/created"
    SESSION_RENAMED = "session/renamed"
    SESSION_CLEARED = "session/cleared"

    TURN_START = "turn/start"
    TURN_END = "turn/end"

    MESSAGE_USER = "message/user"
    MESSAGE_ASSISTANT = "message/assistant"

    TOOL_START = "tool/start"
    TOOL_END = "tool/end"

    ERROR_RAISED = "error/raised"


class TurnStatus(str, Enum):
    """一轮对话的终态."""

    OK = "ok"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class EventSpec:
    """一种事件的元信息（写进注册表，供读写双方共用）.

    Attributes:
        type: 事件类型。
        schema_version: payload 结构版本。
        ignorable: 老读者不认识该类型时能否安全跳过。
        purpose: 一句话说明，出现在 `python -m scripts.replay_session --dict` 里。
    """

    type: EventType
    schema_version: int
    ignorable: bool
    purpose: str


# 事件注册表：写错类型的 append 会在写入前就抛错，而不是在三个月后的回放里才炸。
EVENT_SPECS: dict[str, EventSpec] = {
    spec.type.value: spec
    for spec in (
        EventSpec(
            EventType.SESSION_CREATED,
            schema_version=1,
            ignorable=False,
            purpose="会话创建，携带 owner 与初始名称",
        ),
        EventSpec(
            EventType.SESSION_RENAMED,
            schema_version=1,
            ignorable=False,
            purpose="会话改名（人工改名或自动命名）",
        ),
        EventSpec(
            EventType.SESSION_CLEARED,
            schema_version=1,
            ignorable=False,
            purpose="用户清空聊天历史",
        ),
        EventSpec(
            EventType.TURN_START,
            schema_version=1,
            ignorable=False,
            purpose="一轮对话开始，也是崩溃尾巴的起始边界",
        ),
        EventSpec(
            EventType.TURN_END,
            schema_version=1,
            ignorable=False,
            purpose="一轮对话结束，标志着该轮已提交",
        ),
        EventSpec(
            EventType.MESSAGE_USER,
            schema_version=1,
            ignorable=False,
            purpose="用户消息正文",
        ),
        EventSpec(
            EventType.MESSAGE_ASSISTANT,
            schema_version=1,
            ignorable=False,
            purpose="助手回复正文（已剥离工具信令的纯文本）",
        ),
        EventSpec(
            EventType.TOOL_START,
            schema_version=1,
            ignorable=True,
            purpose="工具调用开始；纯观测信息，缺失不影响消息投影",
        ),
        EventSpec(
            EventType.TOOL_END,
            schema_version=1,
            ignorable=True,
            purpose="工具调用结束；纯观测信息，缺失不影响消息投影",
        ),
        EventSpec(
            EventType.ERROR_RAISED,
            schema_version=1,
            ignorable=True,
            purpose="本轮运行异常；不影响已生成的消息投影",
        ),
    )
}


class UnknownEventError(RuntimeError):
    """回放时遇到不可跳过的事件类型.

    这种情况必须让调用方知道：要么升级代码，要么显式忽略。
    静默丢弃一个非 ignorable 的事件，等于让投影和历史真相悄悄分叉。
    """


@dataclass(frozen=True)
class EventDraft:
    """待写入的事件（还没有 seq / id / created_at）."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class EventRecord:
    """已持久化的事件（回放时的输入，纯数据、不依赖 ORM）."""

    id: int
    session_id: str
    seq: int
    turn_id: Optional[str]
    type: str
    data: dict[str, Any]
    ignorable: bool
    schema_version: int
    created_at: datetime


def spec_for(event_type: str) -> Optional[EventSpec]:
    """按类型取注册表条目；未知类型返回 None."""
    return EVENT_SPECS.get(event_type)


def require_spec(event_type: EventType | str) -> EventSpec:
    """写入前校验事件类型已登记.

    参数：
        event_type: 事件类型枚举或字符串。

    返回：
        EventSpec: 对应注册表条目。

    抛出：
        UnknownEventError: 类型未登记（通常是拼写错误或漏了注册）。
    """
    key = event_type.value if isinstance(event_type, EventType) else event_type
    spec = EVENT_SPECS.get(key)
    if spec is None:
        raise UnknownEventError(f"未登记的事件类型: {key}")
    return spec
