"""会话历史读模型的单元测试（纯函数部分，不依赖数据库）."""

from types import SimpleNamespace

from app.schemas.chat import _USER_MSG_MAX_LEN
from app.services.chat_history import (
    messages_from_projection,
    messages_from_rows,
)


def test_messages_from_rows_pairs_question_and_answer() -> None:
    """每条问答记录展开成 user + assistant 两条消息，顺序不能乱."""
    rows = [
        SimpleNamespace(question="第一问", answer="第一答"),
        SimpleNamespace(question="第二问", answer="第二答"),
    ]

    messages = messages_from_rows(rows)  # type: ignore[arg-type]

    assert [(message.role, message.content) for message in messages] == [
        ("user", "第一问"),
        ("assistant", "第一答"),
        ("user", "第二问"),
        ("assistant", "第二答"),
    ]


def test_messages_from_rows_handles_empty_history() -> None:
    """没有历史时返回空列表，而不是抛错."""
    assert messages_from_rows([]) == []


def test_messages_from_projection_maps_state() -> None:
    """投影状态按顺序映射成消息模型."""
    state = {
        "messages": [
            {"role": "user", "content": "你好", "seq": 2, "turn_id": "t1"},
            {"role": "assistant", "content": "你好，有什么可以帮你？", "seq": 3, "turn_id": "t1"},
        ]
    }

    messages = messages_from_projection(state)

    assert [(message.role, message.content) for message in messages] == [
        ("user", "你好"),
        ("assistant", "你好，有什么可以帮你？"),
    ]


def test_messages_from_projection_handles_empty_state() -> None:
    """空投影（新会话）返回空列表."""
    assert messages_from_projection({}) == []
    assert messages_from_projection({"messages": []}) == []


def test_empty_content_is_skipped_instead_of_raising() -> None:
    """空回复（例如纯工具调用没有文字输出）不能让整个历史接口 500."""
    rows = [SimpleNamespace(question="查一下天气", answer="")]

    messages = messages_from_rows(rows)  # type: ignore[arg-type]

    assert [(message.role, message.content) for message in messages] == [("user", "查一下天气")]


def test_script_tags_are_stripped() -> None:
    """含 <script> 的历史不能把 Message 校验炸掉，标签被去掉、正文保留."""
    rows = [SimpleNamespace(question="<script>alert(1)</script>这是问题", answer="回答")]

    messages = messages_from_rows(rows)  # type: ignore[arg-type]

    assert messages[0].content == "这是问题"
    assert messages[1].content == "回答"


def test_overlong_content_is_truncated() -> None:
    """超长历史截断到上限，而不是抛 ValidationError."""
    rows = [SimpleNamespace(question="问", answer="x" * (_USER_MSG_MAX_LEN + 500))]

    messages = messages_from_rows(rows)  # type: ignore[arg-type]

    assert len(messages) == 2
    assert len(messages[1].content) == _USER_MSG_MAX_LEN
    assert messages[1].content.endswith("…")


def test_unknown_role_falls_back_to_assistant() -> None:
    """事件里出现未知 role 时收敛成 assistant，不影响整段历史."""
    state = {"messages": [{"role": "tool", "content": "工具输出"}]}

    messages = messages_from_projection(state)

    assert [(message.role, message.content) for message in messages] == [("assistant", "工具输出")]
