from __future__ import annotations

from metro_agent.observability.node_instrumentation import traced_node
from metro_agent.planning.factory import create_event, create_plan
from metro_agent.planning.models import PlanStep
from metro_agent.state import MetroAgentState


@traced_node("diagnosis_plan")
def diagnosis_plan_node(state: MetroAgentState) -> dict:
    """Create a single diagnosis step for operational fault handling requests."""
    user_input = state["user_input"]
    plan = create_plan(
        goal=user_input,
        steps=[
            PlanStep(
                step_id="diagnosis",
                description="Analyze the fault safely and provide inspection, recovery, and verification guidance.",
                agent="diagnosis_agent",
                expected_output="A safe, actionable diagnosis response with verification steps.",
            )
        ],
    )
    event = create_event(
        event_type="diagnosis_routed",
        message="Fault-handling request was routed to the diagnosis agent.",
        step_id="diagnosis",
    )
    return {
        "execution_plan": plan.model_dump(mode="json"),
        "planning_status": "planning",
        "planning_error": "",
        "planning_events": [event.model_dump(mode="json")],
    }
