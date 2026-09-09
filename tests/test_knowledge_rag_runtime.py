from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAG_TOOLS = ROOT / "src" / "metro_agent" / "tools" / "Knowledge_RAGtools.py"


def test_rag_runtime_uses_the_read_proxy_and_refreshes_published_pointer() -> None:
    content = RAG_TOOLS.read_text(encoding="utf-8")

    assert "get_knowledge_read_proxy_client" in content
    assert "read_release_pointer(client)" in content
    assert "PersistentClient" not in content
    assert "chunk_artifacts" not in content
