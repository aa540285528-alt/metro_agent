from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from metro_agent.observability.artifacts import write_json
from metro_agent.observability.service import TraceRegistry
from metro_agent.state import MetroAgentState


DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parent.parent / "artifacts"
_trace_registry = TraceRegistry()
TRACE_EVIDENCE_SCHEMA_VERSION = "trace-evidence-v1"


def get_trace_registry() -> TraceRegistry:
    return _trace_registry


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _step_duration_ms(result: Mapping[str, Any]) -> float:
    started_at = _parse_timestamp(result.get("started_at"))
    finished_at = _parse_timestamp(result.get("finished_at"))
    if started_at is None or finished_at is None:
        return 0.0
    return max(0.0, (finished_at - started_at).total_seconds() * 1000)


def _critical_path_latency_ms(state: Mapping[str, Any], results: Mapping[str, Any]) -> float:
    plan = state.get("execution_plan")
    steps = plan.get("steps", []) if isinstance(plan, Mapping) else []
    cumulative: dict[str, float] = {}
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        step_id = step.get("step_id")
        if not isinstance(step_id, str) or not step_id:
            continue
        result = results.get(step_id)
        duration_ms = _step_duration_ms(result) if isinstance(result, Mapping) else 0.0
        dependencies = step.get("dependencies", [])
        dependency_latency = max(
            (cumulative.get(dependency, 0.0) for dependency in dependencies),
            default=0.0,
        )
        cumulative[step_id] = dependency_latency + duration_ms
    if cumulative:
        return max(cumulative.values())
    return max(
        (_step_duration_ms(result) for result in results.values() if isinstance(result, Mapping)),
        default=0.0,
    )


def _terminal_status(state: Mapping[str, Any]) -> str:
    planning_status = state.get("planning_status")
    if isinstance(planning_status, str) and planning_status:
        return planning_status
    return "completed" if state.get("final_answer") else "unknown"


