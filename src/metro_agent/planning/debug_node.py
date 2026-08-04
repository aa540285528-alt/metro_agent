from metro_agent.state import MetroAgentState


def planning_debug_end_node(state: MetroAgentState) -> dict:
    plan = state.get("execution_plan", {})
    steps = plan.get("steps", [])

    lines = [
        "【Planning 调试输出】",
        f"目标：{plan.get('goal', '')}",
        f"状态：{plan.get('status', '')}",
        "",
        "步骤：",
    ]

    for step in steps:
        deps = step.get("dependencies", [])
        dep_text = ", ".join(deps) if deps else "无"

        lines.append(
            f"- {step.get('step_id')} | {step.get('agent')} | "
            f"依赖：{dep_text} | {step.get('description')}"
        )

    return {
        "final_answer": "\n".join(lines),
    }