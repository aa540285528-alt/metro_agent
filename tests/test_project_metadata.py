from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_dependencies_and_local_services_are_declared() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "dev" in config["project"]["optional-dependencies"]
    dependencies = config["project"]["dependencies"]
    assert "pymysql[rsa]>=1.1,<2" in dependencies
    assert "pymysql>=1.1,<2" not in dependencies
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert all(name in compose for name in ("app:", "redis:", "postgres:", "wiremock:"))
