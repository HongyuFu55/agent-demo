"""应用输入清理工具."""

import html
import re
from typing import (
    Any,
    Dict,
    List,
)


def sanitize_string(value: str) -> str:
    """清理字符串，防止 XSS 和其他注入攻击.

    参数：
        value: 要清理的字符串。

    返回：
        str: 清理后的字符串。
    """
    # 如果不是字符串，则先转换为字符串
    if not isinstance(value, str):
        value = str(value)

    # HTML 转义，防止 XSS
    value = html.escape(value)

    # 移除可能被转义过的 script 标签
    value = re.sub(r"&lt;script.*?&gt;.*?&lt;/script&gt;", "", value, flags=re.DOTALL)

    # 移除空字节
    value = value.replace("\0", "")

    return value


def sanitize_email(email: str) -> str:
    """规范化邮箱地址：去空白/空字节 + 大小写归一，不做 HTML 转义.

    邮箱是数据字段，入库前做 HTML 转义会破坏原始数据，并导致注册/登录两端
    邮箱不一致（例如 ``a@b.com`` 被转义成 ``a@b.com`` 之外的形式）。
    """
    if not isinstance(email, str):
        email = str(email)

    email = email.strip().replace("\0", "").lower()

    # 确保邮箱格式正确（简单检查）
    if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
        raise ValueError("Invalid email format")

    return email


def sanitize_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """递归清理字典中的所有字符串值.

    参数：
        data: 要清理的字典。

    返回：
        Dict[str, Any]: 清理后的字典。
    """
    sanitized = {}
    for key, value in data.items():
        if isinstance(value, str):
            sanitized[key] = sanitize_string(value)
        elif isinstance(value, dict):
            sanitized[key] = sanitize_dict(value)
        elif isinstance(value, list):
            sanitized[key] = sanitize_list(value)
        else:
            sanitized[key] = value
    return sanitized


def sanitize_list(data: List[Any]) -> List[Any]:
    """递归清理列表中的所有字符串值.

    参数：
        data: 要清理的列表。

    返回：
        List[Any]: 清理后的列表。
    """
    sanitized = []
    for item in data:
        if isinstance(item, str):
            sanitized.append(sanitize_string(item))
        elif isinstance(item, dict):
            sanitized.append(sanitize_dict(item))
        elif isinstance(item, list):
            sanitized.append(sanitize_list(item))
        else:
            sanitized.append(item)
    return sanitized


def validate_password_strength(password: str) -> bool:
    """校验密码强度.

    参数：
        password: 要校验的密码。

    返回：
        bool: 密码是否足够强。

    抛出：
        ValueError: 密码强度不足时抛出，并说明原因。
    """
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")

    if not re.search(r"[A-Z]", password):
        raise ValueError("Password must contain at least one uppercase letter")

    if not re.search(r"[a-z]", password):
        raise ValueError("Password must contain at least one lowercase letter")

    if not re.search(r"[0-9]", password):
        raise ValueError("Password must contain at least one number")

    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        raise ValueError("Password must contain at least one special character")

    return True
