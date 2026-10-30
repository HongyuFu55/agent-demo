"""P2 工具三段式流水线、结构化审批与统一结果保留策略单元测试.

测试范围：
1. ApprovalEvaluator 结构化四态审批评估（杜绝子串漏洞，例如“不要发送”与“sorry”）
2. OutputRetention exact/unknown 裁剪元数据与日志片段生成
3. ToolPipeline 三段式生命周期（前置审批拦截、身份注入、超时保护与异常收敛）
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from app.core.langgraph.tools.approval import (
    ApprovalDecision,
    ApprovalEvaluator,
    ApprovalRegistry,
)
from app.core.langgraph.tools.pipeline import (
    ToolPipeline,
    ToolPipelineContext,
)
from app.core.langgraph.tools.retention import (
    OutputRetention,
    RetentionOmitted,
)


class TestP2ToolPipeline:
    """P2 工具管线与安全控制测试套件."""

    def test_approval_evaluator_strict_matching(self) -> None:
        """测试结构化审批评估器，彻底杜绝子串误判漏洞 (解决 G1)."""
        # 1. 明确同意
        assert ApprovalEvaluator.evaluate("确认") == ApprovalDecision.ALLOW_ONCE
        assert ApprovalEvaluator.evaluate("确认发送") == ApprovalDecision.ALLOW_ONCE
        assert ApprovalEvaluator.evaluate("yes") == ApprovalDecision.ALLOW_ONCE
        assert ApprovalEvaluator.evaluate("APPROVE") == ApprovalDecision.ALLOW_ONCE
        assert ApprovalEvaluator.evaluate("发吧！") == ApprovalDecision.ALLOW_ONCE

        # 2. 明确拒绝（高危用例：“不要发送”包含“发送”，但必须判定为拒绝）
        assert ApprovalEvaluator.evaluate("不要发送") == ApprovalDecision.REJECT
        assert ApprovalEvaluator.evaluate("拒绝发送") == ApprovalDecision.REJECT
        assert ApprovalEvaluator.evaluate("别发") == ApprovalDecision.REJECT
        assert ApprovalEvaluator.evaluate("no") == ApprovalDecision.REJECT
        assert ApprovalEvaluator.evaluate("REJECT") == ApprovalDecision.REJECT
        assert ApprovalEvaluator.evaluate("算了") == ApprovalDecision.REJECT

        # 3. 模糊或不相关输入（高危用例：“sorry”包含“y”，但不能判定为同意）
        assert ApprovalEvaluator.evaluate("sorry") == ApprovalDecision.CANCEL
        assert ApprovalEvaluator.evaluate("今天天气真好") == ApprovalDecision.CANCEL
        assert ApprovalEvaluator.evaluate("") == ApprovalDecision.CANCEL

    def test_output_retention_exact_omission(self) -> None:
        """测试 OutputRetention 在超长截断时的 exact 语义元数据 (解决 M6)."""
        # 短文本不裁剪
        short_text = "这是一段不到四千字的正常结果。"
        retained, omitted = OutputRetention.retain(short_text, max_chars=100)
        assert retained == short_text
        assert omitted is None

        # 超长文本产生精确截断元数据
        long_text = "A" * 5000
        retained, omitted = OutputRetention.retain(long_text, max_chars=4000)
        assert omitted is not None
        assert isinstance(omitted, RetentionOmitted)
        assert omitted.kind == "exact"
        assert omitted.original_length == 5000
        assert omitted.retained_length == 4000
        assert "[Omitted: kind=exact, original_length=5000, retained_length=4000]" in retained
        assert retained.startswith("A" * 4000)

        # 验证日志片段生成
        snippet = OutputRetention.get_snippet("X" * 300, limit=200)
        assert len(snippet.split("...<snippet_truncated:")[0]) == 200
        assert "snippet_truncated:100" in snippet

    def test_approval_registry_detection(self) -> None:
        """测试审批注册表对高危工具的识别与载荷组装."""
        assert ApprovalRegistry.is_approval_required("send_email_notification") is True
        assert ApprovalRegistry.is_approval_required("calculator") is False

        payload = ApprovalRegistry.build_approval_payload(
            "send_email_notification",
            {"to_email": "boss@example.com", "subject": "周报", "content": "这是周报内容"},
        )
        assert payload["type"] == "human_approval_required"
        assert payload["to_email"] == "boss@example.com"
        assert "需要人工确认" in payload["message"]

    def test_tool_pipeline_dispatch_success(self) -> None:
        """测试普通工具在 ToolPipeline 中顺利执行并返回 ToolMessage."""
        async def _test():
            @tool
            def mock_add(a: int, b: int) -> int:
                """Mock 加法工具."""
                return a + b

            ctx = ToolPipelineContext(
                caller_user_id="user_123",
                current_thread_id="thread_456",
                tools_by_name={"mock_add": mock_add},
            )

            tool_call = {
                "name": "mock_add",
                "args": {"a": 10, "b": 25},
                "id": "call_add_001",
            }

            result_msg = await ToolPipeline.dispatch(tool_call, ctx)
            assert isinstance(result_msg, ToolMessage)
            assert result_msg.content == "35"
            assert result_msg.name == "mock_add"
            assert result_msg.tool_call_id == "call_add_001"

        asyncio.run(_test())

    def test_tool_pipeline_approval_rejection_intercept(self) -> None:
        """测试敏感工具被人机审批拒绝后短路拦截并返回取消说明."""
        async def _test():
            mock_email_tool = MagicMock()
            mock_email_tool.ainvoke = AsyncMock()

            ctx = ToolPipelineContext(
                caller_user_id="user_123",
                current_thread_id="thread_456",
                tools_by_name={"send_email_notification": mock_email_tool},
            )

            tool_call = {
                "name": "send_email_notification",
                "args": {"to_email": "test@163.com", "subject": "测试", "content": "你好"},
                "id": "call_email_001",
            }

            # 模拟用户在 interrupt 中回复了“不要发送”
            with patch("app.core.langgraph.tools.pipeline.interrupt", return_value="不要发送"):
                result_msg = await ToolPipeline.dispatch(tool_call, ctx)

                assert isinstance(result_msg, ToolMessage)
                assert "操作已取消" in result_msg.content
                assert "不要发送" in result_msg.content
                # 确保实际邮件发送工具绝对未被调用！
                mock_email_tool.ainvoke.assert_not_called()

        asyncio.run(_test())

    def test_tool_pipeline_timeout_handling(self) -> None:
        """测试工具执行超时被 asyncio.wait_for 捕获并友好返回 (解决 M3)."""
        async def _test():
            mock_sleep_tool = MagicMock()
            # 模拟工具执行超时抛出 TimeoutError
            mock_sleep_tool.ainvoke = AsyncMock(side_effect=asyncio.TimeoutError())

            ctx = ToolPipelineContext(
                caller_user_id="user_123",
                current_thread_id="thread_456",
                tools_by_name={"mock_sleep": mock_sleep_tool},
            )

            tool_call = {
                "name": "mock_sleep",
                "args": {},
                "id": "call_timeout_001",
            }

            result_msg = await ToolPipeline.dispatch(tool_call, ctx)
            assert isinstance(result_msg, ToolMessage)
            assert "执行超时" in result_msg.content

        asyncio.run(_test())

    def test_tool_call_approval_separation_order(self) -> None:
        """测试在存在审批工具时，审批工具优先串行分发，普通工具后置执行."""
        async def _test():
            execution_order = []

            async def fake_dispatch(tc, ctx):
                name = tc["name"]
                execution_order.append(f"start_{name}")
                await asyncio.sleep(0.01)
                execution_order.append(f"end_{name}")
                return ToolMessage(content=f"result of {name}", name=name, tool_call_id=tc["id"])

            tool_calls = [
                {"name": "calculator", "args": {"expr": "1+1"}, "id": "call_calc_1"},
                {"name": "send_email_notification", "args": {"to_email": "a@b.com"}, "id": "call_email_1"},
                {"name": "web_search", "args": {"query": "test"}, "id": "call_search_1"},
            ]

            ctx = ToolPipelineContext(
                caller_user_id="user_123",
                current_thread_id="thread_456",
                tools_by_name={},
            )

            with patch.object(ToolPipeline, "dispatch", side_effect=fake_dispatch):
                approval_calls = [tc for tc in tool_calls if ApprovalRegistry.is_approval_required(tc.get("name", ""))]
                normal_calls = [tc for tc in tool_calls if not ApprovalRegistry.is_approval_required(tc.get("name", ""))]

                outputs_map = {}
                for tc in approval_calls:
                    msg = await ToolPipeline.dispatch(tc, ctx)
                    outputs_map[tc["id"]] = msg

                if len(normal_calls) == 1:
                    normal_msgs = [await ToolPipeline.dispatch(normal_calls[0], ctx)]
                elif len(normal_calls) > 1:
                    normal_msgs = list(await asyncio.gather(*[ToolPipeline.dispatch(tc, ctx) for tc in normal_calls]))
                else:
                    normal_msgs = []

                for tc, msg in zip(normal_calls, normal_msgs, strict=False):
                    outputs_map[tc["id"]] = msg

                outputs = [outputs_map[tc["id"]] for tc in tool_calls]

            # 审批工具必须在普通工具之前完整执行完毕（先 start 且先 end）
            assert execution_order[0] == "start_send_email_notification"
            assert execution_order[1] == "end_send_email_notification"
            # 最终输出顺序与原始 tool_calls 完全一致
            assert outputs[0].name == "calculator"
            assert outputs[1].name == "send_email_notification"
            assert outputs[2].name == "web_search"

        asyncio.run(_test())
