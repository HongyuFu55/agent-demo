"""MCP Server 模块.

提供标准的 Model Context Protocol 服务端实现，包含 Tools、Resources、Prompts 原语支持，
以及 Stdio CLI 运行器和挂载至 FastAPI 的 ASGI 适配器。
"""

from app.mcp.cli import run_mcp_cli
from app.mcp.server import create_mcp_server, get_mcp_asgi_app, mcp_server

__all__ = [
    "create_mcp_server",
    "get_mcp_asgi_app",
    "mcp_server",
    "run_mcp_cli",
]
