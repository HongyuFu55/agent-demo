"""Milvus 向量数据库底层操作封装，支持 HNSW 索引与标量过滤."""

import asyncio
from typing import Any
from urllib.parse import urlparse
import structlog
from app.core.config import settings

logger = structlog.get_logger(__name__)


async def _is_host_reachable_async(host: str, port: int, timeout: float = 1.0) -> bool:
    """异步探测主机端口 TCP 连通性，不阻塞 asyncio 事件循环."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


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
        self._last_failed_probe_time: float = 0.0

    def _reset_client(self) -> None:
        """重置 MilvusClient 实例，清理过期的 Collection 缓存."""
        try:
            if self._client is not None and hasattr(self._client, "close"):
                self._client.close()
        except Exception:
            pass
        self._client = None

    async def _get_client_async(self, force_refresh: bool = False):
        """异步懒加载 MilvusClient 实例，带多环境候选 URI 自动探测与失败冷却缓存."""
        import time

        if not force_refresh and self._client is None and (time.time() - self._last_failed_probe_time < 30.0):
            return None

        if force_refresh or self._client is None:
            self._reset_client()
            try:
                from pymilvus import MilvusClient
            except ImportError:
                logger.exception("pymilvus 未安装，请执行 pip install pymilvus")
                raise RuntimeError("pymilvus 未安装，无法连接 Milvus 向量数据库")

            candidates = [
                settings.milvus_uri,
                "http://127.0.0.1:19530",
            ]
            seen: set[str] = set()
            unique_candidates = [c for c in candidates if not (c in seen or seen.add(c))]  # type: ignore[func-returns-value]

            # 异步探测，不阻塞事件循环
            valid_uri = None
            for uri in unique_candidates:
                parsed = urlparse(uri)
                host = parsed.hostname or "localhost"
                port = parsed.port or 19530
                if await _is_host_reachable_async(host, port, timeout=1.0):
                    valid_uri = uri
                    logger.info("探测到可用的 Milvus 节点", uri=uri, host=host, port=port)
                    break

            if not valid_uri:
                self._last_failed_probe_time = time.time()
                logger.warning("milvus_node_unreachable", message="未探测到可用的 Milvus 节点，跳过连接初始化（冷却30秒）")
                return None

            kwargs: dict[str, Any] = {"uri": valid_uri}
            if settings.MILVUS_TOKEN:
                kwargs["token"] = settings.MILVUS_TOKEN
            self._client = MilvusClient(**kwargs)
            self._ensure_collection()
        return self._client

    def _ensure_collection(self, force_reconnect: bool = False) -> None:
        """检查并创建带 HNSW 索引的 Collection，必要时刷新连接缓存."""
        if force_reconnect:
            self._reset_client()

        client = self._client
        if client is None:
            return

        try:
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
        except Exception as e:
            logger.warning("检查/创建 Milvus Collection 遇到异常，跳过重试", error=str(e), collection=self.collection_name)

    async def insert_chunks(self, chunks: list[dict[str, Any]], batch_size: int = 100) -> int:
        """向 Milvus 批量分批插入文档切片与向量，支持大文档分批写入与自动保活.

        chunks 列表中每个元素格式必须包含: vector, chunk_text, doc_id, doc_name, chunk_index
        """
        if not chunks:
            return 0

        self._ensure_collection()
        client = await self._get_client_async()
        if client is None:
            logger.warning("milvus_client_unavailable_skip_insert", message="Milvus 未连接，跳过切片写入")
            return 0

        logger.info("向 Milvus 写入切片数据", collection=self.collection_name, count=len(chunks), batch_size=batch_size)
        total_inserted = 0

        # 分批次写入，防止超大文档（如 PDF 1000+ 切片）触发 gRPC 消息过大或超时
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            try:
                res = client.insert(collection_name=self.collection_name, data=batch)
                batch_count = res.get("insert_count", len(batch)) if isinstance(res, dict) else len(batch)
                total_inserted += batch_count
            except Exception as e:
                logger.warning("Milvus 批量写入出现异常，强制重连重刷 Collection 缓存后重试", error=str(e), batch_index=i)
                self._ensure_collection(force_reconnect=True)
                client = self._client
                if client is None:
                    break
                res = client.insert(collection_name=self.collection_name, data=batch)
                batch_count = res.get("insert_count", len(batch)) if isinstance(res, dict) else len(batch)
                total_inserted += batch_count

        if hasattr(client, "flush"):
            getattr(client, "flush")(self.collection_name)

        logger.info("Milvus 切片写入成功", total_inserted=total_inserted)
        return total_inserted

    async def vector_search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        doc_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """执行带标量过滤的向量余弦近邻检索."""
        self._ensure_collection()
        client = await self._get_client_async()
        if client is None:
            logger.warning("milvus_client_unavailable_skip_search", message="Milvus 未连接，跳过向量检索")
            return []

        filter_expr = ""
        if doc_filter:
            filter_expr = f"doc_id == '{doc_filter}' or doc_name == '{doc_filter}'"

        search_params = {
            "metric_type": "COSINE",
            "params": {"ef": 64},  # HNSW 检索搜索宽度
        }

        logger.debug("发起 Milvus 向量检索", collection=self.collection_name, top_k=top_k, filter=filter_expr)
        results = None
        try:
            results = client.search(
                collection_name=self.collection_name,
                data=[query_vector],
                limit=top_k,
                filter=filter_expr,
                output_fields=["chunk_text", "doc_id", "doc_name", "chunk_index", "total_chunks"],
                search_params=search_params,
            )
        except Exception as e:
            logger.warning("Milvus 向量检索异常，尝试自动重建重连后重试", error=str(e))
            self._ensure_collection(force_reconnect=True)
            client = self._client
            if client is None:
                return []
            try:
                results = client.search(
                    collection_name=self.collection_name,
                    data=[query_vector],
                    limit=top_k,
                    filter=filter_expr,
                    output_fields=["chunk_text", "doc_id", "doc_name", "chunk_index", "total_chunks"],
                    search_params=search_params,
                )
            except Exception:
                return []

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
        self._ensure_collection()
        client = await self._get_client_async()
        if client is None:
            logger.warning("milvus_client_unavailable_skip_delete", message="Milvus 未连接，跳过切片删除")
            return 0
        filter_expr = f"doc_id == '{doc_id}'"
        try:
            res = client.delete(collection_name=self.collection_name, filter=filter_expr)
            deleted_count = res.get("delete_count", 0) if isinstance(res, dict) else 1
            logger.info("已删除文档切片", doc_id=doc_id, count=deleted_count)
            return deleted_count
        except Exception as e:
            logger.warning("删除文档切片异常，尝试重连重试", error=str(e), doc_id=doc_id)
            self._ensure_collection(force_reconnect=True)
            client = self._client
            if client is None:
                return 0
            try:
                res = client.delete(collection_name=self.collection_name, filter=filter_expr)
                return res.get("delete_count", 0) if isinstance(res, dict) else 1
            except Exception:
                return 0

    async def get_collection_stats(self) -> dict[str, Any]:
        """获取当前知识库的统计信息."""
        client = await self._get_client_async()
        if client is None:
            return {
                "collection_name": self.collection_name,
                "dimension": self.dimension,
                "row_count": 0,
            }
        try:
            stats = client.get_collection_stats(collection_name=self.collection_name)
            return {
                "collection_name": self.collection_name,
                "dimension": self.dimension,
                "row_count": stats.get("row_count", 0),
            }
        except Exception as e:
            logger.warning("获取集合统计信息异常，尝试重连刷新", error=str(e))
            self._ensure_collection(force_reconnect=True)
            client = self._client
            if client is None:
                return {
                    "collection_name": self.collection_name,
                    "dimension": self.dimension,
                    "row_count": 0,
                }
            try:
                stats = client.get_collection_stats(collection_name=self.collection_name)
                return {
                    "collection_name": self.collection_name,
                    "dimension": self.dimension,
                    "row_count": stats.get("row_count", 0),
                }
            except Exception:
                return {
                    "collection_name": self.collection_name,
                    "dimension": self.dimension,
                    "row_count": 0,
                }


# 全局单例
milvus_service = MilvusService()
