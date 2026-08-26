from pathlib import Path

from metro_agent.storage_paths import configured_storage_path


def test_configured_storage_path_uses_default_when_environment_is_absent(
    monkeypatch,
) -> None:
    monkeypatch.delenv("TEST_STORAGE_PATH", raising=False)

    assert configured_storage_path("TEST_STORAGE_PATH", Path("fallback")) == Path(
        "fallback"
    )


def test_configured_storage_path_uses_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("TEST_STORAGE_PATH", "/var/lib/metro-agent/chroma/current")

    assert configured_storage_path("TEST_STORAGE_PATH", Path("fallback")) == Path(
        "/var/lib/metro-agent/chroma/current"
    )
