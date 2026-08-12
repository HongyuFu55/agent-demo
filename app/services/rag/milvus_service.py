"""Milvus 向量数据库底层操作封装，支持 HNSW 索引与标量过滤."""

from typing import Any
import structlog
from app.core.config import settings

logger = structlog.get_logger(__name__)


class MilvusService:
    """Milvus 向量存储服务，管理知识库 Collection、索引与近邻检索."""

    def __init__(
        self,
        collection_name: str | None = None,
        dimension: int | None = None,
    ):
        """初始化 Milvus 客户端配置."""
        self.collection_name = collection_name or settings.RAG_COLLECTION_NAME
        self.dimension = dimension or settings.RAG_EMBEDDING_DIMS
        self._client = None

    def _get_client(self):
        """延迟懒加载 MilvusClient 实例."""
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError:
                logger.exception("pymilvus 未安装，请执行 pip install pymilvus")
                raise RuntimeError("pymilvus 未安装，无法连接 Milvus 向量数据库")

            kwargs: dict[str, Any] = {"uri": settings.milvus_uri}
            if settings.MILVUS_TOKEN:
                kwargs["token"] = settings.MILVUS_TOKEN
            self._client = MilvusClient(**kwargs)
            self._ensure_collection()
        return self._client

    def _ensure_collection(self) -> None:
        """检查并创建带 HNSW 索引的 Collection."""
        client = self._client
        if client is None:
            return

        if not client.has_collection(self.collection_name):
            logger.info("创建 Milvus 知识库 Collection", collection=self.collection_name, dim=self.dimension)
            client.create_collection(
                collection_name=self.collection_name,
                dimension=self.dimension,
                metric_type="COSINE",
                consistency_level="Strong",
                auto_id=True,
            )
            logger.info("Milvus 知识库 Collection 创建完毕", collection=self.collection_name)

    async def insert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        """向 Milvus 批量插入文档切片与向量.

        chunks 列表中每个元素格式必须包含: vector, chunk_text, doc_id, doc_name, chunk_index
        """
        if not chunks:
            return 0

        client = self._get_client()
        logger.info("向 Milvus 写入切片数据", collection=self.collection_name, count=len(chunks))
        res = client.insert(collection_name=self.collection_name, data=chunks)
        client.flush(self.collection_name)
        insert_count = res.get("insert_count", len(chunks))
        logger.info("Milvus 切片写入成功", insert_count=insert_count)
        return insert_count

    async def vector_search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        doc_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """执行带标量过滤的向量余弦近邻检索."""
        client = self._get_client()

        filter_expr = ""
        if doc_filter:
            filter_expr = f"doc_id == '{doc_filter}' or doc_name == '{doc_filter}'"

        search_params = {
            "metric_type": "COSINE",
            "params": {"ef": 64},  # HNSW 检索搜索宽度
        }

        logger.debug("发起 Milvus 向量检索", collection=self.collection_name, top_k=top_k, filter=filter_expr)
        results = client.search(
            collection_name=self.collection_name,
            data=[query_vector],
            limit=top_k,
            filter=filter_expr,
            output_fields=["chunk_text", "doc_id", "doc_name", "chunk_index", "total_chunks"],
            search_params=search_params,
        )

        hits: list[dict[str, Any]] = []
        if results and len(results) > 0:
            for hit in results[0]:
                entity = hit.get("entity", {})
                hits.append(
                    {
                        "id": hit.get("id"),
                        "score": hit.get("distance", 0.0),  # 余弦相似度分数
                        "chunk_text": entity.get("chunk_text", ""),
                        "doc_id": entity.get("doc_id", ""),
                        "doc_name": entity.get("doc_name", ""),
                        "chunk_index": entity.get("chunk_index", 0),
                        "total_chunks": entity.get("total_chunks", 1),
                        "source": "milvus_dense",
                    }
                )

        logger.info("Milvus 向量检索完成", hit_count=len(hits))
        return hits

    async def delete_by_doc_id(self, doc_id: str) -> int:
        """根据文档 ID 删除指定文档的所有切片."""
        client = self._get_client()
        filter_expr = f"doc_id == '{doc_id}'"
        res = client.delete(collection_name=self.collection_name, filter=filter_expr)
        deleted_count = res.get("delete_count", 0) if isinstance(res, dict) else 1
        logger.info("已删除文档切片", doc_id=doc_id, count=deleted_count)
        return deleted_count

    async def get_collection_stats(self) -> dict[str, Any]:
        """获取当前知识库的统计信息."""
        client = self._get_client()
        stats = client.get_collection_stats(collection_name=self.collection_name)
        return {
            "collection_name": self.collection_name,
            "dimension": self.dimension,
            "row_count": stats.get("row_count", 0),
        }


# 全局单例
milvus_service = MilvusService()
