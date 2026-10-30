"""工具执行管线 (Tool Execution Pipeline).

提供三段式工具执行架构：
1. pre_execute: 身份与上下文注入 (InjectedToolArg) + 注册式高危审批 (Human-in-the-Loop)
2. execute: 工具原生 ainvoke 执行，仅加超时保护 (asyncio.wait_for)，坚决不叠加重试 (解决 M3)
3. post_execute: 结构化日志记录与统一结果保留策略 (OutputRetention)
"""

import asyncio
from typing import Any, Mapping

from langchain_core.messages import ToolMessage
from langchain_core.tools.base import BaseTool
from langgraph.types import interrupt

from app.core.config import settings
from app.core.langgraph.tools.approval import (
    ApprovalDecision,
    ApprovalEvaluator,
    ApprovalRegistry,
)
from app.core.langgraph.tools.retention import OutputRetention
from app.core.logging import logger

USER_SCOPED_TOOLS: frozenset[str] = frozenset({"query_session_history", "query_user_sessions"})
THREAD_SCOPED_TOOLS: frozenset[str] = frozenset({"query_session_history"})


class ToolPipelineContext:
    """工具管线执行上下文."""

    def __init__(
        self,
        caller_user_id: str,
        current_thread_id: str,
        tools_by_name: Mapping[str, BaseTool],
    ) -> None:
        """初始化上下文."""
        self.caller_user_id = caller_user_id
        self.current_thread_id = current_thread_id
        self.tools_by_name = tools_by_name


class ToolPipeline:
    """三段式工具调度流水线."""

    @classmethod
    async def dispatch(
        cls,
        tool_call: dict[str, Any],
        ctx: ToolPipelineContext,
    ) -> ToolMessage:
        """分发并完整执行一次工具调用生命周期.

        参数：
            tool_call: 大模型生成的工具调用字典（含 name, args, id）。
            ctx: 管线运行时上下文。

        返回：
            ToolMessage: 格式化后的工具响应消息。
        """
        tool_name = str(tool_call.get("name", "unknown_tool"))
        tool_args: dict[str, Any] = dict(tool_call.get("args") or {})
        call_id = str(tool_call.get("id", ""))

        # ------------------------------------------------------------
        # 1. pre_execute: 审批守卫与参数注入
        # ------------------------------------------------------------
        pre_result = await cls._pre_execute(tool_name, tool_args, call_id, ctx)
        if isinstance(pre_result, ToolMessage):
            # 被审批拦截或拒绝，直接短路返回
            return pre_result
        tool_input = pre_result

        # ------------------------------------------------------------
        # 2. execute: 工具执行体（仅超时保护，不叠加重试，解决 M3）
        # ------------------------------------------------------------
        raw_result = await cls._execute(tool_name, tool_input, ctx)

        # ------------------------------------------------------------
        # 3. post_execute: 统一结果保留与结构化日志
        # ------------------------------------------------------------
        return cls._post_execute(tool_name, call_id, raw_result)

    @classmethod
    async def _pre_execute(
        cls,
        tool_name: str,
        tool_args: dict[str, Any],
        call_id: str,
        ctx: ToolPipelineContext,
    ) -> dict[str, Any] | ToolMessage:
        """第一阶段：安全守卫、人工审批与上下文注入."""
        # A. 敏感高危操作注册式人工协同确认 (Human-in-the-Loop)
        if ApprovalRegistry.is_approval_required(tool_name):
            payload = ApprovalRegistry.build_approval_payload(tool_name, tool_args)
            logger.info("human_in_the_loop_interrupt_triggered", tool_name=tool_name, args=tool_args)

            # 挂起当前状态机，等待人工确认
            user_decision = interrupt(payload)

            # 结构化评估决策（对标 DSH 四态判定，解决 G1 子串误判）
            decision = ApprovalEvaluator.evaluate(user_decision)
            if decision != ApprovalDecision.ALLOW_ONCE:
                logger.info(
                    "tool_execution_rejected_by_user",
                    tool_name=tool_name,
                    decision=decision.value,
                    raw_input=str(user_decision),
                )
                return ToolMessage(
                    content=(
                        f"❌ 操作已取消：用户已人工拒绝或取消了本次操作（用户指令: {user_decision}）。"
                        "请向用户告知操作未执行，并询问用户是否有其他修改建议或下一步安排。"
                    ),
                    name=tool_name,
                    tool_call_id=call_id,
                )

            logger.info("tool_execution_approved_by_user", tool_name=tool_name, decision=decision.value)

        # B. 调用者身份与会话上下文注入
        tool_input = dict(tool_args)
        if tool_name in USER_SCOPED_TOOLS:
            tool_input["user_id"] = ctx.caller_user_id
        if tool_name in THREAD_SCOPED_TOOLS:
            tool_input["thread_id"] = ctx.current_thread_id

        return tool_input

    @classmethod
    async def _execute(
        cls,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPipelineContext,
    ) -> str:
        """第二阶段：执行工具调用（超时控制，捕获异常，绝不额外嵌套重试）."""
        tool_instance = ctx.tools_by_name.get(tool_name)
        if not tool_instance:
            return f"错误：未找到名为 [{tool_name}] 的可用工具。"

        try:
            # 仅做统一超时保护，重试完全交由 LangGraph 节点级的 RetryPolicy
            timeout_sec = float(getattr(settings, "TOOL_TIMEOUT_SECONDS", 120))
            tool_result = await asyncio.wait_for(
                tool_instance.ainvoke(tool_input),
                timeout=timeout_sec,
            )
            return str(tool_result)
        except asyncio.TimeoutError:
            timeout_sec = getattr(settings, "TOOL_TIMEOUT_SECONDS", 120)
            logger.warning("tool_execution_timeout", name=tool_name, timeout=timeout_sec)
            return f"工具 [{tool_name}] 执行超时（超过 {timeout_sec} 秒），已终止该操作。"
        except Exception as e:
            logger.warning(
                "tool_execution_exception",
                name=tool_name,
                args=tool_input,
                error=str(e),
            )
            return f"工具 [{tool_name}] 执行出错: {str(e)}。提示：如果缺少必要参数 (如 poiId/URL/关键词)，请尝试调整参数或更换其他匹配的工具。"

    @classmethod
    def _post_execute(
        cls,
        tool_name: str,
        call_id: str,
        raw_result: str,
    ) -> ToolMessage:
        """第三阶段：应用统一结果保留策略并记录日志."""
        # 结构化日志记录摘要片段
        snippet = OutputRetention.get_snippet(raw_result)
        logger.info("tool_executed_result", name=tool_name, result_snippet=snippet)

        # 应用保留策略，超出限制时注入精确 Omitted 元数据 (解决 M6)
        retained_content, omitted = OutputRetention.retain(raw_result)
        if omitted:
            logger.debug(
                "tool_output_omitted",
                name=tool_name,
                original_len=omitted.original_length,
                retained_len=omitted.retained_length,
            )

        return ToolMessage(
            content=retained_content,
            name=tool_name,
            tool_call_id=call_id,
        )

