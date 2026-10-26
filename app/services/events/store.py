"""事件存储：追加、读取、折叠、回放.

这一层是整个改造的核心，但它故意做得很薄：
    append  —— 加一条（或一批）事件，唯一的写入口
    read    —— 按 seq 顺序读出事件
    fold    —— 把事件折叠成某个投影
    replay  —— 一次性折叠出所有投影

关于并发
--------
`seq` 要求会话内严格单调。做法是在同一个事务里先取
`pg_advisory_xact_lock(hashtext(session_id))`，把"读最大值 + 插入"串起来。
这样即使两个 worker 同时写同一个会话，也不会算出同一个 seq；
即使退化成冲突，(session_id, seq) 的唯一索引也会兜住。

关于阻塞
--------
项目现有 `DatabaseService` 用的是同步 SQLModel Session。为了不在 M1 就引入
第二套连接池，这里用 `asyncio.to_thread` 把同步调用挪出事件循环，
对调用方仍然是 async 接口。M2 换成 async engine 时，把每处的
`asyncio.to_thread(self._xxx_sync, ...)` 换成 `await` 即可，接口不用动。
"""

import asyncio
from dataclasses import dataclass
from typing import (
    Any,
    Iterable,
    Optional,
    Sequence,
)

from sqlalchemy import (
    Engine,
    func,
    text,
)
from sqlmodel import (
    Session,
    col,
    select,
)

from app.core.logging import logger
from app.models.chat_message import ChatMessage
from app.models.session_event import SessionEvent
from app.services.events.engine import get_engine
from app.services.events.projections import (
    MESSAGES,
    PROJECTIONS,
    Projection,
    get_projection,
)
from app.services.events.types import (
    EVENT_SPECS,
    EventDraft,
    EventRecord,
    EventType,
    UnknownEventError,
    require_spec,
)


@dataclass(frozen=True)
class VerificationResult:
    """投影与既有消息表的比对结果.

    Attributes:
        session_id: 会话 ID。
        expected: 由事件日志折叠出的消息序列。
        actual: chat_message 表里的消息序列。
        matches: 两者是否完全一致。
        first_mismatch: 第一处不一致的下标，一致时为 None。
    """

    session_id: str
    expected: list[dict[str, Any]]
    actual: list[dict[str, Any]]
    matches: bool
    first_mismatch: Optional[int]


def _to_record(row: SessionEvent) -> EventRecord:
    """ORM 行 -> 纯数据记录（投影层不接触 ORM）."""
    return EventRecord(
        id=row.id,
        session_id=row.session_id,
        seq=row.seq,
        turn_id=row.turn_id,
        type=row.type,
        data=dict(row.data or {}),
        ignorable=row.ignorable,
        schema_version=row.schema_version,
        created_at=row.created_at,
    )


def _drop_uncommitted_tail(records: Sequence[EventRecord]) -> list[EventRecord]:
    """丢弃"未提交"的尾巴.

    规则：找出最后一个 turn/start；如果它之后没有 turn/end，说明这一轮没走完
    （进程崩溃 / 客户端断流 / 异常中断），把它整段丢掉。前面的轮次照常保留。

    这就是"崩溃尾巴"的处理方式：不删数据，只是回放时不认它。
    """
    last_start: Optional[int] = None
    for index, record in enumerate(records):
        if record.type == EventType.TURN_START.value:
            last_start = index

    if last_start is None:
        return list(records)

    if any(record.type == EventType.TURN_END.value for record in records[last_start + 1 :]):
        return list(records)

    logger.warning(
        "event_log_uncommitted_tail_dropped",
        session_id=records[last_start].session_id,
        dropped_from_seq=records[last_start].seq,
        dropped_count=len(records) - last_start,
    )
    return list(records[:last_start])


