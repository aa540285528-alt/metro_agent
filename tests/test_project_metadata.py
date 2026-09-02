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


def test_langchain_dependencies_use_one_compatible_generation() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(config["project"]["dependencies"])

    assert {
        "langgraph>=1.2,<2",
        "langgraph-checkpoint-redis>=0.5,<1",
        "langchain>=1.3,<2",
        "langchain-core>=1.4,<2",
        "langchain-openai>=1.2,<2",
        "langchain-community>=0.4,<1",
        "langchain-ollama>=1.1,<2",
        "langchain-huggingface>=1.2,<2",
        "torch>=2.12,<2.13",
        "transformers>=4.57.6,<5",
        "huggingface-hub[inference]>=0.36.2,<1",
        "sentence-transformers>=5.5.1,<5.6",
    } <= dependencies


def test_knowledge_runtime_pins_chromadb() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "chromadb==1.5.9" in config["project"]["dependencies"]


def test_container_uses_cpu_torch_and_runtime_dependencies_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG TORCH_VERSION=2.12.0" in dockerfile
    assert "https://download.pytorch.org/whl/cpu" in dockerfile
    assert '"torch==${TORCH_VERSION}+cpu"' in dockerfile
    assert dockerfile.count("--mount=type=cache,target=/root/.cache/pip") == 1
    assert dockerfile.count("--retries 10 --timeout 120") == 1
    assert 'pip install --retries 10 --timeout 120 "."' in dockerfile
    assert dockerfile.count("pip install") == 2
    assert dockerfile.index('"torch==${TORCH_VERSION}+cpu"') < dockerfile.index("COPY . .")
    assert '".[dev]"' not in dockerfile


def test_container_preloads_tiktoken_encoding_for_offline_startup() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache" in dockerfile
    assert 'tiktoken.get_encoding("cl100k_base")' in dockerfile


def test_model_paths_are_container_configurable() -> None:
    config_source = (ROOT / "src/metro_agent/config.py").read_text(encoding="utf-8")
    llama_source = (ROOT / "src/metro_agent/llama_config.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "D:/models" not in config_source
    assert "D:/models" not in llama_source
    assert 'os.environ.get("EMBEDDING_MODEL_PATH", "/models/bge-m3")' in config_source
    assert 'os.getenv("EMBEDDING_MODEL_PATH", "/models/bge-m3")' in llama_source
    assert 'os.getenv("RERANK_MODEL_PATH", "/models/bge-reranker")' in llama_source
    assert "MODEL_DIR=" in env_example
