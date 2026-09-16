"""MCP Server 独立命令行运行入口.

支持通过标准输入输出（stdio）或独立 SSE 服务启动 MCP Server。
常用于 Cursor、Claude Desktop 等桌面端本地直接拉起。

用法示例：
    # 默认 stdio 模式（供 Cursor / Claude Desktop 本地配置调用）：
    uv run python -m app.mcp
    # 或显式指定 stdio：
    uv run python -m app.mcp --transport stdio

    # 独立 SSE 服务模式：
    uv run python -m app.mcp --transport sse
"""

import argparse
import sys
from typing import Literal

from app.core.logging import logger
from app.mcp.server import mcp_server


def run_mcp_cli() -> None:
    """解析命令行参数并启动 MCP Server."""
    parser = argparse.ArgumentParser(
        description="FastAPI + LangGraph MCP Server CLI 启动器",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP 传输协议模式：stdio（用于本地客户端进程管道）或 sse / streamable-http（网络服务）",
    )

    args = parser.parse_args()

    transport_mode = args.transport.lower()
    logger.info("mcp_server_starting_via_cli", transport=transport_mode)

    try:
        if transport_mode == "stdio":
            # 注意：stdio 模式下日志不能直接输出到 stdout，以免污染 JSON-RPC 消息流
            mcp_server.run(transport="stdio")
        elif transport_mode in ("sse", "streamable-http"):
            target_transport: Literal["stdio", "sse", "streamable-http"] = (
                "sse" if transport_mode == "sse" else "streamable-http"
            )
            mcp_server.run(transport=target_transport)
        else:
            print(f"不支持的 transport 模式: {transport_mode}", file=sys.stderr)
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("mcp_server_stopped_by_user")
    except Exception as e:
        logger.exception("mcp_server_run_failed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run_mcp_cli()
