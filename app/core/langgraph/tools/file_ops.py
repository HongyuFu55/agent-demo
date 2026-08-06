"""工作区文件读写与沙箱防护工具集.

提供工作区（storage/workspace/）内的文件列举、文本读取与内容写入工具，
包含严格的 Path Traversal（路径穿越）安全白名单校验与文件大小保护。
"""

import os
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import logger

# 1. 定义安全的根工作区目录 (d:\Learn\fastapi-langgraph-agent-zh-master\storage\workspace)
PROJECT_ROOT = Path(__file__).resolve().parents[4]
WORKSPACE_DIR = (PROJECT_ROOT / "storage" / "workspace").resolve()

# 读取限制：最大 100KB (102,400 字节)，防止大文件冲垮 LLM 上下文
MAX_READ_BYTES = 102_400


def _get_safe_path(relative_path: str) -> Path:
    """安全路径校验函数（沙箱核心）.

    校验传入的相对路径解析后的绝对路径是否严格锁定在 WORKSPACE_DIR 目录下。
    防止例如 '../../.env' 或 '/etc/passwd' 等路径穿越攻击。

    参数：
        relative_path: 相对工作区的路径，例如 "notes.txt" 或 "docs/readme.md"

    返回：
        Path: 校验安全的绝对路径对象。

    抛出：
        PermissionError: 当路径试图越界离开工作区沙箱时抛出。
    """
    # 确保工作区根目录已存在
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    # 规范化路径字符串（处理相对路径）
    clean_path_str = relative_path.strip().lstrip("/\\")
    target_path = (WORKSPACE_DIR / clean_path_str).resolve()

    # 核心安全校验：目标路径必须以 WORKSPACE_DIR 为前缀
    try:
        target_path.relative_to(WORKSPACE_DIR)
    except ValueError:
        logger.warning("path_traversal_attempt_blocked", input_path=relative_path, resolved_path=str(target_path))
        raise PermissionError(
            f"安全拦截：路径 '{relative_path}' 超出了允许的工作区沙箱范围 ({WORKSPACE_DIR})！"
        )

    return target_path


@tool
async def list_workspace_files(sub_dir: str = "") -> str:
    """列出工作区（storage/workspace/）内的所有文件与目录结构。

    在读写文件前，如果不知道工作区有哪些文件，可调用此工具进行探索。

    参数：
        sub_dir: 子目录相对路径（默认为空，即工作区根目录）
    """
    try:
        target_dir = _get_safe_path(sub_dir)
        if not target_dir.exists():
            return f"目录不存在：'{sub_dir}'"
        if not target_dir.is_dir():
            return f"路径 '{sub_dir}' 不是一个有效的目录"

        file_list = []
        for entry in os.scandir(target_dir):
            rel_path = Path(entry.path).relative_to(WORKSPACE_DIR).as_posix()
            if entry.is_dir():
                file_list.append(f"[DIR]  {rel_path}/")
            else:
                size_kb = round(entry.stat().st_size / 1024, 2)
                file_list.append(f"[FILE] {rel_path} ({size_kb} KB)")

        if not file_list:
            return f"工作区目录 '{sub_dir or '/'}' 目前为空。"

        return f"工作区 '{sub_dir or '/'}' 文件列表（共 {len(file_list)} 项）：\n" + "\n".join(file_list)
    except PermissionError as pe:
        return str(pe)
    except Exception as e:
        logger.exception("list_workspace_files_failed", sub_dir=sub_dir, error=str(e))
        return f"列出工作区文件时发生异常：{str(e)}"


@tool
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
async def read_workspace_file(file_path: str) -> str:
    """从工作区安全读取文本文件内容。

    参数：
        file_path: 文件相对路径，例如 "notes.txt" 或 "reports/daily.md"
    """
    try:
        target_file = _get_safe_path(file_path)

        if not target_file.exists():
            return f"错误：文件 '{file_path}' 不存在！"
        if not target_file.is_file():
            return f"错误：路径 '{file_path}' 不是一个文件！"

        file_size = target_file.stat().st_size
        
        # 尝试读取文本
        with open(target_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_READ_BYTES)

        truncated_warning = ""
        if file_size > MAX_READ_BYTES:
            truncated_warning = f"\n\n[⚠️ 警告：文件总大小为 {file_size} 字节，已自动截断前 {MAX_READ_BYTES} 字节]"

        logger.info("workspace_file_read_success", file_path=file_path, bytes_read=len(content))
        return f"--- 文件内容 [{file_path}] ---\n{content}{truncated_warning}"

    except PermissionError as pe:
        return str(pe)
    except Exception as e:
        logger.exception("read_workspace_file_failed", file_path=file_path, error=str(e))
        return f"读取文件时发生异常：{str(e)}"


@tool
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
async def write_workspace_file(file_path: str, content: str, append: bool = False) -> str:
    """将文本内容安全写入工作区文件。如果父目录不存在会自动创建。

    参数：
        file_path: 目标文件相对路径，例如 "summary.txt" 或 "data/results.json"
        content: 要写入的文本内容
        append: 是否追加模式。True 为追加，False 为覆盖写入（默认 False）
    """
    try:
        target_file = _get_safe_path(file_path)

        # 自动创建父目录
        target_file.parent.mkdir(parents=True, exist_ok=True)

        mode = "a" if append else "w"
        action_name = "追加" if append else "覆盖写入"

        with open(target_file, mode, encoding="utf-8") as f:
            f.write(content)

        written_bytes = len(content.encode("utf-8"))
        logger.info("workspace_file_write_success", file_path=file_path, mode=mode, bytes=written_bytes)
        return f"[成功] 已{action_name}文件 '{file_path}'（写入 {written_bytes} 字节）。"

    except PermissionError as pe:
        return str(pe)
    except Exception as e:
        logger.exception("write_workspace_file_failed", file_path=file_path, error=str(e))
        return f"写入文件时发生异常：{str(e)}"
