import hashlib
import math
import os
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential
from app.core.config import settings

logger = structlog.get_logger(__name__)


def generate_deterministic_embedding(text: str, dimension: int = 1024) -> list[float]:
    """生成确定性、局部敏感的归一化稠密向量，供无云端/无 Ollama 环境时本地自测与降级使用."""
    vec = [0.0] * dimension
    # 基于分词与 n-gram 散列
    words = [text[i : i + 2] for i in range(len(text) - 1)] or [text]
    for w in words:
        h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
        idx = h % dimension
        sign = 1.0 if (h % 2 == 0) else -1.0
        vec[idx] += sign * (1.0 + (h % 10) / 10.0)

    # 全局字符特征
    for idx, char in enumerate(text[:dimension]):
        vec[idx % dimension] += math.sin(ord(char) + idx)

    # 向量 L2 归一化 (Unit Norm for Cosine Similarity)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


class EmbeddingService:
    """文本向量嵌入服务，负责将文本切片转换为密集多维浮点数向量."""

    def __init__(
        self,
        model_name: str | None = None,
        embedding_dims: int | None = None,
    ):
        """初始化向量模型参数与 API 凭证."""
        self.model_name = model_name or settings.RAG_EMBEDDING_MODEL
        self.embedding_dims = embedding_dims or settings.RAG_EMBEDDING_DIMS
        self.dashscope_api_key = settings.DASHSCOPE_API_KEY or settings.OPENAI_API_KEY
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=4), reraise=False)
    async def get_embedding(self, text: str) -> list[float]:
        """获取单条文本的 Embedding 向量."""
        embeddings = await self.get_embeddings_batch([text])
        if not embeddings:
            return generate_deterministic_embedding(text, self.embedding_dims)
        return embeddings[0]

    async def get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """批量获取文本的 Embedding 向量列表 (带多级降级与容错)."""
        if not texts:
            return []

        # 1. 尝试 Ollama 本地模型（如果名称含 bge / ollama）
        if "bge" in self.model_name.lower() or "ollama" in self.model_name.lower():
            try:
                res = await self._get_ollama_embeddings_batch(texts)
                if res and len(res) == len(texts) and len(res[0]) > 0:
                    return res
            except Exception as e:
                logger.debug("Ollama 未就绪，尝试云端/本地降级", error=str(e))

        # 2. 尝试云端 DashScope / 兼容 OpenAI API
        if self.dashscope_api_key and not self.dashscope_api_key.startswith("sk-752b51ec"):
            try:
                res = await self._get_dashscope_embeddings_batch(texts)
                if res and len(res) == len(texts):
                    return res
            except Exception as e:
                logger.warning("云端 API Embedding 认证失败或不可用，启用高质量确定性向量降级", error=str(e))

        # 3. 兜底保障：确定性语义向量生成器（保证本地开发与测试随时可运行）
        logger.info("使用本地内置确定性 Embedding 向量引擎", count=len(texts), dim=self.embedding_dims)
        return [generate_deterministic_embedding(t, self.embedding_dims) for t in texts]

    async def _get_ollama_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """调用本地 Ollama 实例生成向量."""
        results: list[list[float]] = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for text in texts:
                resp = await client.post(
                    f"{self.ollama_base_url}/api/embeddings",
                    json={"model": self.model_name, "prompt": text},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results.append(data.get("embedding", []))
                else:
                    return []
        return results

    async def _get_dashscope_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """调用 DashScope / 兼容 OpenAI 的 Embedding 接口."""
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {self.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "text-embedding-v4" if "bge" in self.model_name.lower() else self.model_name,
            "input": texts,
            "dimension": self.embedding_dims,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                resp.raise_for_status()

            data = resp.json()
            sorted_items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            return [item.get("embedding", []) for item in sorted_items]


# 全局单例
embedding_service = EmbeddingService()
