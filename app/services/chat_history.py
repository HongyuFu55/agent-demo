"""会话历史的读模型（read model）.

M2 的目标：把"读历史"从 chat_message 表切到事件日志的 messages 投影。

切换期间的三条规则：

1. **投影优先**：`SESSION_EVENT_READ_SOURCE=projection`（默认）时，
   直接从事件日志折叠出消息列表，不再读 chat_message。
2. **消息表兜底**：某个会话一条事件都没有（事件日志上线前创建、又没跑过
   `--backfill`），回退去读 chat_message，并打一条 warning。
   兜底是有代价的（多一次查询、两条真值），所以它必须可见，不能悄悄发生。
3. **可回退**：把环境变量改回 `chat_message` 就回到旧路径，不需要改代码。
   投影一旦证明有问题，一个变量就能止血。

为什么每次读都重新折叠，而不是缓存？
    这是 M2 的刻意选择：先把"读对"这件事做成，再谈"读快"。
    折叠是 O(事件数) 的内存计算，几十轮对话的会话可以忽略不计；
    真正的性能瓶颈在长会话，那时再加 session_projection 缓存表（增量折叠）。
    先加缓存再切读路径，等于同时改两件事，出问题分不清是谁的锅。

为什么转换时要做清洗？
    历史是"已经存进去的数据"，而 Message 模型有校验（非空、无 <script>、
    长度 <= 10000）。旧路径直接把 DB 里的字符串塞进 Message，
    一条空回复或一条超长回复就会让整个 GET /messages 返回 500。
    读路径不该被单条脏数据打死，所以这里做"跳过 / 截断 / 去标签"，
    并且每次都记日志——清洗必须是看得见的。
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import (
    Iterable,
    Literal,
    Optional,
    Sequence,
)

from pydantic import ValidationError

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import chat_history_reads_total
from app.models.chat_message import ChatMessage
from app.schemas.chat import (
    _USER_MSG_MAX_LEN,
    Message,
)
from app.services.database import database_service
from app.services.events import (
    MESSAGES,
    EventRecord,
    EventStore,
    event_store,
)

ChatRole = Literal["user", "assistant", "system"]

# 与 Message 的校验保持一致：这类内容一律去掉，否则读取历史会 500
_SCRIPT_TAG_RE = re.compile(r"<script.*?>.*?</script>", re.IGNORECASE | re.DOTALL)


class HistorySource(str, Enum):
    """这条历史是从哪读出来的（会写进响应头，便于线上排查）."""

    PROJECTION = "projection"
    CHAT_MESSAGE = "chat_message"
    CHAT_MESSAGE_FALLBACK = "chat_message_fallback"


@dataclass(frozen=True)
class HistoryRead:
    """一次历史读取的结果.

    Attributes:
        messages: 展开成 user/assistant 交替的消息列表。
        source: 数据来源，见 HistorySource。
        event_count: 参与折叠的事件条数（来源为 chat_message 时为 0）。
    """

    messages: list[Message]
    source: HistorySource
    event_count: int = 0


def _coerce_role(value: object) -> ChatRole:
    """把事件里的 role 收敛到 Message 允许的三个字面量.

    参数：
        value: 事件载荷里的原始 role。

    返回：
        ChatRole: 合法角色；无法识别时按 assistant 处理。
    """
    if value == "user":
        return "user"
    if value == "system":
        return "system"
    if value not in (None, "assistant"):
        logger.warning("history_message_unknown_role", role=str(value))
    return "assistant"


def _to_message(role_value: object, content_value: object) -> Optional[Message]:
    """把存下来的 role/content 转成可返回的 Message.

    参数：
        role_value: 原始角色。
        content_value: 原始内容。

    返回：
        Optional[Message]: 转换成功返回消息；内容为空或校验不通过返回 None。
    """
    role = _coerce_role(role_value)
    content = str(content_value or "")

    if "\0" in content:
        content = content.replace("\0", "")

    if _SCRIPT_TAG_RE.search(content):
        logger.warning("history_message_script_tag_stripped", role=role)
        content = _SCRIPT_TAG_RE.sub("", content)

    if not content.strip():
        logger.warning("history_message_empty_content_skipped", role=role)
        return None

    if role != "system" and len(content) > _USER_MSG_MAX_LEN:
        logger.warning("history_message_truncated", role=role, length=len(content), limit=_USER_MSG_MAX_LEN)
        content = f"{content[: _USER_MSG_MAX_LEN - 1]}…"

    try:
        return Message(role=role, content=content)
    except ValidationError:
        logger.warning("history_message_skipped_invalid", role=role, length=len(content))
        return None


def messages_from_rows(rows: Iterable[ChatMessage]) -> list[Message]:
    """把 chat_message 行展开成 user/assistant 交替的消息列表.

    参数：
        rows: 按时间升序的问答记录。

    返回：
        list[Message]: 每条记录展开成"问 + 答"两条消息（空内容会被跳过）。
    """
    messages: list[Message] = []
    for row in rows:
        for role, content in (("user", row.question), ("assistant", row.answer)):
            message = _to_message(role, content)
            if message is not None:
                messages.append(message)
    return messages


def messages_from_projection(state: dict) -> list[Message]:
    """把 messages 投影状态转成响应模型.

    参数：
        state: `messages` 投影折叠出的状态。

    返回：
        list[Message]: 消息列表（空内容会被跳过）。
    """
    messages: list[Message] = []
    for item in state.get("messages", []):
        message = _to_message(item.get("role"), item.get("content"))
        if message is not None:
            messages.append(message)
    return messages


async def read_history(session_id: str, user_id: int) -> HistoryRead:
    """读取一个会话的聊天历史.

    参数：
        session_id: 会话 ID。
        user_id: 会话所属用户（兜底读 chat_message 时用于隔离）。

    返回：
        HistoryRead: 消息列表 + 数据来源。
    """
    if settings.SESSION_EVENT_READ_SOURCE == "chat_message":
        rows = await database_service.get_chat_messages(session_id, user_id)
        chat_history_reads_total.labels(source=HistorySource.CHAT_MESSAGE.value).inc()
        return HistoryRead(messages=messages_from_rows(rows), source=HistorySource.CHAT_MESSAGE)

    records: Sequence[EventRecord] = await event_store.read(session_id)

    if not records:
        # 一条事件都没有：这个会话的历史还停留在旧模型里
        rows = await database_service.get_chat_messages(session_id, user_id)
        if rows:
            logger.warning(
                "history_read_fallback_to_chat_message",
                session_id=session_id,
                rows=len(rows),
                hint="该会话没有事件日志，建议执行 scripts/replay_session.py --backfill",
            )
            chat_history_reads_total.labels(source=HistorySource.CHAT_MESSAGE_FALLBACK.value).inc()
            return HistoryRead(
                messages=messages_from_rows(rows),
                source=HistorySource.CHAT_MESSAGE_FALLBACK,
            )
        # 新会话、还没有任何对话
        chat_history_reads_total.labels(source=HistorySource.PROJECTION.value).inc()
        return HistoryRead(messages=[], source=HistorySource.PROJECTION)

    state = EventStore.fold_records(records, MESSAGES, balanced=True)
    chat_history_reads_total.labels(source=HistorySource.PROJECTION.value).inc()
    return HistoryRead(
        messages=messages_from_projection(state),
        source=HistorySource.PROJECTION,
        event_count=len(records),
    )
