"""LangGraph 智能体/工作流以及与 LLM 交互的实现."""

import asyncio
import time
from typing import (
    AsyncGenerator,
    Optional,
    cast,
)
from urllib.parse import quote_plus

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools.base import BaseTool
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
    convert_to_openai_messages,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.errors import GraphInterrupt, GraphRecursionError
from langgraph.graph import (
    END,
    StateGraph,
)
from langchain_core.runnables.config import RunnableConfig
from langgraph.graph.state import (
    Command,
    CompiledStateGraph,
)
from langgraph.types import (
    RetryPolicy,
    StateSnapshot,
    interrupt,
)
from psycopg import (
    AsyncConnection,
    sql,
)
from psycopg.rows import (
    DictRow,
    dict_row,
)
from psycopg_pool import AsyncConnectionPool

from app.core.config import (
    Environment,
    settings,
)
from app.core.langgraph.tools import static_tools, load_all_tools
from app.core.logging import logger
from app.core.metrics import llm_inference_duration_seconds
from app.core.observability import langfuse_callback_handler
from app.core.prompts import load_system_prompt
from app.schemas import (
    GraphState,
    Message,
    SSEEvent,
    SSEEventType,
)
from app.services.llm import llm_service
from app.services.memory import memory_service
from app.services.memory_queue import memory_queue_service
from app.utils import (
    dump_messages,
    extract_text_content,
    prepare_messages,
    process_llm_response,
)

PostgresConnPool = AsyncConnectionPool[AsyncConnection[DictRow]]

TOOL_DISPLAY_NAMES: dict[str, str] = {
    "query_knowledge_base": "检索企业私有知识库",
    "execute_python_code": "运行 Python 数据分析/代码执行",
    "scrape_webpage": "抓取并解析网页正文",
    "duckduckgo_search": "DuckDuckGo 网页搜索",
    "send_email_notification": "发送邮件通知 (需人机审批)",
    "analyze_image": "分析图片/视觉图表",
    "calculator": "数值计算器",
    "get_weather_now": "查询实时天气 (和风天气)",
    "search_location_id": "查询城市地理编码 (和风天气)",
    "maps_weather": "查询实时与预报天气 (高德地图)",
    "execute_sql_query": "执行只读数据库查询",
    "query_session_history": "查询历史对话记录",
    "query_user_sessions": "查询用户会话列表",
    "read_workspace_file": "读取工作区文件",
    "write_workspace_file": "写入工作区文件",
    "list_workspace_files": "浏览工作区目录",
    "knowledge_search": "MCP 私有知识库检索",
    "get_system_health": "MCP 系统健康探针",
    "execute_calculator": "MCP 安全数学计算",
    "query_order_summary": "MCP 订单数据汇总",
}

# 需要"调用者身份"的工具名。
#
# 这些工具带 `Annotated[..., InjectedToolArg]` 参数：对 LLM 隐藏，由下面 _tool_call
# 在执行前从 config 注入。名单必须显式维护——注入是安全边界，不应该靠反射猜。
USER_SCOPED_TOOLS: frozenset[str] = frozenset({"query_session_history", "query_user_sessions"})

# 需要注入"当前会话 ID"的工具：模型不知道当前 thread 的 UUID，
# 不注入的话它永远只能查用户手动粘贴了 ID 的会话。
THREAD_SCOPED_TOOLS: frozenset[str] = frozenset({"query_session_history"})



