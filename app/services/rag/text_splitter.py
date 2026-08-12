"""智能文本分块器，支持递归切分、重叠度控制与元数据富化."""

import re
from typing import Any
import structlog

logger = structlog.get_logger(__name__)


class RecursiveTextSplitter:
    """递归字符分块器，优先在段落和标点符号处切断，保留上下文语义完整性."""

    def __init__(
        self,
        chunk_size: int = 600,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
    ):
        """初始化分块器参数."""
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or ["\n\n", "\n", "。", "！", "？", "；", ";", ". ", " ", ""]

    def split_text(self, text: str) -> list[str]:
        """将长文本切分成多个带有 Overlap 的文本块."""
        if not text or not text.strip():
            return []

        # 规范化换行与多余空白
        cleaned_text = re.sub(r"\r\n", "\n", text).strip()
        return self._split_recursive(cleaned_text, self.separators)

    def _split_recursive(self, text: str, separators: list[str]) -> list[str]:
        """递归尝试按分隔符列表进行分块."""
        final_chunks: list[str] = []
        if not separators:
            # 兜底：直接按固定字符切
            return [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size - self.chunk_overlap)]

        separator = separators[0]
        remaining_separators = separators[1:]

        if separator == "":
            splits = list(text)
        else:
            splits = text.split(separator)

        current_doc: list[str] = []
        current_len = 0

        for split in splits:
            split_len = len(split)
            if split_len > self.chunk_size:
                # 当前小片段本身就超过了 chunk_size，继续用更小级别的分隔符递归切
                if current_doc:
                    doc_str = separator.join(current_doc).strip()
                    if doc_str:
                        final_chunks.append(doc_str)
                    current_doc = []
                    current_len = 0
                sub_chunks = self._split_recursive(split, remaining_separators)
                final_chunks.extend(sub_chunks)
            elif current_len + split_len + (len(separator) if current_doc else 0) <= self.chunk_size:
                current_doc.append(split)
                current_len += split_len + (len(separator) if len(current_doc) > 1 else 0)
            else:
                if current_doc:
                    doc_str = separator.join(current_doc).strip()
                    if doc_str:
                        final_chunks.append(doc_str)
                # 保留部分尾部内容作为 Overlap
                overlap_doc: list[str] = []
                overlap_len = 0
                for item in reversed(current_doc):
                    if overlap_len + len(item) <= self.chunk_overlap:
                        overlap_doc.insert(0, item)
                        overlap_len += len(item)
                    else:
                        break
                current_doc = overlap_doc + [split]
                current_len = sum(len(x) for x in current_doc) + len(separator) * max(0, len(current_doc) - 1)

        if current_doc:
            doc_str = separator.join(current_doc).strip()
            if doc_str:
                final_chunks.append(doc_str)

        return [c for c in final_chunks if c]

    def create_chunks_with_metadata(
        self,
        text: str,
        doc_id: str,
        doc_name: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """分块并为每个切片附加结构化元数据."""
        raw_chunks = self.split_text(text)
        total_chunks = len(raw_chunks)
        extra = extra_metadata or {}

        results: list[dict[str, Any]] = []
        for idx, chunk in enumerate(raw_chunks):
            results.append(
                {
                    "doc_id": doc_id,
                    "doc_name": doc_name,
                    "chunk_index": idx,
                    "total_chunks": total_chunks,
                    "chunk_text": chunk,
                    "char_count": len(chunk),
                    **extra,
                }
            )

        logger.info(
            "文档分块完成",
            doc_id=doc_id,
            doc_name=doc_name,
            total_chunks=total_chunks,
            total_chars=len(text),
        )
        return results
