"""Python 代码执行工具 (Code Interpreter / 受限沙箱).

在受限子进程环境中动态运行 Python 代码，捕获 stdout/stderr，内置超时防爆控制。

⚠️ 重要声明：本实现是「进程级最佳努力加固」，**不是**操作系统级沙箱
（没有容器 / 命名空间 / 用户权限隔离）。它能阻止最常见的逃逸手段——
读取环境变量密钥、``os.system`` / ``subprocess`` / ``socket`` 等执行命令或联网、
用户代码通过 ``open()`` 越界读写工作区外文件。但它不能抵御攻击者利用 CPython
自身漏洞或第三方 C 扩展的绕过。若面向不可信用户开放生产环境，必须替换为真实隔离
（gVisor / Firecracker / nsjail / Docker）。
"""

import asyncio
import os
import sys

from langchain_core.tools import tool

from app.core.logging import logger
from app.core.langgraph.tools.file_ops import WORKSPACE_DIR

# 代码执行默认超时限制 (12 秒)，快速失败以避免爬虫卡死与死循环
EXEC_TIMEOUT_SECONDS = 12.0

# 结果文本最大返回字符数，防止输出过大冲垮 LLM 上下文
MAX_OUTPUT_LENGTH = 4000

# 注入到子进程的防护前奏（-I 隔离模式下运行）。用户代码通过环境变量
# SANDBOX_USER_CODE 传入，在受限 globals 中执行，避免直接拼接导致的引号/长度问题。
_SANDBOX_PRELUDE = r'''
import sys as _sys
import builtins as _b
import os as _os
from pathlib import Path as _Path

_SANDBOX_ROOT = _Path(_os.environ.get("SANDBOX_ROOT", ".")).resolve()

def _sandbox_blocked(*a, **k):
    raise RuntimeError("sandbox: this operation is blocked for security reasons")

# 1) 阻断高危「逃逸/进程/网络」模块导入（保留 os/sys 供 pandas、numpy 使用）
_BLOCKED_MODULES = {
    "subprocess", "socket", "ctypes", "requests", "urllib", "http",
    "ftplib", "smtplib", "imaplib", "poplib", "telnetlib", "websockets",
    "paramiko", "shutil", "pty", "fcntl",
}

class _BlockedImportFinder:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED_MODULES:
            raise ImportError("sandbox: import of '%s' is blocked" % name)
        return None
    def find_module(self, name, path=None):
        return self.find_spec(name, path)

_sys.meta_path.insert(0, _BlockedImportFinder())

# 2) 阉割 os 的进程执行/派生命令，防止 os.system('...') / os.popen 直接执行任意命令
for _n in ("system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe",
           "spawnv", "spawnve", "spawnvp", "spawnvpe", "execl", "execle",
           "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
           "fork", "forkpty", "kill", "chdir", "startfile"):
    if hasattr(_os, _n):
        setattr(_os, _n, _sandbox_blocked)

# 3) 受限 open：只注入到用户代码的 globals，不全局替换 builtins.open。
#    这样第三方库 import 时读取自身资源文件不受影响，只有用户代码的文件访问被限制在工作区内。
_original_open = _b.open
def _sandboxed_open(file, mode="r", *args, **kwargs):
    if isinstance(file, (str, bytes)):
        try:
            p = _Path(file.decode("utf-8", "replace") if isinstance(file, bytes) else file)
            if not p.is_absolute():
                p = _Path(_os.getcwd()) / p
            p.resolve().relative_to(_SANDBOX_ROOT)
        except ValueError:
            raise PermissionError("sandbox: access outside workspace blocked: %r" % (file,))
    return _original_open(file, mode, *args, **kwargs)

# 4) 用户代码运行环境：仅替换 open，保留 __import__/print 等（库导入不受影响）。
#    用户代码里的 eval/exec/compile 即便可用，也只能在这个受限环境里运行，无法绕过上面的全局防护。
_user_builtins = dict(vars(_b))
_user_builtins["open"] = _sandboxed_open
_user_globals = {"__builtins__": _user_builtins, "__name__": "__main__"}

# 5) 执行用户代码
exec(_os.environ["SANDBOX_USER_CODE"], _user_globals)
'''

# 这些关键字一旦出现在环境变量名中，就可能是密钥/凭据，子进程环境一律剔除
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASS", "CREDENTIAL", "PRIVATE", "AUTH")


def _build_clean_env() -> dict:
    """构造剔除密钥后的子进程环境，避免代码通过 os.environ 窃取 API Key."""
    env: dict = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if any(m in ku for m in _SECRET_MARKERS):
            continue
        env[k] = v
    env["PYTHONIOENCODING"] = "utf-8"
    env["SANDBOX_ROOT"] = str(WORKSPACE_DIR)
    return env


@tool
async def execute_python_code(code: str) -> str:
    """在受限子进程中安全执行 Python 代码并返回 stdout/stderr 输出。

    代码执行的根工作目录为 storage/workspace/，代码中可直接使用相对路径读写工作区文件
    （如 "docs/2026/数据.xlsx"），并可用 print(...) 输出需要查看的结果。

    已做防护：环境变量密钥已剔除、禁止 os.system/subprocess/socket 等命令执行与联网、
    用户代码的 open() 限制在工作区内。

    参数：
        code: 需要执行的完整 Python 代码字符串。
    """
    if not code or not code.strip():
        return "错误：未传入任何有效的 Python 代码。"

    # 清理 markdown 代码块标记 (如 ```python ... ```)
    clean_code = code.strip()
    if clean_code.startswith("```"):
        lines = clean_code.splitlines()
        if len(lines) >= 2:
            clean_code = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    logger.info("executing_python_code_start", code_snippet=clean_code[:100])

    try:
        # 剔除密钥后的环境变量 + 沙箱根目录 + 用户代码（经环境变量传入，规避引号转义）
        env = _build_clean_env()
        env["SANDBOX_USER_CODE"] = clean_code

        # 保证 WORKSPACE_DIR 目录存在
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

        # -I 隔离模式：忽略 PYTHON* 环境变量与用户 site-packages，降低代码注入面
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            _SANDBOX_PRELUDE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE_DIR,
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
            return (
                f"[ERROR] 代码执行超时（超过 {EXEC_TIMEOUT_SECONDS} 秒限制），已强制终止！\n"
                "提示：如果目标网站存在防爬验证、卡住或处理数据量过大，请勿继续盲目编写重复的爬虫或死循环代码，直接告知用户无法抓取该目标。"
            )

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
