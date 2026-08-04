"""Planning 结果聚合：仅汇总可信步骤，并正确结束失败或降级计划。"""

import re

from metro_agent.observability.finalize_node import get_trace_registry
from metro_agent.planning.factory import create_event
from metro_agent.state import MetroAgentState


def strip_nested_summary_titles(text: str) -> str:
    """移除子 Agent 不应进入 Supervisor 汇总层的标题。"""
    return re.sub(r"^\s*【最终汇总】\s*", "", (text or "").strip()).strip()


def strip_agent_generated_citations(text: str) -> str:
    """Keep citations owned by the aggregation layer rather than the answer model."""
    return re.sub(
        r"(?:^|\n)【引用文档】\s*\n.*?(?=\n【[^】]+】|\Z)",
        "",
        text or "",
        flags=re.DOTALL,
    ).strip()


def remove_sections(text: str, headings: set[str]) -> str:
    """删除指定的同级中文标题段落。"""
    if not text:
        return ""

    pattern = re.compile(r"(【([^】]+)】[\s\S]*?)(?=\n【[^】]+】|\Z)")
    pieces = []
    last_end = 0
    for match in pattern.finditer(text):
        pieces.append(text[last_end:match.start()])
        if match.group(2).strip() not in headings:
            pieces.append(match.group(1))
        last_end = match.end()
    pieces.append(text[last_end:])
    return "".join(pieces).strip()


def format_step_issues(title: str, results: list[dict], conclusion: str) -> str:
    lines = [f"【{title}】"]
    for result in results:
        error = result.get("error") or {}
        message = error.get("message", "未提供错误信息")
        lines.append(
            f"{result.get('step_id', 'unknown_step')}"
            f"（{result.get('agent', 'unknown_agent')}）：{message}"
        )
    lines.append(conclusion)
    return "\n".join(lines)


def merge_knowledge_sources(source_groups: list[str]) -> str:
    """按首次出现顺序合并多个知识检索步骤的引用文档。"""
    merged = []
    for source_group in source_groups:
        for source in str(source_group or "").split("、"):
            source = source.strip()
            if source and source not in merged:
                merged.append(source)
    return "、".join(merged)


def _aggregate_span_attributes(state: MetroAgentState, planning_status: str) -> dict:
    results = state.get("plan_results", {})
    if not isinstance(results, dict):
        results = {}
    failed_count = sum(result.get("status") == "failed" for result in results.values())
    degraded_count = sum(result.get("status") == "degraded" for result in results.values())
    return {
        "planning_status": planning_status,
        "step_count": len(results),
        "failed_count": failed_count,
        "degraded_count": degraded_count,
    }


def _run_aggregate_with_span(state: MetroAgentState, work):
    trace_id = state.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id:
        return work()
    try:
        recorder = get_trace_registry().get(trace_id)
    except Exception:
        return work()
    if recorder is None:
        return work()
    with recorder.span("aggregate") as span:
        update = work()
        planning_status = str(update.get("planning_status", "unknown"))
        span.set_result(planning_status, _aggregate_span_attributes(state, planning_status))
        span_id = getattr(span, "span_id", None)
        if isinstance(span_id, str) and span_id:
            update["aggregate_span_id"] = span_id
        return update


