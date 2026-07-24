"""计算器工具，用于 LangGraph Agent."""

from langchain_core.tools import tool


@tool
def calculator(expression: str) -> str:
    """计算简单的数学表达式.

    参数：
        expression: 要计算的数学表达式字符串，如 "1 + 1" 或 "2 * (3 + 4)"。

    返回：
        str: 计算结果或错误信息。
    """
    try:
        # 限制只允许基础数学运算符和数字，防止安全风险
        allowed_chars = "0123456789+-*/(). "
        if not all(char in allowed_chars for char in expression):
            return "错误：包含非法字符，仅支持基础数字与算术运算符"
        
        # 安全计算
        result = eval(expression, {"__builtins__": None}, {})
        return str(result)
    except Exception as e:
        return f"计算失败: {str(e)}"
