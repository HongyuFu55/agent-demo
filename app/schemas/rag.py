"""RAG 知识库相关的 Pydantic 请求与响应数据契约模型."""

from typing import Any
from pydantic import BaseModel, Field


class RAGIngestTextRequest(BaseModel):
    """纯文本录入知识库请求."""

    text: str = Field(..., min_length=1, description="要录入的文档文本内容")
    doc_name: str = Field(..., min_length=1, max_length=200, description="文档名称，例如 '企业请假管理制度.md'")
    doc_id: str | None = Field(default=None, description="自定义文档ID，不传则自动生成唯一标识")
    category: str = Field(default="general", description="文档分类/类别")
    extra_metadata: dict[str, Any] = Field(default_factory=dict, description="额外自定义元数据")


class RAGIngestResponse(BaseModel):
    """知识库录入响应."""

    doc_id: str = Field(..., description="已录入的文档ID")
    doc_name: str = Field(..., description="文档名称")
    chunk_count: int = Field(..., description="分块切片总数")
    inserted_count: int = Field(..., description="向量库成功写入条数")
    status: str = Field(default="success", description="录入状态")


class RAGQueryRequest(BaseModel):
    """混合检索查询请求."""

    query: str = Field(..., min_length=1, description="用户检索提问关键词或自然语言问题")
    top_k: int = Field(default=5, ge=1, le=50, description="召回切片数量")
    doc_filter: str | None = Field(default=None, description="指定过滤的文档ID或文档名")


class RAGSearchResult(BaseModel):
    """单条召回的切片数据与元数据."""

    id: Any = Field(default=None, description="切片主键ID")
    chunk_text: str = Field(..., description="文本切片内容")
    doc_id: str = Field(..., description="所属文档ID")
    doc_name: str = Field(..., description="所属文档名称")
    chunk_index: int = Field(..., description="切片序号")
    total_chunks: int = Field(default=1, description="所属文档切片总数")
    source: str = Field(default="hybrid_rrf", description="召回源 (milvus_dense / bm25_sparse / hybrid_rrf)")
    score: float | None = Field(default=None, description="原始检索得分")
    rrf_score: float | None = Field(default=None, description="加权 RRF 融合得分")
    rrf_rank: int | None = Field(default=None, description="融合排序名次")


class RAGQueryResponse(BaseModel):
    """混合检索查询响应."""

    query: str = Field(..., description="原始检索词")
    total_hits: int = Field(..., description="命中切片总数")
    results: list[RAGSearchResult] = Field(default_factory=list, description="精排召回结果列表")


class RAGStatsResponse(BaseModel):
    """知识库统计信息响应."""

    collection_name: str = Field(..., description="Milvus Collection 名称")
    dimension: int = Field(..., description="向量维度")
    row_count: int = Field(..., description="向量库中文档切片总行数")
