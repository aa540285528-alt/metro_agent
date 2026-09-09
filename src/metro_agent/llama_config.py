import os
from pathlib import Path

from metro_agent.knowledge.config import KnowledgeSettings
from metro_agent.storage_paths import configured_storage_path


# LlamaIndex基础配置
def init_llama_index_components():
    from llama_index.core import Settings
    if os.getenv("KNOWLEDGE_E2E") == "1":
        from llama_index.core.embeddings import MockEmbedding
        from llama_index.core.llms import MockLLM

        Settings.llm = MockLLM()
        Settings.embed_model = MockEmbedding(embed_dim=8)
        return None

    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    from llama_index.llms.deepseek import DeepSeek
    from llama_index.postprocessor.sbert_rerank import SentenceTransformerRerank

    # 1. 设置 LLM
    Settings.llm = DeepSeek(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        streaming=True
    )
    
    # 2. 设置 Embedding
    Settings.embed_model = HuggingFaceEmbedding(
    model_name=os.getenv("EMBEDDING_MODEL_PATH", "/models/bge-m3"),
    device="cpu",
    embed_batch_size=32,
    normalize=True,
    query_instruction="Represent this sentence for searching relevant passages: "
    )
    
    # 3. 构建 Reranker（不直接赋给 Settings，而是返回）
    reranker = SentenceTransformerRerank(
        model=os.getenv("RERANK_MODEL_PATH", "/models/bge-reranker"),
        top_n=3
    )
    
    return reranker

# RAG检索配置
SIMILARITY_TOP_K = 10
SPARSE_TOP_K = 5
ALPHA = 0.4


def read_similarity_cutoff(raw_value: str | None = None) -> float:
    """Return the post-rerank cutoff, rejecting unsafe configuration values."""
    raw = (
        os.getenv("RAG_SIMILARITY_CUTOFF", "0.5")
        if raw_value is None
        else raw_value
    )
    try:
        cutoff = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "RAG_SIMILARITY_CUTOFF must be a number in [0, 1]"
        ) from exc
    if not 0.0 <= cutoff <= 1.0:
        raise ValueError("RAG_SIMILARITY_CUTOFF must be a number in [0, 1]")
    return cutoff


SIMILARITY_CUTOFF = read_similarity_cutoff()

# RAG检索数据库配置
BASE_DIR = Path(__file__).resolve().parent
CHROMA_DB_DIR = configured_storage_path("CHROMA_DB_DIR", BASE_DIR / "chroma_db")
COLLECTION_NAME = "Metro_Knowledge_Obsidian_v1"
INDEX_REGISTRY_COLLECTION_NAME = "Metro_Knowledge_Index_Registry_v1"
KNOWLEDGE_PATH = KnowledgeSettings.from_environment().source_root
