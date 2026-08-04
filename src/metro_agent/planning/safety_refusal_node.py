from __future__ import annotations

from metro_agent.observability.node_instrumentation import traced_node
from metro_agent.planning.factory import create_event, create_plan
from metro_agent.planning.models import PlanStep
from metro_agent.safety_policy import safety_refusal_response
from metro_agent.state import MetroAgentState


@traced_node("safety_refusal_plan")
def safety_refusal_plan_node(state: MetroAgentState) -> dict:
    """Create a single general-agent step for a deterministic safety refusal."""
    user_input = state["user_input"]
    answer = safety_refusal_response(user_input)
    if answer is None:
        raise ValueError("safety refusal node received a non-safety request")
    plan = create_plan(
        goal="Safely refuse a high-risk request",
        steps=[
            PlanStep(
                step_id="safety_refusal",
                description="Return the approved safety refusal without operational guidance.",
                agent="general_agent",
                expected_output=answer,
            )
        ],
    )
    event = create_event(
        event_type="safety_refusal_routed",
        message="High-risk request was routed to the deterministic safety refusal path.",
        step_id="safety_refusal",
    )
    return {
        "execution_plan": plan.model_dump(mode="json"),
        "planning_status": "planning",
        "planning_error": "",
        "planning_events": [event.model_dump(mode="json")],
    }
