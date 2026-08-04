# GitHub-ready Metro Agent Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a clean, reproducible, publicly shareable Metro Agent repository at `D:\metro_agent` while preserving the existing offline Agent behavior.

**Architecture:** Whitelist-copy the legacy source and package it beneath `src/metro_agent`. Preserve domain behavior, but isolate runtime configuration, provider adapters, persistence, evaluation and future MCP clients behind explicit packages and public interfaces.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, ChromaDB, Redis, PostgreSQL, Alembic, Docker Compose, pytest, Ruff, GitHub Actions.

---

### Task 1: Define the public boundary

**Files:**
- Create: `.gitignore`, `.dockerignore`, `.env.example`, `LICENSE`
- Create: `tests/test_repository_hygiene.py`

- [ ] **Step 1: Write the failing hygiene test**

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_public_boundary() -> None:
    assert all((ROOT / name).is_file() for name in ("README.md", ".gitignore", ".env.example", "LICENSE", "pyproject.toml"))
    excluded = {".env", ".venv-ragas", "artifacts", "chroma_db", "memory_chroma_db", "resumes", "相关材料"}
    assert not (excluded & {entry.name for entry in ROOT.iterdir()})
```

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest tests/test_repository_hygiene.py -q`

Expected: FAIL before publication metadata exists.

- [ ] **Step 3: Add exclusions and secret placeholders**

Use this minimum `.gitignore`:

```gitignore
.env
.env.*
!.env.example
.venv/
.venv-*/
__pycache__/
.pytest_cache/
.ruff_cache/
.deepeval/
.task3-test-artifacts/
artifacts/
chroma_db/
memory_chroma_db/
*.db
*.sqlite3
*.log
```

