"""P0 时序正确性收敛（四步提交）核心验证测试.

包含以下 3 个关键测试：
1. 正常流程四步提交：验证完成信号 DONE 包含 committed=True、message_id，且在落盘后发出；
2. 异常失败不宣告：当数据库持久化抛出异常时，必须产出 ERROR 帧，绝不允许发射 DONE 帧；
3. 落盘降级可见性：当事件日志写入返回失败时，DONE 帧中的 metadata.committed 为 False。
"""

import json
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from app.api.v1.auth import get_current_user
from app.main import app
from app.models.chat_message import ChatMessage
from app.models.session import Session
from app.models.user import User
from app.schemas.sse import SSEEvent, SSEEventType


@pytest.fixture
def client() -> TestClient:
    """提供 FastAPI TestClient 实例."""
    return TestClient(app)


@pytest.fixture
def mock_user() -> User:
    """提供测试用 User 对象."""
    return User(
        id=1,
        email="p0_test@example.com",
        username="P0User",
        hashed_password="fake-hashed-password",
    )


@pytest.fixture
def mock_session() -> Session:
    """提供测试用 Session 对象."""
    return Session(
        id="session_p0_test",
        user_id=1,
        name="P0测试会话",
        username="P0User",
    )


def _parse_sse_events(raw_text: str) -> list[dict]:
    """辅助函数：将原始 SSE 文本解析为结构化字典列表."""
    events = []
    lines = raw_text.strip().split("\n")
    current_event = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str:
                data = json.loads(data_str)
                data["_event"] = current_event or data.get("event")
                events.append(data)
    return events


class TestP0StreamSettlement:
    """P0 四步提交时序测试套件."""

    def test_stream_settlement_success_order(
        self, client: TestClient, mock_user: User, mock_session: Session
    ) -> None:
        """测试 1：断言正常流式下，四步提交成功，DONE 帧携带 committed=True 和 message_id."""
        app.dependency_overrides[get_current_user] = lambda: mock_user

        async def fake_stream(*args, **kwargs):
            yield SSEEvent(event=SSEEventType.TOKEN, content="测试四步提交回答", done=False)
            yield SSEEvent(event=SSEEventType.DONE, content="", done=True)

        mock_chat_message = ChatMessage(
            id=999,
            session_id=mock_session.id,
            user_id=mock_user.id,
            question="测试问题",
            answer="测试四步提交回答",
        )

        with patch("app.api.v1.chatbot.get_owned_session", return_value=mock_session), \
             patch("app.api.v1.chatbot.agent.get_stream_response", side_effect=fake_stream), \
             patch("app.api.v1.chatbot.database_service.create_chat_message", new_callable=AsyncMock, return_value=mock_chat_message) as mock_create_msg, \
             patch("app.services.events.store.event_store.append", new_callable=AsyncMock, return_value=None):

            response = client.post(
                "/api/v1/chatbot/chat/stream",
                json={
                    "session_id": mock_session.id,
                    "messages": [{"role": "user", "content": "测试问题"}],
                },
            )

            assert response.status_code == 200
            events = _parse_sse_events(response.text)
            
            # 必须调用了数据库落库
            mock_create_msg.assert_awaited_once()

            # 最后一个事件必须是 DONE
            assert len(events) >= 2
            done_event = events[-1]
            assert done_event["event"] == "done"
            assert done_event["done"] is True
            assert "metadata" in done_event
            assert done_event["metadata"]["committed"] is True
            assert done_event["metadata"]["message_id"] == 999
            assert "turn_id" in done_event["metadata"]

        app.dependency_overrides.clear()

    def test_stream_settlement_failure_no_done(
        self, client: TestClient, mock_user: User, mock_session: Session
    ) -> None:
        """测试 2：断言持久化异常时，只发送 ERROR 帧，绝不发送 DONE 帧."""
        app.dependency_overrides[get_current_user] = lambda: mock_user

        async def fake_stream(*args, **kwargs):
            yield SSEEvent(event=SSEEventType.TOKEN, content="即将落库失败的回复", done=False)

        with patch("app.api.v1.chatbot.get_owned_session", return_value=mock_session), \
             patch("app.api.v1.chatbot.agent.get_stream_response", side_effect=fake_stream), \
             patch("app.api.v1.chatbot.database_service.create_chat_message", new_callable=AsyncMock, side_effect=RuntimeError("数据库崩溃")), \
             patch("app.services.events.store.event_store.append", new_callable=AsyncMock):

            response = client.post(
                "/api/v1/chatbot/chat/stream",
                json={
                    "session_id": mock_session.id,
                    "messages": [{"role": "user", "content": "测试崩溃"}],
                },
            )

            assert response.status_code == 200
            events = _parse_sse_events(response.text)

            # 断言包含 ERROR 帧
            error_events = [e for e in events if e.get("event") == "error"]
            assert len(error_events) == 1
            assert "数据库崩溃" in error_events[0]["content"]

            # 断言绝对没有 DONE 帧！杜绝假成功宣告
            done_events = [e for e in events if e.get("event") == "done"]
            assert len(done_events) == 0

        app.dependency_overrides.clear()

    def test_stream_settlement_uncommitted_fallback(
        self, client: TestClient, mock_user: User, mock_session: Session
    ) -> None:
        """测试 3：断言当事件追加失败时，DONE 帧正常返回但 committed 降级为 False."""
        app.dependency_overrides[get_current_user] = lambda: mock_user

        async def fake_stream(*args, **kwargs):
            yield SSEEvent(event=SSEEventType.TOKEN, content="回复文本", done=False)

        mock_chat_message = ChatMessage(
            id=1001,
            session_id=mock_session.id,
            user_id=mock_user.id,
            question="测试降级",
            answer="回复文本",
        )

        with patch("app.api.v1.chatbot.get_owned_session", return_value=mock_session), \
             patch("app.api.v1.chatbot.agent.get_stream_response", side_effect=fake_stream), \
             patch("app.api.v1.chatbot.database_service.create_chat_message", new_callable=AsyncMock, return_value=mock_chat_message), \
             patch("app.services.events.store.event_store.append", new_callable=AsyncMock, side_effect=Exception("事件表锁定")):

            response = client.post(
                "/api/v1/chatbot/chat/stream",
                json={
                    "session_id": mock_session.id,
                    "messages": [{"role": "user", "content": "测试降级"}],
                },
            )

            assert response.status_code == 200
            events = _parse_sse_events(response.text)

            done_events = [e for e in events if e.get("event") == "done"]
            assert len(done_events) == 1
            # 事件日志落盘失败，committed 必须为 False
            assert done_events[0]["metadata"]["committed"] is False
            assert done_events[0]["metadata"]["message_id"] == 1001

        app.dependency_overrides.clear()

