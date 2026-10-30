"""工具结果输出保留与结构化裁剪策略 (Output Retention).

对标 DSH dsh-output-retention 规范，消除散落各处的魔法数字 [:4000] 与 [:200]，
向 LLM 明确告知被裁剪字符量与精确度元数据。
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class RetentionOmitted(BaseModel):
    """描述工具调用结果输出裁剪的元数据模型 (对齐 DSH 规范)."""

    kind: Literal["exact", "unknown"] = Field(
        ..., description="裁剪量是否精确可知（exact: 精确字符量，unknown: 估算或未知）"
    )
    content_type: Literal["text", "structured"] = Field(
        default="text", description="被裁剪的内容类型"
    )
    original_length: int = Field(..., description="原始输出字符长度")
    retained_length: int = Field(..., description="保留的字符长度")


class OutputRetention:
    """工具输出保留与截断管理器."""

    MAX_TOOL_OUTPUT_CHARS: int = 4000
    SNIPPET_LOG_LIMIT: int = 200

    @classmethod
    def retain(
        cls,
        output: str,
        max_chars: Optional[int] = None,
    ) -> tuple[str, Optional[RetentionOmitted]]:
        """对工具原始输出应用保留策略.

        参数：
            output: 工具执行产出的原始文本字符串。
            max_chars: 最大允许保留字符数，未指定时使用 MAX_TOOL_OUTPUT_CHARS (4000)。

        返回：
            tuple[str, Optional[RetentionOmitted]]: 处理后的文本及裁剪元数据（未发生裁剪时为 None）。
        """
        limit = max_chars or cls.MAX_TOOL_OUTPUT_CHARS
        if len(output) <= limit:
            return output, None

        # 将标记文本计入限额，确保返回值总长度严格不超过 limit (解决约 70 字符的溢出)
        marker = (
            f"\n\n[Omitted: kind=exact, original_length={len(output)}, retained_length={limit}]"
        )
        retained_body_len = max(limit - len(marker), 0)
        truncated_text = f"{output[:retained_body_len]}{marker}"

        omitted = RetentionOmitted(
            kind="exact",
            content_type="text",
            original_length=len(output),
            retained_length=len(truncated_text),
        )
        return truncated_text, omitted

    @classmethod
    def get_snippet(cls, output: str, limit: Optional[int] = None) -> str:
        """获取用于结构化日志记录的摘要片段."""
        snip_limit = limit or cls.SNIPPET_LOG_LIMIT
        if len(output) <= snip_limit:
            return output
        return f"{output[:snip_limit]}...<snippet_truncated:{len(output) - snip_limit}>"
