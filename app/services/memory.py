"""基于 mem0 和 pgvector 的长期记忆服务，带可选缓存层与防污染双层防御机制."""

import os
import re

from mem0 import AsyncMemory

from app.core.cache import (
    cache_key,
    cache_service,
)
from app.core.config import settings
from app.core.logging import logger

# 第一性原理记忆准入与防污染抽取 Prompt
LONG_TERM_MEMORY_EXTRACTION_PROMPT = """
你是一个严格的用户长期画像与偏好事实提取引擎（Long-term Persona & Profile Extractor）。
你的唯一职责是从对话中提取用户【长期、稳定、跨会话有效】的个人属性、背景与偏好事实。

【准入核心三原则（必须同时满足）】：
1. 主体性 (Subject-Centric)：必须描述“用户本人的身份、长期偏好、居住地、职业技术栈、固定习惯或背景信息”。
2. 时效性 (Time-Invariance)：该事实在未来数月甚至数年后依然对了解该用户具有长期参考价值。
3. 严禁提取任何临时动作：严禁提取任何单次数学计算、临时天气/路线查询、特定文件/发票处理、单次网页爬取、单次翻译/写信请求（即便用户使用了“我想要/我需要”等字眼）。

【Few-Shot 对抗案例对比】：
❌ 错误提取（临时动作指令，必须忽略，严禁生成记忆）：
- 对话："帮我算下 1.05 的 365 次方" ──► 忽略 (纯数学计算)
- 对话："帮我总结这个 report.pdf" ──► 忽略 (单次文件处理)
- 对话："从重庆冉家坝到观音桥怎么走" ──► 忽略 (单次路线查询)
- 对话："整理 https://hima.auto/ 的车辆配置及价格" ──► 忽略 (单次网页抓取)
- 对话："分析网易财经股票频道最近信息" ──► 忽略 (单次资讯查询)
- 对话："帮我写个快速排序算法" ──► 忽略 (单次编程任务)
- 对话："查看我的 session 列表" ──► 忽略 (系统操作)

✅ 正确提取（长期身份与偏好事实，予以提取）：
- 对话："我平时主要用 Python 和 FastAPI 开发" ──► 提取："用户的技术栈主要是 Python 和 FastAPI"
- 对话："我常驻在重庆市渝北区" ──► 提取："用户居住在重庆市渝北区"
- 对话："以后的回答都用表格形式展现，保持简洁" ──► 提取："用户偏好表格化、简洁直接的回答风格"
- 对话："我对海鲜过敏，不吃辣" ──► 提取："用户海鲜过敏，且不吃辣"
- 对话："请记住我的工号是 9527" ──► 提取："用户的工号是 9527"

若当前对话中未包含符合长期准入标准的个人背景事实或明确偏好，请严格返回空列表 []，不得提取任何虚假或临时记忆。
"""


