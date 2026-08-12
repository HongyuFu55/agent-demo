"""LangGraph 专用的 RAG 知识库检索增强 Tool."""

import json
from langchain_core.tools import tool
import structlog
from app.services.rag.service import rag_service

logger = structlog.get_logger(__name__)


@tool
async def query_knowledge_base(query: str, top_k: int = 4, doc_filter: str | None = None) -> str:
    """在私有企业/领域知识库中进行多路混合检索（密集向量+BM25关键词精排）.

    当你需要回答涉及企业内部规章制度、产品操作手册、技术文档、FAQ 等专属私有知识库内容时，调用此工具检索切片证据。

    Args:
        query: 检索关键词或具体自然语言提问（例如：“公司年假申请流程是怎样的？”、“Milvus 怎么建索引？”）
        top_k: 期望返回的高相关切片数量（默认 4 条）
        doc_filter: 可选，按特定文档名或 doc_id 精确过滤

    Returns:
        JSON 格式的检索结果字符串，包含切片文本、相关度分数、来源文档名称及序号。
    """
    logger.info("LangGraph 触发知识库检索工具", query=query, top_k=top_k, doc_filter=doc_filter)
    try:
        results = await rag_service.query(query_text=query, top_k=top_k, doc_filter=doc_filter)
        if not results:
            return json.dumps(
                {"status": "empty", "message": f"未在知识库中检索到与 '{query}' 相关的片段内容。"},
                ensure_ascii=False,
            )

        formatted_chunks = []
        for r in results:
            formatted_chunks.append(
                {
                    "doc_name": r.get("doc_name", "未知文档"),
                    "doc_id": r.get("doc_id"),
                    "chunk_index": r.get("chunk_index"),
                    "chunk_text": r.get("chunk_text", ""),
                    "score": round(r.get("rrf_score", 0.0), 4),
                    "source": r.get("source", "hybrid_rrf"),
                }
            )

        return json.dumps({"status": "success", "total_hits": len(formatted_chunks), "results": formatted_chunks}, ensure_ascii=False)
    except Exception as e:
        logger.exception("知识库检索工具执行异常", error=str(e), query=query)
        return json.dumps({"status": "error", "message": f"知识库检索出错: {str(e)}"}, ensure_ascii=False)