def derive_trace_summary(
    state: Mapping[str, Any], recorder_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    results = state.get("plan_results")
    results = results if isinstance(results, Mapping) else {}
    statuses = [
        result.get("status")
        for result in results.values()
        if isinstance(result, Mapping)
    ]
    return {
        "status": _terminal_status(state),
        "step_count": len(results),
        "success_count": statuses.count("success"),
        "failed_count": statuses.count("failed"),
        "degraded_count": statuses.count("degraded"),
        "e2e_latency_ms": float(recorder_snapshot["e2e_latency_ms"]),
        "critical_path_latency_ms": _critical_path_latency_ms(state, results),
        "total_tokens": int(recorder_snapshot["total_tokens"]),
        "estimated_cost": float(recorder_snapshot["estimated_cost"]),
    }


def trace_evidence_digest(evidence: Mapping[str, Any]) -> str:
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_trace_evidence(
    state: Mapping[str, Any], recorder_snapshot: Mapping[str, Any], trace_id: str,
    summary: Mapping[str, Any], final_answer: str,
) -> dict[str, Any]:
    plan = state.get("execution_plan")
    plan = plan if isinstance(plan, Mapping) else {}
    steps = [step for step in plan.get("steps", []) if isinstance(step, Mapping)]
    results = state.get("plan_results")
    results = results if isinstance(results, Mapping) else {}
    agent_path: list[str] = []
    nodes: list[str] = []
    edges: list[list[str]] = []
    agents: dict[str, str] = {}
    for step in steps:
        step_id = step.get("step_id")
        if not isinstance(step_id, str) or not step_id:
            continue
        nodes.append(step_id)
        planned_agent = step.get("agent")
        if isinstance(planned_agent, str) and planned_agent:
            agents[step_id] = planned_agent
        for dependency in step.get("dependencies", []):
            if isinstance(dependency, str) and dependency:
                edges.append([dependency, step_id])
        result = results.get(step_id)
        agent = result.get("agent") if isinstance(result, Mapping) else None
        if isinstance(agent, str) and agent:
            agent_path.append(agent)

    safe_events: list[dict[str, Any]] = []
    events = state.get("planning_events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, Mapping):
                continue
            metadata = event.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            safe_events.append(
                {
                    "event_type": str(event.get("event_type", "")),
                    "step_id": event.get("step_id"),
                    "agent": metadata.get("agent"),
                    "attempt": metadata.get("attempt"),
                    "error_type": metadata.get("error_type"),
                }
            )

    spans = recorder_snapshot.get("spans")
    spans = spans if isinstance(spans, list) else []
    usages = recorder_snapshot.get("llm_usages")
    usages = usages if isinstance(usages, list) else []
    raw_tools = recorder_snapshot.get("tool_calls")
    raw_tools = raw_tools if isinstance(raw_tools, list) else []
    errors = [str(error) for error in recorder_snapshot.get("errors", []) if isinstance(error, str)]
    for step_id, result in results.items():
        if not isinstance(result, Mapping):
            continue
        error = result.get("error")
        error_type = error.get("error_type") if isinstance(error, Mapping) else None
        if isinstance(error_type, str) and error_type:
            errors.append(f"{step_id}:{error_type}")
    tool_calls: list[dict[str, Any]] = []
    for call in raw_tools:
        if not isinstance(call, Mapping):
            continue
        arguments_summary = call.get("arguments_summary")
        arguments_summary = arguments_summary if isinstance(arguments_summary, Mapping) else {}
        arguments = arguments_summary.get("evaluation_arguments")
        tool_calls.append(
            {
                "tool_name": call.get("tool_name"),
                "status": call.get("status"),
                "attempt": call.get("attempt"),
                "duration_ms": call.get("duration_ms"),
                "arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
            }
        )
    retrieval_contexts: list[str] = []
    raw_contexts = state.get("evaluation_retrieval_contexts")
    if isinstance(raw_contexts, list):
        retrieval_contexts = list(
            dict.fromkeys(
                context.strip()
                for context in raw_contexts
                if isinstance(context, str) and context.strip()
            )
        )
    return {
        "schema_version": TRACE_EVIDENCE_SCHEMA_VERSION,
        "trace_id": trace_id,
        "final_answer_sha256": hashlib.sha256(final_answer.encode("utf-8")).hexdigest(),
        "summary_binding": {
            key: summary.get(key)
            for key in ("status", "e2e_latency_ms", "critical_path_latency_ms", "total_tokens", "estimated_cost")
        },
        "agent_path": agent_path,
        "plan": {"nodes": nodes, "edges": edges, "agents": agents},
        "retrieval_contexts": retrieval_contexts,
        "planning_events": safe_events,
        "tool_calls": tool_calls,
        "spans": [
            {
                key: span.get(key)
                for key in ("span_id", "event_type", "status", "agent", "step_id", "attempt", "duration_ms")
            }
            for span in spans
            if isinstance(span, Mapping)
        ],
        "llm_usages": [
            {
                key: usage.get(key)
                for key in ("span_id", "provider", "model", "prompt_version", "prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens", "total_tokens", "estimated_cost", "latency_ms", "usage_category")
            }
            for usage in usages
            if isinstance(usage, Mapping)
        ],
        "errors": errors,
    }


def trace_finalize_node(
    state: MetroAgentState,
    *,
    registry: TraceRegistry | None = None,
    artifact_root: Path | str | None = None,
) -> dict[str, Any]:
    artifact_uri = ""
    try:
        trace_id = state.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("missing trace id")

        active_registry = registry if registry is not None else get_trace_registry()
        recorder = active_registry.get(trace_id)
        if recorder is None:
            raise RuntimeError("trace recorder is unavailable")





        finish_persisted = active_registry.finish(trace_id, _terminal_status(state))
        recorder_snapshot = recorder.final_summary
        if not recorder_snapshot:
            raise RuntimeError("trace recorder did not produce a final snapshot")

        summary = derive_trace_summary(state, recorder_snapshot)
        evidence_snapshot = recorder.evidence_snapshot() if hasattr(recorder, "evidence_snapshot") else {"errors": []}
        final_answer = str(state.get("final_answer", ""))
        evidence = build_trace_evidence(state, evidence_snapshot, trace_id, summary, final_answer)
        expected_uri = (Path("traces") / trace_id / "summary.json").as_posix()
        summary["artifact_uri"] = expected_uri
        summary["final_answer_artifact_uri"] = expected_uri
        aggregate_span_id = state.get("aggregate_span_id")
        if isinstance(aggregate_span_id, str) and aggregate_span_id:
            summary["aggregate_span_id"] = aggregate_span_id
        if not finish_persisted:
            summary["trace_degraded"] = True
            summary["trace_error"] = "trace_persistence_failed"
        artifact = write_json(
            artifact_root or DEFAULT_ARTIFACT_ROOT,
            expected_uri,
            {
                "trace_id": trace_id,
                "summary": summary,
                "final_answer": final_answer,
                "evidence": evidence,
                "evidence_sha256": trace_evidence_digest(evidence),
            },
        )
        artifact_uri = artifact.uri
        summary["artifact_uri"] = artifact_uri
        summary["final_answer_artifact_uri"] = artifact_uri
        if not finish_persisted:
            return {"trace_summary": summary, "trace_artifact_uri": artifact_uri}

        span_artifact_persisted = True
        if isinstance(aggregate_span_id, str) and aggregate_span_id:
            span_artifact_persisted = recorder.update_span_artifact(
                aggregate_span_id,
                artifact_uri,
                {"final_answer_artifact_uri": artifact_uri},
            )
        if not span_artifact_persisted:
            summary["trace_degraded"] = True
            summary["trace_error"] = "artifact_persistence_failed"
        if not recorder.update_final_summary(summary):
            summary["trace_degraded"] = True
            summary["trace_error"] = "artifact_persistence_failed"
        if summary.get("trace_degraded"):
            try:
                write_json(
                    artifact_root or DEFAULT_ARTIFACT_ROOT,
                    artifact_uri,
                    {
                        "trace_id": trace_id,
                        "summary": summary,
                        "final_answer": final_answer,
                        "evidence": evidence,
                        "evidence_sha256": trace_evidence_digest(evidence),
                    },
                )
            except Exception:
                pass
        return {"trace_summary": summary, "trace_artifact_uri": artifact_uri}
    except Exception as error:
        return {
            "trace_summary": {"trace_error": type(error).__name__},
            "trace_artifact_uri": "",
        }
