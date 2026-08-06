"""数据库查询与 Text-to-SQL 工具集.

包含已知会话/历史消息检索工具以及带有严格只读安全校验的 Text-to-SQL 动态查询工具。
复用项目已有的 database_service 与 SQLModel ORM 架构。
"""

import re
from typing import Any, List, Optional
from langchain_core.tools import tool
from sqlalchemy import text
from sqlmodel import Session, select, col
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import logger
from app.models.chat_message import ChatMessage
from app.models.session import Session as ChatSession
from app.services.database import database_service

# 危险关键字黑名单，严格禁止任何写/修改/删除/结构变更操作
DANGEROUS_SQL_KEYWORDS = {
    "drop",
    "delete",
    "update",
    "insert",
    "alter",
    "truncate",
    "grant",
    "revoke",
    "create",
    "exec",
    "execute",
    "copy",
}

# 数据库 Schema 描述，用于提供给 LLM 进行 Text-to-SQL 生成
DB_SCHEMA_DESCRIPTION = """
数据库只读查询 Schema 说明（仅支持 SELECT 查询）：

1. 表 "user" (用户信息表):
   - id (INTEGER, 主键)
   - email (VARCHAR, 用户邮箱)
   - username (VARCHAR, 用户名)
   - created_at (DATETIME, 注册时间)

2. 表 "session" (聊天会话表):
   - id (VARCHAR, 会话ID, UUID)
   - user_id (INTEGER, 所属用户ID, 关联 "user".id)
   - name (VARCHAR, 会话主题/名称)
   - username (VARCHAR, 副本用户名)
   - created_at (DATETIME, 创建时间)

3. 表 "chat_message" (聊天问答记录表):
   - id (INTEGER, 主键)
   - session_id (VARCHAR, 所属会话ID, 关联 "session".id)
   - user_id (INTEGER, 用户ID, 关联 "user".id)
   - question (TEXT, 用户提问)
   - answer (TEXT, AI回答)
   - created_at (DATETIME, 消息发送时间)
"""


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def query_session_history(session_id: str, limit: int = 10) -> str:
    """查询指定聊天会话 (session_id) 内的历史对话问答记录。

    参数：
        session_id: 聊天会话 UUID 标识符，例如 "d330cec5-fd23-44f0-80ec-cb707405fcf6"
        limit: 最多返回的消息轮数（默认 10 轮）
    """
    if not session_id or not session_id.strip():
        return "错误：未传入有效的 session_id。"

    try:
        with Session(database_service.engine) as session:
            statement = (
                select(ChatMessage)
                .where(col(ChatMessage.session_id) == session_id.strip())
                .order_by(col(ChatMessage.created_at).desc())
                .limit(limit)
            )
            messages = session.exec(statement).all()

        if not messages:
            return f"未在会话 '{session_id}' 中查找到任何历史消息记录。"

        # 按时间正序排列展示
        messages.reverse()
        formatted_dialogues = []
        for idx, msg in enumerate(messages, 1):
            formatted_dialogues.append(
                f"【轮次 {idx}】({msg.created_at.strftime('%Y-%m-%d %H:%M:%S') if msg.created_at else '未知时间'})\n"
                f"[用户提问] {msg.question}\n"
                f"[AI回答] {msg.answer}"
            )

        logger.info("query_session_history_success", session_id=session_id, count=len(messages))
        return f"--- 会话 [{session_id}] 历史记录 (共 {len(messages)} 轮) ---\n\n" + "\n\n".join(formatted_dialogues)

    except Exception as e:
        logger.exception("query_session_history_failed", session_id=session_id, error=str(e))
        return f"查询会话历史发生异常：{str(e)}"


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def query_user_sessions(user_id: int, limit: int = 5) -> str:
    """查询指定用户 (user_id) 零散创建的历史会话列表与会话名称。

    参数：
        user_id: 用户 ID 整数，例如 12
        limit: 返回的最新会话个数限制（默认 5）
    """
    try:
        with Session(database_service.engine) as session:
            statement = (
                select(ChatSession)
                .where(col(ChatSession.user_id) == user_id)
                .order_by(col(ChatSession.created_at).desc())
                .limit(limit)
            )
            sessions = session.exec(statement).all()

        if not sessions:
            return f"用户 (ID: {user_id}) 目前没有任何历史会话记录。"

        session_list = []
        for s in sessions:
            time_str = s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "未知时间"
            session_list.append(f"- 会话名称: '{s.name or '未命名会话'}' | SessionID: {s.id} | 创建时间: {time_str}")

        logger.info("query_user_sessions_success", user_id=user_id, count=len(sessions))
        return f"用户 (ID: {user_id}) 的历史会话列表（最新 {len(sessions)} 个）：\n" + "\n".join(session_list)

    except Exception as e:
        logger.exception("query_user_sessions_failed", user_id=user_id, error=str(e))
        return f"查询用户会话列表发生异常：{str(e)}"


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def execute_sql_query(sql_query: str) -> str:
    """在 PostgreSQL 数据库中安全执行只读 SELECT SQL 查询（Text-to-SQL 动态数据分析核心工具）。

    只允许执行单条 SELECT 只读查询。

    数据库架构说明：
    1. 表 "user" (用户信息表):
       - id (INTEGER, 主键)
       - email (VARCHAR, 用户邮箱)
       - username (VARCHAR, 用户名)
       - created_at (DATETIME, 注册时间)

    2. 表 "session" (聊天会话表):
       - id (VARCHAR, 会话ID, UUID)
       - user_id (INTEGER, 所属用户ID, 关联 "user".id)
       - name (VARCHAR, 会话主题/名称)
       - username (VARCHAR, 副本用户名)
       - created_at (DATETIME, 创建时间)

    3. 表 "chat_message" (聊天问答记录表):
       - id (INTEGER, 主键)
       - session_id (VARCHAR, 所属会话ID, 关联 "session".id)
       - user_id (INTEGER, 用户ID, 关联 "user".id)
       - question (TEXT, 用户提问)
       - answer (TEXT, AI回答)
       - created_at (DATETIME, 消息发送时间)

    参数：
        sql_query: 需要执行的单条 SELECT SQL 语句，例如 'SELECT count(*) FROM "user"'
    """
    if not sql_query or not sql_query.strip():
        return "错误：未传入有效的 SQL 语句。"

    raw_sql = sql_query.strip()

    # 1. 安全防范：防分号多语句注入拼接
    if ";" in raw_sql.rstrip(";"):
        logger.warning("sql_security_blocked_semicolon", sql=raw_sql)
        return "安全拦截：禁止使用分号 ';' 拼接执行多条 SQL 语句！"

    # 2. 安全防范：强制只允许 SELECT 开头
    clean_sql = raw_sql.rstrip(";").strip()
    if not clean_sql.lower().startswith("select"):
        logger.warning("sql_security_blocked_non_select", sql=raw_sql)
        return f"安全拦截：只允许执行 SELECT 只读查询语句！传入语句不可用：'{clean_sql[:30]}...'"

    # 3. 安全防范：黑名单关键字扫描
    words = set(re.findall(r"\w+", clean_sql.lower()))
    intersection = words.intersection(DANGEROUS_SQL_KEYWORDS)
    if intersection:
        logger.warning("sql_security_blocked_dangerous_keyword", sql=raw_sql, keywords=list(intersection))
        return f"安全拦截：检测到危险/写操作关键字 {list(intersection)}，已禁止执行！"

    # 4. LIMIT 自动兜底保护
    if "limit" not in clean_sql.lower():
        clean_sql += " LIMIT 50"

    logger.info("executing_text_to_sql", sql=clean_sql)

    try:
        with Session(database_service.engine) as session:
            result = session.exec(text(clean_sql))
            keys = list(result.keys())
            rows = result.all()

        if not rows:
            return f"SQL 查询执行成功，但没有匹配到任何数据。\n执行 SQL: `{clean_sql}`"

        # 格式化输出为 Markdown 表格
        header_row = "| " + " | ".join(keys) + " |"
        sep_row = "| " + " | ".join(["---"] * len(keys)) + " |"
        data_rows = []
        for r in rows[:50]:
            row_str = "| " + " | ".join([str(val) if val is not None else "NULL" for val in r]) + " |"
            data_rows.append(row_str)

        table_md = "\n".join([header_row, sep_row] + data_rows)
        logger.info("text_to_sql_success", rows_count=len(rows))
        return f"--- SQL 查询结果 (共 {len(rows)} 行) ---\n`{clean_sql}`\n\n{table_md}"

    except Exception as e:
        logger.exception("text_to_sql_failed", sql=clean_sql, error=str(e))
        return f"[ERROR] SQL 执行报错：{str(e)}\n请检查表名/列名拼写是否正确。（注意：表名 \"user\" 必须加双引号）"
