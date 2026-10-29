"""人机协同审批与安全决策评估模块 (对齐 DSH 规范).

提供结构化四态审批判定，杜绝简单的子串包含匹配漏洞（如"不要发送"包含"发送"导致误判）。
"""

from enum import Enum
import re
from typing import Any


class ApprovalDecision(str, Enum):
    """人工审批四态决策枚举 (对齐 DSH user-approval 规范)."""

    ALLOW_ONCE = "allowed-once"   # 本次明确批准
    REJECT = "rejected"           # 明确拒绝
    CANCEL = "cancelled"         # 用户取消或意图不明
    UNAVAILABLE = "unavailable"   # 无法获取审批决策


class ApprovalEvaluator:
    """人机协同审批决策评估器."""

    # 严格词边界与意图精确判定，严禁使用 substring in 匹配
    _APPROVE_PATTERNS = [
        r"^(yes|y|approve|ok)$",
        r"^(确认|同意|发送|批准|通过|发吧|好|可以)$",
        r"^(确认发送|同意发送|批准发送|准许发送|可以发送|确定发送)$",
    ]

    _REJECT_PATTERNS = [
        r"^(no|n|reject|cancel)$",
        r"^(拒绝|取消|不发|别发|不要发送|不能发送|驳回|停止|算了)$",
        r"^(拒绝发送|取消发送|不要发|别发送|不要发了)$",
    ]

    @classmethod
    def evaluate(cls, user_input: Any) -> ApprovalDecision:
        """评估用户输入并收敛为结构化四态决策.

        参数：
            user_input: 用户回复决策文本或载荷。

        返回：
            ApprovalDecision: 判定结果。
        """
        if not user_input:
            return ApprovalDecision.CANCEL

        cleaned = str(user_input).strip().lower()
        # 清理常见的首尾标点符号
        cleaned = re.sub(r"^[\s!?,.:;\"'，。？！“”‘’]+|[\s!?,.:;\"'，。？！“”‘’]+$", "", cleaned)

        for pat in cls._APPROVE_PATTERNS:
            if re.match(pat, cleaned, re.IGNORECASE):
                return ApprovalDecision.ALLOW_ONCE

        for pat in cls._REJECT_PATTERNS:
            if re.match(pat, cleaned, re.IGNORECASE):
                return ApprovalDecision.REJECT

        return ApprovalDecision.CANCEL


class ApprovalRegistry:
    """高危工具人机审批注册表."""

    _APPROVAL_REQUIRED_TOOLS = {"send_email_notification"}

    @classmethod
    def is_approval_required(cls, tool_name: str) -> bool:
        """检查工具是否需要触发人工审批."""
        return tool_name in cls._APPROVAL_REQUIRED_TOOLS

    @classmethod
    def build_approval_payload(cls, tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        """构建传给前端或 interrupt 的审批元数据载荷."""
        if tool_name == "send_email_notification":
            to_email = str(tool_args.get("to_email", ""))
            subject = str(tool_args.get("subject", ""))
            content = str(tool_args.get("content", ""))

            prompt = (
                "⚠️ **【需要人工确认】智能体申请发送以下电子邮件：**\n\n"
                f"- 📧 **收件人**：`{to_email}`\n"
                f"- 📌 **主题**：`{subject}`\n\n"
                "📄 **邮件正文预览：**\n"
                "```text\n"
                f"{content}\n"
                "```\n\n"
                "请确认是否批准发送？您可以回复 **“确认发送”** 或 **“拒绝发送”**。"
            )

            return {
                "type": "human_approval_required",
                "action": "send_email",
                "tool_name": tool_name,
                "to_email": to_email,
                "subject": subject,
                "content": content,
                "message": prompt,
            }

        return {
            "type": "human_approval_required",
            "action": "execute_tool",
            "tool_name": tool_name,
            "args": tool_args,
            "message": f"⚠️ **【需要人工确认】智能体申请调用敏感工具 `{tool_name}`，是否批准？**",
        }

