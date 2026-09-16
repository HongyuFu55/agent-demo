"""标准 SSE 结构化事件帧 (Typed SSE Events) 单元与集成测试.

测试范围：
1. SSEEvent 与 SSEEventType 模型的序列化与 W3C 规范检验
2. 向后兼容性字段验证（保证旧客户端仍可正确提取 content 与 done）
3. 模拟工具调用时 TOOL_START、TOOL_END、TOKEN 与 DONE 事件帧的顺序与载荷验证
4. FastAPI /api/v1/chatbot/chat/stream 端点的响应头与 SSE 格式协议测试
"""

import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api.v1.auth import get_current_user
from app.main import app
from app.models.session import Session
from app.models.user import User
from app.schemas.sse import SSEEvent, SSEEventType


class TestSSEModels:
    """测试 SSE 数据结构与序列化方法."""

    def test_sse_event_token_packet(self) -> None:
        """测试文本 token 事件帧输出格式."""
        event = SSEEvent(
            event=SSEEventType.TOKEN,
            content="你好，世界！",
            done=False,
        )
        packet = event.to_sse_packet()
        assert packet.startswith("event: token\n")
        assert "data: " in packet
        assert packet.endswith("\n\n")

        # 校验 data 载荷 JSON
        lines = packet.strip().split("\n")
        assert lines[0] == "event: token"
        assert lines[1].startswith("data: ")
        data = json.loads(lines[1][6:])
        assert data["type"] == "token"
        assert data["content"] == "你好，世界！"
        assert data["done"] is False

    def test_sse_event_tool_start_packet(self) -> None:
        """测试工具调用启动事件帧输出."""
        event = SSEEvent(
            event=SSEEventType.TOOL_START,
            tool_name="query_knowledge_base",
            tool_display_name="检索企业私有知识库",
            tool_args={"query": "系统部署"},
            tool_call_id="call_123456",
        )
        packet = event.to_sse_packet()
        assert "event: tool_start\n" in packet
        lines = packet.strip().split("\n")
        data = json.loads(lines[1][6:])
        assert data["type"] == "tool_start"
        assert data["tool_name"] == "query_knowledge_base"
        assert data["tool_display_name"] == "检索企业私有知识库"
        assert data["tool_args"] == {"query": "系统部署"}
        # 向后兼容：content 为空字符串，done 为 false
        assert data["content"] == ""
        assert data["done"] is False

    def test_sse_event_tool_end_packet(self) -> None:
        """测试工具执行完毕事件帧输出."""
        event = SSEEvent(
            event=SSEEventType.TOOL_END,
            tool_name="calculator",
            tool_display_name="数值计算",
            duration_ms=125.4,
            tool_result="计算结果: 100",
        )
        packet = event.to_sse_packet()
        assert "event: tool_end\n" in packet
        lines = packet.strip().split("\n")
        data = json.loads(lines[1][6:])
        assert data["type"] == "tool_end"
        assert data["duration_ms"] == 125.4
        assert data["tool_result"] == "计算结果: 100"

    def test_sse_event_interrupt_packet(self) -> None:
        """测试人机协同审批中断事件帧输出."""
        event = SSEEvent(
            event=SSEEventType.INTERRUPT,
            content="需要人工确认是否发送邮件",
            interrupt_payload={"action": "send_email", "to": "test@example.com"},
        )
        packet = event.to_sse_packet()
        assert "event: interrupt\n" in packet
        lines = packet.strip().split("\n")
        data = json.loads(lines[1][6:])
        assert data["type"] == "interrupt"
        assert data["interrupt_payload"]["action"] == "send_email"
        assert data["content"] == "需要人工确认是否发送邮件"

    def test_sse_event_done_packet(self) -> None:
        """测试流式结束事件帧输出."""
        event = SSEEvent(
            event=SSEEventType.DONE,
            content="",
            done=True,
        )
        packet = event.to_sse_packet()
        assert "event: done\n" in packet
        lines = packet.strip().split("\n")
        data = json.loads(lines[1][6:])
        assert data["type"] == "done"
        assert data["done"] is True


class TestSSEStreamEndpoint:
    """测试 /api/v1/chatbot/chat/stream 端点的完整 SSE 协议输出."""

    def test_stream_endpoint_returns_event_stream_headers(self) -> None:
        """测试流式接口返回的媒体类型与防缓冲响应头."""
        client = TestClient(app)

        mock_user = User(
            id=1,
            email="test_user@example.com",
            username="TestUser",
            hashed_password="fake-hashed-password",
        )
        mock_session = Session(
            id="session_test_123",
            user_id=1,
            name="测试会话",
            username="TestUser",
        )

        async def fake_stream_response(*args, **kwargs):
            yield SSEEvent(
                event=SSEEventType.TOOL_START,
                type=SSEEventType.TOOL_START.value,
                tool_name="calculator",
                tool_display_name="数值计算",
                content="",
                done=False,
            )
            yield SSEEvent(
                event=SSEEventType.TOOL_END,
                type=SSEEventType.TOOL_END.value,
                tool_name="calculator",
                tool_display_name="数值计算",
                duration_ms=45.0,
                tool_result="42",
                content="",
                done=False,
            )
            yield SSEEvent(
                event=SSEEventType.TOKEN,
                type=SSEEventType.TOKEN.value,
                content="计算得出结果是 42。",
                done=False,
            )
            yield SSEEvent(
                event=SSEEventType.DONE,
                type=SSEEventType.DONE.value,
                content="",
                done=True,
            )

        app.dependency_overrides[get_current_user] = lambda: mock_user

        with patch("app.api.v1.chatbot.get_owned_session", return_value=mock_session), \
             patch("app.api.v1.chatbot.agent.get_stream_response", side_effect=fake_stream_response), \
             patch("app.api.v1.chatbot.database_service.create_chat_message", new_callable=AsyncMock):

            response = client.post(
                "/api/v1/chatbot/chat/stream",
                json={
                    "session_id": "session_test_123",
                    "messages": [{"role": "user", "content": "计算 6 * 7"}],
                },
            )

            # 1. 验证 HTTP 状态与响应头
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
            assert response.headers.get("cache-control") == "no-cache"

            # 2. 验证响应流中收到的 SSE 事件序列
            raw_text = response.text
            assert "event: tool_start" in raw_text
            assert "event: tool_end" in raw_text
            assert "event: token" in raw_text
            assert "event: done" in raw_text
            assert "计算得出结果是 42。" in raw_text

        app.dependency_overrides.clear()
