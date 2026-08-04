"""Read-only online retrieval over a prebuilt Obsidian knowledge index."""

import logging
import os
from pathlib import Path

import chromadb
from llama_index.core import StorageContext, VectorStoreIndex
from llama_index.core.indices.query.query_transform import HyDEQueryTransform
from llama_index.core.postprocessor import SimilarityPostprocessor
from llama_index.core.query_engine import TransformQueryEngine
from llama_index.vector_stores.chroma import ChromaVectorStore

from metro_agent.tools.knowledge_index_registry import (
    KnowledgeIndexUnavailableError,
    read_published_collection_name,
)
from metro_agent.tools.chunk_artifacts import (
    ChunkManifestUnavailableError,
    read_published_chunk_manifest,
)
from metro_agent.llama_config import (
    ALPHA,
    CHROMA_DB_DIR,
    INDEX_REGISTRY_COLLECTION_NAME,
    SIMILARITY_CUTOFF,
    SIMILARITY_TOP_K,
    SPARSE_TOP_K,
    init_llama_index_components,
)

_query_engine = None
_query_engine_collection_name = None
logger = logging.getLogger(__name__)
DEBUG_HYDE = os.getenv("RAG_DEBUG_HYDE", "0") == "1"
DEFAULT_CHUNK_ARTIFACT_ROOT = Path(__file__).resolve().parent.parent / "artifacts"


class DebugHyDEQueryTransform(HyDEQueryTransform):
    def _run(self, query_bundle, metadata):
        transformed_bundle = super()._run(query_bundle, metadata)

        if DEBUG_HYDE:
            logger.debug(
                "HyDE query=%r retrieval_texts=%r",
                query_bundle.query_str,
                transformed_bundle.embedding_strs,
            )

        return transformed_bundle


def get_chroma_client():
    if not CHROMA_DB_DIR.exists():
        raise KnowledgeIndexUnavailableError(
            "知识库索引尚未构建，请先运行离线构建命令。"
        )
    return chromadb.PersistentClient(path=str(CHROMA_DB_DIR))


def resolve_published_collection_name(
    client=None,
    *,
    artifact_root: Path | str = DEFAULT_CHUNK_ARTIFACT_ROOT,
) -> str:
    if client is None:
        client = get_chroma_client()
    collection_name = read_published_collection_name(
        client,
        INDEX_REGISTRY_COLLECTION_NAME,
    )
    index_build_id = _index_build_id(collection_name)
    if index_build_id is None:
        raise KnowledgeIndexUnavailableError(
            "published index collection has no valid build identifier"
        )
    try:
        manifest = read_published_chunk_manifest(artifact_root, index_build_id)
    except ChunkManifestUnavailableError as exc:
        raise KnowledgeIndexUnavailableError(
            "published index chunk manifest is unavailable"
        ) from exc
    lifecycle = manifest.get("lifecycle")
    if (
        not isinstance(lifecycle, dict)
        or lifecycle.get("status") != "published"
        or lifecycle.get("index_build_id") != index_build_id
        or lifecycle.get("collection_name") != collection_name
    ):
        raise KnowledgeIndexUnavailableError(
            "published index chunk manifest does not match the registry collection"
        )
    return collection_name


def get_published_collection(client, collection_name: str):
    try:
        collection = client.get_collection(name=collection_name)
    except Exception as exc:
        raise KnowledgeIndexUnavailableError(
            "知识库已发布索引不可读取，请重新运行离线构建命令。"
        ) from exc

    if collection.count() <= 0:
        raise KnowledgeIndexUnavailableError(
            "知识库已发布索引为空，请重新运行离线构建命令。"
        )
    return collection


def build_storage_context(chromadb_collection):
    vector_store = ChromaVectorStore(
        chroma_collection=chromadb_collection,
        hybrid_search=True,
    )
    return StorageContext.from_defaults(vector_store=vector_store)


def build_query_engine():
    global _query_engine, _query_engine_collection_name

    client = get_chroma_client()
    collection_name = resolve_published_collection_name(client)
    if (
        _query_engine is not None
        and _query_engine_collection_name == collection_name
    ):
        return _query_engine

    chromadb_collection = get_published_collection(client, collection_name)
    storage_context = build_storage_context(chromadb_collection)
    reranker = init_llama_index_components()
    index = VectorStoreIndex.from_vector_store(
        vector_store=storage_context.vector_store,
        storage_context=storage_context,
    )

    query_engine = index.as_query_engine(
        vector_store_query_mode="hybrid",
        similarity_top_k=SIMILARITY_TOP_K,
        sparse_top_k=SPARSE_TOP_K,
        alpha=ALPHA,
        streaming=True,
        node_postprocessors=[
            reranker,
            SimilarityPostprocessor(similarity_cutoff=SIMILARITY_CUTOFF),
        ],
    )
    _query_engine = TransformQueryEngine(
        query_engine,
        DebugHyDEQueryTransform(include_original=True),
    )
    _query_engine_collection_name = collection_name
    return _query_engine


def build_rag_search(query):
    """Run retrieval and return the answer with source metadata."""
    response = build_query_engine().query(query)
    sources = []
    source_docs = []
    index_build_id = _index_build_id(_query_engine_collection_name)

    for rank, source_node in enumerate(response.source_nodes, start=1):
        metadata = source_node.node.metadata or {}
        file_name = (
            metadata.get("file_name")
            or metadata.get("filename")
            or metadata.get("file_path")
            or "unknown"
        )
        score = _optional_score(getattr(source_node, "score", None))
        raw_value = getattr(source_node, "raw_score", None)
        if raw_value is None:
            raw_value = metadata.get("raw_score")
        rerank_value = getattr(source_node, "rerank_score", None)
        if rerank_value is None:
            rerank_value = metadata.get("rerank_score")
        rerank_score = _optional_score(score if rerank_value is None else rerank_value)
        raw_score = _optional_score(raw_value)
        sources.append(
            {
                "chunk_id": str(
                    getattr(source_node.node, "node_id", "")
                    or getattr(source_node.node, "id_", "")
                ),
                "text": source_node.node.get_content()[:500],
                "score": score,
                "raw_score": raw_score,
                "rerank_score": rerank_score,
                "rank": rank,
                "metadata": metadata,
                "file_name": file_name,
            }
        )
        source_docs.append(file_name)

    return {
        "answer": str(response),
        "sources": sources,
        "source_docs": list(dict.fromkeys(source_docs)),
        "index_build_id": index_build_id,
        "top_k": SIMILARITY_TOP_K,
        "retrieved_count": len(sources),
    }


def _index_build_id(collection_name):
    if not isinstance(collection_name, str) or not collection_name:
        return None
    prefix, separator, suffix = collection_name.rpartition("__build_")
    return suffix if separator and prefix and suffix else None


def _optional_score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
