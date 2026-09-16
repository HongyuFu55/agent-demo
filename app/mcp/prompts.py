"""MCP Server Prompts 注册与定义.

提供结构化、可复用的 Prompt 模板（Prompts），供外部 MCP 客户端按需生成标准提示词。
"""

from mcp.server.fastmcp.prompts.base import (
    AssistantMessage,
    Message,
    UserMessage,
)

from app.core.logging import logger


def agent_task_planner_prompt(task_description: str, constraints: str = "") -> list[Message]:
    """生成复杂任务的 Agent 拆解与多步执行规划提示词.

    参数:
        task_description: 需要规划与执行的核心任务目标描述。
        constraints: 可选，约束条件（例如时间限制、使用特定工具、输出格式要求等）。

    返回:
        格式化后的 MCP Message 对话历史列表。
    """
    logger.info("mcp_prompt_task_planner_rendered", task_length=len(task_description))

    constraint_text = f"\n### 约束条件与安全规范\n{constraints}" if constraints else ""

    content = f"""你是一名资深 AI 架构师与任务规划专家。请针对以下用户目标，输出结构化的执行计划：

### 任务目标
{task_description}
{constraint_text}

### 规划输出规范
1. **目标确认**：用一句话精炼重述目标与成功标准。
2. **步骤拆解**：按时间先后将任务分解为 3-6 个原子可执行步骤。
3. **工具与依赖**：为每个步骤明确指出所需调用的工具（如 RAG 检索、计算器、DB 查询等）。
4. **风险与回滚**：指出可能出现的失败点及应对预案。

请立即按上述规范生成清晰的 Markdown 执行清单。"""

    return [
        UserMessage(content=content),
        AssistantMessage(content="我已经理解了任务规划的目标和规范，现在为您生成详细的执行规划：\n\n"),
    ]


def sql_expert_analyzer_prompt(table_name: str, user_question: str) -> list[Message]:
    """生成 PostgreSQL 数据查询与分析专家提示词.

    参数:
        table_name: 待查询的目标数据库表名称（例如："users", "orders", "menu_items"）。
        user_question: 用户的自然语言业务查询问题。

    返回:
        格式化后的 MCP Message 对话历史列表。
    """
    logger.info("mcp_prompt_sql_analyzer_rendered", table_name=table_name)

    content = f"""你是一名精通 PostgreSQL 的数据库工程师与数据分析专家。

### 查询上下文
- 目标数据表: `{table_name}`
- 业务问题: {user_question}

### 任务要求
1. 生成标准、高效且符合 PostgreSQL 语法的 SQL 查询语句。
2. **只允许生成只读（SELECT）语句**，严格禁止任何 INSERT、UPDATE、DELETE、DROP、ALTER 等写操作或变更操作。
3. 详细解释 SQL 中各字段过滤、聚合函数及索引命中建议。
4. 预估可能的数据量与执行性能风险。"""

    return [
        UserMessage(content=content),
    ]
