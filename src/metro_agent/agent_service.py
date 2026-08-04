from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from langgraph.types import Overwrite

from metro_agent.observability.finalize_node import get_trace_registry


class AgentRunError(RuntimeError):
    """Raised when a graph run does not produce a usable final answer."""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    answer: str
    trace_id: str


def build_initial_state(*, thread_id: str, user_id: str, message: str) -> dict[str, Any]:
    trace_id = str(uuid4())
    trace_started_at = datetime.now(timezone.utc).isoformat()
    return {
        "thread_id": thread_id,
        "session_id": thread_id,
        "user_id": user_id,
        "memory_saved": [],
        "pending_memories": [],
        "memory_notifications": [],
        "rule_change_requested": False,
        "user_input": message,
        "agents_output": {},
        "final_answer": "",
        "execution_plan": Overwrite({}),
        "plan_results": Overwrite({}),
        "planning_events": Overwrite([]),
        "planning_error": "",
        "planning_status": "",
        "current_plan_step": {},
        "planning_instruction": "",
        "dependency_outputs": {},
        "tool_results": Overwrite({}),
        "trace_id": trace_id,
        "trace_started_at": trace_started_at,
        "trace_summary": {},
        "trace_artifact_uri": "",
    }


def _run_graph(
    graph: Any,
    *,
    initial_state: dict[str, Any],
    thread_id: str,
    user_id: str,
) -> str:
    config = {"configurable": {"thread_id": thread_id}}
    registry = None
    try:
        registry = get_trace_registry()
        registry.start(
            initial_state["trace_id"],
            {
                "thread_id": thread_id,
                "user_id": user_id,
                "trace_started_at": initial_state["trace_started_at"],
            },
        )
    except Exception:
        registry = None

    try:
        for _ in graph.stream(initial_state, config=config, stream_mode="updates"):
            pass

        snapshot = graph.get_state(config)
        final_answer = snapshot.values.get("final_answer")
        if not isinstance(final_answer, str) or not final_answer.strip():
            raise AgentRunError("Graph state final_answer is missing or blank")

        if registry is not None:
            try:
                recorder = registry.get(initial_state["trace_id"])
                if recorder is not None:
                    registry.finish(initial_state["trace_id"], "completed", {})
            except AttributeError:
                registry.finish(initial_state["trace_id"], "completed", {})
            except Exception:
                pass

        return final_answer.strip()
    except Exception as error:
        if registry is not None:
            try:
                registry.finish(
                    initial_state["trace_id"],
                    "failed",
                    {"error_type": type(error).__name__},
                )
            except Exception:
                pass
        raise


def run_chat_with_trace(
    graph: Any,
    *,
    thread_id: str,
    user_id: str,
    message: str,
) -> AgentRunResult:
    initial_state = build_initial_state(
        thread_id=thread_id,
        user_id=user_id,
        message=message,
    )
    answer = _run_graph(
        graph,
        initial_state=initial_state,
        thread_id=thread_id,
        user_id=user_id,
    )
    return AgentRunResult(answer=answer, trace_id=initial_state["trace_id"])


def run_chat(graph: Any, *, thread_id: str, user_id: str, message: str) -> str:
    return run_chat_with_trace(
        graph,
        thread_id=thread_id,
        user_id=user_id,
        message=message,
    ).answer
