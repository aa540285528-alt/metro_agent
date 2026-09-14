import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metro_agent.planning.models import ExecutionPlan
from metro_agent.config import PLANNING_ALLOWED_AGENTS, PLANNING_MAX_STEPS
"""确定性代码保护 DAG 计划不乱"""






def validate_plan(plan: ExecutionPlan) -> list[str]:
    errors: list[str] = []

    if len(plan.steps) == 0:
        errors.append("计划至少需要包含一个步骤")

    if len(plan.steps) > PLANNING_MAX_STEPS:
        errors.append(f"计划步骤数不能超过 {PLANNING_MAX_STEPS}")

    step_ids = [step.step_id for step in plan.steps]
    step_id_set = set(step_ids)

    if len(step_ids) != len(step_id_set):
        errors.append("计划中存在重复的 step_id")

    for step in plan.steps:
        if step.agent not in PLANNING_ALLOWED_AGENTS:
            errors.append(f"步骤 {step.step_id} 使用了不允许的 agent: {step.agent}")

        for dep in step.dependencies:
            if dep not in step_id_set:
                errors.append(f"步骤 {step.step_id} 依赖了不存在的步骤: {dep}")
    dependency_map = {step.step_id: step.dependencies for step in plan.steps}
    if has_cycle(dependency_map):
        errors.append("计划存在循环依赖")
    return errors

def has_cycle(dependency_map: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(step_id: str) -> bool:
        if step_id in visiting:
            return True

        if step_id in visited:
            return False

        visiting.add(step_id)

        for dep in dependency_map.get(step_id, []):
            if dfs(dep):
                return True

        visiting.remove(step_id)
        visited.add(step_id)
        return False

    for step_id in dependency_map:
        if dfs(step_id):
            return True

    return False