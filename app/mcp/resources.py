"""MCP Server Resources 注册与定义.

暴露给外部 MCP 客户端的只读上下文与数据源（Resources）。
客户端可直接通过标准 Resource URI（如 server://info）读取结构化系统上下文。
"""

from datetime import datetime
import json
import platform
import sys

from app.core.config import settings
from app.core.logging import logger


async def get_server_info_resource() -> str:
    """读取当前 MCP Server 的服务元数据与系统上下文.

    返回:
        JSON 格式的服务元数据，包含版本、环境、核心能力及设计说明。
    """
    logger.info("mcp_resource_server_info_read")
    info = {
        "server_name": settings.MCP_SERVER_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT.value,
        "description": "基于 FastAPI + LangGraph 构建的生产级 Agent & MCP 服务端",
        "capabilities": {
            "tools": [
                "knowledge_search",
                "get_system_health",
                "execute_calculator",
                "query_order_summary",
            ],
            "resources": [
                "server://info",
                "server://system_metrics",
            ],
            "prompts": [
                "agent_task_planner",
                "sql_expert_analyzer",
            ],
        },
        "endpoints": {
            "http_sse": f"{settings.MCP_SERVER_PREFIX}/sse",
            "http_messages": f"{settings.MCP_SERVER_PREFIX}/messages/",
            "streamable_http": f"{settings.MCP_SERVER_PREFIX}/mcp",
            "api_docs": "/docs",
        },
    }
    return json.dumps(info, ensure_ascii=False, indent=2)


async def get_system_metrics_resource() -> str:
    """读取实时系统指标与运行时状态.

    返回:
        JSON 格式的系统运行指标，包含操作系统平台、Python版本、时间戳与配置概要。
    """
    logger.info("mcp_resource_system_metrics_read")
    metrics = {
        "timestamp": datetime.now().isoformat(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "system": platform.system(),
        "architecture": platform.machine(),
        "config_summary": {
            "debug": settings.DEBUG,
            "default_model": settings.DEFAULT_LLM_MODEL,
            "langfuse_enabled": settings.LANGFUSE_TRACING_ENABLED,
            "mcp_auth_required": bool(settings.MCP_SERVER_API_KEY),
        },
    }
    return json.dumps(metrics, ensure_ascii=False, indent=2)
