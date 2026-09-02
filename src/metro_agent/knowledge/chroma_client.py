from __future__ import annotations

import chromadb

from metro_agent.knowledge.config import KnowledgeSettings


def get_chroma_client(settings: KnowledgeSettings | None = None):
    """Create the knowledge runtime's HTTP-only Chroma client."""
    value = settings or KnowledgeSettings.from_environment()
    return chromadb.HttpClient(host=value.chroma_host, port=value.chroma_port)
