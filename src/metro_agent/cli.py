from __future__ import annotations

import sys
from typing import Any, Callable
from uuid import uuid4

from metro_agent.agent_service import AgentRunError, build_initial_state
from metro_agent.graph import build_graph
from metro_agent.observability.finalize_node import get_trace_registry


def run_cli_turn(
    app: Any,
    *,
    thread_id: str,
    user_id: str,
    message: str,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Run one CLI request with the same trace lifecycle as the API entrypoint."""
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = build_initial_state(
        thread_id=thread_id,
        user_id=user_id,
        message=message,
    )
    registry = None
    try:
        registry = get_trace_registry()
        registry.start(
            initial_state["trace_id"],
            {
                "thread_id": thread_id,
                "session_id": initial_state["session_id"],
                "user_id": user_id,
                "trace_started_at": initial_state["trace_started_at"],
                "entrypoint": "cli",
            },
        )
    except Exception:
        registry = None

    try:
        for event in app.stream(initial_state, config=config, stream_mode="updates"):
            if on_event is not None:
                on_event(event)
        final_answer = app.get_state(config).values.get("final_answer")
        if not isinstance(final_answer, str) or not final_answer.strip():
            raise AgentRunError("graph completed without a valid final_answer")
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


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    app = build_graph()
    user_id = input("请输入用户ID:").strip() or "user-001"
    thread_id = input("请输入会话ID，留空则新建:").strip() or str(uuid4())
    print(f"当前会话ID: {thread_id}")
    while True:
        user_input = input("请输入问题:").strip()
        if not user_input:
            continue
        if user_input.lower() == "exit":
            break

        def print_event(event: dict[str, Any]) -> None:
            print("\n========== LangGraph Event ==========")
            print(event)

        final_answer = run_cli_turn(
            app,
            thread_id=thread_id,
            user_id=user_id,
            message=user_input,
            on_event=print_event,
        )
        print("\n========== 最终回答 ==========")
        print(final_answer)
        print("==============================")


if __name__ == "__main__":
    main()
