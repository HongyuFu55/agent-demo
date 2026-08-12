"""企业级 RAG 知识库管理与混合检索 API 路由."""

from pathlib import Path
import tempfile
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from app.api.v1.auth import get_current_user
from app.core.limiter import limiter
from app.models.user import User
from app.schemas.rag import (
    RAGIngestTextRequest,
    RAGIngestResponse,
    RAGQueryRequest,
    RAGQueryResponse,
    RAGSearchResult,
    RAGStatsResponse,
)
from app.services.rag.service import rag_service

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/ingest/text", response_model=RAGIngestResponse)
@limiter.limit("30/minute")
async def ingest_text(
    request: Request,
    body: RAGIngestTextRequest,
    current_user: User = Depends(get_current_user),
) -> RAGIngestResponse:
    """录入纯文本至知识库，自动分块并构建 Dense & Sparse 索引."""
    logger.info("收到文本录入请求", doc_name=body.doc_name, user_id=current_user.id)
    try:
        res = await rag_service.ingest_text(
            text=body.text,
            doc_name=body.doc_name,
            doc_id=body.doc_id,
            category=body.category,
            extra_metadata={**body.extra_metadata, "user_id": current_user.id},
        )
        return RAGIngestResponse(**res)
    except Exception as e:
        logger.exception("文本录入知识库失败", error=str(e), user_id=current_user.id)
        raise HTTPException(status_code=500, detail=f"录入知识库失败: {str(e)}")


@router.post("/ingest/file", response_model=RAGIngestResponse)
@limiter.limit("20/minute")
async def ingest_file(
    request: Request,
    file: UploadFile = File(..., description="上传的知识库文档文件"),
    doc_name: str | None = Form(default=None, description="自定义文档名称"),
    category: str = Form(default="general", description="文档类别"),
    current_user: User = Depends(get_current_user),
) -> RAGIngestResponse:
    """上传文件（.md, .txt, .docx, .json 等）并解析切片录入知识库."""
    actual_name = doc_name or file.filename or "uploaded_doc"
    logger.info("收到文件录入请求", filename=actual_name, user_id=current_user.id)

    # 写入临时文件解析
    suffix = Path(actual_name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
        tmp_path = Path(tmp_file.name)
        content = await file.read()
        tmp_path.write_bytes(content)

    try:
        res = await rag_service.ingest_file(
            file_path=tmp_path,
            doc_name=actual_name,
            category=category,
        )
        return RAGIngestResponse(**res)
    except Exception as e:
        logger.exception("文件录入知识库失败", error=str(e), filename=actual_name)
        raise HTTPException(status_code=500, detail=f"文件录入失败: {str(e)}")
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@router.post("/query", response_model=RAGQueryResponse)
@limiter.limit("60/minute")
async def query_knowledge_base(
    request: Request,
    body: RAGQueryRequest,
    current_user: User = Depends(get_current_user),
) -> RAGQueryResponse:
    """执行 Dense 向量 + Sparse BM25 多路混合检索与 RRF 加权融合精排."""
    logger.info("收到知识库混合检索请求", query=body.query, top_k=body.top_k, user_id=current_user.id)
    try:
        hits = await rag_service.query(
            query_text=body.query,
            top_k=body.top_k,
            doc_filter=body.doc_filter,
        )
        parsed_results = [RAGSearchResult(**hit) for hit in hits]
        return RAGQueryResponse(
            query=body.query,
            total_hits=len(parsed_results),
            results=parsed_results,
        )
    except Exception as e:
        logger.exception("知识库检索失败", error=str(e), query=body.query)
        raise HTTPException(status_code=500, detail=f"检索知识库失败: {str(e)}")


@router.delete("/documents/{doc_id}")
@limiter.limit("30/minute")
async def delete_document(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
) -> dict[str, str | int]:
    """从知识库中删除指定文档的所有切片."""
    logger.info("收到删除文档请求", doc_id=doc_id, user_id=current_user.id)
    try:
        count = await rag_service.delete_document(doc_id=doc_id)
        return {"status": "success", "doc_id": doc_id, "deleted_chunks": count}
    except Exception as e:
        logger.exception("删除文档切片失败", doc_id=doc_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"删除文档失败: {str(e)}")


@router.get("/stats", response_model=RAGStatsResponse)
@limiter.limit("30/minute")
async def get_stats(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> RAGStatsResponse:
    """获取知识库 Milvus 集合状态与总行数."""
    try:
        stats = await rag_service.get_stats()
        return RAGStatsResponse(**stats)
    except Exception as e:
        logger.exception("获取知识库状态失败", error=str(e))
        raise HTTPException(status_code=500, detail=f"获取状态失败: {str(e)}")
