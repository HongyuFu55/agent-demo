"""db_query 工具的用户隔离与轮次配对测试."""

import asyncio

from langchain_core.utils.function_calling import convert_to_openai_tool

from app.core.langgraph.tools.db_query import (
    pair_turns,
    query_session_history,
    query_user_sessions,
)
from app.schemas.chat import Message


def make_message(role: str, content: str) -> Message:
    """构造一条消息（测试用）."""
    return Message(role=role, content=content)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 隔离机制


def llm_visible_parameters(candidate) -> set[str]:  # type: ignore[no-untyped-def]
    """返回模型真正能看到的参数名.

    参数：
        candidate: 被检查的工具。

    返回：
        set[str]: 参数名集合。
    """
    # convert_to_openai_tool 就是 bind_tools 内部用的那个转换，
    # 断言它等于断言"模型真正看到的参数表"
    definition = convert_to_openai_tool(candidate)
    return set(definition["function"]["parameters"]["properties"])


def test_user_id_is_hidden_from_llm_schema() -> None:
    """user_id 不能出现在模型可见的 schema 里，否则隔离等于靠模型自觉."""
    for candidate in (query_session_history, query_user_sessions):
        assert "user_id" not in llm_visible_parameters(candidate), f"{candidate.name} 把 user_id 暴露给了模型"


def test_thread_id_is_hidden_but_session_id_is_not() -> None:
    """当前会话 ID 由系统注入；session_id 仍可由模型显式指定（查别的会话）."""
    parameters = llm_visible_parameters(query_session_history)

    assert "thread_id" not in parameters
    assert "session_id" in parameters


def test_session_history_requires_caller_identity() -> None:
    """没有注入用户身份时必须拒绝执行（fail closed）."""
    result = asyncio.run(query_session_history.ainvoke({"session_id": "any-session"}))

    assert "无法识别当前用户身份" in result


def test_session_history_without_any_session_is_rejected() -> None:
    """既没给 session_id 也没有当前会话时，必须明确报错而不是查全表."""
    result = asyncio.run(query_session_history.ainvoke({"user_id": "1"}))

    assert "无法确定当前会话" in result


def test_user_sessions_requires_caller_identity() -> None:
    """同一套规则适用于会话列表工具."""
    result = asyncio.run(query_user_sessions.ainvoke({}))

    assert "无法识别当前用户身份" in result


def test_non_numeric_caller_is_rejected() -> None:
    """注入的身份不是合法用户 ID 时也不能放行."""
    result = asyncio.run(query_session_history.ainvoke({"session_id": "any-session", "user_id": "admin"}))

    assert "无法识别当前用户身份" in result


# ---------------------------------------------------------------- 轮次配对


def test_pair_turns_pairs_question_and_answer() -> None:
    """正常的 user/assistant 交替还原成问答对."""
    messages = [
        make_message("user", "第一问"),
        make_message("assistant", "第一答"),
        make_message("user", "第二问"),
        make_message("assistant", "第二答"),
    ]

    assert pair_turns(messages) == [("第一问", "第一答"), ("第二问", "第二答")]


def test_pair_turns_keeps_trailing_question() -> None:
    """结尾的用户消息没有回答时也要保留（不能悄悄丢掉用户说过的话）."""
    messages = [make_message("user", "问"), make_message("assistant", "答"), make_message("user", "追问")]

    assert pair_turns(messages) == [("问", "答"), ("追问", "")]


def test_pair_turns_handles_consecutive_questions() -> None:
    """连续两条用户消息：前一条记成"没有回答的轮次"."""
    messages = [make_message("user", "问一"), make_message("user", "问二"), make_message("assistant", "答二")]

    assert pair_turns(messages) == [("问一", ""), ("问二", "答二")]


def test_pair_turns_handles_answer_without_question() -> None:
    """历史被清空后可能出现"孤立的回答"，不能丢也不能崩."""
    messages = [make_message("assistant", "孤立的回答")]

    assert pair_turns(messages) == [("", "孤立的回答")]


def test_pair_turns_handles_empty_input() -> None:
    """空历史返回空列表."""
    assert pair_turns([]) == []