def build_final_answer(
    plan: dict,
    results: dict,
    degraded_results: list[dict] | None = None,
    failed_results: list[dict] | None = None,
) -> str:
    del plan
    degraded_results = degraded_results or []
    failed_results = failed_results or []
    has_execution_issues = bool(degraded_results or failed_results)

    realtime_output = ""
    knowledge_output = ""
    knowledge_source_groups = []
    diagnosis_output = ""
    general_output = ""

    for result in results.values():
        if result.get("status") == "failed":
            continue

        agent = result.get("agent")
        output = result.get("output", "")

        if agent == "realtime_agent":
            realtime_output = strip_nested_summary_titles(output)
        elif agent == "knowledge_agent":
            knowledge_output = strip_agent_generated_citations(
                strip_nested_summary_titles(output)
            )
            agents_output = result.get("data", {}).get("agents_output", {})
            if isinstance(agents_output, dict):
                knowledge_source_groups.append(agents_output.get("sources", ""))
        elif agent == "diagnosis_agent":
            diagnosis_output = strip_nested_summary_titles(output)
        elif agent == "general_agent":
            general_output = strip_nested_summary_titles(output)

    if general_output and not realtime_output and not knowledge_output and not diagnosis_output:
        if not has_execution_issues:
            return general_output
        parts = [general_output]
    elif knowledge_output and not realtime_output and not diagnosis_output:
        if not has_execution_issues:
            answer_parts = [knowledge_output]
            knowledge_sources = merge_knowledge_sources(knowledge_source_groups)
            if knowledge_sources:
                answer_parts.append(f"【引用文档】\n{knowledge_sources}")
            return "\n\n".join(answer_parts)
        parts = ["【最终汇总】", f"【知识依据】\n{knowledge_output}"]
        knowledge_sources = merge_knowledge_sources(knowledge_source_groups)
        if knowledge_sources:
            parts.append(f"【引用文档】\n{knowledge_sources}")
    else:
        parts = ["【最终汇总】"]

    if realtime_output:
        parts.append(f"【实时状态】\n{realtime_output}")

    if knowledge_output and (realtime_output or diagnosis_output):
        if diagnosis_output:
            knowledge_output = remove_sections(
                knowledge_output,
                {"处理建议", "诊断建议", "下一步"},
            )
        parts.append(f"【知识依据】\n{knowledge_output}")

    knowledge_sources = merge_knowledge_sources(knowledge_source_groups)
    if knowledge_sources and (realtime_output or diagnosis_output):
        parts.append(f"【引用文档】\n{knowledge_sources}")

    if diagnosis_output:
        parts.append(f"【诊断建议】\n{diagnosis_output}")

    if degraded_results:
        parts.append(
            format_step_issues(
                "降级说明",
                degraded_results,
                "以上实时查询未能获得可信接口数据；后续建议仅供人工核实，不能视为已确认的实时状态。",
            )
        )

    if failed_results:
        parts.append(
            format_step_issues(
                "执行异常",
                failed_results,
                "系统已保留其他步骤的可信结果；请根据异常信息检查依赖服务或稍后重试。",
            )
        )

    if realtime_output or diagnosis_output:
        parts.append(
            "【下一步】\n请根据现场实际补充故障时间、影响范围、设备编号和已采取措施，必要时升级人工确认。"
        )

    return "\n\n".join(parts)


def planning_aggregate_node(state: MetroAgentState) -> dict:
    def aggregate() -> dict:
        plan = state.get("execution_plan", {})
        steps = plan.get("steps", [])
        results = state.get("plan_results", {})

        plan_step_ids = {
            step["step_id"] for step in steps if step.get("step_id")
        }
        missing_step_ids = plan_step_ids - set(results)
        failed_results = [
            result for result in results.values() if result.get("status") == "failed"
        ]
        degraded_results = [
            result for result in results.values() if result.get("status") == "degraded"
        ]

        if failed_results:
            event = create_event(
                event_type="plan_failed",
                message="计划执行存在失败步骤",
                metadata={
                    "failed_steps": [item.get("step_id") for item in failed_results],
                    "degraded_steps": [item.get("step_id") for item in degraded_results],
                },
            )
            return {
                "planning_status": "failed",
                "final_answer": build_final_answer(
                    plan, results, degraded_results, failed_results
                ),
                "planning_events": [event.model_dump(mode="json")],
            }

        if missing_step_ids:
            event = create_event(
                event_type="plan_waiting",
                message="计划仍有步骤未完成，继续调度",
                metadata={
                    "total_steps": len(plan_step_ids),
                    "finished_steps": len(results),
                    "missing_step_ids": sorted(missing_step_ids),
                },
            )
            return {
                "planning_status": "running",
                "planning_events": [event.model_dump(mode="json")],
            }

        event = create_event(
            event_type="plan_completed",
            message="计划执行完成",
            metadata={
                "total_steps": len(plan_step_ids),
                "degraded_steps": [item.get("step_id") for item in degraded_results],
            },
        )
        return {
            "planning_status": "completed",
            "final_answer": build_final_answer(
                plan, results, degraded_results, failed_results
            ),
            "planning_events": [event.model_dump(mode="json")],
        }

    return _run_aggregate_with_span(state, aggregate)
