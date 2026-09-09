from __future__ import annotations

from typing import Any

import tiktoken

# Unit graph execution must not download the optional tokenizer artifact.
# The graph test exercises routing/planning, not token accounting.
class _OfflineEncoding:
    def encode(self, text: str) -> list[int]:
        return [0] * len(text)


tiktoken.get_encoding = lambda _name: _OfflineEncoding()  # type: ignore[assignment]

from metro_agent import graph as graph_module
from metro_agent.planning import agent_adapter


def _no_op(_state: dict[str, Any]) -> dict[str, Any]:
    return {}


def test_explicit_knowledge_request_runs_the_validated_plan_and_returns_retrieval(
    monkeypatch,
) -> None:
    """A successful knowledge preflight must lead to an actual RAG plan execution."""

    retrieved_states: list[dict[str, Any]] = []

    def published_knowledge_preflight() -> None:
        return None

    def retrieve_published_knowledge(state: dict[str, Any]) -> dict[str, Any]:
        retrieved_states.append(state)
        return {
            "agents_output": {
                "knowledge": "信号系统架构由车载、地面和中心子系统组成。",
                "sources": "governed/signalling-architecture.md",
            }
        }

    # The integration test executes the real plan/validator/scheduler/worker/
    # aggregation chain while replacing unrelated persistence side effects.
    for name in (
        "apply_compression_results",
        "prune_short_memory",
        "build_conversation_context",
        "record_assistant_message",
        "detect_compression_jobs",
        "enqueue_compression_jobs",
        "collect_short_memory_stats",
        "trace_finalize_node",
    ):
        monkeypatch.setattr(graph_module, name, _no_op)
    monkeypatch.setattr(graph_module, "build_redis_checkpointer", lambda: None)
    monkeypatch.setitem(
        agent_adapter.AGENT_FUNCTIONS,
        "knowledge_agent",
        retrieve_published_knowledge,
    )

    published_knowledge_preflight()
    result = graph_module.build_graph().invoke(
        {"user_input": "查询信号系统架构", "messages": []}
    )

    assert len(retrieved_states) == 1
    assert retrieved_states[0]["user_input"] == "查询信号系统架构"
    assert retrieved_states[0]["planning_instruction"].startswith("当前计划步骤")
    assert result["planning_status"] == "completed"
    assert "信号系统架构由车载、地面和中心子系统组成。" in result["final_answer"]
    assert "governed/signalling-architecture.md" in result["final_answer"]
