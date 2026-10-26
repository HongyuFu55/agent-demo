"""会话事件日志（append-only ledger）的 ORM 模型.

设计要点（对照 DSH 的 event-sourced session）：

1. 只追加，不更新、不删除（唯一例外：会话本身被删除时级联清理）。
2. `seq` 是"会话内"单调递增序号，不是全局 ID。回放时按 (session_id, seq) 排序，
   不依赖 created_at —— 同一毫秒内的多条事件也能稳定排序。
3. `type` 是字符串而不是数据库枚举。事件类型会随版本演进，用枚举意味着每加一个
   类型就要 ALTER TYPE，而字符串 + 代码侧注册表（app/services/events/types.py）
   可以把兼容性判断放在应用层。
4. `ignorable` 在写入时固化。老版本代码回放时遇到不认识的事件类型，读到
   ignorable=True 就跳过，读到 False 就报错——这就是"向前兼容但不静默丢语义"。
5. `schema_version` 记录事件 payload 的结构版本，为将来的事件迁移留出依据。

注意：这里刻意没有用数据库默认值（server_default）。
    默认值只写在一处才不会漂移：值由应用在写入时提供。
    否则"应用建表"和"手写 SQL 建表"会得到两张结构不同的表。
"""

from typing import (
    Any,
    ClassVar,
    Optional,
)

from sqlalchemy import (
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.models.base import BaseModel


class SessionEvent(BaseModel, table=True):
    """一条已提交的会话事件.

    Attributes:
        id: 全局自增主键，只用于物理定位，业务语义一律以 seq 为准。
        session_id: 所属会话，外键指向 session.id。
        seq: 会话内单调递增序号，从 1 开始。
        turn_id: 该事件属于哪一轮对话；会话级事件（改名、清空）为 None。
        type: 事件类型，取值见 EventType 枚举。
        data: 事件载荷，JSONB，不同 type 有不同结构。
        ignorable: 不认识此类型时是否允许跳过。
        schema_version: data 的结构版本。
        created_at: 写入时间（仅用于观测，不用于排序）。
    """

    __tablename__: ClassVar[str] = "session_event"  # pyright: ignore[reportIncompatibleVariableOverride]

    id: int = Field(default=None, primary_key=True)
    # 不单独建 (session_id) 索引：UNIQUE(session_id, seq) 的最左前缀已经覆盖它。
    # 外键写在 __table_args__ 里，是为了能给它一个确定的名字，
    # 这样"应用建表"和 docs/sql/001_session_event.sql 建出来的约束名也一致。
    session_id: str
    seq: int
    turn_id: Optional[str] = Field(default=None, index=True)
    type: str
    data: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    ignorable: bool = False
    schema_version: int = 1

    __table_args__ = (
        # 外键：会话被删除时事件也失去意义（实际清理在 DatabaseService.delete_session 里做，
        # 这里只声明引用关系，不用 ON DELETE CASCADE，避免和 chat_message 的处理方式不一致）
        ForeignKeyConstraint(["session_id"], ["session.id"], name="fk_session_event_session"),
        # 同一会话内 seq 唯一：既是排序保证，也是并发写入的最终防线
        UniqueConstraint("session_id", "seq", name="uq_session_event_session_seq"),
        # 回放时经常按类型过滤（例如只看 message/*）
        Index("ix_session_event_session_type", "session_id", "type"),
    )
