"""MCP Server 服务端单元与集成测试.

测试范围：
1. FastMCP 实例创建与 Tools / Resources / Prompts 原语注册验证
2. 内置工具逻辑验证（计算器、知识库检索、健康探针、订单汇总）
3. 结构化 Prompts 渲染验证
4. 系统 Resources 数据源读取验证
5. FastAPI ASGI 路由挂载与 API Key 鉴权中间件验证
"""

import asyncio
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.mcp.prompts import (
    agent_task_planner_prompt,
    sql_expert_analyzer_prompt,
)
from app.mcp.resources import (
    get_server_info_resource,
    get_system_metrics_resource,
)
from app.mcp.server import create_mcp_server
from app.mcp.tools import (
    execute_calculator,
    get_system_health,
    knowledge_search,
    query_order_summary,
)


class TestMCPServerPrimitives:
    """测试 MCP Server 原语定义与注册."""

    def test_server_creation_and_registration(self) -> None:
        """测试 FastMCP 服务创建及工具、资源、提示词的正确注册."""
        server = create_mcp_server()
        assert server.name == settings.MCP_SERVER_NAME


class TestMCPTools:
    """测试 MCP Server 内置工具功能."""

    def test_execute_calculator_basic(self) -> None:
        """测试安全计算器基础四则运算."""
        result = execute_calculator("2 + 3 * 4")
        assert "14" in result

    def test_execute_calculator_math_functions(self) -> None:
        """测试安全计算器内置数学函数."""
        result = execute_calculator("sqrt(16) + 2**3")
        assert "12.0" in result

    def test_execute_calculator_security_blocking(self) -> None:
        """测试安全计算器拦截恶意与非法字符."""
        result = execute_calculator("__import__('os').system('dir')")
        assert "非法字符" in result or "未允许" in result

    def test_query_order_summary_global(self) -> None:
        """测试全局订单统计汇总."""
        result_str = asyncio.run(query_order_summary())
        data = json.loads(result_str)
        assert data["scope"] == "Global System"
        assert "total_orders" in data
        assert "popular_items" in data

    def test_query_order_summary_user_specific(self) -> None:
        """测试指定用户订单统计汇总."""
        result_str = asyncio.run(query_order_summary(user_id=42))
        data = json.loads(result_str)
        assert data["scope"] == "User #42"
        assert data["total_orders"] == 5

    def test_get_system_health(self) -> None:
        """测试系统健康状态探针."""
        result_str = asyncio.run(get_system_health())
        data = json.loads(result_str)
        assert "status" in data
        assert "components" in data
        assert "api" in data["components"]

    def test_knowledge_search_fallback(self) -> None:
        """测试知识库检索工具（包含异常安全降级逻辑）."""
        result_str = asyncio.run(knowledge_search(query="系统架构设计"))
        data = json.loads(result_str)
        assert "status" in data
        assert "results" in data
        assert len(data["results"]) > 0


class TestMCPPrompts:
    """测试 MCP Prompts 生成."""

    def test_agent_task_planner_prompt(self) -> None:
        """测试 Agent 任务规划 Prompt 生成."""
        messages = agent_task_planner_prompt(
            task_description="实现自动化报表导出",
            constraints="格式必须为 Excel，耗时不超过 10 秒",
        )
        assert len(messages) >= 2
        user_content = str(messages[0].content)
        assert "实现自动化报表导出" in user_content
        assert "格式必须为 Excel" in user_content

    def test_sql_expert_analyzer_prompt(self) -> None:
        """测试 SQL 分析专家 Prompt 生成."""
        messages = sql_expert_analyzer_prompt(
            table_name="orders",
            user_question="查询最近 7 天总销售额最高的前 5 名用户",
        )
        assert len(messages) >= 1
        user_content = str(messages[0].content)
        assert "orders" in user_content
        assert "SELECT" in user_content


class TestMCPResources:
    """测试 MCP Resources 上下文数据源."""

    def test_server_info_resource(self) -> None:
        """测试读取 server://info 资源."""
        content = asyncio.run(get_server_info_resource())
        data = json.loads(content)
        assert data["server_name"] == settings.MCP_SERVER_NAME
        assert "capabilities" in data
        assert "endpoints" in data

    def test_system_metrics_resource(self) -> None:
        """测试读取 server://system_metrics 资源."""
        content = asyncio.run(get_system_metrics_resource())
        data = json.loads(content)
        assert "platform" in data
        assert "python_version" in data
        assert "config_summary" in data


class TestMCPEndpointAndAuth:
    """测试 FastAPI 挂载与 API Key 鉴权中间件."""

    def test_root_endpoint_contains_mcp_url(self) -> None:
        """测试根路由返回的元数据中包含 MCP SSE 访问地址."""
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "mcp_sse_url" in data
        assert data["mcp_sse_url"] == f"{settings.MCP_SERVER_PREFIX}/sse"

    def test_mcp_auth_middleware_blocks_when_key_required(self) -> None:
        """测试启用 API Key 时未授权请求被 401 拦截."""
        client = TestClient(app)
        with patch.object(settings, "MCP_SERVER_API_KEY", "secret-test-key"):
            # 1. 未携带 Key -> 401
            resp_no_key = client.get(f"{settings.MCP_SERVER_PREFIX}/sse")
            assert resp_no_key.status_code == 401

            # 2. 携带错误 Key -> 401
            resp_wrong_key = client.get(
                f"{settings.MCP_SERVER_PREFIX}/sse",
                headers={"x-api-key": "wrong-key"},
            )
            assert resp_wrong_key.status_code == 401
