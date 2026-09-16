"""标准 SSE 结构化事件帧 (Typed SSE Events) 模型与协议定义.

遵循 W3C EventSource 标准，通过 event 与 data 字段解耦传输控制信令与正文流。
保持对旧版 JSON 解析的完全向后兼容。
"""

from enum import Enum
import json
import time
from typing import Any, Optional

from pydantic import BaseModel, Field


class SSEEventType(str, Enum):
    """SSE 事件类型枚举."""

    TOKEN = "token"                    # LLM 生成的文字增量
    TOOL_START = "tool_start"          # 工具开始调用
    TOOL_END = "tool_end"              # 工具执行完毕
    INTERRUPT = "interrupt"            # 人机协同审批中断
    SUBGRAPH_STEP = "subgraph_step"    # 二级子图执行步骤
    ERROR = "error"                    # 运行时异常错误
    DONE = "done"                      # 流式响应全流程完成


class SSEEvent(BaseModel):
    """标准 SSE 结构化事件帧模型.

    属性:
        event: W3C SSE event 名称。
        type: 与 event 一致的字符串标识。
        content: 文本增量或提示内容（供纯文本展示或向后兼容）。
        done: 是否已完成生成。
        tool_name: 工具名称（如 query_knowledge_base）。
        tool_display_name: 人类友好的工具展示名称。
        tool_call_id: 工具调用 ID。
        tool_args: 工具调用入参。
        duration_ms: 工具执行耗时（毫秒）。
        tool_result: 工具返回结果摘要。
        interrupt_payload: 人机协同审批信息载荷。
        subgraph_name: 二级子图名称。
        subgraph_step: 二级子图当前步骤。
        timestamp: 事件生成时间戳。
    """

    event: SSEEventType = Field(default=SSEEventType.TOKEN, description="W3C SSE 事件名称")
    type: str = Field(default="token", description="事件类型标识符")
    content: str = Field(default="", description="文本增量或提示内容")
    done: bool = Field(default=False, description="是否全流程结束")

    # 工具相关扩展数据
    tool_name: Optional[str] = Field(default=None, description="工具名称")
    tool_display_name: Optional[str] = Field(default=None, description="工具友好展示名称")
    tool_call_id: Optional[str] = Field(default=None, description="工具调用唯一ID")
    tool_args: Optional[Any] = Field(default=None, description="工具调用入参")
    duration_ms: Optional[float] = Field(default=None, description="工具执行耗时(毫秒)")
    tool_result: Optional[str] = Field(default=None, description="工具返回的结果摘要")

    # 人机协同审批扩展数据
    interrupt_payload: Optional[dict[str, Any]] = Field(default=None, description="审批详情")

    # 二级子图扩展数据
    subgraph_name: Optional[str] = Field(default=None, description="子图名称")
    subgraph_step: Optional[str] = Field(default=None, description="子图当前步骤")

    # 元数据
    timestamp: float = Field(default_factory=time.time, description="时间戳")

    def to_sse_packet(self) -> str:
        r"""生成符合 W3C 规范的完整 SSE 文本数据包.

        输出格式:
            event: <event_name>\n
            data: <json_string>\n\n
        """
        payload = self.model_dump(mode="json", exclude_none=True)
        # 确保 type 始终与 event 值保持一致
        payload["type"] = self.event.value
        json_str = json.dumps(payload, ensure_ascii=False)
        return f"event: {self.event.value}\ndata: {json_str}\n\n"
