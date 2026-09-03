from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KnowledgeSettings:
    """Environment-backed configuration for HTTP knowledge services."""

    chroma_host: str
    chroma_port: int
    artifact_root: Path | None
    source_root: Path | None
    e2e: bool
    knowledge_read_proxy_url: str = "http://knowledge-read-proxy:8000"

    @classmethod
    def from_environment(cls) -> KnowledgeSettings:
        e2e = os.getenv("KNOWLEDGE_E2E", "0") == "1"
        if os.getenv("METRO_AGENT_ENV", "").lower() == "production" and e2e:
            raise ValueError("KNOWLEDGE_E2E=1 is not allowed in production")

        return cls(
            chroma_host=os.getenv("CHROMA_HOST", "chroma"),
            chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
            artifact_root=_optional_path("KNOWLEDGE_ARTIFACT_ROOT"),
            source_root=_optional_path("KNOWLEDGE_PATH"),
            e2e=e2e,
        )


def _optional_path(variable: str) -> Path | None:
    value = os.getenv(variable)
    return Path(value) if value else None


def require_knowledge_source_root(source_root: Path | None) -> Path:
    """Return a configured source root or reject an indexing attempt early."""
    if source_root is None:
        raise ValueError("KNOWLEDGE_PATH must be configured before indexing")
    return source_root
