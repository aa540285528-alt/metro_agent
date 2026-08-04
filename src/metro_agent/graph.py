from langgraph.graph import END, StateGraph

from metro_agent.memory.long_term.memory_intent import is_memory_query
from metro_agent.memory.long_term.memory_recall_nodes import memory_recall_node
from metro_agent.observability.finalize_node import trace_finalize_node
from metro_agent.observability.node_instrumentation import traced_node
from metro_agent.planning import (
    plan_validator_node,
    planner_node,
    planning_aggregate_node,
    planning_worker_node,
    route_ready_steps,
)
from metro_agent.planning.safety_refusal_node import safety_refusal_plan_node
from metro_agent.planning.diagnosis_plan_node import diagnosis_plan_node
from metro_agent.safety_policy import is_diagnosis_request, is_high_risk_safety_request
from metro_agent.memory.short_term import (
    apply_compression_results,
    build_conversation_context,
    build_redis_checkpointer,
    collect_short_memory_stats,
    detect_compression_jobs,
    enqueue_compression_jobs,
    prune_short_memory,
    record_assistant_message,
)
from metro_agent.state import MetroAgentState


def entry_router(state: MetroAgentState) -> dict:
    return {}


def route_entry(state: MetroAgentState) -> str:
    if is_high_risk_safety_request(state["user_input"]):
        return "safety_refusal_plan_node"
    if is_diagnosis_request(state["user_input"]):
        return "diagnosis_plan_node"
    if is_memory_query(state["user_input"]):
        return "memory_recall_node"
    return "planner_node"


def route_after_planner(state: MetroAgentState) -> str:
    if state.get("planning_status") == "failed":
        return "record_assistant_message"
    return "plan_validator_node"


def route_after_plan_validator(state: MetroAgentState) -> str:
    if state.get("planning_status") == "validated":
        return "planning_scheduler_node"
    return "record_assistant_message"


@traced_node("planning_scheduler_node")
def planning_scheduler_node(state: MetroAgentState) -> dict:
    return {}


def route_after_aggregate(state: MetroAgentState) -> str:
    if state.get("planning_status") in {"completed", "failed"}:
        return "record_assistant_message"
    return "planning_scheduler_node"


def build_graph() -> StateGraph:
    graph = StateGraph(MetroAgentState)
    graph.add_node("entry_router", entry_router)
    graph.add_node("planner_node", planner_node)
    graph.add_node("safety_refusal_plan_node", safety_refusal_plan_node)
    graph.add_node("diagnosis_plan_node", diagnosis_plan_node)
    graph.add_node("plan_validator_node", plan_validator_node)
    graph.add_node("planning_scheduler_node", planning_scheduler_node)
    graph.add_node("planning_worker_node", planning_worker_node)
    graph.add_node("planning_aggregate_node", planning_aggregate_node)
    graph.add_node("trace_finalize_node", trace_finalize_node)
    graph.add_node("memory_recall_node", memory_recall_node)
    graph.add_node("record_assistant_message", record_assistant_message)
    graph.add_node("build_conversation_context", build_conversation_context)
    graph.add_node("detect_compression_jobs", detect_compression_jobs)
    graph.add_node("enqueue_compression_jobs", enqueue_compression_jobs)
    graph.add_node("apply_compression_results", apply_compression_results)
    graph.add_node("prune_short_memory", prune_short_memory)
    graph.add_node("collect_short_memory_stats", collect_short_memory_stats)

    graph.set_entry_point("apply_compression_results")
    graph.add_edge("apply_compression_results", "prune_short_memory")
    graph.add_edge("prune_short_memory", "build_conversation_context")
    graph.add_edge("build_conversation_context", "entry_router")
    graph.add_conditional_edges(
        "entry_router",
        route_entry,
        {
            "memory_recall_node": "memory_recall_node",
            "planner_node": "planner_node",
            "safety_refusal_plan_node": "safety_refusal_plan_node",
            "diagnosis_plan_node": "diagnosis_plan_node",
        },
    )
    graph.add_conditional_edges(
        "planner_node",
        route_after_planner,
        {
            "plan_validator_node": "plan_validator_node",
            "record_assistant_message": "record_assistant_message",
        },
    )
    graph.add_conditional_edges(
        "safety_refusal_plan_node",
        route_after_planner,
        {
            "plan_validator_node": "plan_validator_node",
            "record_assistant_message": "record_assistant_message",
        },
    )
    graph.add_conditional_edges(
        "diagnosis_plan_node",
        route_after_planner,
        {
            "plan_validator_node": "plan_validator_node",
            "record_assistant_message": "record_assistant_message",
        },
    )
    graph.add_conditional_edges(
        "plan_validator_node",
        route_after_plan_validator,
        {
            "planning_scheduler_node": "planning_scheduler_node",
            "record_assistant_message": "record_assistant_message",
        },
    )
    graph.add_conditional_edges(
        "planning_scheduler_node",
        route_ready_steps,
        {
            "planning_worker_node": "planning_worker_node",
            "planning_aggregate_node": "planning_aggregate_node",
        },
    )
    graph.add_edge("planning_worker_node", "planning_aggregate_node")
    graph.add_conditional_edges(
        "planning_aggregate_node",
        route_after_aggregate,
        {
            "planning_scheduler_node": "planning_scheduler_node",
            "record_assistant_message": "record_assistant_message",
        },
    )
    graph.add_edge("memory_recall_node", "record_assistant_message")
    graph.add_edge("record_assistant_message", "detect_compression_jobs")
    graph.add_edge("detect_compression_jobs", "enqueue_compression_jobs")
    graph.add_edge("enqueue_compression_jobs", "collect_short_memory_stats")
    graph.add_edge("collect_short_memory_stats", "trace_finalize_node")
    graph.add_edge("trace_finalize_node", END)
    return graph.compile(checkpointer=build_redis_checkpointer())
