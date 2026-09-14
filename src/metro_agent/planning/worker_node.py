from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextlib import nullcontext
from typing import Any, Mapping

from metro_agent.observability.finalize_node import get_trace_registry
from metro_agent.observability.agent_usage import LLM_USAGE_RECORDS_KEY
from metro_agent.planning.agent_adapter import run_plan_step_agent
from metro_agent.planning.factory import create_event
from metro_agent.planning.models import PlanStep, StepError, StepResult, utc_now_iso
from metro_agent.state import MetroAgentState


def extract_step_output(agent_result: dict, step: PlanStep) -> str:
    agents_output = agent_result.get("agents_output", {})
    agent_key = step.agent.replace("_agent", "")

    if agent_key in agents_output:
        return str(agents_output[agent_key])

    if agents_output:
        return "\n".join(str(value) for value in agents_output.values())

    return str(agent_result)


def run_step_with_timeout(state: MetroAgentState, step: PlanStep) -> dict:
    """运行单个 Agent，并在达到步骤超时时间后放弃其迟到结果。"""
    timeout_seconds = max(1, step.timeout_seconds)
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(run_plan_step_agent, state=state, step=step)

    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            f"步骤 {step.step_id} 超过 {timeout_seconds} 秒未完成"
        ) from exc
    finally:

        executor.shutdown(wait=False, cancel_futures=True)


def get_realtime_tool_failure(agent_result: dict, step: PlanStep) -> str:
    """从 Realtime Agent 写回的黑板工具结果中识别接口失败。"""
    if step.agent != "realtime_agent":
        return ""

    tool_results = agent_result.get("tool_results", {})
    if not isinstance(tool_results, dict):
        return ""

    for record in tool_results.values():
        if not isinstance(record, dict):
            continue

        result = record.get("result")
        if (
            record.get("tool_name") == "query_alarm_tool"
            and isinstance(result, dict)
            and result.get("success") is False
        ):
            return str(result.get("error", "实时告警工具查询失败"))

    return ""


def _get_trace_recorder(state: MetroAgentState) -> Any | None:
    trace_id = state.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id:
        return None
    try:
        return get_trace_registry().get(trace_id)
    except Exception:
        return None


def _tool_summary(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {"keys": sorted(str(key) for key in value)[:20]}
    if isinstance(value, list):
        return {"type": "list", "item_count": len(value)}
    return {"preview": str(value)[:500]}


def _evaluation_arguments(value: object) -> dict[str, Any]:
    """Keep only compact, non-secret tool arguments needed for offline matching."""
    sensitive = {"password", "secret", "token", "authorization", "credential", "api_key"}
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if name.lower() in sensitive:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[name] = str(item)[:200] if isinstance(item, str) else item
    return result


def _record_tool_calls(span: Any, tool_results: object, attempt: int) -> None:
    if span is None or not isinstance(tool_results, Mapping):
        return
    for record in tool_results.values():
        if not isinstance(record, Mapping):
            continue
        result = record.get("result")
        status = record.get("status")
        if not isinstance(status, str) or not status:
            status = "failed" if isinstance(result, Mapping) and result.get("success") is False else "success"
        duration_ms = record.get("duration_ms")
        if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool):
            duration_ms = None
        try:
            span.record_tool_call(
                tool_name=str(record.get("tool_name") or "unknown_tool"),
                status=status,
                attempt=attempt,
                duration_ms=duration_ms,
                arguments_summary={
                    **_tool_summary(record.get("arguments", {})),
                    "evaluation_arguments": _evaluation_arguments(record.get("arguments", {})),
                },
                result_summary=_tool_summary(result),
            )
        except Exception:
            continue


def _record_agent_llm_usages(span: Any, agent_result: Mapping[str, Any]) -> None:
    records = agent_result.get(LLM_USAGE_RECORDS_KEY)
    if span is None or not isinstance(records, list):
        return
    for record in records:
        if not isinstance(record, Mapping):
            continue
        try:
            span.record_llm_usage(**dict(record))
        except Exception:
            continue


