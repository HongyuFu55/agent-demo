"""图状态相关 schema."""

from typing import Annotated, Optional

from langgraph.graph.message import add_messages
from pydantic import (
    BaseModel,
    Field,
)


class GraphState(BaseModel):
    """LangGraph 智能体/工作流的状态定义."""

    messages: Annotated[list, add_messages] = Field(
        default_factory=list, description="会话中的消息（当前活跃轮）"
    )
    history_messages: list = Field(
        default_factory=list,
        description="来自事件日志的只读历史投影消息（注：存入 checkpoint，存在已知重复持久化开销）",
    )
    long_term_memory: str = Field(default="", description="会话相关的长期记忆")
    assembled_context: Optional[str] = Field(
        default=None, description="按轮缓存的已组装系统上下文与提示词"
    )
    turn_start_index: int = Field(
        default=0,
        description="本轮在 messages 中的起始下标游标（用于 projection 模式切片活跃轮消息）",
    )
