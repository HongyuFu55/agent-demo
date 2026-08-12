"""多路混合检索器：融合 Milvus 密集向量与 BM25 稀疏关键词，经加权 RRF 算法重排序，可选 Reranker 精排."""

from typing import Any
import structlog
from app.services.rag.milvus_service import milvus_service
from app.services.rag.embedding_service import embedding_service
from app.services.rag.reranker_service import reranker_service

logger = structlog.get_logger(__name__)


def tokenize_chinese(text: str) -> list[str]:
    """中文分词器，优先使用 jieba，缺失时降级为字符级切分."""
    try:
        import jieba

        return [w.strip() for w in jieba.cut_for_search(text) if w.strip()]
    except ImportError:
        # 降级：按字符切分
        return list(text.strip())


class InMemoryBM25Index:
    """轻量级内存 BM25 索引，用于快速关键词召回."""

    def __init__(self) -> None:
        """初始化内存 BM25 语料库与分词索引."""
        self.corpus_docs: list[dict[str, Any]] = []
        self.tokenized_corpus: list[list[str]] = []
        self._bm25_model = None

    def add_documents(self, docs: list[dict[str, Any]]) -> None:
        """向 BM25 索引增量添加文档切片."""
        if not docs:
            return

        for doc in docs:
            text = doc.get("chunk_text", "")
            tokens = tokenize_chinese(text)
            self.corpus_docs.append(doc)
            self.tokenized_corpus.append(tokens)

        # 重新初始化 BM25 模型
        try:
            from rank_bm25 import BM25Okapi

            self._bm25_model = BM25Okapi(self.tokenized_corpus)
        except ImportError:
            logger.warning("未安装 rank-bm25，BM25 关键词检索将降级为子串匹配")
            self._bm25_model = None

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """执行 BM25 关键词检索."""
        if not self.corpus_docs:
            return []

        tokens = tokenize_chinese(query)
        if not tokens:
            return []

        if self._bm25_model is not None:
            doc_scores = self._bm25_model.get_scores(tokens)
            # 取得分最高的前 top_k 项
            top_indices = sorted(range(len(doc_scores)), key=lambda i: doc_scores[i], reverse=True)[:top_k]

            results = []
            for idx in top_indices:
                if doc_scores[idx] > 0:
                    item = dict(self.corpus_docs[idx])
                    item["score"] = float(doc_scores[idx])
                    item["source"] = "bm25_sparse"
                    results.append(item)
            return results

        # 降级：简单关键词命中频次统计
        scored = []
        for doc in self.corpus_docs:
            text = doc.get("chunk_text", "")
            hit_count = sum(1 for t in tokens if t in text)
            if hit_count > 0:
                item = dict(doc)
                item["score"] = float(hit_count)
                item["source"] = "bm25_fallback"
                scored.append(item)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]


# 全局内存 BM25 索引单例
bm25_index = InMemoryBM25Index()


class HybridRetriever:
    """企业级双路混合检索 + RRF 融合 + Reranker 精排引擎."""

    def __init__(
        self,
        rrf_k: int = 60,
        dense_weight: float = 0.7,
        sparse_weight: float = 0.3,
        use_reranker: bool = True,
    ):
        """初始化混合检索参数与权重分配."""
        self.rrf_k = rrf_k
        self.dense_weight = dense_weight
        self.sparse_weight = sparse_weight
        self.use_reranker = use_reranker

    async def search(
        self,
        query: str,
        top_k: int = 5,
        doc_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """执行向量与关键词混合检索，加权 RRF 合并后可选 Reranker 精排."""
        logger.info("发起双路混合检索", query=query, top_k=top_k)

        # 1. 密集向量检索 (Dense Retrieval)
        query_vector = await embedding_service.get_embedding(query)
        dense_hits = await milvus_service.vector_search(
            query_vector=query_vector,
            top_k=top_k * 2,  # 多召回一些候选供 RRF 融合
            doc_filter=doc_filter,
        )

        # 2. 稀疏关键词检索 (Sparse BM25)
        sparse_hits = bm25_index.search(query=query, top_k=top_k * 2)

        # 3. 加权 RRF（Reciprocal Rank Fusion）融合
        # RRF_Score = dense_weight / (k + rank_dense) + sparse_weight / (k + rank_sparse)
        merged_scores: dict[str, float] = {}
        item_map: dict[str, dict[str, Any]] = {}

        for rank, hit in enumerate(dense_hits, start=1):
            key = f"{hit['doc_id']}_{hit['chunk_index']}_{hit['chunk_text'][:20]}"
            item_map[key] = hit
            merged_scores[key] = merged_scores.get(key, 0.0) + (self.dense_weight / (self.rrf_k + rank))

        for rank, hit in enumerate(sparse_hits, start=1):
            key = f"{hit.get('doc_id', '')}_{hit.get('chunk_index', 0)}_{hit.get('chunk_text', '')[:20]}"
            if key not in item_map:
                item_map[key] = hit
            merged_scores[key] = merged_scores.get(key, 0.0) + (self.sparse_weight / (self.rrf_k + rank))

        # 按 RRF 融合得分降序排序，多保留候选供 Reranker 精排
        rrf_pool_size = top_k * 3 if self.use_reranker else top_k
        sorted_keys = sorted(merged_scores.keys(), key=lambda k: merged_scores[k], reverse=True)[:rrf_pool_size]

        rrf_results: list[dict[str, Any]] = []
        for rank, key in enumerate(sorted_keys, start=1):
            res = item_map[key]
            res["rrf_score"] = merged_scores[key]
            res["rrf_rank"] = rank
            rrf_results.append(res)

        logger.info(
            "混合检索与 RRF 融合完成",
            dense_candidates=len(dense_hits),
            sparse_candidates=len(sparse_hits),
            rrf_pool=len(rrf_results),
        )

        # 4. Reranker 精排（可选，Ollama bge-reranker-v2-m3）
        if self.use_reranker and rrf_results:
            final_results = await reranker_service.rerank(
                query=query,
                candidates=rrf_results,
                top_k=top_k,
            )
        else:
            final_results = rrf_results[:top_k]

        logger.info("检索完成", final_count=len(final_results))
        return final_results


# 全局单例
hybrid_retriever = HybridRetriever()
