import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Literal
from pydantic import BaseModel, Field
from metro_agent.config import (
    PLANNING_DEFAULT_MAX_ATTEMPTS,
    PLANNING_DEFAULT_TIMEOUT_SECONDS,
)
from datetime import datetime, timezone

StepStatus = Literal[
    "pending",
    "ready",
    "running",
    "success",
    "failed",
    "waiting_human",
    "degraded",
    "skipped",
    "cancelled",
]


class PlanStep(BaseModel):
    step_id: str
    description: str
    agent: str
    dependencies: list[str] = Field(default_factory=list)
    status: StepStatus = "pending"
    expected_output: str
    input_refs: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    max_attempts: int = PLANNING_DEFAULT_MAX_ATTEMPTS
    timeout_seconds: int = PLANNING_DEFAULT_TIMEOUT_SECONDS


class PlannerStepOutput(BaseModel):
    """Planner LLM 输出中的单个步骤，不包含运行时状态。"""

    step_id: str
    description: str
    agent: str
    dependencies: list[str] = Field(default_factory=list)
    expected_output: str


class PlannerOutput(BaseModel):
    """Planner LLM 输出的最小结构化契约。"""

    goal: str
    steps: list[PlannerStepOutput]


PlanStatus = Literal[
    "draft",
    "validated",
    "running",
    "waiting_human",
    "completed",
    "failed",
    "cancelled",
]


class ExecutionPlan(BaseModel):
    plan_id: str
    goal: str
    version: int = 1
    status: PlanStatus = "draft"
    steps: list[PlanStep]
    created_at: str
    updated_at: str


class StepError(BaseModel):
    error_type: str
    message: str
    recoverable: bool = True


class StepResult(BaseModel):
    step_id: str
    agent: str
    status: StepStatus
    output: str = ""
    data: dict = Field(default_factory=dict)
    error: StepError | None = None
    attempt: int = 1
    started_at: str | None = None
    finished_at: str | None = None


class PlanEvent(BaseModel):
    event_type: str
    message: str
    step_id: str | None = None
    metadata: dict = Field(default_factory=dict)
    created_at: str


ReviewStatus = Literal[
    "approved",
    "needs_revision",
    "rejected",
]


class ReviewDecision(BaseModel):
    status: ReviewStatus
    reason: str
    issues: list[str] = Field(default_factory=list)
    suggested_changes: list[str] = Field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