class EventStore:
    """会话事件存储（append-only）."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        """初始化存储.

        参数：
            engine: 可选的自有 engine（测试注入用）；不传则复用全局数据库连接池。
        """
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """延迟取全局 engine，避免导入事件模块时就触发数据库初始化."""
        if self._engine is None:
            self._engine = get_engine()
        return self._engine

    # ------------------------------------------------------------------ 写入

    async def append(
        self,
        session_id: str,
        drafts: Iterable[EventDraft],
        *,
        turn_id: Optional[str] = None,
    ) -> list[EventRecord]:
        """追加一批事件（同一事务，全部成功或全部失败）.

        参数：
            session_id: 会话 ID。
            drafts: 待写入的事件；批次内顺序即 seq 顺序。
            turn_id: 批次默认 turn_id，单条 draft 自带 turn_id 时以 draft 为准。

        返回：
            list[EventRecord]: 已落库的事件（带 seq / id / created_at）。

        抛出：
            UnknownEventError: 出现未登记的事件类型。
        """
        prepared: list[tuple[EventDraft, str, bool, int]] = []
        for draft in drafts:
            spec = require_spec(draft.type)
            prepared.append((draft, spec.type.value, spec.ignorable, spec.schema_version))

        if not prepared:
            return []

        return await asyncio.to_thread(self._append_sync, session_id, prepared, turn_id)

    def _append_sync(
        self,
        session_id: str,
        prepared: list[tuple[EventDraft, str, bool, int]],
        turn_id: Optional[str],
    ) -> list[EventRecord]:
        """同步写入实现：会话级 advisory lock 保证 seq 连续."""
        with Session(self.engine) as db:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:sid))"), {"sid": session_id})
            current_seq = db.execute(
                text("SELECT COALESCE(MAX(seq), 0) FROM session_event WHERE session_id = :sid"),
                {"sid": session_id},
            ).scalar_one()

            rows: list[SessionEvent] = []
            for offset, (draft, event_type, ignorable, schema_version) in enumerate(prepared, start=1):
                rows.append(
                    SessionEvent(
                        session_id=session_id,
                        seq=int(current_seq) + offset,
                        turn_id=draft.turn_id or turn_id,
                        type=event_type,
                        data=draft.data,
                        ignorable=ignorable,
                        schema_version=schema_version,
                    )
                )

            db.add_all(rows)
            db.commit()
            for row in rows:
                db.refresh(row)

        records = [_to_record(row) for row in rows]
        logger.debug(
            "session_events_appended",
            session_id=session_id,
            count=len(records),
            from_seq=records[0].seq if records else None,
            types=[record.type for record in records],
        )
        return records

    # ------------------------------------------------------------------ 读取

    async def read(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        until_seq: Optional[int] = None,
        types: Optional[Sequence[EventType | str]] = None,
        limit: Optional[int] = None,
    ) -> list[EventRecord]:
        """按 seq 顺序读取事件.

        参数：
            session_id: 会话 ID。
            after_seq: 只读 seq 大于它的事件（增量同步用）。
            until_seq: 只读 seq 小于等于它的事件（历史快照用）。
            types: 只看这些类型。
            limit: 最多返回多少条。

        返回：
            list[EventRecord]: 按 seq 升序的事件。
        """
        type_values = [item.value if isinstance(item, EventType) else item for item in types] if types else None
        return await asyncio.to_thread(
            self._read_sync, session_id, after_seq, until_seq, type_values, limit
        )

    def _read_sync(
        self,
        session_id: str,
        after_seq: int,
        until_seq: Optional[int],
        type_values: Optional[list[str]],
        limit: Optional[int],
    ) -> list[EventRecord]:
        """同步读取实现."""
        with Session(self.engine) as db:
            statement = select(SessionEvent).where(
                col(SessionEvent.session_id) == session_id,
                col(SessionEvent.seq) > after_seq,
            )
            if until_seq is not None:
                statement = statement.where(col(SessionEvent.seq) <= until_seq)
            if type_values:
                statement = statement.where(col(SessionEvent.type).in_(type_values))
            statement = statement.order_by(col(SessionEvent.seq))
            if limit is not None:
                statement = statement.limit(limit)

            return [_to_record(row) for row in db.exec(statement).all()]

    async def last_seq(self, session_id: str) -> int:
        """返回会话当前最大 seq（没有事件时为 0）."""
        return await asyncio.to_thread(self._last_seq_sync, session_id)

    def _last_seq_sync(self, session_id: str) -> int:
        """同步实现."""
        with Session(self.engine) as db:
            value = db.exec(
                select(func.coalesce(func.max(SessionEvent.seq), 0)).where(
                    col(SessionEvent.session_id) == session_id
                )
            ).one()
            return int(value or 0)

    async def committed_tail(self, session_id: str) -> Optional[int]:
        """返回最后一条 turn/end 的 seq（没有已提交轮次时为 None）."""
        return await asyncio.to_thread(self._committed_tail_sync, session_id)

    def _committed_tail_sync(self, session_id: str) -> Optional[int]:
        """同步实现."""
        with Session(self.engine) as db:
            value = db.exec(
                select(func.max(SessionEvent.seq)).where(
                    col(SessionEvent.session_id) == session_id,
                    col(SessionEvent.type) == EventType.TURN_END.value,
                )
            ).one()
            return int(value) if value is not None else None

    # ------------------------------------------------------------------ 投影

    @staticmethod
    def fold_records(
        records: Sequence[EventRecord],
        projection: Projection,
        *,
        balanced: bool = False,
    ) -> dict[str, Any]:
        """把一批事件折叠成一个投影状态（纯计算，不碰数据库）.

        参数：
            records: 按 seq 升序的事件。
            projection: 投影定义。
            balanced: 是否丢弃未提交的尾巴（见 _drop_uncommitted_tail）。

        返回：
            dict: 投影状态。

        抛出：
            UnknownEventError: 遇到不认识的、且标记为不可跳过的事件类型。
        """
        selected = _drop_uncommitted_tail(records) if balanced else list(records)

        state = projection.init()
        for record in selected:
            if record.type not in EVENT_SPECS:
                if record.ignorable:
                    # 老代码遇到新事件：跳过，但记一笔，方便排查"为什么状态不一样"
                    logger.debug(
                        "session_event_skipped_unknown_ignorable",
                        session_id=record.session_id,
                        seq=record.seq,
                        type=record.type,
                    )
                    continue
                raise UnknownEventError(
                    f"会话 {record.session_id} 第 {record.seq} 条事件类型 {record.type} 未登记且不可忽略"
                )
            state = projection.apply(state, record)

        return state

    async def fold(
        self,
        session_id: str,
        projection: Projection | str,
        *,
        upto_seq: Optional[int] = None,
        balanced: bool = False,
    ) -> dict[str, Any]:
        """折叠出会话的某个投影状态.

        参数：
            session_id: 会话 ID。
            projection: 投影定义或投影名。
            upto_seq: 只折叠到某个 seq（重放历史时刻）。
            balanced: 是否跳过未提交的尾巴。

        返回：
            dict: 投影状态。
        """
        resolved = get_projection(projection) if isinstance(projection, str) else projection
        records = await self.read(session_id, until_seq=upto_seq)
        return self.fold_records(records, resolved, balanced=balanced)

    async def replay(
        self,
        session_id: str,
        *,
        keys: Optional[Sequence[str]] = None,
        upto_seq: Optional[int] = None,
        balanced: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """一次性折叠出多个投影（只读一次日志）.

        参数：
            session_id: 会话 ID。
            keys: 投影名列表，默认全部已注册投影。
            upto_seq: 只折叠到某个 seq。
            balanced: 是否跳过未提交的尾巴。

        返回：
            dict[str, dict]: 投影名 -> 状态。
        """
        selected = {key: get_projection(key) for key in keys} if keys else dict(PROJECTIONS)
        records = await self.read(session_id, until_seq=upto_seq)
        return {
            key: self.fold_records(records, projection, balanced=balanced)
            for key, projection in selected.items()
        }

    # ------------------------------------------------------------------ 校验

    async def verify_messages(
        self,
        session_id: str,
        user_id: int,
        *,
        balanced: bool = True,
    ) -> VerificationResult:
        """比对 messages 投影与 chat_message 表，确认两者一致.

        这是影子写入阶段的验收手段：只要所有会话都 matches，
        就可以把读路径从 chat_message 切到投影。

        默认 balanced=True：只比较"已提交的轮次"。一轮对话如果写到一半就崩溃，
        事件日志里有残留、chat_message 里没有对应记录，这属于正常现象，
        不应该被报成不一致；真正的不一致是"双方都认为这轮结束了，但内容不同"。

        参数：
            session_id: 会话 ID。
            user_id: 会话所属用户（用于按用户隔离查 chat_message）。
            balanced: 是否丢弃未提交的尾巴再比较。

        返回：
            VerificationResult: 比对结果。
        """
        state = await self.fold(session_id, MESSAGES, balanced=balanced)
        expected = [
            {"role": message["role"], "content": message["content"]}
            for message in state.get("messages", [])
        ]
        actual = await asyncio.to_thread(self._read_chat_messages_sync, session_id, user_id)

        first_mismatch: Optional[int] = None
        for index in range(max(len(expected), len(actual))):
            left = expected[index] if index < len(expected) else None
            right = actual[index] if index < len(actual) else None
            if left != right:
                first_mismatch = index
                break

        return VerificationResult(
            session_id=session_id,
            expected=expected,
            actual=actual,
            matches=first_mismatch is None,
            first_mismatch=first_mismatch,
        )

    def _read_chat_messages_sync(self, session_id: str, user_id: int) -> list[dict[str, Any]]:
        """同步读取既有消息表，展开成 role/content 序列."""
        with Session(self.engine) as db:
            statement = (
                select(ChatMessage)
                .where(
                    col(ChatMessage.session_id) == session_id,
                    col(ChatMessage.user_id) == user_id,
                )
                .order_by(col(ChatMessage.created_at), col(ChatMessage.id))
            )
            rows = db.exec(statement).all()

        flattened: list[dict[str, Any]] = []
        for row in rows:
            flattened.append({"role": "user", "content": row.question})
            flattened.append({"role": "assistant", "content": row.answer})
        return flattened

    # ------------------------------------------------------------------ 运维

    async def delete_session_events(self, session_id: str) -> int:
        """删除某会话的全部事件（仅在会话本身被删除时调用）.

        参数：
            session_id: 会话 ID。

        返回：
            int: 删除条数。
        """
        return await asyncio.to_thread(self._delete_sync, session_id)

    def _delete_sync(self, session_id: str) -> int:
        """同步删除实现."""
        with Session(self.engine) as db:
            rows = db.exec(select(SessionEvent).where(col(SessionEvent.session_id) == session_id)).all()
            for row in rows:
                db.delete(row)
            db.commit()
            return len(rows)


# 全局单例：engine 懒加载，路由层直接用
event_store = EventStore()

__all__ = [
    "EventStore",
    "VerificationResult",
    "event_store",
]