def should_update_memory(messages: list[dict]) -> bool:
    """工程前置守卫：判断本次对话是否可能包含值得提取的长期画像.

    规则：
    1. 消息过短 (< 4 字符) 或无用户消息时，直接跳过；
    2. 优先检查是否存在第一人称真实画像事实 (如 "我是", "我住在", "我喜欢", "请记住")；
    3. 若属于典型的临时动词任务 (计算、规划、总结、翻译、写代码、分析网页、爬虫等)，坚决拦截！
    """
    if not messages:
        return False

    # 提取最新的用户提问
    user_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_text = msg.get("content", "").strip()
            break

    if len(user_text) < 4:
        return False

    # 1. 显式第一人称个人属性/偏好指示词（排除了单纯的祈使助词“帮我”）
    profile_keywords = [
        "我是",
        "我叫",
        "我住在",
        "我常驻",
        "我的家",
        "我喜欢",
        "我偏好",
        "我的公司",
        "我的职位",
        "我的工作",
        "我的技术",
        "我经常",
        "我的习惯",
        "我不吃",
        "我吃素",
        "过敏",
        "我的手机",
        "我的邮箱",
        "我的名字",
        "我的工号",
        "请记住",
        "记住我",
        "以后回答",
        "以后请",
    ]
    has_profile_hint = any(kw in user_text for kw in profile_keywords)
    if has_profile_hint:
        return True

    # 2. 常见的一次回调任务动词与临时词汇（只要命中且没有真实个人画像，一律拦截）
    transient_keywords = [
        "计算",
        "算一下",
        "算下",
        "求值",
        "次方",
        "查询",
        "查一下",
        "查看",
        "天气",
        "降温",
        "气温",
        "规划",
        "怎么走",
        "怎么去",
        "出行方案",
        "最佳路线",
        "打车",
        "公交",
        "地铁",
        "总结",
        "概括",
        "统计",
        "抓取",
        "爬取",
        "爬虫",
        "股票",
        "行情",
        "写一个",
        "写一段",
        "编写",
        "翻译",
        "润色",
        "帮我算",
        "帮我写",
        "帮我看",
        "帮我查",
        "帮我总结",
        "帮我整理",
        "帮我分析",
        "整理",
        "分析",
    ]

    for kw in transient_keywords:
        if kw in user_text:
            logger.info("memory_update_skipped_by_gatekeeper", reason="transient_keyword_matched", keyword=kw, snippet=user_text[:60])
            return False

    return True


class MemoryService:
    """使用 mem0 和 pgvector 管理长期记忆的服务."""

    def __init__(self):
        """初始化记忆服务."""
        self._memory: AsyncMemory | None = None

    async def _get_memory(self) -> AsyncMemory:
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
        """预热 mem0 AsyncMemory 实例和 pgvector 连接池.

        启动时调用一次，避免首次 search() 或 add() 承担 from_config 和 pgvector.list_cols() 的冷启动成本。
        """
        await self._get_memory()
        logger.info("长期记忆服务已初始化")

    async def search(self, user_id: str | None, query: str) -> str:
        """检索指定用户的相关长期记忆.

        先查缓存；未命中时查询 mem0，并缓存结果。

        返回格式化后的记忆字符串；失败或未提供 user_id 时返回空字符串，避免匿名请求共享同一记忆分区。
        """
        if user_id is None:
            # 匿名会话不使用长期记忆，避免跨用户数据污染
            return ""
        try:
            # 以 user_id + query 的哈希作为缓存键，避免向量数据库重复查询
            key = cache_key("memory", str(user_id), query)
            cached = await cache_service.get(key)
            if cached is not None:
                logger.debug("长期记忆命中缓存", user_id=user_id)
                return cached

            logger.info("开始检索长期记忆", user_id=user_id)
            memory = await self._get_memory()
            results = await memory.search(user_id=str(user_id), query=query)
            result = "\n".join([f"* {r['memory']}" for r in results["results"]])
            result_count = len(results["results"])
            logger.info("长期记忆检索完成", user_id=user_id, memory_count=result_count)

            # 只缓存非空结果；空结果不缓存，下次仍会查询以防有新记忆写入
            if result:
                await cache_service.set(key, result)

            return result
        except Exception as e:
            # 记忆搜索失败不应中断对话，降级返回空字符串
            logger.error("长期记忆检索失败", error=str(e), user_id=user_id)
            return ""

    async def add(self, user_id: str | None, messages: list[dict], metadata: dict | None = None) -> None:
        """为指定用户添加长期记忆 (受工程前置守卫与第一性原理 Prompt 双重保护)."""
        if user_id is None:
            return

        # 前置代码级工程守卫拦截
        if not should_update_memory(messages):
            logger.info("长期记忆更新已跳过", user_id=user_id, reason="gatekeeper_filtered")
            return

        try:
            logger.info("开始更新长期记忆", user_id=user_id)
            memory = await self._get_memory()
            await memory.add(messages, user_id=str(user_id), metadata=metadata, prompt=LONG_TERM_MEMORY_EXTRACTION_PROMPT)
            logger.info("长期记忆更新成功", user_id=user_id)
        except Exception as e:
            logger.exception("长期记忆更新失败", user_id=user_id, error=str(e))


memory_service = MemoryService()