Create `.env.example` with only variable names and `CHANGE_ME` placeholders. Add Apache-2.0 as `LICENSE`; exclude source, tests, secrets and local data from Docker context.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_repository_hygiene.py -q`

Expected: PASS.

```powershell
git add .gitignore .dockerignore .env.example LICENSE tests/test_repository_hygiene.py
git commit -m "chore: define public repository boundary"
```

### Task 2: Copy the whitelist and create the package

**Files:**
- Create: `src/metro_agent/__init__.py`, `src/metro_agent/**`, `tests/conftest.py`
- Copy: application source, `alembic`, tests, static UI and sanitized WireMock mappings

- [ ] **Step 1: Write a failing package import test**

```python
def test_package_exposes_api_factory() -> None:
    from metro_agent.api import create_app
    assert callable(create_app)
```

- [ ] **Step 2: Verify it fails before copying**

Run: `python -m pytest tests/test_package_import.py -q`

Expected: `ModuleNotFoundError: metro_agent`.

- [ ] **Step 3: Whitelist-copy and map packages**

Copy only root Python modules, `agents`, `planning`, `observability`, `evaluation`, `conversation_history`, `long_memory_system`, `short_memory_system`, `Agents_tools`, `Agents_skills`, `alembic`, `static`, tests and `wiremock/mappings`.

Map paths:

```text
Agents_tools -> src/metro_agent/tools
Agents_skills -> src/metro_agent/skills
conversation_history -> src/metro_agent/storage/history
long_memory_system -> src/metro_agent/memory/long_term
short_memory_system -> src/metro_agent/memory/short_term
main.py -> src/metro_agent/cli.py
```

Do not copy `.env`, `.git`, caches, virtual environments, artifacts, Chroma databases, resumes, private materials or raw knowledge files.

- [ ] **Step 4: Update imports and tests**

Replace legacy imports with package imports, for example:

```python
from metro_agent.config import build_Chat_QwenLLM
from metro_agent.storage.history.database import SessionLocal
from metro_agent.memory.short_term.redis_checkpointer import build_redis_checkpointer
```

Update Alembic and tests to import from `metro_agent`.

- [ ] **Step 5: Verify and commit**

Run:

```powershell
python -m compileall -q src
python -m pytest tests/test_package_import.py -q
```

Expected: compilation succeeds and importing the API makes no network call.

```powershell
git add src tests alembic alembic.ini static deploy/wiremock
git commit -m "refactor: package Metro Agent source"
```

### Task 3: Remove import-time provider side effects

**Files:**
- Modify: `src/metro_agent/skills/skill_runtime.py`, `src/metro_agent/config.py`
- Create: `tests/test_skill_runtime.py`, `tests/test_config.py`

- [ ] **Step 1: Write a failing regression test**

```python
import importlib

def test_skill_runtime_import_has_no_provider_call(monkeypatch) -> None:
    monkeypatch.setattr("metro_agent.config.build_Chat_QwenLLM", lambda: (_ for _ in ()).throw(AssertionError("provider call")))
    importlib.import_module("metro_agent.skills.skill_runtime")
```

- [ ] **Step 2: Run the test before the change**

Run: `python -m pytest tests/test_skill_runtime.py -q`

Expected: FAIL because the legacy module constructs a Qwen client at import time.

- [ ] **Step 3: Make the demo explicit**

Keep only parsing/discovery/selection functions at import scope. Move the legacy demonstration to:

```python
if __name__ == "__main__":
    print(select_skills_with_llm(build_Chat_QwenLLM(), "生成故障分析报告", discover_skills("diagnosis")))
```

Settings retrieval must never print a secret. Provider client constructors must validate their required key only when invoked.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_skill_runtime.py tests/test_config.py -q`

Expected: PASS with no API request.

```powershell
git add src/metro_agent/skills/skill_runtime.py src/metro_agent/config.py tests/test_skill_runtime.py tests/test_config.py
git commit -m "fix: remove skill import side effects"
```

### Task 4: Add reproducible packaging and deployment

**Files:**
- Create: `pyproject.toml`, `Dockerfile`, `compose.yml`
- Modify: `requirements-dev.txt`, `requirements-web.txt`
- Create: `tests/test_project_metadata.py`, `tests/test_deployment_files.py`

- [ ] **Step 1: Write failing package and deployment checks**

```python
from pathlib import Path
import tomllib

def test_dependencies_and_local_services_are_declared() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "dev" in config["project"]["optional-dependencies"]
    compose = Path("compose.yml").read_text(encoding="utf-8")
    assert all(name in compose for name in ("app:", "redis:", "postgres:", "wiremock:"))
```

- [ ] **Step 2: Verify the checks fail**

Run: `python -m pytest tests/test_project_metadata.py tests/test_deployment_files.py -q`

Expected: FAIL before metadata exists.

- [ ] **Step 3: Implement runtime metadata and containers**

`pyproject.toml` must require Python `>=3.12,<3.13`, use `src` package discovery, and declare FastAPI, Uvicorn, LangChain components, LangGraph, ChromaDB, Redis, SQLAlchemy, Psycopg, Alembic, HTTPX, Pydantic, dotenv, tiktoken, PyYAML, NumPy and requests. Add `evaluation` extras for RAGAS/DeepEval/datasets/sentence-transformers/LlamaIndex/LangMem and `dev` extras for pytest/Ruff.

Docker must start:

```dockerfile
CMD ["uvicorn", "metro_agent.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

Compose must bind app, Redis, PostgreSQL and WireMock only to `127.0.0.1`; WireMock belongs to a `mock` profile and provider credentials are never embedded in Compose.

- [ ] **Step 4: Verify and commit**

Run:

```powershell
python -m pytest tests/test_project_metadata.py tests/test_deployment_files.py -q
docker compose -f compose.yml config
```

Expected: PASS and valid rendered Compose configuration.

```powershell
git add pyproject.toml requirements-dev.txt requirements-web.txt Dockerfile compose.yml tests
git commit -m "build: add reproducible local runtime"
```

### Task 5: Add documentation, fixtures and CI

**Files:**
- Create: `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `docs/architecture/overview.md`, `docs/evaluation.md`
- Create: `fixtures/knowledge/metro_demo.md`, `fixtures/evaluation/sample_cases.jsonl`
- Create: `.github/workflows/ci.yml`, `tests/test_public_fixtures.py`, `tests/test_ci_workflow.py`

- [ ] **Step 1: Write failing fixture and workflow checks**

```python
from pathlib import Path

def test_fixture_and_ci_are_safe_for_public_execution() -> None:
    fixture = "\n".join(file.read_text(encoding="utf-8") for file in Path("fixtures").rglob("*.md"))
    assert "API_KEY=" not in fixture and "身份证" not in fixture and "简历" not in fixture
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "-m \"not live and not integration\"" in workflow
    assert "gitleaks" in workflow.lower() and "ruff check" in workflow
```

- [ ] **Step 2: Verify failure before the files exist**

Run: `python -m pytest tests/test_public_fixtures.py tests/test_ci_workflow.py -q`

Expected: FAIL.

- [ ] **Step 3: Add public onboarding and offline CI**

README covers architecture, capabilities, non-goals, Docker startup, database migration, offline test commands, optional provider configuration, evaluation methodology and data policy. SECURITY states that production identity must not trust a client-supplied `user_id`. Fixtures use fabricated station/device/alarm data only.

CI installs `.[dev]`, runs `ruff check src tests`, runs `pytest -m "not live and not integration"`, and performs a pinned Gitleaks scan. It does not receive provider credentials.

- [ ] **Step 4: Run the release gate and commit**

Run:

```powershell
python -m ruff check src tests
python -m pytest -m "not live and not integration" -q
git ls-files | Select-String -Pattern '(^|/)(\.env|artifacts|chroma_db|memory_chroma_db|resumes|相关材料)(/|$)'
git status --short --branch
```

Expected: Ruff and offline tests pass; the secret/data path scan has no output.

```powershell
git add README.md CONTRIBUTING.md SECURITY.md docs fixtures .github tests
git commit -m "ci: add public offline release gate"
```

## Plan self-review

- Each design requirement has an implementation task: confidentiality (1-2), package boundaries (2), no import side effects (3), dependency/Docker reproducibility (4), and documentation/CI (5).
- Live models and external services are explicitly opt-in and excluded from CI.
- Every task has a focused test command and an intentional commit boundary.
