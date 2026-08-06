"""Python 代码执行工具 (Code Interpreter / 沙箱).

在安全的子进程环境中动态运行 Python 代码，捕获标准输出 (stdout) 和标准错误 (stderr)，
内置超时防爆控制，支持数据分析 (pandas) 与复杂逻辑运算。
"""

import asyncio
import os
import sys
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import logger

# 代码执行默认超时限制 (30 秒)，防止死循环卡死服务，同时兼容 pandas/numpy 模块加载
EXEC_TIMEOUT_SECONDS = 30.0

# 结果文本最大返回字符数，防止输出过大冲垮 LLM 上下文
MAX_OUTPUT_LENGTH = 4000


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def execute_python_code(code: str) -> str:
    """在独立的子进程中安全动态执行 Python 代码并返回 stdout/stderr 输出。

    适用于数据分析 (如 pandas 读取 Excel/CSV)、复杂数学运算、数据格式转换等任务。
    代码中可以通过 print(...) 输出需要查看的结果。

    参数：
        code: 需要执行的完整 Python 代码字符串。
    """
    if not code or not code.strip():
        return "错误：未传入任何有效的 Python 代码。"

    # 清理 markdown 代码块标记 (如 ```python ... ```)
    clean_code = code.strip()
    if clean_code.startswith("```"):
        lines = clean_code.splitlines()
        # 去掉第一行 ```python 和最后一行 ```
        if len(lines) >= 2:
            clean_code = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    logger.info("executing_python_code_start", code_snippet=clean_code[:100])

    try:
        # 强制子进程 IO 使用 UTF-8 编码
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"

        # 使用当前的 sys.executable 启动独立 Python 子进程运行代码
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            clean_code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=EXEC_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            # 发生超时，强制终止子进程防死循环
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            logger.warning("python_code_execution_timeout", code_snippet=clean_code[:100])
            return f"[ERROR] 代码执行超时（超过 {EXEC_TIMEOUT_SECONDS} 秒限制），已强行终止！可能包含死循环。"

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        result_parts = []
        if stdout_text:
            result_parts.append(f"--- [标准输出 stdout] ---\n{stdout_text}")
        if stderr_text:
            result_parts.append(f"--- [错误输出/警告 stderr] ---\n{stderr_text}")

        if not result_parts:
            output_str = "[INFO] 代码成功执行，但没有产生任何 stdout 打印输出。(提示：请在代码中使用 print(...) 输出需要查看的数据结果)"
        else:
            output_str = "\n\n".join(result_parts)

        # 截断过长输出
        if len(output_str) > MAX_OUTPUT_LENGTH:
            output_str = output_str[:MAX_OUTPUT_LENGTH] + f"\n\n[警告：输出已截断前 {MAX_OUTPUT_LENGTH} 字符]"

        logger.info(
            "python_code_execution_success",
            exit_code=process.returncode,
            has_stdout=bool(stdout_text),
            has_stderr=bool(stderr_text),
        )
        return output_str

    except Exception as e:
        logger.exception("python_code_execution_failed", error=str(e))
        return f"[ERROR] 执行代码发生系统异常：{str(e)}"
