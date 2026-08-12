"""RAG 知识库统一业务门面服务，串联分块、向量化、存储与混合检索."""

import uuid
from pathlib import Path
from typing import Any
import structlog

try:
    import docx
except ImportError:
    docx = None

try:
    import pypdf
except ImportError:
    pypdf = None

from app.services.rag.text_splitter import RecursiveTextSplitter
from app.services.rag.embedding_service import embedding_service
from app.services.rag.milvus_service import milvus_service
from app.services.rag.hybrid_retriever import hybrid_retriever, bm25_index

logger = structlog.get_logger(__name__)


class RAGService:
    """企业级 RAG 检索增强服务核心门面."""

    def __init__(self):
        """初始化文档切分器与配置."""
        self.text_splitter = RecursiveTextSplitter(chunk_size=600, chunk_overlap=100)

    async def ingest_text(
        self,
        text: str,
        doc_name: str,
        doc_id: str | None = None,
        category: str = "general",
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """将纯文本切片、计算向量并录入 Milvus 和 BM25 索引."""
        if not text or not text.strip():
            raise ValueError("录入的文本内容不能为空")

        actual_doc_id = doc_id or f"doc_{uuid.uuid4().hex[:8]}"
        meta = extra_metadata or {}
        meta["category"] = category

        # 1. 递归分块
        chunks = self.text_splitter.create_chunks_with_metadata(
            text=text,
            doc_id=actual_doc_id,
            doc_name=doc_name,
            extra_metadata=meta,
        )

        if not chunks:
            return {"doc_id": actual_doc_id, "doc_name": doc_name, "chunk_count": 0}

        # 2. 批量计算 Embedding 向量
        chunk_texts = [c["chunk_text"] for c in chunks]
        embeddings = await embedding_service.get_embeddings_batch(chunk_texts)

        # 3. 将向量注入切片结构中
        for chunk, vector in zip(chunks, embeddings, strict=False):
            chunk["vector"] = vector

        # 4. 写入 Milvus 向量数据库
        inserted_count = await milvus_service.insert_chunks(chunks)

        # 5. 同步写入内存 BM25 稀疏索引
        bm25_index.add_documents(chunks)

        logger.info(
            "文档成功录入知识库",
            doc_id=actual_doc_id,
            doc_name=doc_name,
            chunk_count=len(chunks),
            inserted_count=inserted_count,
        )

        return {
            "doc_id": actual_doc_id,
            "doc_name": doc_name,
            "chunk_count": len(chunks),
            "inserted_count": inserted_count,
            "status": "success",
        }

    async def ingest_file(
        self,
        file_path: str | Path,
        doc_name: str | None = None,
        category: str = "general",
    ) -> dict[str, Any]:
        """读取指定路径文件（TXT/Markdown/PDF/DOCX）并切片录入知识库."""
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"未找到目标文件: {file_path}")

        actual_name = doc_name or p.name
        content = ""

        if p.suffix.lower() in [".txt", ".md", ".json", ".csv"]:
            content = p.read_text(encoding="utf-8", errors="ignore")
        elif p.suffix.lower() == ".pdf":
            if pypdf is None:
                raise RuntimeError("pypdf 未安装，无法解析 PDF 文件，请执行 pip install pypdf")
            try:
                reader = pypdf.PdfReader(str(p))
                extracted: list[str] = []
                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        extracted.append(f"<!-- Page {i + 1} -->\n{page_text.strip()}")
                content = "\n\n".join(extracted)
                logger.info("PDF 文件解析完成", filename=actual_name, total_pages=len(reader.pages), chars=len(content))
            except Exception as e:
                logger.exception("解析 PDF 文件失败", file_path=str(p), error=str(e))
                raise ValueError(f"无法解析 PDF 文件: {e}")
        elif p.suffix.lower() == ".docx":
            if docx is None:
                raise RuntimeError("python-docx 未安装，无法解析 DOCX 文件，请执行 pip install python-docx")
            try:
                doc = docx.Document(str(p))
                content = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])
            except Exception as e:
                logger.exception("解析 DOCX 文件失败", file_path=str(p), error=str(e))
                raise ValueError(f"无法解析 DOCX 文件: {e}")
        else:
            # 其它文件默认尝试文本读取
            content = p.read_text(encoding="utf-8", errors="ignore")

        if not content.strip():
            raise ValueError(f"文档 [{actual_name}] 解析后未提取到有效文本内容，请确认文件是否为空或为扫描件图片")

        return await self.ingest_text(
            text=content,
            doc_name=actual_name,
            category=category,
            extra_metadata={"source_file": str(p.name)},
        )

    async def query(
        self,
        query_text: str,
        top_k: int = 5,
        doc_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """执行多路混合检索，返回精排后的高相关切片列表."""
        if not query_text or not query_text.strip():
            return []

        return await hybrid_retriever.search(
            query=query_text.strip(),
            top_k=top_k,
            doc_filter=doc_filter,
        )

    async def delete_document(self, doc_id: str) -> int:
        """从 Milvus 中删除指定文档切片."""
        return await milvus_service.delete_by_doc_id(doc_id)

    async def get_stats(self) -> dict[str, Any]:
        """获取知识库统计信息."""
        return await milvus_service.get_collection_stats()


# 全局单例
rag_service = RAGService()
