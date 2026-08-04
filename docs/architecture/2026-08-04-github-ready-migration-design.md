# Metro Agent: GitHub-ready migration design

## Objective

Create an independently runnable, publicly shareable Metro Agent repository without publishing credentials, personal data, locally generated indexes, conversation history, or evaluation artifacts.

## Source selection

Copy application source, migrations, tests, static UI, and sanitized WireMock mappings from the legacy workspace. Do not copy `.env`, `.git`, virtual environments, caches, vector indexes, runtime artifacts, resumes, private materials, or raw knowledge documents.

## Target architecture

The initial public baseline keeps the current package boundaries intact to reduce behavior risk, while introducing a `src/metro_agent` package as the stable import boundary. API, CLI, agents, planning, retrieval, memory, persistence, observability, evaluation, and external adapters will be separated beneath it. Future MCP clients live in `adapters/mcp`.

## Reproducibility

Use `pyproject.toml` to define runtime, evaluation, and development dependency groups. Provide `.env.example`, a Dockerfile, a single Compose stack for the application, Redis, PostgreSQL, and optional WireMock, plus documented commands for migrations, local startup, and offline tests.

## Security and data policy

Secrets are only injected through environment variables. Public fixtures are synthetic and anonymized. The public API documentation declares that client-provided `user_id` is development-only and that production deployments must derive identity from an authenticated principal.

## Quality gate

CI runs formatting/linting, secret scanning, and deterministic unit tests. Integration and paid live-model tests are explicitly marked and excluded by default. The migration also removes model invocation side effects from skill module imports.

## Acceptance criteria

- A clean clone contains no secret, personal file, runtime database, index, or artifact.
- The repository initializes as a valid Git repository with an intentional first commit.
- Dependencies and startup instructions are reproducible on a new machine.
- The application and its backing services have a documented Docker startup path.
- Offline CI does not require API keys or paid model calls.
