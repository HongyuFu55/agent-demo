"""数据库查询与 Text-to-SQL 工具集.

包含已知会话/历史消息检索工具以及带有严格只读安全校验的 Text-to-SQL 动态查询工具。
复用项目已有的 database_service 与 SQLModel ORM 架构。

关于"当前用户"
--------------
这两个查会话的工具都带一个 `user_id: Annotated[str, InjectedToolArg]` 参数：
    - 它被 InjectedToolArg 标记，**不会出现在 LLM 看到的工具 schema 里**；
    - 由 graph.py 的工具执行层在调用前注入调用者身份；
    - 工具内部先用它校验会话归属，再读数据。

为什么不能把 user_id 暴露给模型？
    模型能填任何参数，包括别人的 user_id。只要 user_id 在 schema 里，
    "隔离"就等于"请模型自觉"。隐藏 + 执行层注入之后，模型根本无法表达
    "查别人的会话"这个请求 —— 这是协议层的约束，不是提示词约束。
"""

from typing import (
    Annotated,
    Optional,
    Sequence,
)
import re

from langchain_core.tools import (
    InjectedToolArg,
    tool,
)
from sqlalchemy import text
from sqlmodel import (
    Session,
    col,
    select,
)
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import logger
from app.models.session import Session as ChatSession
from app.schemas.chat import Message
from app.services.chat_history import read_history
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


def _parse_caller_id(value: object) -> Optional[int]:
    """把注入进来的调用者标识解析成用户 ID.

    参数：
        value: 执行层注入的 user_id（字符串）。

    返回：
        Optional[int]: 解析成功返回用户 ID；缺失或非法返回 None（调用方据此拒绝执行）。
    """
    text_value = str(value or "").strip()
    if not text_value.isdigit():
        return None
    return int(text_value)


def pair_turns(messages: Sequence[Message]) -> list[tuple[str, str]]:
    """把 user/assistant 交替的消息序列还原成"问 + 答"的轮次.

    投影里存的是扁平消息列表，工具要的是成对的问答。异常情况这样处理：
        - 连续两条用户消息：前一条算作"没有回答的轮次"；
        - 只有回答没有提问：记为"没有提问的轮次"；
        - 结尾的用户消息没有回答：保留，标为空回答。

    参数：
        messages: 按时间升序的消息列表。

    返回：
        list[tuple[str, str]]: (提问, 回答) 列表。
    """
    turns: list[tuple[str, str]] = []
    pending_question: Optional[str] = None

    for message in messages:
        if message.role == "user":
            if pending_question is not None:
                turns.append((pending_question, ""))
            pending_question = message.content
        elif message.role == "assistant":
            turns.append((pending_question or "", message.content))
            pending_question = None

    if pending_question is not None:
        turns.append((pending_question, ""))

    return turns


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def query_session_history(
    session_id: str = "",
    limit: int = 10,
    user_id: Annotated[str, InjectedToolArg] = "",
    thread_id: Annotated[str, InjectedToolArg] = "",
) -> str:
    """查询当前用户某个会话里的历史对话记录.

    数据来源与会话列表接口一致（事件日志的 messages 投影）。

    参数：
        session_id: 会话 UUID。**不传则查询"当前正在聊的这个会话"**；
                    要查别的会话，先用 query_user_sessions 拿到 SessionID 再传进来。
        limit: 最多返回多少轮问答（默认 10 轮）

    说明：用户身份与当前会话由系统注入，调用方不需要也无法指定。
    """
    caller_id = _parse_caller_id(user_id)
    if caller_id is None:
        logger.warning("session_history_missing_caller")
        return "错误：无法识别当前用户身份，已拒绝查询会话历史。"

    # 模型给了 session_id 就用它（后面还要校验归属）；
    # 没给就落到"当前会话"——否则模型根本不知道当前会话的 UUID，这个工具等于不可用
    session_key = (session_id or "").strip() or (thread_id or "").strip()
    if not session_key:
        return "错误：未传入有效的 session_id，且无法确定当前会话。"

    try:
        # 先校验归属：不是自己的会话，直接当作"不存在"，不泄露会话是否存在
        owned = await database_service.get_user_session(session_key, caller_id)
        if owned is None:
            logger.warning(
                "session_history_access_denied",
                session_id=session_key,
                user_id=caller_id,
            )
            return f"未找到会话 '{session_key}'，或该会话不属于当前用户。"

        history = await read_history(session_key, caller_id)
        all_turns = pair_turns(history.messages)
        turns = all_turns[-max(limit, 1) :] if all_turns else []

        if not turns:
            return f"会话 '{session_key}' 中还没有任何对话记录。"

        lines = [
            f"--- 会话 [{session_key}] 历史记录（最近 {len(turns)} 轮 / 共 {len(all_turns)} 轮）---",
            "",
        ]
        for index, (question, answer) in enumerate(turns, 1):
            lines.append(f"【轮次 {index}】")
            lines.append(f"[用户提问] {question or '（该轮没有提问内容）'}")
            lines.append(f"[AI回答] {answer or '（该轮没有文字回答）'}")
            lines.append("")

        logger.info(
            "query_session_history_success",
            session_id=session_key,
            user_id=caller_id,
            turns=len(turns),
            source=history.source.value,
        )
        return "\n".join(lines).strip()

    except Exception as e:
        logger.exception("query_session_history_failed", session_id=session_key, error=str(e))
        return f"查询会话历史发生异常：{str(e)}"


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def query_user_sessions(
    limit: int = 5,
    user_id: Annotated[str, InjectedToolArg] = "",
) -> str:
    """查询当前用户自己最近创建的会话列表与会话名称.

    参数：
        limit: 返回的最新会话个数限制（默认 5）

    说明：用户身份由系统注入，调用方无法指定要查哪个用户。
    """
    caller_id = _parse_caller_id(user_id)
    if caller_id is None:
        logger.warning("user_sessions_missing_caller")
        return "错误：无法识别当前用户身份，已拒绝查询会话列表。"

    try:
        with Session(database_service.engine) as session:
            statement = (
                select(ChatSession)
                .where(col(ChatSession.user_id) == caller_id)
                .order_by(col(ChatSession.created_at).desc())
                .limit(max(limit, 1))
            )
            sessions = session.exec(statement).all()

        if not sessions:
            return "你目前还没有任何历史会话记录。"

        session_list = []
        for s in sessions:
            time_str = s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "未知时间"
            session_list.append(f"- 会话名称: '{s.name or '未命名会话'}' | SessionID: {s.id} | 创建时间: {time_str}")

        logger.info("query_user_sessions_success", user_id=caller_id, count=len(sessions))
        return f"你的历史会话列表（最新 {len(sessions)} 个）：\n" + "\n".join(session_list)

    except Exception as e:
        logger.exception("query_user_sessions_failed", user_id=caller_id, error=str(e))
        return f"查询会话列表发生异常：{str(e)}"


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
            result = session.execute(text(clean_sql))
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
