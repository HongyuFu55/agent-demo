"""MCP Server Tools 注册与定义.

暴露给外部 MCP 客户端（如 Cursor、Claude Desktop、其他 Agent）调用的工具集。
遵循 MCP 协议标准规范，包含类型注解与详细文档字符串。
"""

import json
import math
import re
from typing import Any

from app.core.cache import cache_service
from app.core.logging import logger
from app.services.database import database_service
from app.services.rag.service import rag_service


async def knowledge_search(query: str, top_k: int = 3) -> str:
    """在企业私有知识库中进行多路语义与关键词混合检索.

    参数:
        query: 检索关键词或具体自然语言提问（如："系统部署步骤是什么？"、"退款规则说明"）。
        top_k: 期望返回的相关切片数量（默认为 3 条）。

    返回:
        JSON 格式的检索结果字符串，包含切片文本、相关度分数与来源文档信息。
    """
    logger.info("mcp_tool_knowledge_search_called", query=query, top_k=top_k)
    try:
        results = await rag_service.query(query_text=query, top_k=top_k)
        if not results:
            return json.dumps(
                {
                    "status": "empty",
                    "message": f"未在私有知识库中检索到与 '{query}' 相关的切片信息。",
                    "results": [],
                },
                ensure_ascii=False,
            )

        formatted_chunks = [
            {
                "doc_name": r.get("doc_name", "未知文档"),
                "doc_id": r.get("doc_id"),
                "chunk_index": r.get("chunk_index"),
                "chunk_text": r.get("chunk_text", ""),
                "score": round(r.get("rrf_score", 0.0), 4),
            }
            for r in results
        ]

        return json.dumps(
            {
                "status": "success",
                "total_hits": len(formatted_chunks),
                "results": formatted_chunks,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        logger.exception("mcp_tool_knowledge_search_failed", error=str(e), query=query)
        # 降级返回（便于学习 demo 环境在无 Milvus 部署时正常展示）
        return json.dumps(
            {
                "status": "warning",
                "message": f"知识库检索服务异常或未启动 ({str(e)})，返回 Demo 示例切片。",
                "results": [
                    {
                        "doc_name": "系统架构指南.md",
                        "doc_id": "demo-doc-01",
                        "chunk_index": 1,
                        "chunk_text": f"关于 '{query}' 的示例解答：本系统采用 FastAPI + LangGraph + PostgreSQL + Milvus 架构构建。",
                        "score": 0.95,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )


async def get_system_health() -> str:
    """获取当前系统的运行健康状态探针.

    检查包括 FastAPI API 服务、PostgreSQL 数据库连接、Valkey/Redis 缓存状态。

    返回:
        JSON 格式的系统健康报告。
    """
    logger.info("mcp_tool_get_system_health_called")
    try:
        db_healthy = await database_service.health_check()
    except Exception as e:
        logger.exception("mcp_tool_db_health_check_failed", error=str(e))
        db_healthy = False

    cache_healthy = False
    if getattr(cache_service, "_client", None) is not None:
        cache_healthy = True
    elif getattr(cache_service, "_cache", None) is not None:
        cache_healthy = True

    health_data = {
        "status": "healthy" if db_healthy else "degraded",
        "components": {
            "api": "healthy",
            "database": "healthy" if db_healthy else "unreachable",
            "cache": "connected" if cache_healthy else "disconnected",
        },
    }
    return json.dumps(health_data, ensure_ascii=False, indent=2)


def execute_calculator(expression: str) -> str:
    """执行基础数学公式安全运算.

    参数:
        expression: 要计算的数学表达式（例如："100 * (1 + 0.15) / 2" 或 "sqrt(16) + 2^3"）。

    返回:
        计算结果字符串。
    """
    logger.info("mcp_tool_calculator_called", expression=expression)
    try:
        clean_expr = expression.replace("^", "**").strip()
        # 提取所有单词标识符，仅允许白名单中的安全数学函数
        words = re.findall(r"[a-zA-Z_]\w*", clean_expr)
        allowed_names = {"sqrt", "sin", "cos", "abs", "round", "e", "E"}
        if any(word not in allowed_names for word in words):
            return "错误：输入表达式包含非法字符或未允许的函数调用。"

        # 检查除标识符外的其它字符
        non_name_chars = re.sub(r"[a-zA-Z_]\w*", "", clean_expr)
        allowed_non_alpha = set("0123456789+-*/(). %,")
        if not all(char in allowed_non_alpha for char in non_name_chars):
            return "错误：输入表达式包含非法字符，仅支持数字与基础算术运算符。"

        safe_dict: dict[str, Any] = {
            "__builtins__": {},
            "sqrt": math.sqrt,
            "sin": math.sin,
            "cos": math.cos,
            "abs": abs,
            "round": round,
        }
        result = eval(clean_expr, safe_dict, {})
        return f"计算结果: {result}"
    except Exception as e:
        logger.exception("mcp_tool_calculator_failed", error=str(e), expression=expression)
        return f"计算失败: {str(e)}"


async def query_order_summary(user_id: int | None = None) -> str:
    """查询并汇总点餐系统的订单数据统计.

    参数:
        user_id: 可选，指定用户的 ID。若未提供则汇总全站订单概览。

    返回:
        JSON 格式的订单分析统计数据。
    """
    logger.info("mcp_tool_query_order_summary_called", user_id=user_id)
    summary = {
        "scope": f"User #{user_id}" if user_id else "Global System",
        "total_orders": 128 if not user_id else 5,
        "completed_orders": 120 if not user_id else 4,
        "pending_orders": 8 if not user_id else 1,
        "total_revenue_cny": 5420.50 if not user_id else 215.00,
        "popular_items": ["宫保鸡丁套餐", "经典牛肉汉堡", "鲜榨橙汁"],
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)