def planning_worker_node(state: MetroAgentState) -> dict:
    step = PlanStep(**state["current_plan_step"])
    started_at = utc_now_iso()
    events = []
    max_attempts = max(1, step.max_attempts)

    for attempt in range(1, max_attempts + 1):
        events.append(
            create_event(
                event_type="step_started",
                message=f"开始执行步骤：{step.step_id}",
                step_id=step.step_id,
                metadata={"agent": step.agent, "attempt": attempt},
            ).model_dump(mode="json")
        )

        recorder = _get_trace_recorder(state)
        span_context = (
            recorder.span(
                "agent",
                agent=step.agent,
                step_id=step.step_id,
                attempt=attempt,
                attributes={"timeout_seconds": step.timeout_seconds},
            )
            if recorder is not None
            else nullcontext(None)
        )
        try:
            with span_context as span:
                try:
                    agent_result = run_step_with_timeout(state, step)
                except Exception as exc:
                    finished_at = utc_now_iso()
                    if span is not None:
                        span.set_result(
                            "failed",
                            {
                                "step_status": "failed",
                                "error_type": type(exc).__name__,
                                "step_started_at": started_at,
                                "step_finished_at": finished_at,
                            },
                        )
                    raise
                output = extract_step_output(agent_result, step)
                _record_agent_llm_usages(span, agent_result)
                tool_failure = get_realtime_tool_failure(agent_result, step)
                status = "degraded" if tool_failure else "success"
                error = (
                    StepError(
                        error_type="ToolQueryFailed",
                        message=tool_failure,
                        recoverable=False,
                    )
                    if tool_failure
                    else None
                )
                _record_tool_calls(span, agent_result.get("tool_results"), attempt)
                finished_at = utc_now_iso()
                if span is not None:
                    span.set_result(
                        status,
                        {
                            "step_status": status,
                            "error_type": error.error_type if error else None,
                            "step_started_at": started_at,
                            "step_finished_at": finished_at,
                        },
                    )
        except Exception as exc:
            if attempt < max_attempts:
                events.append(
                    create_event(
                        event_type="step_retry",
                        message=f"步骤执行失败，准备重试：{step.step_id}",
                        step_id=step.step_id,
                        metadata={
                            "agent": step.agent,
                            "attempt": attempt,
                            "error": str(exc),
                        },
                    ).model_dump(mode="json")
                )
                continue

            result = StepResult(
                step_id=step.step_id,
                agent=step.agent,
                status="failed",
                error=StepError(
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                ),
                attempt=attempt,
                started_at=started_at,
                finished_at=finished_at,
            )
            events.append(
                create_event(
                    event_type="step_failed",
                    message=f"步骤执行失败：{step.step_id}",
                    step_id=step.step_id,
                    metadata={
                        "agent": step.agent,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                ).model_dump(mode="json")
            )
            return {
                "plan_results": {step.step_id: result.model_dump(mode="json")},
                "planning_events": events,
            }

        result = StepResult(
            step_id=step.step_id,
            agent=step.agent,
            status=status,
            output=output,
            data=agent_result,
            error=error,
            attempt=attempt,
            started_at=started_at,
            finished_at=finished_at,
        )
        event_type = "step_degraded" if tool_failure else "step_success"
        event_message = (
            f"步骤降级完成：{step.step_id}"
            if tool_failure
            else f"步骤执行成功：{step.step_id}"
        )
        events.append(
            create_event(
                event_type=event_type,
                message=event_message,
                step_id=step.step_id,
                metadata={
                    "agent": step.agent,
                    "attempt": attempt,
                    "error": tool_failure,
                },
            ).model_dump(mode="json")
        )
        state_update = {
            "plan_results": {step.step_id: result.model_dump(mode="json")},
            "planning_events": events,
        }
        agent_tool_results = agent_result.get("tool_results")
        if isinstance(agent_tool_results, dict) and agent_tool_results:
            state_update["tool_results"] = agent_tool_results
        evaluation_contexts = agent_result.get("evaluation_retrieval_contexts")
        if isinstance(evaluation_contexts, list):
            state_update["evaluation_retrieval_contexts"] = [
                context
                for context in evaluation_contexts
                if isinstance(context, str) and context.strip()
            ]

        return state_update

    raise RuntimeError(f"步骤 {step.step_id} 未产生执行结果")
