from pathlib import Path

import pytest

from metro_agent.knowledge.config import KnowledgeSettings


ROOT = Path(__file__).resolve().parents[1]
LLAMA_CONFIG = ROOT / "src" / "metro_agent" / "llama_config.py"
ENV_EXAMPLE = ROOT / ".env.example"


def test_settings_read_http_endpoint_and_optional_paths(monkeypatch) -> None:
    monkeypatch.setenv("CHROMA_HOST", "knowledge-read-proxy")
    monkeypatch.setenv("CHROMA_PORT", "8123")
    monkeypatch.setenv("KNOWLEDGE_ARTIFACT_ROOT", "/var/lib/knowledge-artifacts")
    monkeypatch.setenv("KNOWLEDGE_PATH", "/knowledge/source")
    monkeypatch.setenv("KNOWLEDGE_E2E", "1")
    monkeypatch.setenv("METRO_AGENT_ENV", "development")

    assert KnowledgeSettings.from_environment() == KnowledgeSettings(
        chroma_host="knowledge-read-proxy",
        chroma_port=8123,
        artifact_root=Path("/var/lib/knowledge-artifacts"),
        source_root=Path("/knowledge/source"),
        e2e=True,
    )


def test_settings_default_to_chroma_http_endpoint(monkeypatch) -> None:
    for variable in (
        "CHROMA_HOST",
        "CHROMA_PORT",
        "KNOWLEDGE_ARTIFACT_ROOT",
        "KNOWLEDGE_PATH",
        "KNOWLEDGE_E2E",
        "METRO_AGENT_ENV",
    ):
        monkeypatch.delenv(variable, raising=False)

    assert KnowledgeSettings.from_environment() == KnowledgeSettings(
        chroma_host="chroma",
        chroma_port=8000,
        artifact_root=None,
        source_root=None,
        e2e=False,
    )


def test_production_rejects_e2e_embedding(monkeypatch) -> None:
    monkeypatch.setenv("METRO_AGENT_ENV", "production")
    monkeypatch.setenv("KNOWLEDGE_E2E", "1")

    with pytest.raises(ValueError, match="KNOWLEDGE_E2E"):
        KnowledgeSettings.from_environment()


def test_knowledge_environment_is_documented_without_a_fixed_source_path() -> None:
    environment = ENV_EXAMPLE.read_text(encoding="utf-8")
    llama_config = LLAMA_CONFIG.read_text(encoding="utf-8")

    for variable in (
        "METRO_AGENT_ENV=",
        "CHROMA_HOST=",
        "CHROMA_PORT=",
        "KNOWLEDGE_PATH=",
        "KNOWLEDGE_ARTIFACT_ROOT=",
        "KNOWLEDGE_E2E=",
        "CHROMA_DB_DIR=",
    ):
        assert variable in environment
    assert "d:/AIknowledge/wiki" not in llama_config
    assert "KnowledgeSettings.from_environment().source_root" in llama_config


def test_client_factory_connects_through_configured_http_endpoint(monkeypatch) -> None:
    from metro_agent.knowledge.chroma_client import get_chroma_client

    calls: list[tuple[str, int]] = []

    def fake_http_client(*, host: str, port: int) -> object:
        calls.append((host, port))
        return object()

    monkeypatch.setattr(
        "metro_agent.knowledge.chroma_client.chromadb.HttpClient", fake_http_client
    )

    client = get_chroma_client(
        KnowledgeSettings(
            chroma_host="knowledge-read-proxy",
            chroma_port=8000,
            artifact_root=None,
            source_root=None,
            e2e=False,
        )
    )

    assert client is not None
    assert calls == [("knowledge-read-proxy", 8000)]
