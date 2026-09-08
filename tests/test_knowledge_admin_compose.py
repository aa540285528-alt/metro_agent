from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict[str, object]:
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def test_compose_adds_an_internal_knowledge_publisher_worker() -> None:
    compose = _compose()
    services = compose["services"]
    publisher = services["knowledge-publisher"]

    assert publisher["image"] == "${METRO_AGENT_IMAGE:-metro-agent:local}"
    assert publisher["command"][:3] == [
        "uvicorn",
        "metro_agent.knowledge_admin.worker:app",
        "--host",
    ]
    assert "ports" not in publisher
    assert "docker.sock" not in " ".join(publisher.get("volumes", []))
    assert publisher["environment"]["KNOWLEDGE_UPLOAD_STAGING_ROOT"] == (
        "/var/lib/metro-agent/knowledge-upload-staging"
    )
    assert publisher["environment"]["KNOWLEDGE_ARTIFACT_ROOT"] == (
        "/var/lib/metro-agent/knowledge-artifacts"
    )
    assert (
        publisher["environment"]["KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET"]
        == "${KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET:?请在 .env 中设置 KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET}"
    )
    assert publisher["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/internal/healthz', timeout=3)",
    ]
    assert publisher["networks"] == ["app_backend", "knowledge_backend"]
    assert "knowledge_frontend" not in publisher["networks"]
    assert "knowledge_upload_staging_data:/var/lib/metro-agent/knowledge-upload-staging" in publisher["volumes"]
    assert "knowledge_artifact_data:/var/lib/metro-agent/knowledge-artifacts" in publisher["volumes"]

    services_with_staging = {
        name
        for name, service in services.items()
        if any("knowledge-upload-staging" in value for value in service.get("volumes", []))
    }
    assert services_with_staging == {"knowledge-publisher"}
    assert "KNOWLEDGE_UPLOAD_STAGING_ROOT" not in services["app"]["environment"]
    assert "KNOWLEDGE_UPLOAD_STAGING_ROOT" not in services["knowledge-read-proxy"]["environment"]
    assert "KNOWLEDGE_UPLOAD_STAGING_ROOT" not in services["knowledge-indexer"]["environment"]
