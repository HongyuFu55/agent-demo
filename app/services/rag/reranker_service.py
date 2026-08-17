"""Ollama Reranker 服务，通过 bge-reranker-v2-m3 对候选切片进行精排."""

import math
import os
import httpx
import structlog

logger = structlog.get_logger(__name__)


class OllamaRerankerService:
    """调用本地 Ollama bge-reranker-v2-m3 模型对候选文档进行 Cross-Encoder 精排."""

    def __init__(
        self,
        model_name: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """初始化 Reranker 配置."""
        self.model_name = model_name or os.getenv(
            "RAG_RERANKER_MODEL", "bona/bge-reranker-v2-m3:latest"
        )
        self.base_url = (base_url or os.getenv(
            "OLLAMA_BASE_URL", "http://host.docker.internal:11434"
        )).rstrip("/")
        self._available: bool | None = None  # None = 未探测
        self._last_failed_at: float = 0.0     # 上次探活失败的时间戳
        self._retry_cooldown: float = 60.0    # 失败后 60 秒内不重复探活

    async def _check_available(self) -> bool:
        """探测 Ollama reranker 是否可用（成功后永久缓存；失败后 60s 冷却重试）."""
        import time
        # 已确认可用 → 直接返回
        if self._available is True:
            return True
        # 失败冷却期内 → 跳过重试
        if self._available is False and (time.monotonic() - self._last_failed_at) < self._retry_cooldown:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.base_url}/api/embed",
                    json={"model": self.model_name, "input": ["test"]},
                )
                self._available = resp.status_code == 200
                if self._available:
                    logger.info(
                        "reranker_available",
                        model=self.model_name,
                        base_url=self.base_url,
                    )
                else:
                    self._last_failed_at = time.monotonic()
                    logger.warning(
                        "reranker_unavailable",
                        model=self.model_name,
                        status=resp.status_code,
                    )
        except Exception as e:
            self._available = False
            self._last_failed_at = time.monotonic()
            logger.warning("reranker_check_failed", error=str(e), model=self.model_name)
        return bool(self._available)

    def _score_from_embedding(self, vec: list[float]) -> float:
        """从 Cross-Encoder 的 embedding 输出提取相关性分数.

        bge-reranker-v2-m3 经 Ollama /api/embed 封装后，
        输出向量的前几维包含 [CLS] token 隐层状态，
        其 L1 归一化后第一维近似代表相关性得分。
        使用 sigmoid(sum(vec[:4])) 将其映射到 [0,1]。
        """
        raw = sum(vec[:4])
        return 1.0 / (1.0 + math.exp(-raw))  # sigmoid 归一化

    async def rerank(
        self,
        query: str,
        candidates: list[dict],
        top_k: int | None = None,
    ) -> list[dict]:
        """对候选切片按 query 相关度精排，返回降序排列结果.

        Args:
            query: 用户原始查询
            candidates: RRF 初排后的候选切片列表（含 chunk_text 字段）
            top_k: 最终保留的切片数，None 表示全部保留

        Returns:
            按 rerank_score 降序排列的切片列表
        """
        if not candidates:
            return candidates

        if not await self._check_available():
            logger.debug("reranker_skipped_unavailable", candidates=len(candidates))
            return candidates[:top_k] if top_k else candidates

        # 构造 [query + "\n\n" + doc] 格式输入（Cross-Encoder 标准格式）
        inputs = [f"{query}\n\n{c.get('chunk_text', '')}" for c in candidates]

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"{self.base_url}/api/embed",
                    json={"model": self.model_name, "input": inputs},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "reranker_api_failed",
                        status=resp.status_code,
                        model=self.model_name,
                    )
                    return candidates[:top_k] if top_k else candidates

                embeddings: list[list[float]] = resp.json().get("embeddings", [])
                if len(embeddings) != len(candidates):
                    return candidates[:top_k] if top_k else candidates

            # 打分并注入 rerank_score 字段
            scored: list[dict] = []
            for chunk, vec in zip(candidates, embeddings):
                item = dict(chunk)
                item["rerank_score"] = self._score_from_embedding(vec)
                scored.append(item)

            # 按 rerank_score 降序
            scored.sort(key=lambda x: x["rerank_score"], reverse=True)

            logger.info(
                "reranker_completed",
                model=self.model_name,
                input_count=len(candidates),
                output_count=len(scored[:top_k] if top_k else scored),
                top_score=round(scored[0]["rerank_score"], 4) if scored else 0,
            )

            return scored[:top_k] if top_k else scored

        except Exception as e:
            logger.warning("reranker_exception", error=str(e), model=self.model_name)
            return candidates[:top_k] if top_k else candidates


# 全局单例
reranker_service = OllamaRerankerService()
