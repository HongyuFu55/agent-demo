"""多模态图片分析工具模块 (image_analyzer).

通过调用本地 Ollama 多模态模型 (如 qwen2.5-vl:3b)，赋予纯文本大语言模型 (如 DeepSeek) “看懂图片”的能力。
实现 DeepSeek 主大脑与 Ollama Vision 视觉工具的高效解耦协同。
"""

import base64
import os
from pathlib import Path
from typing import Optional

import ollama
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.langgraph.tools.file_ops import WORKSPACE_DIR, _get_safe_path
from app.core.logging import logger

# Ollama 视觉模型配置，默认使用用户本地已拉取的 qwen2.5vl:3b
VISION_MODEL_NAME = os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:3b")

# Ollama 服务 Endpoint (兼容 Docker 容器与宿主机)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def analyze_image(image_path: str, question: str = "请详细分析图片中的主要内容、文字与关键数据信息。") -> str:
    """分析工作区中的图片文件 (JPG/PNG/WEBP)，提取图片中的文字、表单数据或场景内容。

    适用场景：发票报销识别、代码截图分析、表格图片数据提取、场景图片描述。

    参数：
        image_path: 工作区图片的相对路径 (如 "docs/invoice.jpg" 或 "sample.png")
        question: 关于图片的具体分析要求或提问 (如 "提取发票金额与日期" 或 "总结图表数据趋势")
    """
    if not image_path or not image_path.strip():
        return "[ERROR] 未传入有效的图片路径。"

    try:
        # 1. 路径安全防护与存在性校验
        try:
            target_path = _get_safe_path(image_path)
        except Exception:
            # 若不是相对路径，尝试作为工作区路径补全
            target_path = (WORKSPACE_DIR / image_path.strip()).resolve()

        if not target_path.exists() or not target_path.is_file():
            return f"[ERROR] 找不到指定的图片文件：'{image_path}' (预期物理路径: {target_path})"

        # 校验扩展名
        valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        if target_path.suffix.lower() not in valid_extensions:
            return f"[ERROR] 不支持的文件格式：'{target_path.suffix}' (仅支持 {valid_extensions})"

        # 2. 读取图片字节并转为 Base64 编码
        with open(target_path, "rb") as f:
            image_bytes = f.read()
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        logger.info(
            "analyzing_image_start",
            image_path=str(target_path),
            model=VISION_MODEL_NAME,
            question=question,
        )

        # 3. 初始化 Ollama AsyncClient 并发起多模态请求
        # 如果从宿主机运行，host 回退为 http://127.0.0.1:11434
        host_url = OLLAMA_HOST
        
        # 避免 HTTP_PROXY 代理绕路拦截 localhost 导致 502 Bad Gateway
        os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",localhost,127.0.0.1,host.docker.internal"

        client = ollama.AsyncClient(host=host_url)

        try:
            response = await client.chat(
                model=VISION_MODEL_NAME,
                messages=[
                    {
                        "role": "user",
                        "content": question,
                        "images": [image_base64],
                    }
                ],
            )
        except Exception as conn_err:
            # 兼容如果在宿主机直接调用时 host.docker.internal 无法解析或代理 502
            fallback_host = "http://127.0.0.1:11434"
            logger.info("ollama_host_fallback", fallback_host=fallback_host, orig_error=str(conn_err))
            client = ollama.AsyncClient(host=fallback_host)
            response = await client.chat(
                model=VISION_MODEL_NAME,
                messages=[
                    {
                        "role": "user",
                        "content": question,
                        "images": [image_base64],
                    }
                ],
            )

        content = response["message"]["content"].strip()
        logger.info("analyzing_image_success", image_path=str(target_path), result_length=len(content))

        return (
            f"--- 图像视觉识别成功 (模型: {VISION_MODEL_NAME}) ---\n"
            f"图片文件: {image_path}\n"
            f"提问/指令: {question}\n\n"
            f"=== 识别与分析结论 ===\n"
            f"{content}"
        )

    except Exception as e:
        logger.exception("analyzing_image_failed", image_path=image_path, error=str(e))
        return f"[ERROR] 调用视觉模型分析图片失败：{str(e)}"
