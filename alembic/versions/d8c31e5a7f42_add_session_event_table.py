"""新增会话事件日志表 session_event.

Revision ID: d8c31e5a7f42
Revises: c7f2a08e4b1d
Create Date: 2026-10-26 15:40:00.000000

把会话状态改成事件溯源的第一步：新增只追加的事件表。
与 docs/sql/001_session_event.sql 等价，二选一执行即可
（应用启动时的 SQLModel.metadata.create_all 也会自动建表）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from sqlalchemy.dialects import postgresql

from alembic import op

# Alembic 使用的迁移版本标识符
revision: str = "d8c31e5a7f42"
down_revision: Union[str, Sequence[str], None] = "c7f2a08e4b1d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级数据库结构."""
    op.create_table(
        "session_event",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("turn_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ignorable", sa.Boolean(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], name="fk_session_event_session"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "seq", name="uq_session_event_session_seq"),
    )
    op.create_index("ix_session_event_session_type", "session_event", ["session_id", "type"], unique=False)
    op.create_index("ix_session_event_turn_id", "session_event", ["turn_id"], unique=False)


def downgrade() -> None:
    """回滚数据库结构."""
    op.drop_index("ix_session_event_turn_id", table_name="session_event")
    op.drop_index("ix_session_event_session_type", table_name="session_event")
    op.drop_table("session_event")
