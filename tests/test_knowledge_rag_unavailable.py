import pytest

from metro_agent.tools.Knowledge_RAGtools import build_rag_search
from metro_agent.tools.knowledge_index_registry import KnowledgeIndexUnavailableError


def test_rag_normalizes_read_proxy_rejection_to_published_knowledge_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def rejecting_proxy_client():
        raise RuntimeError("read proxy returned 503")

    monkeypatch.setattr(
        "metro_agent.tools.Knowledge_RAGtools.get_chroma_client",
        rejecting_proxy_client,
    )

    with pytest.raises(
        KnowledgeIndexUnavailableError,
        match="已发布知识库暂不可用，请稍后重试",
    ):
        build_rag_search("查询规程")
