from __future__ import annotations

import chromadb
from urllib.parse import urlparse

from metro_agent.knowledge.config import KnowledgeSettings


def get_chroma_client(settings: KnowledgeSettings | None = None):
    """Create the knowledge runtime's HTTP-only Chroma client."""
    value = settings or KnowledgeSettings.from_environment()
    return chromadb.HttpClient(host=value.chroma_host, port=value.chroma_port)


def get_knowledge_read_proxy_client(settings: KnowledgeSettings | None = None):
    """Connect the online RAG runtime only to its fixed read proxy."""
    value = settings or KnowledgeSettings.from_environment()
    endpoint = urlparse(value.knowledge_read_proxy_url)
    if (
        endpoint.scheme != "http"
        or not endpoint.hostname
        or endpoint.path not in ("", "/")
        or endpoint.params
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("KNOWLEDGE_READ_PROXY_URL must be a plain http endpoint")
    return chromadb.HttpClient(host=endpoint.hostname, port=endpoint.port or 8000)
