"""MCP Server 核心实例、服务生命周期与安全鉴权中间件.

负责创建 FastMCP 实例，注册所有 Tools、Resources、Prompts 原语，
并提供可挂载至 FastAPI 的 ASGI 应用与 API Key 安全中间件。
"""

from urllib.parse import parse_qs

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.core.logging import logger
from app.mcp.prompts import (
    agent_task_planner_prompt,
    sql_expert_analyzer_prompt,
)
from app.mcp.resources import (
    get_server_info_resource,
    get_system_metrics_resource,
)
from app.mcp.tools import (
    execute_calculator,
    get_system_health,
    knowledge_search,
    query_order_summary,
)


class MCPAuthMiddleware:
    """MCP HTTP/SSE 访问安全鉴权 ASGI 中间件.

    当环境变量配置了 MCP_SERVER_API_KEY 时，拦截所有对 MCP 端点的 HTTP 请求。
    客户端可通过以下任一方式传递密钥：
      1. HTTP 请求头: `x-api-key: <YOUR_KEY>`
      2. Authorization 请求头: `Bearer <YOUR_KEY>`
      3. URL Query 参数: `?api_key=<YOUR_KEY>` 或 `?token=<YOUR_KEY>`
    """

    def __init__(self, app: ASGIApp) -> None:
        """初始化 MCP 鉴权中间件.

        参数:
            app: 下一层 ASGI 应用程序实例。
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理 ASGI 请求拦截与鉴权校验."""
        # 仅对 HTTP 请求做 API Key 检查（stdio 等模式不受此影响）
        if scope["type"] == "http" and settings.MCP_SERVER_API_KEY:
            headers = dict(scope.get("headers", []))
            
            # 1. 检查 x-api-key 请求头
            x_api_key = headers.get(b"x-api-key", b"").decode("utf-8").strip()
            
            # 2. 检查 Authorization: Bearer <key>
            auth_header = headers.get(b"authorization", b"").decode("utf-8").strip()
            bearer_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
            
            # 3. 检查 Query 参数
            query_string = scope.get("query_string", b"").decode("utf-8")
            query_params = parse_qs(query_string)
            query_key = query_params.get("api_key", [""])[0] or query_params.get("token", [""])[0]

            provided_key = x_api_key or bearer_token or query_key

            if provided_key != settings.MCP_SERVER_API_KEY:
                logger.warning(
                    "mcp_request_unauthorized",
                    client_ip=scope.get("client", ("unknown", 0))[0],
                    path=scope.get("path", ""),
                )
                response_body = b'{"detail": "Unauthorized: Invalid or missing MCP API Key"}'
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(response_body)).encode("utf-8")),
                    ],
                })
                await send({
                    "type": "http.response.body",
                    "body": response_body,
                })
                return

        await self.app(scope, receive, send)


def create_mcp_server() -> FastMCP:
    """创建并初始化全功能的 FastMCP Server 实例.

    注册 Tools、Resources 与 Prompts，并禁用 DNS 重绑定检查以适配代理环境。

    返回:
        配置完整的 FastMCP 实例。
    """
    server = FastMCP(
        name=settings.MCP_SERVER_NAME,
        instructions=(
            "这是由 FastAPI + LangGraph 架构驱动的生产级 MCP Server。"
            "提供私有知识库检索、系统健康探针、安全计算器与订单统计等工具能力，"
            "同时暴露系统上下文 Resources 与专家 Prompt 模板。"
        ),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # 1. 注册 MCP Tools
    server.tool(name="knowledge_search")(knowledge_search)
    server.tool(name="get_system_health")(get_system_health)
    server.tool(name="execute_calculator")(execute_calculator)
    server.tool(name="query_order_summary")(query_order_summary)

    # 2. 注册 MCP Resources
    server.resource("server://info")(get_server_info_resource)
    server.resource("server://system_metrics")(get_system_metrics_resource)

    # 3. 注册 MCP Prompts
    server.prompt(name="agent_task_planner")(agent_task_planner_prompt)
    server.prompt(name="sql_expert_analyzer")(sql_expert_analyzer_prompt)

    logger.info(
        "mcp_server_created",
        server_name=settings.MCP_SERVER_NAME,
        tool_count=4,
        resource_count=2,
        prompt_count=2,
    )
    return server


# 全局 FastMCP Server 单例
mcp_server = create_mcp_server()


def get_mcp_asgi_app() -> ASGIApp:
    """获取包裹了安全鉴权中间件的 MCP SSE ASGI 应用.

    可直接挂载到 FastAPI 应用中（例如 app.mount("/mcp", get_mcp_asgi_app())）。

    返回:
        ASGI 应用实例。
    """
    base_app = mcp_server.sse_app()
    return MCPAuthMiddleware(base_app)
