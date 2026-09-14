"""
Chroma 向量存储层
================
负责记忆向量的持久化存储和相似度检索，基于 ChromaDB + HuggingFace Embedding。

职责边界：
  - 只负责向量写入（upsert）和按 user_id + 活跃状态检索（search）
  - 不负责冲突检测、价值更新等业务逻辑（那些在上层 policy 中处理）

存储隔离：每条记忆按 user_id 分区，查询时过滤 status="active"
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chromadb
from langchain_huggingface.embeddings import HuggingFaceEmbeddings
from metro_agent.config import (
    MEMORY_CHROMA_DB_DIR,
    MEMORY_COLLECTION_NAME,
)
from metro_agent.memory.long_term.models import MemoryRecord
from metro_agent.config import EMBEDDING_MODEL





@dataclass(frozen=True)
class MemorySearchResult:
    """单条向量检索结果，不可变"""

    memory_id: str
    content: str
    metadata: dict[str, Any]
    score: float





class ChromaMemoryStore:
    """
    基于 ChromaDB 的记忆向量存储。

    功能：
      - upsert: 将 MemoryRecord 向量化后写入/更新
      - search: 按 user_id 过滤 + 向量相似度检索，只返回 status="active" 的记忆

    配置：
      - HNSW 索引 + 余弦距离（cosine）
      - embedding 由外部注入（支持 llama_index 或 LangChain 的 embedding 模型）
    """

    def __init__(
        self,
        persist_directory: Path,
        collection_name: str,
        embedding_model: HuggingFaceEmbeddings,
    ):

        persist_directory.mkdir(parents=True, exist_ok=True)

        self.embedding_model = embedding_model


        self.client = chromadb.PersistentClient(path=str(persist_directory))



        self.collection = (
            self.client.get_or_create_collection(
                name=collection_name,
                configuration={
                    "hnsw": {
                        "space": "cosine",
                    }
                },
                embedding_function=None,
            )
        )

    def delete(self, memory_id: str) -> None:
        self.collection.delete(ids=[memory_id])



    def upsert(self, record: MemoryRecord) -> None:
        """
        将一条记忆记录写入 ChromaDB（已存在则覆盖）。

        流程：
          1. 对 memory.content 做文本向量化
          2. 提取关键字段构建 metadata 字典
          3. 调用 collection.upsert 写入
        """
        if not record.content.strip():
            raise ValueError("禁止向 Chroma 写入空记忆")

        embedding = self.embedding_model.embed_query(
            record.content
        )


        metadata = {
            "user_id": record.user_id,
            "category": record.category,
            "source": record.source,
            "sensitivity": record.sensitivity,
            "status": record.status,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "initial_value": record.initial_value,
            "current_value": record.current_value,
            "pinned": record.pinned,
            "version": record.version,
        }


        if record.target_date is not None:
            metadata["target_date"] = record.target_date.isoformat()

        if record.valid_until is not None:
            metadata["valid_until"] = record.valid_until.isoformat()

        if record.previous_id is not None:
            metadata["previous_id"] = record.previous_id


        self.collection.upsert(
            ids=[record.memory_id],
            documents=[record.content],
            embeddings=[embedding],
            metadatas=[metadata],
        )
    def list_active(
    self,
    *,
    user_id: str,
    category: str | None = None,
    ) -> list[dict]:
        conditions = [
        {"user_id": {"$eq": user_id}},
        {"status": {"$eq": "active"}},
        ]

        if category:
            conditions.append(
            {"category": {"$eq": category}}
        )

        result = self.collection.get(
            where={"$and": conditions},
            include=["documents", "metadatas"],
        )
        memories = []

        for memory_id, content, metadata in zip(
            result["ids"],
            result["documents"],
            result["metadatas"],
        ):
            if not content or not content.strip():
                continue

            memories.append({
                "memory_id": memory_id,
                "content": content,
                "metadata": metadata,
            })

        memories.sort(
            key=lambda item: item["metadata"].get(
                "updated_at", ""
            ),
            reverse=True,
        )

        return memories



    def search(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[MemorySearchResult]:
        """
        按用户过滤 + 向量相似度检索活跃记忆。

        检索流程：
          1. 空集合快速返回
          2. 将 query 文本向量化
          3. 调用 ChromaDB 查询，where 条件过滤 user_id + status="active"
          4. 组装 MemorySearchResult 列表并返回

        score 计算：将 ChromaDB 的 cosine distance 转换为相似度分数（1.0 - distance）
        """

        if self.collection.count() == 0:
            return []


        query_embedding = self.embedding_model.embed_query(query)


        result = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(limit, self.collection.count()),
            where={
                "$and": [
                    {"user_id": {"$eq": user_id}},
                    {"status": {"$eq": "active"}},
                ]
            },
            include=[
                "documents",
                "metadatas",
                "distances",
            ],
        )


        memories = []
        ids = result["ids"][0]
        documents = result["documents"][0]
        metadatas = result["metadatas"][0]
        distances = result["distances"][0]

        for memory_id, content, metadata, distance in zip(
            ids,
            documents,
            metadatas,
            distances,
        ):

            memories.append(
                MemorySearchResult(
                    memory_id=memory_id,
                    content=content,
                    score=max(0.0, 1.0 - distance),
                    metadata=metadata,
                )
            )
        return memories






_memory_store = None

def build_memory_store() -> ChromaMemoryStore:
    """
    构建/获取 ChromaMemoryStore 全局单例。

    首次调用时：
      1. 使用 config 中的 EMBEDDING_MODEL 作为嵌入模型
      2. 创建 ChromaMemoryStore 实例（连接 ChromaDB）
    后续调用直接返回已创建的实例。
    """
    global _memory_store
    if _memory_store is None:
        embedding_model = EMBEDDING_MODEL
        _memory_store = ChromaMemoryStore(
            persist_directory=MEMORY_CHROMA_DB_DIR,
            collection_name=MEMORY_COLLECTION_NAME,
            embedding_model=embedding_model,
        )
    return _memory_store
