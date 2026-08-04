from uuid import uuid4

from metro_agent.planning.models import ExecutionPlan, PlanEvent, PlanStep, utc_now_iso


def create_plan(goal: str, steps: list[PlanStep]) -> ExecutionPlan:
    now = utc_now_iso()

    return ExecutionPlan(
        plan_id=str(uuid4()),
        goal=goal,
        steps=steps,
        created_at=now,
        updated_at=now,
    )


def create_event(
    event_type: str,
    message: str,
    step_id: str | None = None,
    metadata: dict | None = None,
) -> PlanEvent:
    return PlanEvent(
        event_type=event_type,
        message=message,
        step_id=step_id,
        metadata=metadata or {},
        created_at=utc_now_iso(),
    )