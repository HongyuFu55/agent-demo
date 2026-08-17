"""基于 mem0 和 pgvector 的长期记忆服务，带可选缓存层与防污染双层防御机制."""

import asyncio
import os
import re
from typing import Any

from mem0 import AsyncMemory

from app.core.cache import (
    cache_key,
    cache_service,
)
from app.core.config import settings
from app.core.logging import logger

# 智能语义门控 Prompt
MEMORY_GATEKEEPER_PROMPT = """
你是一个精密的长期记忆准入分类器。
请分析用户的最新发言及上下文，判断其中是否包含了值得长期保存的用户画像、居住地、职业技术栈、生活习惯、固定偏好或明确的“请记住”指令。

【准入规则】：
1. 包含用户长期事实/偏好（如“我住在观音桥”、“我平时用 Python”、“我对海鲜过敏”）：必须判定为 true。
2. 复合提问（如“我住在观音桥，去解放碑怎么走”）：虽然包含临时问路，但前半句包含长期居住地信息，必须判定为 true。
3. 纯临时请求（如“写个快排”、“计算 1+1”、“总结网页”、“今天天气”）：判定为 false。

请严格输出 JSON 格式：
{"should_extract": true, "reason": "判定原因简述"}
"""

# 第一性原理结构化记忆抽取 Prompt
LONG_TERM_MEMORY_EXTRACTION_PROMPT = """
你是一个严格的用户长期画像与结构化属性事实提取引擎 (Persona & EAV Fact Extractor)。
你的唯一职责是从对话中提取用户【长期、稳定、跨会话有效】的个人事实、属性与核心偏好。

【准入核心原则】：
1. 主体性：必须描述用户本人的身份、居住地、职业、固定偏好或背景信息。
2. 时效性：坚决排除临时状态（如“今天很累”、“下周去出差”）。
3. 结构化消解：提取尽可能清晰规范，如 "居住地：重庆市观音桥", "技术栈：Python/FastAPI"。

【Few-Shot 对抗案例对比】：
❌ 忽略临时任务：
- "帮我总结这个 report.pdf" ──► 忽略
- "明天深圳天气怎么样" ──► 忽略
- "下周三我要去上海出差" ──► 忽略

✅ 正确提取长期属性：
- "我住在观音桥，去解放碑怎么走" ──► 提取："居住地：重庆市观音桥"
- "我平时主要用 Python 和 FastAPI" ──► 提取："技术栈：Python, FastAPI"
- "我对海鲜过敏，不吃辣" ──► 提取："饮食忌口：海鲜过敏，不吃辣"
- "请记住我的工号是 9527" ──► 提取："工号：9527"

若当前对话未包含符合条件的长期个人事实，请严格返回空列表 []。
"""


async def classify_memory_intent(messages: list[dict[str, Any]]) -> bool:
    """使用大模型进行高准确率的智能语义记忆准入分类，降级时使用代码前置守卫."""
    if not messages:
        return False

    user_text = ""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            user_text = str(messages[i].get("content", "")).strip()
            break

    if not user_text:
        return False

    try:
        from app.services.llm import llm_service

        payload = [
            {"role": "system", "content": MEMORY_GATEKEEPER_PROMPT},
            {"role": "user", "content": f"用户提问：{user_text}"},
        ]
        response = await llm_service.call(payload)
        from app.utils.graph import extract_text_content
        import json

        text_resp = extract_text_content(response.content)
        # 匹配 JSON
        match = re.search(r"\{.*\}", text_resp, re.DOTALL)
        if match:
            data = json.loads(match.group())
            should = bool(data.get("should_extract", False))
            logger.info("memory_gatekeeper_classified", should_extract=should, reason=data.get("reason", ""))
            return should
    except Exception as e:
        logger.warning("memory_gatekeeper_llm_failed_fallback_to_code", error=str(e))

    return should_update_memory(messages)


def should_update_memory(messages: list[dict[str, Any]]) -> bool:
    """工程前置守卫 (兜底备用)：预检本次对话是否包含值得持久化的用户画像."""
    if not messages:
        return False

    user_text = ""
    prev_assistant_text = ""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user" and not user_text:
            user_text = str(messages[i].get("content", "")).strip()
            if i > 0 and messages[i - 1].get("role") == "assistant":
                prev_assistant_text = str(messages[i - 1].get("content", "")).strip()
            break

    if not user_text:
        return False

    profile_keywords = [
        "我是", "我叫", "我住在", "我常驻", "我的家", "家在",
        "我喜欢", "我偏好", "我习惯", "我不吃", "我吃素", "过敏",
        "我的公司", "我的职位", "我的工作", "我的技术栈", "我平时主要用",
        "我的手机", "我的邮箱", "我的名字", "我的工号", "我的生日",
        "请记住", "记住我", "以后的回答", "以后请", "每次回答"
    ]
    if any(kw in user_text for kw in profile_keywords):
        return True

    transient_patterns = [
        r"^(帮我|请|给我)?(计算|算一下|求值|总结|概括|统计|翻译|润色|解释|分析|爬取|抓取|查询|查一下|查看)",
        r"(怎么走|怎么去|出行方案|最佳路线|天气|降温|气温|股票|行情|汇率)",
        r"^(写一个|写一段|编写|实现).*(代码|脚本|函数|类|算法|正则)",
    ]
    for pattern in transient_patterns:
        if re.search(pattern, user_text):
            logger.debug("memory_update_skipped_by_gatekeeper", reason="transient_pattern_matched", snippet=user_text[:60])
            return False

    context_inquiry_keywords = ["您叫什么", "您的名字", "您的职业", "您常驻", "您住在", "您的技术栈", "您的偏好"]
    if prev_assistant_text and any(ik in prev_assistant_text for ik in context_inquiry_keywords):
        if len(user_text) >= 2:
            return True

    return False