class LangGraphAgent:
    """管理 LangGraph 智能体/工作流以及与 LLM 的交互.

    该类负责创建和管理 LangGraph 工作流，包括 LLM 交互、数据库连接和响应处理。
    """

    def __init__(self):
        """使用必要组件初始化 LangGraph 智能体.

        注意：工具绑定（含 MCP 工具）在 create_graph() 中异步完成。
        __init__ 阶段先用静态工具占位，确保同步初始化不阻塞。
        """
        self.llm_service = llm_service
        # 先绑定静态工具（同步），MCP 工具在 create_graph() 异步加载后重新绑定
        self.llm_service.bind_tools(static_tools)
        self.tools_by_name: dict[str, BaseTool] = {tool.name: tool for tool in static_tools}
        self._connection_pool: Optional[PostgresConnPool] = None
        self._graph: Optional[CompiledStateGraph] = None
        logger.info(
            "langgraph_agent_initialized",
            model=settings.DEFAULT_LLM_MODEL,
            environment=settings.ENVIRONMENT.value,
        )

    async def _get_connection_pool(self) -> Optional[PostgresConnPool]:
        """根据当前环境配置获取 PostgreSQL 连接池.

        返回：
            AsyncConnectionPool；生产环境连接池初始化失败时返回 None，应用会以降级模式继续运行。
        """
        if self._connection_pool is None:
            try:
                max_size = settings.POSTGRES_POOL_SIZE

                # 用 quote_plus 对用户名和密码做 URL 编码，防止特殊字符（如 @、:）导致连接串解析失败
                connection_url = (
                    "postgresql://"
                    f"{quote_plus(settings.POSTGRES_USER)}:{quote_plus(settings.POSTGRES_PASSWORD)}"
                    f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
                )

                self._connection_pool = AsyncConnectionPool(
                    connection_url,
                    open=False,
                    max_size=max_size,
                    kwargs={
                        "autocommit": True,
                        "connect_timeout": 5,
                        # prepare_threshold=None 禁用服务端 prepared statement 缓存，
                        # 避免在连接池复用时出现 "prepared statement already exists" 错误
                        "prepare_threshold": None,
                        "row_factory": dict_row,
                    },
                )
                await self._connection_pool.open()
                logger.info("connection_pool_created", max_size=max_size, environment=settings.ENVIRONMENT.value)
            except Exception as e:
                logger.error("connection_pool_creation_failed", error=str(e), environment=settings.ENVIRONMENT.value)
                # 生产环境降级处理：DB 不可用时允许应用继续启动，聊天功能会失效但服务不崩溃
                if settings.ENVIRONMENT == Environment.PRODUCTION:
                    logger.warning("continuing_without_connection_pool", environment=settings.ENVIRONMENT.value)
                    return None
                raise e
        return self._connection_pool

    async def _chat(self, state: GraphState, config: RunnableConfig) -> Command:
        """处理聊天状态并生成回复.

        参数：
            state (GraphState): 当前对话状态。
            config (RunnableConfig): 本次调用的 runnable 配置。

        返回：
            Command: 包含更新后状态和下一个执行节点的命令对象。
        """
        # 获取当前 LLM 实例用于指标统计
        current_llm = self.llm_service.get_llm()
        model_name = (
            current_llm.model_name
            if current_llm and hasattr(current_llm, "model_name")
            else settings.DEFAULT_LLM_MODEL
        )

        username = config.get("metadata", {}).get("username")
        thread_id = config.get("configurable", {}).get("thread_id")
        SYSTEM_PROMPT = load_system_prompt(username=username, long_term_memory=state.long_term_memory)

        # 使用系统提示词准备消息
        messages = prepare_messages(state.messages, SYSTEM_PROMPT)

        try:
            # 使用带自动重试和循环 fallback 的 LLM 服务
            with llm_inference_duration_seconds.labels(model=model_name).time():
                response_message = await self.llm_service.call(dump_messages(messages))

            # 处理响应，兼容结构化内容块
            response_message = process_llm_response(response_message)

            logger.info("大模型回答已生成", session_id=thread_id, model=model_name)

            # 根据响应是否包含工具调用决定下一个节点：
            # 有工具调用 → tool_call 节点执行工具 → 再回到 chat（TODO 不断循环，只调没有工具调用，但是这也有一个问题，可能会无限循环，所以需要有最大循环次数）
            # 无工具调用 → 直接结束
            if isinstance(response_message, AIMessage) and response_message.tool_calls:
                goto = "tool_call"
            else:
                goto = END

            return Command(update={"messages": [response_message]}, goto=goto)
        except Exception as e:
            logger.error(
                "llm_call_failed_all_models",
                session_id=thread_id,
                error=str(e),
                environment=settings.ENVIRONMENT.value,
            )
            raise Exception(f"failed to get llm response after trying all models: {str(e)}")

    # 定义工具节点
    async def _tool_call(self, state: GraphState, config: RunnableConfig) -> Command:
        """处理最后一条消息中的工具调用.

        参数：
            state: 当前智能体状态，包含消息和工具调用。
            config: 本次调用的 runnable 配置（metadata 里有调用者 user_id）。

        返回：
            Command: 包含更新后消息，并路由回 chat 节点的命令对象。
        """
        tool_calls = state.messages[-1].tool_calls

        # 调用者身份唯一来源：路由层写进 config.metadata 的 user_id。
        # 绝不从 LLM 的工具参数里取——那是模型可以随便填的东西。
        caller_user_id = str(config.get("metadata", {}).get("user_id") or "")
        current_thread_id = str(config.get("configurable", {}).get("thread_id") or "")

        async def _execute_tool(tool_call: dict) -> ToolMessage:
            tool_name = tool_call["name"]
            tool_args = tool_call.get("args", {})

            # 🛡️ 敏感操作人机协同中断确认 (Human-in-the-Loop)
            if tool_name == "send_email_notification":
                to_email = str(tool_args.get("to_email", ""))
                subject = str(tool_args.get("subject", ""))
                content = str(tool_args.get("content", ""))

                approval_prompt = (
                    "⚠️ **【需要人工确认】智能体申请发送以下电子邮件：**\n\n"
                    f"- 📧 **收件人**：`{to_email}`\n"
                    f"- 📌 **主题**：`{subject}`\n\n"
                    "📄 **邮件正文预览：**\n"
                    "```text\n"
                    f"{content}\n"
                    "```\n\n"
                    "请确认是否批准发送？您可以回复 **“确认发送”** 或 **“拒绝发送”**。"
                )

                approval_payload = {
                    "type": "human_approval_required",
                    "action": "send_email",
                    "to_email": to_email,
                    "subject": subject,
                    "content": content,
                    "message": approval_prompt,
                }

                logger.info(
                    "human_in_the_loop_interrupt_triggered",
                    tool_name=tool_name,
                    to_email=to_email,
                    subject=subject,
                )

                # 挂起当前状态机，等待人工确认
                user_decision = interrupt(approval_payload)

                # 恢复后检查用户决策
                decision_str = str(user_decision).lower().strip()
                is_approved = any(kw in decision_str for kw in ["approve", "yes", "y", "确认", "同意", "发送", "确定", "准许", "通过", "发吧", "好"])
                is_rejected = any(kw in decision_str for kw in ["reject", "no", "n", "拒绝", "取消", "别发", "不发", "驳回", "停止", "算了"])

                if not is_approved or is_rejected:
                    logger.info(
                        "email_sending_rejected_by_user",
                        user_decision=decision_str,
                        to_email=to_email,
                    )
                    return ToolMessage(
                        content=(
                            f"❌ 操作已取消：用户已人工拒绝或取消了本次邮件发送（用户指令: {user_decision}）。"
                            "请向用户告知邮件未发出，并询问用户是否有其他修改建议或下一步安排。"
                        ),
                        name=tool_name,
                        tool_call_id=tool_call["id"],
                    )

                logger.info(
                    "email_sending_approved_by_user",
                    user_decision=decision_str,
                    to_email=to_email,
                )

            try:
                tool_instance = self.tools_by_name[tool_name]
                # 用户维度的工具：注入调用者身份与当前会话（对应工具里的 InjectedToolArg 参数）
                tool_input = dict(tool_args)
                if tool_name in USER_SCOPED_TOOLS:
                    tool_input["user_id"] = caller_user_id
                if tool_name in THREAD_SCOPED_TOOLS:
                    tool_input["thread_id"] = current_thread_id
                tool_result = await tool_instance.ainvoke(tool_input)
                str_result = str(tool_result)
            except Exception as e:
                logger.warning(
                    "tool_execution_exception",
                    name=tool_name,
                    args=tool_args,
                    error=str(e),
                )
                str_result = f"工具 [{tool_name}] 执行出错: {str(e)}。提示：如果缺少必要参数 (如 poiId/URL/关键词)，请尝试调整参数或更换其他匹配的工具。"

            logger.info("tool_executed_result", name=tool_name, result_snippet=str_result[:200])
            if len(str_result) > 4000:
                str_result = str_result[:4000] + "...<tool_result_truncated>"
            return ToolMessage(
                content=str_result,
                name=tool_name,
                tool_call_id=tool_call["id"],
            )

        # 单个工具调用直接 await，多个工具调用并发执行以节省等待时间
        if len(tool_calls) == 1:
            outputs = [await _execute_tool(tool_calls[0])]
        else:
            outputs = list(await asyncio.gather(*[_execute_tool(tc) for tc in tool_calls]))

        return Command(update={"messages": outputs}, goto="chat")

    async def create_graph(self) -> Optional[CompiledStateGraph]:
        """创建并配置 LangGraph 工作流.

        返回：
            Optional[CompiledStateGraph]: 配置完成的 LangGraph 实例；初始化失败时为 None。
        """
        if self._graph is None:
            try:
                # 异步加载全部工具（静态工具 + MCP 工具），然后重新绑定给 LLM
                # MCP 加载失败时 load_all_tools() 会自动降级，这里无需额外处理
                all_tools = await load_all_tools()
                self.llm_service.bind_tools(all_tools)
                self.tools_by_name = {tool.name: tool for tool in all_tools}
                logger.info(
                    "agent_tools_bound",
                    tool_names=list(self.tools_by_name.keys()),
                    total=len(all_tools),
                )

                graph_builder = StateGraph(GraphState)
                graph_builder.add_node("chat", self._chat, destinations=("tool_call", END))
                graph_builder.add_node(
                    "tool_call",
                    self._tool_call,
                    destinations=("chat",),
                    retry_policy=RetryPolicy(max_attempts=3),
                )
                graph_builder.set_entry_point("chat")
                graph_builder.set_finish_point("chat")

                # 获取连接池；生产环境数据库不可用时可能为 None
                connection_pool = await self._get_connection_pool()
                if connection_pool:
                    # LangGraph 的 AsyncPostgresSaver 在处理同一个 session 时会加数据库锁,也就是说同一个session，同一时间发出多个请求，在代码层次还是串行执行。
                    checkpointer = AsyncPostgresSaver(connection_pool)
                    await checkpointer.setup()
                else:
                    # 生产环境必要时允许不带 checkpointer 继续运行
                    checkpointer = None
                    if settings.ENVIRONMENT != Environment.PRODUCTION:
                        raise Exception("Connection pool initialization failed")

                self._graph = graph_builder.compile(
                    checkpointer=checkpointer, name=f"{settings.PROJECT_NAME} Agent ({settings.ENVIRONMENT.value})"
                )

                logger.info(
                    "graph_created",
                    graph_name=f"{settings.PROJECT_NAME} Agent",
                    environment=settings.ENVIRONMENT.value,
                    has_checkpointer=checkpointer is not None,
                )
            except Exception as e:
                logger.error("graph_creation_failed", error=str(e), environment=settings.ENVIRONMENT.value)
                # 生产环境不希望因此导致应用崩溃
                if settings.ENVIRONMENT == Environment.PRODUCTION:
                    logger.warning("continuing_without_graph")
                    return None
                raise e

        return self._graph

    async def _get_graph(self) -> CompiledStateGraph:
        """返回已编译的图；首次访问时创建.

        抛出：
            RuntimeError: 当 ``create_graph()`` 在生产环境路径吞掉初始化失败并返回 ``None`` 时抛出；
                调用方可以依赖本方法返回值一定不是 ``None``。
        """
        if self._graph is None:
            self._graph = await self.create_graph()
        if self._graph is None:
            raise RuntimeError("graph initialization failed")
        return self._graph

    async def get_response(
        self,
        messages: list[Message],
        session_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> list[Message]:
        """从 LLM 获取回复.

        参数：
            messages (list[Message]): 要发送给 LLM 的消息。
            session_id (str): 当前对话的 session ID。
            user_id (Optional[str]): 当前对话的用户 ID。
            username (Optional[str]): 用户展示名称。

        返回：
            list[Message]: LLM 返回的响应消息。
        """
        graph = await self._get_graph()
        callbacks: list[BaseCallbackHandler] = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "callbacks": callbacks,
            # recursion_limit：chat→tool_call 算 2 步，超出后抛 GraphRecursionError，防止无限循环
            "recursion_limit": settings.LANGGRAPH_RECURSION_LIMIT,
            "metadata": {
                "user_id": user_id,
                "username": username,
                "session_id": session_id,
                "environment": settings.ENVIRONMENT.value,
                "debug": settings.DEBUG,
            },
        }

        try:
            # 并发获取 graph 状态和长期记忆，避免串行等待节省 200-500ms
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, messages[-1].content),
            )

            if state.next:
                # state.next 非空说明上次执行被 interrupt（如 ask_human 工具），
                # 此次用用户输入作为 resume 值继续执行，而不是重新开始
                logger.info("继续执行被中断的图流程", session_id=session_id, next_nodes=state.next)
                response = await graph.ainvoke(
                    Command(resume=messages[-1].content),
                    config=config,
                )
            else:
                relevant_memory = relevant_memory or "No relevant memory found."
                # 关键：Checkpointer 已通过 add_messages reducer 维护完整消息历史。
                # 每次只注入最新一条用户消息，避免前端全量历史与 checkpoint 历史叠加，
                # 导致 LLM 看到重复混乱的上下文（"你说测试一下" 幻觉的根因）。
                latest_user_message = [msg for msg in messages if msg.role == "user"][-1]
                response = await graph.ainvoke(
                    input={"messages": [latest_user_message.model_dump()], "long_term_memory": relevant_memory},
                    config=config,
                )

            # ainvoke 返回后再检查一次状态：图在本次执行中可能又遇到了 interrupt
            state = await graph.aget_state(config)
            if state.next:
                interrupt_value = state.tasks[0].interrupts[0].value if state.tasks and state.tasks[0].interrupts else "Waiting for input."
                logger.info("图流程已中断等待用户输入", session_id=session_id, interrupt_value=str(interrupt_value))
                content_text = interrupt_value.get("message", str(interrupt_value)) if isinstance(interrupt_value, dict) else str(interrupt_value)
                return [Message(role="assistant", content=content_text)]

            openai_msgs = cast(list[dict], convert_to_openai_messages(response["messages"]))
            # 记忆投递至 Valkey/Redis Stream 解耦队列与防抖服务，主流程 1ms 内完成
            asyncio.create_task(memory_queue_service.enqueue(user_id, openai_msgs, config.get("metadata")))
            return self.__process_messages(response["messages"])
        except GraphInterrupt:
            state = await graph.aget_state(config)
            interrupt_value = state.tasks[0].interrupts[0].value if state.tasks and state.tasks[0].interrupts else "Waiting for input."
            logger.info("图流程已中断等待用户输入", session_id=session_id, interrupt_value=str(interrupt_value))
            content_text = interrupt_value.get("message", str(interrupt_value)) if isinstance(interrupt_value, dict) else str(interrupt_value)
            return [Message(role="assistant", content=content_text)]
        except GraphRecursionError:
            logger.warning(
                "graph_recursion_limit_exceeded",
                session_id=session_id,
                recursion_limit=settings.LANGGRAPH_RECURSION_LIMIT,
            )
            return [Message(
                role="assistant",
                content="抱歉，这个问题需要的处理步骤过多，我无法完成。请尝试拆分问题或换一种方式提问。",
            )]
        except Exception as e:
            logger.exception("get_response_failed", error=str(e), session_id=session_id)
            raise

    async def get_stream_response(
        self,
        messages: list[Message],
        session_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> AsyncGenerator[SSEEvent, None]:
        """从 LLM 获取标准 SSE 结构化事件流.

        参数：
            messages (list[Message]): 要发送给 LLM 的消息。
            session_id (str): 当前对话的 session ID。
            user_id (Optional[str]): 当前对话的用户 ID。
            username (Optional[str]): 用户展示名称。

        产出：
            SSEEvent: 结构化事件对象（包含 tool_start, tool_end, token, interrupt, done 等）。
        """
        callbacks: list[BaseCallbackHandler] = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "callbacks": callbacks,
            "recursion_limit": settings.LANGGRAPH_RECURSION_LIMIT,
            "metadata": {
                "user_id": user_id,
                "username": username,
                "session_id": session_id,
                "environment": settings.ENVIRONMENT.value,
                "debug": settings.DEBUG,
            },
        }
        graph = await self._get_graph()

        try:
            # 并发检查状态和检索记忆，节省 200-500ms
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, messages[-1].content),
            )

            if state.next:
                logger.info("继续执行被中断的流式图流程", session_id=session_id, next_nodes=state.next)
                graph_input = Command(resume=messages[-1].content)
            else:
                relevant_memory = relevant_memory or "No relevant memory found."
                # 同 get_response：只传最新一条用户消息，避免 add_messages 造成历史叠加
                latest_user_message = [msg for msg in messages if msg.role == "user"][-1]
                graph_input = {"messages": [latest_user_message.model_dump()], "long_term_memory": relevant_memory}

            seen_tool_names: set[str] = set()
            tool_start_times: dict[str, float] = {}

            async for token, _ in graph.astream(
                graph_input,
                config,
                stream_mode="messages",
            ):
                # 1. 捕获大模型输出块，当遇到工具调用时透传 TOOL_START 事件
                if isinstance(token, (AIMessage, AIMessageChunk)):
                    tool_chunks = getattr(token, "tool_call_chunks", None)
                    if tool_chunks:
                        for chunk in tool_chunks:
                            name = chunk.get("name")
                            if name and name not in seen_tool_names:
                                seen_tool_names.add(name)
                                tool_start_times[name] = time.time()
                                display_name = TOOL_DISPLAY_NAMES.get(name, name)
                                raw_args = chunk.get("args")
                                parsed_args = raw_args if raw_args else None
                                yield SSEEvent(
                                    event=SSEEventType.TOOL_START,
                                    type=SSEEventType.TOOL_START.value,
                                    tool_name=name,
                                    tool_display_name=display_name,
                                    tool_args=parsed_args,
                                    tool_call_id=chunk.get("id"),
                                    content="",
                                    done=False,
                                )

                    text = extract_text_content(token.content)
                    if text:
                        yield SSEEvent(
                            event=SSEEventType.TOKEN,
                            type=SSEEventType.TOKEN.value,
                            content=text,
                            done=False,
                        )

                # 2. 捕获工具节点执行完成消息，透传 TOOL_END 事件
                elif isinstance(token, ToolMessage):
                    name = token.name or "unknown_tool"
                    start_t = tool_start_times.get(name, time.time())
                    duration_ms = max(round((time.time() - start_t) * 1000, 1), 1.0)
                    display_name = TOOL_DISPLAY_NAMES.get(name, name)
                    raw_result = str(token.content)
                    result_summary = raw_result[:200] + ("..." if len(raw_result) > 200 else "")
                    yield SSEEvent(
                        event=SSEEventType.TOOL_END,
                        type=SSEEventType.TOOL_END.value,
                        tool_name=name,
                        tool_display_name=display_name,
                        tool_call_id=token.tool_call_id,
                        duration_ms=duration_ms,
                        tool_result=result_summary,
                        content="",
                        done=False,
                    )

            # 流式结束后检查是否触发了 interrupt（如 send_email 人机审批）
            state = await graph.aget_state(config)
            if state.next:
                interrupt_value = (
                    state.tasks[0].interrupts[0].value
                    if state.tasks and state.tasks[0].interrupts
                    else "Waiting for input."
                )
                logger.info("流式图流程已中断等待用户输入", session_id=session_id, interrupt_value=str(interrupt_value))
                content_text = interrupt_value.get("message", str(interrupt_value)) if isinstance(interrupt_value, dict) else str(interrupt_value)
                payload = interrupt_value if isinstance(interrupt_value, dict) else {"message": content_text}
                yield SSEEvent(
                    event=SSEEventType.INTERRUPT,
                    type=SSEEventType.INTERRUPT.value,
                    content=content_text,
                    interrupt_payload=payload,
                    done=False,
                )
            elif state.values and "messages" in state.values:
                openai_msgs = cast(list[dict], convert_to_openai_messages(state.values["messages"]))
                # 记忆投递至 Valkey/Redis Stream 解耦队列与防抖服务，主流程 1ms 内完成
                asyncio.create_task(memory_queue_service.enqueue(user_id, openai_msgs, config.get("metadata")))

            yield SSEEvent(
                event=SSEEventType.DONE,
                type=SSEEventType.DONE.value,
                content="",
                done=True,
            )
        except GraphInterrupt:
            state = await graph.aget_state(config)
            interrupt_value = (
                state.tasks[0].interrupts[0].value
                if state.tasks and state.tasks[0].interrupts
                else "Waiting for input."
            )
            logger.info("流式图流程已中断等待用户输入", session_id=session_id, interrupt_value=str(interrupt_value))
            content_text = interrupt_value.get("message", str(interrupt_value)) if isinstance(interrupt_value, dict) else str(interrupt_value)
            payload = interrupt_value if isinstance(interrupt_value, dict) else {"message": content_text}
            yield SSEEvent(
                event=SSEEventType.INTERRUPT,
                type=SSEEventType.INTERRUPT.value,
                content=content_text,
                interrupt_payload=payload,
                done=False,
            )
        except GraphRecursionError:
            logger.warning(
                "graph_recursion_limit_exceeded",
                session_id=session_id,
                recursion_limit=settings.LANGGRAPH_RECURSION_LIMIT,
            )
            yield SSEEvent(
                event=SSEEventType.ERROR,
                type=SSEEventType.ERROR.value,
                content="抱歉，这个问题需要的处理步骤过多，我无法完成。请尝试拆分问题或换一种方式提问。",
                done=True,
            )
        except Exception as stream_error:
            logger.exception("stream_processing_failed", error=str(stream_error), session_id=session_id)
            yield SSEEvent(
                event=SSEEventType.ERROR,
                type=SSEEventType.ERROR.value,
                content=f"流式生成异常: {str(stream_error)}",
                done=True,
            )
            raise stream_error

    async def get_chat_history(self, session_id: str) -> list[Message]:
        """获取指定 thread ID 的聊天历史.

        参数：
            session_id (str): 当前对话的 session ID。

        返回：
            list[Message]: 聊天历史。
        """
        graph = await self._get_graph()

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        state: StateSnapshot = await graph.aget_state(config=config)
        return self.__process_messages(state.values["messages"]) if state.values else []

    def __process_messages(self, messages: list[BaseMessage]) -> list[Message]:
        openai_style_messages = convert_to_openai_messages(messages)
        filtered = []
        for message in openai_style_messages:
            role = message.get("role")
            content = str(message.get("content", ""))
            # 过滤掉带有 tool_calls 的中间思考/过渡消息，只保留真正向用户回答的消息
            if role == "assistant" and message.get("tool_calls"):
                continue
            if role in ["assistant", "user"] and content:
                filtered.append(Message(role=role, content=content))
        return filtered

    async def clear_chat_history(self, session_id: str) -> None:
        """清空指定 thread ID 的全部聊天历史.

        参数：
            session_id: 要清理聊天历史的 session ID。

        抛出：
            Exception: 清理聊天历史出错时抛出。
        """
        try:
            # 确保连接池已在当前事件循环初始化
            conn_pool = await self._get_connection_pool()
            if conn_pool is None:
                raise RuntimeError("connection pool unavailable; cannot clear chat history")

            # 将所有 DELETE 合并到一次 pipeline 往返中
            async with conn_pool.connection() as conn:
                # pipeline() 将多条 DELETE 合并为一次网络往返，减少清理多张表的延迟
                async with conn.pipeline():
                    for table in settings.CHECKPOINT_TABLES:
                        await conn.execute(
                            sql.SQL("DELETE FROM {} WHERE thread_id = %s").format(sql.Identifier(table)),
                            (session_id,),
                        )
                logger.info(
                    "checkpoint_tables_cleared_for_session",
                    tables=settings.CHECKPOINT_TABLES,
                    session_id=session_id,
                )

        except Exception as e:
            logger.error(
                "clear_chat_history_operation_failed",
                session_id=session_id,
                error=str(e),
            )
            raise
