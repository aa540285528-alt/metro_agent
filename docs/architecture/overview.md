# Architecture Overview

Metro Agent is a FastAPI application that routes a request through a LangGraph workflow.
The workflow selects domain agents for knowledge retrieval, real-time service queries,
diagnosis, and general assistance. Short-term state is checkpointed in Redis, while
conversation history and trace records use PostgreSQL. Chroma stores the published
knowledge index; source knowledge and indexes are deliberately excluded from this
repository.

## Runtime Boundaries

- `metro_agent.api` exposes the HTTP and streaming interfaces.
- `metro_agent.graph` builds the LangGraph workflow and the agent routing path.
- `metro_agent.tools` contains retrieval and external-service adapters.
- `metro_agent.observability` records traces, tool calls, latency, token usage, and
  evaluation references.
- `metro_agent.storage` and `metro_agent.memory` own persistence adapters.

All provider credentials come from environment variables. The supplied Compose stack is
for local development only; production deployments must authenticate callers on the
server and must not trust a client-provided `user_id`.