class MemoryService:
    """使用 mem0 和 pgvector 管理长期记忆的服务."""

    def __init__(self):
        self._memory: AsyncMemory | None = None
        self._init_lock = asyncio.Lock()

    async def _get_memory(self) -> AsyncMemory:
        """获取或初始化 AsyncMemory 单例（并发安全）."""
        if self._memory is None:
            async with self._init_lock:
                if self._memory is None:
                    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

                    self._memory = await AsyncMemory.from_config(
                        config_dict={
                            "custom_prompt": LONG_TERM_MEMORY_EXTRACTION_PROMPT,
                            "vector_store": {
                                "provider": "pgvector",
                                "config": {
                                    "embedding_model_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIMS,
                                    "collection_name": settings.LONG_TERM_MEMORY_COLLECTION_NAME,
                                    "dbname": settings.POSTGRES_DB,
                                    "user": settings.POSTGRES_USER,
                                    "password": settings.POSTGRES_PASSWORD,
                                    "host": settings.POSTGRES_HOST,
                                    "port": settings.POSTGRES_PORT,
                                },
                            },
                            "llm": {
                                "provider": "openai",
                                "config": {
                                    "model": settings.LONG_TERM_MEMORY_MODEL,
                                    "api_key": settings.DASHSCOPE_API_KEY or settings.OPENAI_API_KEY,
                                    "openai_base_url": settings.DASHSCOPE_BASE_URL,
                                },
                            },
                            "embedder": {
                                "provider": "ollama",
                                "config": {
                                    "model": settings.LONG_TERM_MEMORY_EMBEDDER_MODEL,
                                    "ollama_base_url": ollama_base_url,
                                    "embedding_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIMS,
                                },
                            },
                        }
                    )
        return self._memory

    async def initialize(self) -> None:
        """预热连接池与实例."""
        await self._get_memory()
        logger.info("长期记忆服务已初始化")

    async def search(self, user_id: str | None, query: str) -> str:
        """检索指定用户的相关长期记忆."""
        if not user_id or not query.strip():
            return ""

        try:
            # 增加缓存前缀与 TTL 保护（建议 TTL 设为 300~600 秒）
            key = cache_key("memory", str(user_id), query.strip())
            cached = await cache_service.get(key)
            if cached is not None:
                logger.debug("长期记忆命中缓存", user_id=user_id)
                return cached

            logger.info("开始检索长期记忆", user_id=user_id)
            memory = await self._get_memory()
            search_response = await memory.search(user_id=str(user_id), query=query)

            # 防御性解析结果（兼容 dict 或 list 结构）
            memories_list = []
            if isinstance(search_response, dict):
                memories_list = search_response.get("results", [])
            elif isinstance(search_response, list):
                memories_list = search_response

            facts = [r.get("memory", "").strip() for r in memories_list if r.get("memory")]
            result = "\n".join([f"* {f}" for f in facts if f])

            logger.info("长期记忆检索完成", user_id=user_id, memory_count=len(facts))

            # 仅缓存有效内容，建议配置 TTL 防止内存泄漏
            if result:
                await cache_service.set(key, result, ttl=300)

            return result
        except Exception as e:
            logger.error("长期记忆检索失败", error=str(e), user_id=user_id)
            return ""

    async def add(self, user_id: str | None, messages: list[dict], metadata: dict | None = None) -> None:
        """为指定用户添加长期记忆 (受工程前置守卫与第一性原理 Prompt 双重保护)."""
        if not user_id:
            return

        # 1. 智能 LLM 语义门控结合工程兜底校验
        if not await classify_memory_intent(messages):
            logger.debug("长期记忆更新已跳过", user_id=user_id, reason="gatekeeper_filtered")
            return

        try:
            logger.info("开始提取并更新长期记忆", user_id=user_id)
            memory = await self._get_memory()
            
            # 执行记忆写入
            await memory.add(
                messages,
                user_id=str(user_id),
                metadata=metadata,
                prompt=LONG_TERM_MEMORY_EXTRACTION_PROMPT,
            )
            
            # 2. 缓存一致性维护：失效当前用户的记忆检索缓存
            await self._invalidate_user_cache(str(user_id))
            logger.info("长期记忆更新成功并已失效旧缓存", user_id=user_id)
        except Exception as e:
            logger.exception("长期记忆更新失败", user_id=user_id, error=str(e))

    async def _invalidate_user_cache(self, user_id: str) -> None:
        """清理该用户的旧搜索缓存，确保记忆实时一致."""
        try:
            # 如果 cache_service 支持通配符删除或前缀扫描：
            delete_fn = getattr(cache_service, "delete_pattern", None)
            if delete_fn is not None:
                res = delete_fn(f"*memory:{user_id}:*")
                if asyncio.iscoroutine(res):
                    await res
        except Exception as e:
            logger.warning("清理用户记忆缓存失败", user_id=user_id, error=str(e))


memory_service = MemoryService()