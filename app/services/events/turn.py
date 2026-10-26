"""把"一轮对话"写成一组事件.

用法：

    async with record_turn(session_id=sid, user_message=text, source="chat_stream") as turn:
        await turn.assistant(answer, model=model_name)

上下文管理器负责三件事：
    1. 进入时写 turn/start + message/user（同一事务，保证不会出现半条用户消息）；
    2. 退出时写 turn/end（成功 / 异常 / 中断）；
    3. 生成器被客户端断流打断时，尽力补一条 turn/end —— 补不上也没关系，
       回放时 balanced=True 会把这段尾巴整个丢掉，不会污染投影。

为什么写入失败不让请求失败？
    事件日志是"记录"，不是"回答"。表缺失、连接抖动不应该让用户拿不到答案。
    但也不能静默：每次失败都打 exception 日志 + 计数器 +1，
    这样监控面板上能立刻看到，而不是等三个月后发现投影和消息表对不上。
"""

import time
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Optional,
)
from uuid import uuid4

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import (
    session_event_append_failures_total,
    session_events_appended_total,
)
from app.services.events.store import event_store
from app.services.events.types import (
    EventDraft,
    EventType,
    TurnStatus,
)

# 工具入参 / 结果写进日志前的截断长度，避免一条事件被大 payload 撑爆
_PREVIEW_LIMIT = 600


def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    """把任意值压成一段可入日志的短文本."""
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…<truncated:{len(text) - limit}>"


class TurnHandle:
    """一轮对话的写入句柄（面向业务代码的窄接口）."""

    def __init__(
        self,
        session_id: str,
        turn_id: str,
        source: str,
        *,
        enabled: bool = True,
    ) -> None:
        """初始化句柄.

        参数：
            session_id: 会话 ID。
            turn_id: 本轮唯一 ID，贯穿本轮所有事件。
            source: 触发来源（chat / chat_stream / backfill）。
            enabled: 关闭时所有写入方法退化为空操作。
        """
        self.session_id = session_id
        self.turn_id = turn_id
        self.source = source
        self.enabled = enabled
        self.status = TurnStatus.OK
        self._started_at = time.perf_counter()
        self._finished = False

    # ------------------------------------------------------------ 内部

    async def _append(self, drafts: list[EventDraft]) -> bool:
        """写一批事件；失败只记录不抛出."""
        if not self.enabled:
            return False
        try:
            await event_store.append(self.session_id, drafts, turn_id=self.turn_id)
        except Exception:
            session_event_append_failures_total.labels(type=drafts[0].type.value).inc()
            logger.exception(
                "session_event_append_failed",
                session_id=self.session_id,
                turn_id=self.turn_id,
                types=[draft.type.value for draft in drafts],
            )
            return False

        for draft in drafts:
            session_events_appended_total.labels(type=draft.type.value).inc()
        return True

    # ------------------------------------------------------------ 生命周期

    async def start(self, user_message: str) -> None:
        """写入 turn/start 与 message/user."""
        await self._append(
            [
                EventDraft(EventType.TURN_START, {"source": self.source}),
                EventDraft(EventType.MESSAGE_USER, {"content": user_message}),
            ]
        )

    async def finish(self) -> None:
        """写入 turn/end（幂等：重复调用只写一次）."""
        if self._finished:
            return
        self._finished = True
        duration_ms = round((time.perf_counter() - self._started_at) * 1000, 2)
        await self._append(
            [EventDraft(EventType.TURN_END, {"status": self.status.value, "duration_ms": duration_ms})]
        )

    # ------------------------------------------------------------ 业务事件

    async def assistant(self, content: str, *, model: Optional[str] = None) -> None:
        """记录助手回复的纯文本（已剥离工具信令）."""
        await self._append([EventDraft(EventType.MESSAGE_ASSISTANT, {"content": content, "model": model})])

    async def tool_started(
        self,
        *,
        tool_call_id: Optional[str],
        name: str,
        args: Any = None,
    ) -> None:
        """记录工具开始调用."""
        await self._append(
            [
                EventDraft(
                    EventType.TOOL_START,
                    {"tool_call_id": tool_call_id, "name": name, "args": _preview(args)},
                )
            ]
        )

    async def tool_finished(
        self,
        *,
        tool_call_id: Optional[str],
        name: Optional[str],
        ok: bool = True,
        duration_ms: Optional[float] = None,
        result_preview: Any = None,
    ) -> None:
        """记录工具执行完毕（只存摘要，避免把完整工具输出灌进日志）."""
        await self._append(
            [
                EventDraft(
                    EventType.TOOL_END,
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "ok": ok,
                        "duration_ms": duration_ms,
                        "result_preview": _preview(result_preview),
                    },
                )
            ]
        )

    async def error(self, *, kind: str, message: str) -> None:
        """记录本轮异常，并把 turn 状态置为 error."""
        self.status = TurnStatus.ERROR
        await self._append(
            [EventDraft(EventType.ERROR_RAISED, {"kind": kind, "message": _preview(message, 1000)})]
        )

    def interrupted(self, reason: str) -> None:
        """标记本轮因中断（断流 / 等待人工审批）而未正常完成."""
        self.status = TurnStatus.INTERRUPTED
        logger.info("turn_interrupted", session_id=self.session_id, turn_id=self.turn_id, reason=reason)


@asynccontextmanager
async def record_turn(
    *,
    session_id: str,
    user_message: str,
    source: str = "chat",
    turn_id: Optional[str] = None,
) -> AsyncIterator[TurnHandle]:
    """把一段对话包成"已提交的一轮".

    参数：
        session_id: 会话 ID。
        user_message: 本轮用户输入。
        source: 触发来源。
        turn_id: 可选的自定义轮次 ID，默认自动生成。

    产出：
        TurnHandle: 供业务代码追加 assistant / tool / error 事件。
    """
    handle = TurnHandle(
        session_id=session_id,
        turn_id=turn_id or uuid4().hex,
        source=source,
        enabled=settings.SESSION_EVENTS_ENABLED,
    )

    await handle.start(user_message)
    try:
        yield handle
    except Exception as exc:
        await handle.error(kind=exc.__class__.__name__, message=str(exc))
        raise
    finally:
        await handle.finish()
