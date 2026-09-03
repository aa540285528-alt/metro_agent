from __future__ import annotations

from metro_agent.observability.node_instrumentation import traced_node
from metro_agent.planning.factory import create_event, create_plan
from metro_agent.planning.models import PlanStep
from metro_agent.state import MetroAgentState


@traced_node("knowledge_plan")
def knowledge_plan_node(state: MetroAgentState) -> dict:
    """Create the deterministic plan used by explicit knowledge requests."""

    user_input = state["user_input"]
    plan = create_plan(
        goal=user_input,
        steps=[
            PlanStep(
                step_id="knowledge",
                description="Query the governed published knowledge base for evidence.",
                agent="knowledge_agent",
                expected_output="An evidence-based answer with cited knowledge sources.",
            )
        ],
    )
    event = create_event(
        event_type="knowledge_routed",
        message="Explicit knowledge request was routed to the knowledge agent.",
        step_id="knowledge",
    )
    return {
        "execution_plan": plan.model_dump(mode="json"),
        "planning_status": "planning",
        "planning_error": "",
        "planning_events": [event.model_dump(mode="json")],
    }
