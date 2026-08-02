"""RAG 召回器 — 基于 FAISS 的上下文检索"""
import datetime
import logging

import numpy as np
from google import genai

from ..config import ContextConfig
from .block_store import ContextBlock
from .ports import BlockRepository

logger = logging.getLogger(__name__)


class RAGRetriever:
    """RAG 召回器

    通过 gemini-embedding-001 嵌入查询，
    FAISS top-K 搜索，贪心选择直到接近 max_tokens。
    """

    def __init__(self, context_config: ContextConfig, block_store: BlockRepository, api_key: str):
        self.config = context_config
        self.store = block_store
        self.client = genai.Client(api_key=api_key)
        self.embedding_model = context_config.embedding_model   # gemini-embedding-001
        self.embedding_dim = context_config.embedding_dim       # 768

    async def embed_text(self, text: str) -> np.ndarray:
        """使用 embedding 模型嵌入文本"""
        try:
            response = await self.client.aio.models.embed_content(
                model=self.embedding_model,
                contents=text,
                config={"output_dimensionality": self.embedding_dim},
            )
            return np.array(response.embeddings[0].values, dtype=np.float32)
        except Exception as e:
            logger.error("[RAG] Embedding failed: %s", e)
            return np.zeros(self.embedding_dim, dtype=np.float32)

    async def retrieve(self, query: str, max_tokens: int = 2000) -> str:
        """执行 RAG 召回

        Args:
            query: 检索查询
            max_tokens: 最大召回 token 数

        Returns:
            格式化的召回结果文本
        """
        if not self.store.blocks:
            return "[RECALLED MEMORY]\nNo memories available."

        # 1. 嵌入查询
        query_embedding = await self.embed_text(query)

        # 2. FAISS 搜索（多取一些，后面按 token 限制筛选）
        candidates = self.store.search_similar(query_embedding, top_k=20)

        # 3. 贪心选择直到接近 max_tokens
        selected = []
        total_tokens = 0
        for block, score in candidates:
            if total_tokens + block.token_estimate > max_tokens:
                continue
            selected.append((block, score))
            total_tokens += block.token_estimate

        # 4. 按时间排序
        selected.sort(key=lambda x: x[0].timestamp)

        # 5. 格式化输出
        if not selected:
            return f"[RECALLED MEMORY]\nNo relevant memories found for: {query}"

        lines = [
            f"[RECALLED MEMORY] (query: {query}, {len(selected)} blocks, ~{total_tokens} tokens)"
        ]
        for block, score in selected:
            content = block.summary if block.summary else block.content[:200]
            time_str = _format_timestamp(block.timestamp)
            lines.append(
                f"- [{time_str}] ({block.block_type}, "
                f"imp={block.importance_score:.1f}, sim={score:.2f}): {content}"
            )

        return "\n".join(lines)


def _format_timestamp(ts: float) -> str:
    """格式化时间戳"""
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M:%S")
