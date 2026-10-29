"""聊天上下文与提示词组装管线 (Context Pipeline).

提供可拦截、可扩展的系统提示词与上下文组装流水线。
使用户画像、多租户指令、敏感输入阻断等特性与 LangGraph 图节点彻底解耦。
"""

from typing import (
    Awaitable,
    Callable,
    Optional,
    Protocol,
)

from pydantic import BaseModel, Field

from app.core.prompts import load_system_prompt


class ChatRequestContext(BaseModel):
    """请求上下文实体，贯穿 ContextPipeline 中的所有中间件."""

    username: Optional[str] = None
    thread_id: str = ""
    user_id: Optional[str] = None
    long_term_memory: str = ""
    system_prompt: str = ""
    dynamic_instructions: list[str] = Field(default_factory=list, description="动态场景指令列表")


class ContextMiddleware(Protocol):
    """上下文拦截器中间件协议."""

    async def __call__(
        self,
        ctx: ChatRequestContext,
        call_next: Callable[[ChatRequestContext], Awaitable[ChatRequestContext]],
    ) -> ChatRequestContext:
        """执行中间件处理."""
        ...


class SystemPromptMiddleware:
    """系统提示词组装中间件，负责加载系统提示词模板并注入用户上下文与长期记忆."""

    async def __call__(
        self,
        ctx: ChatRequestContext,
        call_next: Callable[[ChatRequestContext], Awaitable[ChatRequestContext]],
    ) -> ChatRequestContext:
        """组装系统提示词并传递至下游."""
        ctx.system_prompt = load_system_prompt(
            username=ctx.username,
            long_term_memory=ctx.long_term_memory,
        )
        if ctx.dynamic_instructions:
            instructions_block = "\n\n# Dynamic Instructions\n" + "\n".join(ctx.dynamic_instructions)
            ctx.system_prompt += instructions_block
        return await call_next(ctx)


class InputGuardMiddleware:
    """输入安全审计与规则拦截中间件."""

    async def __call__(
        self,
        ctx: ChatRequestContext,
        call_next: Callable[[ChatRequestContext], Awaitable[ChatRequestContext]],
    ) -> ChatRequestContext:
        """安全审计检查并传递至下游."""
        # 可在此扩展敏感词过滤、安全沙箱拦截等短路机制
        return await call_next(ctx)


class ContextPipeline:
    """上下文组装管线，采用责任链模式按顺序执行注册的中间件."""

    def __init__(self, middlewares: Optional[list[ContextMiddleware]] = None) -> None:
        """初始化管线."""
        self.middlewares: list[ContextMiddleware] = (
            middlewares
            if middlewares is not None
            else [
                SystemPromptMiddleware(),
                InputGuardMiddleware(),
            ]
        )

    async def execute(self, ctx: ChatRequestContext) -> ChatRequestContext:
        """洋葱模型执行所有中间件."""

        async def _run(index: int, current_ctx: ChatRequestContext) -> ChatRequestContext:
            if index >= len(self.middlewares):
                return current_ctx
            middleware = self.middlewares[index]
            return await middleware(
                current_ctx,
                lambda next_ctx: _run(index + 1, next_ctx),
            )

        return await _run(0, ctx)


default_context_pipeline = ContextPipeline()

