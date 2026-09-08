from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_package_exposes_api_factory() -> None:
    from metro_agent.api import create_app

    assert callable(create_app)


def test_internal_worker_app_can_be_created_with_multipart_support() -> None:
    from metro_agent.knowledge_admin.worker import create_app

    app = create_app(
        service=SimpleNamespace(staging_root=ROOT),
        internal_bearer_secret="publisher-secret",
    )

    assert any(route.path == "/internal/drafts" for route in app.routes)


def test_package_declares_python_multipart_runtime_dependency() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python-multipart" in pyproject
