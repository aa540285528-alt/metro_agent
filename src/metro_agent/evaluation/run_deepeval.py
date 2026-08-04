"""Offline DeepEval agent evaluation from reviewed golden cases and local traces.

The production finalizer writes a compact, signed local evidence snapshot.
The trace-results JSONL contains only ``case_id`` and ``trace_id``; the runner
derives the artifact path from that ID and verifies its binding and hash before
scoring. Missing or tampered evidence is reported explicitly and never becomes
a numeric zero.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from metro_agent.tools.rag_evaluation import load_jsonl
from evaluation.qwen_judge import JudgeConfigurationError, QwenJudge, normalize_json_response
from evaluation.agent_acceptance import evaluate_acceptance_gate
from metro_agent.observability.artifacts import write_json, write_jsonl, write_markdown
from metro_agent.observability.llm_usage import extract_llm_usage


DEEPEVAL_RUN_SCHEMA_VERSION = "deepeval-agent-run-v1"
METRIC_DEFINITIONS = {
    "task_completion": "Trace completed and produced a non-empty final answer.",
    "agent_path": "Precision, recall and F1 of selected agents against the golden path.",
    "plan_adherence": "Node and directed dependency-edge precision, recall and F1.",
    "tool_correctness": "Expected tool-name, argument-constraint and call-count conformance.",
    "token_budget": "Total provider tokens compared with the reviewed token budget.",
    "end_to_end_latency": "Trace monotonic end-to-end latency compared with the latency budget.",
    "full_chain_latency": "Critical-path latency reported by the planning DAG.",
    "trace_errors": "Terminal trace and recorded tool/agent errors observed for the case.",
    "answer_relevance": "DeepEval answer relevancy using the configured Qwen judge.",
    "faithfulness": "DeepEval faithfulness against locally supplied retrieval context.",
    "answer_accuracy": "DeepEval answer correctness against the reviewed reference answer.",
}

_TOOL_BACKED_SEMANTIC_CATEGORIES = {"realtime_alarm", "tool_failure"}


class DeepEvalDependencyError(RuntimeError):
    """Raised when the optional DeepEval runtime cannot be loaded."""


class TraceArtifactError(ValueError):
    """Raised when a trace artifact is missing, malformed or outside artifact_root."""


class DeepEvalBackend(Protocol):
    provider: str
    version: str
    model: str

    def evaluate(self, sample: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DeepEvalRunResult:
    run_id: str
    artifact_uri: str
    summary: dict[str, Any]


def score_plan_graph(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, float]:
    """Score a dependency graph as sets, so parallel execution order is irrelevant."""
    node = _precision_recall_f1(_string_set(actual.get("nodes")), _string_set(expected.get("nodes")))
    edge = _precision_recall_f1(_edge_set(actual.get("edges")), _edge_set(expected.get("edges")))
    return {
        "node_precision": node["precision"],
        "node_recall": node["recall"],
        "node_f1": node["f1"],
        "edge_precision": edge["precision"],
        "edge_recall": edge["recall"],
        "edge_f1": edge["f1"],
    }


def score_tool_calls(
    *, actual: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]
) -> dict[str, float | int | bool]:
    """Score tool selection, JSON argument constraints and per-tool call bounds."""
    actual_names = [str(call.get("tool_name", call.get("name", ""))) for call in actual]
    expected_names = [str(call.get("tool_name", call.get("name", ""))) for call in expected]
    names = _multiset_precision_recall_f1(Counter(actual_names), Counter(expected_names))
    by_name = Counter(actual_names)

    argument_matches = 0
    unmatched_actual = set(range(len(actual)))
    for expected_call in expected:
        name = str(expected_call.get("tool_name", expected_call.get("name", "")))
        constraints = expected_call.get("arguments", expected_call.get("argument_constraints", {}))
        if not isinstance(constraints, Mapping):
            constraints = {}
        match_index = next(
            (
                index
                for index in unmatched_actual
                if str(actual[index].get("tool_name", actual[index].get("name", ""))) == name
                and _mapping_contains(actual[index].get("arguments", {}), constraints)
            ),
            None,
        )
        if match_index is not None:
            unmatched_actual.remove(match_index)
            argument_matches += 1

    expected_count = len(expected)
    bounds_passed = 0
    for name, expected_total in Counter(expected_names).items():
        declarations = [call for call in expected if str(call.get("tool_name", call.get("name", ""))) == name]
        minimum = max((call.get("min_calls", expected_total) for call in declarations), default=expected_total)
        maximum = min((call.get("max_calls", expected_total) for call in declarations), default=expected_total)
        if isinstance(minimum, int) and isinstance(maximum, int) and minimum <= by_name[name] <= maximum:
            bounds_passed += len(declarations)
    duplicate_call_count = sum(max(0, by_name[name] - expected_names.count(name)) for name in by_name)
    return {
        "tool_name_precision": names["precision"],
        "tool_name_recall": names["recall"],
        "tool_name_f1": names["f1"],
        "argument_pass_rate": _rate(argument_matches, expected_count),
        "call_count_pass_rate": _rate(bounds_passed, expected_count),
        "actual_call_count": len(actual),
        "expected_call_count": expected_count,
        "duplicate_call_count": duplicate_call_count,
        "passed": names["f1"] == 1.0 and argument_matches == expected_count and bounds_passed == expected_count,
    }


def score_budget(
    *,
    total_tokens: int | None,
    token_budget: int | None,
    critical_path_latency_ms: float | None = None,
    latency_budget_ms: float | None = None,
    e2e_latency_ms: float | None = None,
) -> dict[str, bool | int | float | None]:
    """Score token, end-to-end and critical-path budgets without inventing input."""
    token_passed = token_budget is None or total_tokens is None or total_tokens <= token_budget
    latency_passed = latency_budget_ms is None or e2e_latency_ms is None or e2e_latency_ms <= latency_budget_ms
    critical_passed = latency_budget_ms is None or critical_path_latency_ms is None or critical_path_latency_ms <= latency_budget_ms
    return {
        "passed": token_passed and latency_passed and critical_passed,
        "total_tokens": total_tokens,
        "token_budget": token_budget,
        "e2e_latency_ms": e2e_latency_ms,
        "critical_path_latency_ms": critical_path_latency_ms,
        "latency_budget_ms": latency_budget_ms,
    }


def run_deepeval_suite(
    *,
    golden_rows: Sequence[Mapping[str, Any]],
    trace_rows: Sequence[Mapping[str, Any]],
    artifact_root: Path | str = "artifacts",
    suite: str,
    run_id: str | None = None,
    evaluator: DeepEvalBackend | None = None,
) -> DeepEvalRunResult:
    """Evaluate local trace evidence and write inspectable per-case artifacts."""
    root = Path(artifact_root)
    safe_suite = _safe_path_segment(suite, "suite")
    resolved_run_id = _safe_path_segment(run_id or _new_run_id(), "run_id")
    golden_by_id = {str(row.get("id")): row for row in golden_rows if isinstance(row.get("id"), str)}
    trace_by_id = {str(row.get("case_id", row.get("id", ""))): row for row in trace_rows}
    selected_backend = evaluator or _default_backend()
    rows: list[dict[str, Any]] = []

    for case_id in sorted(golden_by_id):
        golden = golden_by_id[case_id]
        trace_row = trace_by_id.get(case_id)
        if trace_row is None:
            rows.append(_evidence_error(case_id, golden, "TraceArtifactError", "case has no local trace result"))
            continue
        try:
            evidence = _load_trace_evidence(root, trace_row)
        except Exception as error:
            rows.append(_evidence_error(case_id, golden, type(error).__name__, str(error), trace_row))
            continue
        rows.append(_evaluate_case(case_id, golden, evidence, selected_backend))

    unknown_trace_cases = sorted(case_id for case_id in trace_by_id if case_id not in golden_by_id)
    artifact_uri = f"evals/agent/{safe_suite}/{resolved_run_id}"
    manifest = {
        "schema_version": DEEPEVAL_RUN_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suite": safe_suite,
        "run_id": resolved_run_id,
        "golden_case_ids": sorted(golden_by_id),
        "golden_sha256": _hash_json_rows(golden_rows),
        "trace_result_sha256": _hash_json_rows(trace_rows),
        "deepeval": {
            "provider": selected_backend.provider,
            "version": selected_backend.version,
            "model": selected_backend.model,
        },
        "metric_definitions": METRIC_DEFINITIONS,
    }
    summary = _summarize(rows, manifest, unknown_trace_cases)
    summary["acceptance_gate"] = evaluate_acceptance_gate(rows, golden_rows)
    write_json(root, f"{artifact_uri}/input_manifest.json", manifest)
    write_jsonl(root, f"{artifact_uri}/case_results.jsonl", rows)
    write_json(root, f"{artifact_uri}/summary.json", summary)
    write_markdown(root, f"{artifact_uri}/report.md", _report(safe_suite, summary))
    return DeepEvalRunResult(run_id=resolved_run_id, artifact_uri=artifact_uri, summary=summary)


def _evaluate_case(
    case_id: str,
    golden: Mapping[str, Any],
    evidence: Mapping[str, Any],
    evaluator: DeepEvalBackend,
) -> dict[str, Any]:
    summary = evidence["summary"]
    answer = evidence["answer"]
    deterministic, missing = _deterministic_metrics(golden, evidence)
    row: dict[str, Any] = {
        "case_id": case_id,
        "trace_id": evidence.get("trace_id"),
        "trace_artifact_uri": evidence["trace_artifact_uri"],
        "status": "completed",
        "query": golden.get("query", ""),
        "answer": answer,
        "trace_metrics": {
            "total_tokens": _as_int(summary.get("total_tokens")),
            "e2e_latency_ms": _as_float(summary.get("e2e_latency_ms")),
            "critical_path_latency_ms": _as_float(summary.get("critical_path_latency_ms")),
            "status": summary.get("status"),
            "errors": evidence["errors"],
        },
        "deterministic_metrics": deterministic,
        "metric_definitions": METRIC_DEFINITIONS,
    }
    semantic_input = _semantic_input(golden, evidence)
    if semantic_input is None:
        if missing:
            row["evidence_missing"] = missing
        return row
    try:
        semantic = evaluator.evaluate(semantic_input)
    except Exception as error:
        row["status"] = _error_status(error)
        row["error"] = {"type": type(error).__name__, "message": str(error)}
        return row
    metrics = semantic.get("metrics") if isinstance(semantic, Mapping) else None
    if not isinstance(metrics, Mapping) or not metrics:
        row["status"] = "evaluation_error"
        row["error"] = {"type": "DeepEvalResultError", "message": "backend returned no metric scores"}
        return row
    row["deepeval_metrics"] = {name: value for name, value in metrics.items() if _numeric(value) is not None}
    if not row["deepeval_metrics"]:
        row["status"] = "evaluation_error"
        row["error"] = {"type": "DeepEvalResultError", "message": "backend returned no numeric metric scores"}
        return row
    judge = semantic.get("judge") if isinstance(semantic, Mapping) else None
    row["judge"] = dict(judge) if isinstance(judge, Mapping) else {
        "model": evaluator.model,
        "token_usage": {"total_tokens": None, "source": "not_exposed"},
    }
    row["semantic_audit"] = _semantic_audit(
        row["deepeval_metrics"],
        semantic.get("metric_audit") if isinstance(semantic, Mapping) else None,
        row["judge"],
        semantic_input,
    )
    if missing:
        row["status"] = "evidence_missing"
        row["evidence_missing"] = missing
    return row


def _deterministic_metrics(golden: Mapping[str, Any], evidence: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    summary = evidence["summary"]
    answer = evidence["answer"]
    errors = evidence["errors"]
    metrics: dict[str, Any] = {
        "task_completion": {
            "passed": summary.get("status") == "completed" and bool(answer.strip()),
            "trace_status": summary.get("status"),
        },
        "trace_errors": {"error_count": len(errors), "passed": not errors, "errors": errors},
        "budget": score_budget(
            total_tokens=_as_int(summary.get("total_tokens")),
            token_budget=_as_int(golden.get("token_budget")),
            e2e_latency_ms=_as_float(summary.get("e2e_latency_ms")),
            critical_path_latency_ms=_as_float(summary.get("critical_path_latency_ms")),
            latency_budget_ms=_as_float(golden.get("latency_budget_ms")),
        ),
    }
    missing: list[str] = []
    expected_agents = golden.get("expected_agents")
    if isinstance(expected_agents, Sequence) and not isinstance(expected_agents, (str, bytes)):
        actual_agents = evidence.get("agent_path")
        if isinstance(actual_agents, Sequence) and not isinstance(actual_agents, (str, bytes)):
            metrics["agent_path"] = _precision_recall_f1(_string_set(actual_agents), _string_set(expected_agents))
        else:
            missing.append("agent_path")
    expected_plan = golden.get("expected_plan")
    if isinstance(expected_plan, Mapping):
        actual_plan = evidence.get("plan")
        if isinstance(actual_plan, Mapping):
            metrics["plan"] = score_plan_graph(actual_plan, expected_plan)
        else:
            missing.append("plan")
    expected_tools = golden.get("expected_tools")
    if isinstance(expected_tools, Sequence) and not isinstance(expected_tools, (str, bytes)):
        actual_tools = evidence.get("tool_calls")
        if isinstance(actual_tools, Sequence) and not isinstance(actual_tools, (str, bytes)):
            metrics["tools"] = score_tool_calls(
                actual=[item for item in actual_tools if isinstance(item, Mapping)],
                expected=[item for item in expected_tools if isinstance(item, Mapping)],
            )
        else:
            missing.append("tool_calls")
    required_dependencies = golden.get("required_agent_dependencies")
    forbidden_agents = golden.get("forbidden_agents")
    if isinstance(required_dependencies, Sequence) or isinstance(forbidden_agents, Sequence):
        actual_plan = evidence.get("plan")
        actual_agents = evidence.get("agent_path")
        if isinstance(actual_plan, Mapping) and isinstance(actual_agents, Sequence):
            metrics["agent_dependencies"] = score_agent_dependencies(
                actual_plan=actual_plan,
                actual_agent_path=actual_agents,
                required_dependencies=required_dependencies if isinstance(required_dependencies, Sequence) else [],
                forbidden_agents=forbidden_agents if isinstance(forbidden_agents, Sequence) else [],
            )
        else:
            missing.append("agent_dependencies")
    if golden.get("category") == "safety_refusal":
        required_markers = golden.get("required_refusal_markers")
        markers = [str(marker) for marker in required_markers] if isinstance(required_markers, Sequence) and not isinstance(required_markers, (str, bytes)) else []
        matched_markers = [marker for marker in markers if marker and marker in answer]
        metrics["refusal_safety"] = {
            "passed": metrics["task_completion"]["passed"] and bool(markers) and len(matched_markers) == len(markers),
            "required_markers": markers,
            "matched_markers": matched_markers,
        }
    return metrics, missing


def score_agent_dependencies(
    *, actual_plan: Mapping[str, Any], actual_agent_path: Sequence[Any], required_dependencies: Sequence[Any], forbidden_agents: Sequence[Any],
) -> dict[str, Any]:
    agents = actual_plan.get("agents")
    agents = agents if isinstance(agents, Mapping) else {}
    actual_pairs = {
        (str(agents.get(edge[0])), str(agents.get(edge[1])))
        for edge in _edge_set(actual_plan.get("edges"))
        if agents.get(edge[0]) and agents.get(edge[1])
    }
    expected_pairs = {
        (str(item.get("from_agent")), str(item.get("to_agent")))
        for item in required_dependencies
        if isinstance(item, Mapping) and item.get("from_agent") and item.get("to_agent")
    }
    scores = _precision_recall_f1(actual_pairs, expected_pairs)
    forbidden = _string_set(forbidden_agents)
    forbidden_count = sum(agent in forbidden for agent in _string_set(actual_agent_path))
    return {
        "dependency_precision": scores["precision"], "dependency_recall": scores["recall"], "dependency_f1": scores["f1"],
        "forbidden_agent_count": forbidden_count, "passed": scores["f1"] == 1.0 and forbidden_count == 0,
    }


def _semantic_input(golden: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    if golden.get("category") in _TOOL_BACKED_SEMANTIC_CATEGORIES:
        return None
    answer = str(evidence["answer"])
    if golden.get("category") == "knowledge" and "【引用文档】" in answer:
        answer = answer.split("【引用文档】", 1)[0].rstrip()
    reference = golden.get("reference_answer")
    if not isinstance(reference, str) or not reference.strip():
        expected_evidence = golden.get("expected_evidence")
        if isinstance(expected_evidence, Sequence) and not isinstance(expected_evidence, (str, bytes)):
            reference = "\n".join(str(item) for item in expected_evidence if str(item).strip())
    rubric = golden.get("answer_rubric")
    contexts = evidence.get("contexts")
    if not answer.strip() or not isinstance(reference, str) or not reference.strip():
        return None
    return {
        "query": str(golden.get("query", "")),
        "answer": answer,
        "reference_answer": reference,
        "answer_rubric": rubric if isinstance(rubric, str) else "",
        "retrieval_contexts": list(contexts) if isinstance(contexts, Sequence) and not isinstance(contexts, (str, bytes)) else [],
    }


def _answer_accuracy_params(params: Any, *, has_retrieval_context: bool) -> list[Any]:
    values = [params.ACTUAL_OUTPUT, params.EXPECTED_OUTPUT]
    if has_retrieval_context:
        values.append(params.RETRIEVAL_CONTEXT)
    return values


def _parse_structured_response(schema: Any, content: str, *, repair) -> Any:
    """Validate schema JSON, requesting one constrained re-encoding on malformed output."""
    normalized = normalize_json_response(content)

    def parse(value: str) -> Any:
        if hasattr(schema, "model_validate_json"):
            return schema.model_validate_json(value)
        return schema.parse_raw(value)

    try:
        return parse(normalized)
    except (TypeError, ValueError):
        repaired = repair(normalized)
        if not isinstance(repaired, str):
            raise ValueError("Qwen DeepEval JSON repair response must be text")
        return parse(normalize_json_response(repaired))


def _load_trace_evidence(root: Path, trace_row: Mapping[str, Any]) -> dict[str, Any]:
    allowed_fields = {"case_id", "id", "trace_id"}
    unexpected_fields = set(trace_row) - allowed_fields
    if unexpected_fields:
        raise TraceArtifactError("trace result contains untrusted trajectory fields")
    trace_id = trace_row.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id or not _safe_trace_id(trace_id):
        raise TraceArtifactError("trace_id is required and must be a safe identifier")
    uri = f"traces/{trace_id}/summary.json"
    path = _resolve_under_root(root, uri)
    if not path.is_file():
        raise TraceArtifactError("trace artifact does not exist")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TraceArtifactError("trace artifact is not valid JSON") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("summary"), Mapping):
        raise TraceArtifactError("trace artifact is missing summary")
    if payload.get("trace_id") != trace_id:
        raise TraceArtifactError("trace artifact trace_id does not match requested trace")
    evidence = payload.get("evidence")
    evidence_hash = payload.get("evidence_sha256")
    if not isinstance(evidence, Mapping) or not isinstance(evidence_hash, str):
        raise TraceArtifactError("trace artifact is missing signed evidence")
    if evidence.get("trace_id") != trace_id or _evidence_digest(evidence) != evidence_hash:
        raise TraceArtifactError("trace evidence binding or hash verification failed")
    answer = str(payload.get("final_answer", ""))
    answer_hash = evidence.get("final_answer_sha256")
    if not isinstance(answer_hash, str) or hashlib.sha256(answer.encode("utf-8")).hexdigest() != answer_hash:
        raise TraceArtifactError("trace final answer does not match signed evidence")
    summary_binding = evidence.get("summary_binding")
    if not isinstance(summary_binding, Mapping) or any(
        summary_binding.get(key) != payload["summary"].get(key)
        for key in ("status", "e2e_latency_ms", "critical_path_latency_ms", "total_tokens", "estimated_cost")
    ):
        raise TraceArtifactError("trace summary does not match signed evidence")
    return {
        "trace_id": trace_id,
        "trace_artifact_uri": uri,
        "summary": dict(payload["summary"]),
        "answer": answer,
        "agent_path": _first_sequence(evidence, {}, "agent_path"),
        "plan": _first_mapping(evidence, {}, "plan"),
        "tool_calls": _first_sequence(evidence, {}, "tool_calls"),
        "contexts": _first_sequence(evidence, {}, "retrieval_contexts", "contexts"),
        "errors": _trace_errors(evidence, payload),
    }


def _default_backend() -> DeepEvalBackend:
    try:
        return NativeDeepEvalBackend(QwenJudge())
    except Exception as error:
        return _UnavailableDeepEvalBackend(error)


class _UnavailableDeepEvalBackend:
    provider = "deepeval"
    version = "unavailable"
    model = "unconfigured"

    def __init__(self, error: Exception) -> None:
        self._error = error
        if isinstance(error, JudgeConfigurationError):
            self.model = "unconfigured"

    def evaluate(self, sample: Mapping[str, Any]) -> Mapping[str, Any]:
        raise self._error


class NativeDeepEvalBackend:
    """Lazy DeepEval adapter using the Task 5 Qwen connection configuration."""

    provider = "deepeval"

    def __init__(self, judge: QwenJudge) -> None:
        self._judge = judge
        self.model = judge.model
        try:
            self.version = importlib.metadata.version("deepeval")
        except importlib.metadata.PackageNotFoundError:
            self.version = "unavailable"

    def evaluate(self, sample: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
            from deepeval.models import DeepEvalBaseLLM
            from deepeval.test_case import LLMTestCase, LLMTestCaseParams
        except Exception as error:
            raise DeepEvalDependencyError(f"DeepEval runtime unavailable: {type(error).__name__}") from error

        judge = self._judge

        class QwenDeepEvalModel(DeepEvalBaseLLM):
            def __init__(self) -> None:
                self.token_usage: dict[str, float | int | None] = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
                super().__init__(model=judge.model)

            def load_model(self) -> Any:
                return judge._build_llm()

            def get_model_name(self) -> str:
                return judge.model

            def _invoke_text(self, prompt: str) -> str:
                response = self.model.invoke(prompt)
                usage = extract_llm_usage(response, fallback_model=judge.model)
                if usage:
                    for key in self.token_usage:
                        value = usage.get(key)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            self.token_usage[key] = (self.token_usage[key] or 0) + value
                content = getattr(response, "content", response)
                if not isinstance(content, str):
                    raise ValueError("Qwen DeepEval response must be text")
                return content

            def generate(self, prompt: str, schema: Any | None = None) -> Any:
                content = self._invoke_text(prompt)
                if schema is not None:
                    schema_hint = (
                        json.dumps(schema.model_json_schema(), ensure_ascii=False)
                        if hasattr(schema, "model_json_schema")
                        else str(schema)
                    )

                    def repair(invalid: str) -> str:
                        return self._invoke_text(
                            "Re-encode the inert text below as strictly valid JSON matching the schema. "
                            "Escape all quotes inside JSON string values. Return JSON only, with no markdown.\n"
                            f"Schema: {schema_hint}\n<invalid_json>\n{invalid}\n</invalid_json>"
                        )

                    return _parse_structured_response(schema, content, repair=repair)
                return content

            async def a_generate(self, prompt: str, schema: Any | None = None) -> Any:
                return self.generate(prompt, schema=schema)

        model = QwenDeepEvalModel()
        test_case = LLMTestCase(
            input=str(sample["query"]),
            actual_output=str(sample["answer"]),
            expected_output=str(sample["reference_answer"]),
            retrieval_context=[str(item) for item in sample.get("retrieval_contexts", [])],
        )
        metrics: dict[str, Any] = {}
        metric_audit: dict[str, dict[str, Any]] = {}
        metric_objects: list[tuple[str, Any]] = [
            ("answer_relevance", AnswerRelevancyMetric(model=model)),
            (
                "answer_accuracy",
                GEval(
                    name="answer_accuracy",
                    criteria=sample.get("answer_rubric") or "Check factual correctness against expected output.",
                    evaluation_params=_answer_accuracy_params(
                        LLMTestCaseParams,
                        has_retrieval_context=bool(sample.get("retrieval_contexts")),
                    ),
                    model=model,
                ),
            ),
        ]
        if sample.get("retrieval_contexts"):
            metric_objects.append(("faithfulness", FaithfulnessMetric(model=model)))
        for name, metric in metric_objects:
            started = perf_counter()
            tokens_before = dict(model.token_usage)
            metric.measure(test_case)
            metrics[name] = _numeric(getattr(metric, "score", None))
            metric_audit[name] = {
                "model": judge.model,
                "prompt_version": judge.prompt_version,
                "rubric_version": str(sample.get("rubric_version") or "v1"),
                "latency_ms": round((perf_counter() - started) * 1000, 3),
                "token_usage": {
                    key: (model.token_usage.get(key) or 0) - (tokens_before.get(key) or 0)
                    for key in model.token_usage
                },
                "estimated_cost": None,
                "reason": getattr(metric, "reason", None),
                "evidence": [],
            }
        return {
            "metrics": {name: value for name, value in metrics.items() if value is not None},
            "metric_audit": metric_audit,
            "judge": {
                "model": judge.model,
                "prompt_version": judge.prompt_version,
                "token_usage": model.token_usage,
            },
        }


def _evidence_error(
    case_id: str,
    golden: Mapping[str, Any],
    error_type: str,
    message: str,
    trace_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "trace_id": trace_row.get("trace_id") if trace_row else None,
        "trace_artifact_uri": trace_row.get("trace_artifact_uri") if trace_row else None,
        "status": "evidence_missing",
        "query": golden.get("query", ""),
        "error": {"type": error_type, "message": message},
        "metric_definitions": METRIC_DEFINITIONS,
    }


def _semantic_audit(
    metrics: Mapping[str, Any],
    supplied_audit: Any,
    judge: Mapping[str, Any],
    sample: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    audit_source = supplied_audit if isinstance(supplied_audit, Mapping) else {}
    output: dict[str, dict[str, Any]] = {}
    for name in metrics:
        supplied = audit_source.get(name)
        supplied = supplied if isinstance(supplied, Mapping) else {}
        output[name] = {
            "model": supplied.get("model", judge.get("model")),
            "prompt_version": supplied.get("prompt_version", judge.get("prompt_version", "v1")),
            "rubric_version": supplied.get("rubric_version", judge.get("rubric_version", sample.get("rubric_version", "v1"))),
            "latency_ms": supplied.get("latency_ms", judge.get("latency_ms")),
            "token_usage": dict(supplied.get("token_usage", judge.get("token_usage", {})) or {}),
            "estimated_cost": supplied.get("estimated_cost", judge.get("estimated_cost")),
            "reason": supplied.get("reason", judge.get("reason")),
            "evidence": list(supplied.get("evidence", judge.get("evidence", [])) or []),
            "raw_structured_evidence": supplied.get("raw_structured_evidence", judge.get("raw_structured_evidence")),
        }
    return output


def _error_status(error: Exception) -> str:
    if isinstance(error, JudgeConfigurationError):
        return "configuration_error"
    if isinstance(error, DeepEvalDependencyError):
        return "dependency_unavailable"
    return "evaluation_error"


def _summarize(rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any], unknown_trace_cases: Sequence[str]) -> dict[str, Any]:
    semantic_values: dict[str, list[float]] = {name: [] for name in ("answer_relevance", "faithfulness", "answer_accuracy")}
    system_tokens = 0
    judge_tokens = 0
    for row in rows:
        trace_metrics = row.get("trace_metrics")
        if isinstance(trace_metrics, Mapping) and isinstance(trace_metrics.get("total_tokens"), int):
            system_tokens += trace_metrics["total_tokens"]
        metrics = row.get("deepeval_metrics")
        if isinstance(metrics, Mapping):
            for name in semantic_values:
                score = _numeric(metrics.get(name))
                if score is not None:
                    semantic_values[name].append(score)
        judge = row.get("judge")
        usage = judge.get("token_usage") if isinstance(judge, Mapping) else None
        if isinstance(usage, Mapping) and isinstance(usage.get("total_tokens"), (int, float)):
            judge_tokens += int(usage["total_tokens"])
    completed = sum(row.get("status") == "completed" for row in rows)
    errors = sum(row.get("status") == "evaluation_error" for row in rows)
    configuration_errors = sum(row.get("status") == "configuration_error" for row in rows)
    dependency_errors = sum(row.get("status") == "dependency_unavailable" for row in rows)
    missing = sum(row.get("status") == "evidence_missing" for row in rows)
    return {
        "schema_version": DEEPEVAL_RUN_SCHEMA_VERSION,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if not errors and not configuration_errors and not dependency_errors and not missing else "degraded",
        "case_count": len(rows),
        "completed_count": completed,
        "evaluation_error_count": errors,
        "configuration_error_count": configuration_errors,
        "dependency_unavailable_count": dependency_errors,
        "evidence_missing_count": missing,
        "unknown_trace_cases": list(unknown_trace_cases),
        "semantic_metrics": {name: round(sum(values) / len(values), 4) if values else None for name, values in semantic_values.items()},
        "semantic_metric_case_counts": {name: len(values) for name, values in semantic_values.items()},
        "token_usage": {"system_total_tokens": system_tokens, "judge_total_tokens": judge_tokens},
        "deepeval": manifest["deepeval"],
        "metric_definitions": METRIC_DEFINITIONS,
    }


def _report(suite: str, summary: Mapping[str, Any]) -> str:
    lines = [f"# DeepEval agent evaluation: {suite}", "", "## Run", ""]
    lines.extend(f"- {name}: {summary[name]}" for name in ("status", "case_count", "completed_count", "evaluation_error_count", "evidence_missing_count"))
    lines.extend(["", "## Semantic metrics", ""])
    lines.extend(f"- {name}: {value}" for name, value in summary["semantic_metrics"].items())
    lines.extend(["", "## Token usage", ""])
    lines.extend(f"- {name}: {value}" for name, value in summary["token_usage"].items())
    gate = summary.get("acceptance_gate")
    if isinstance(gate, Mapping):
        lines.extend(["", "## 验收门槛", "", "| 规则 | 实际值 | 门槛 | 通过 |", "|---|---:|---:|---|"])
        rules = gate.get("rules")
        if isinstance(rules, Mapping):
            lines.extend(f"| {name} | {rule.get('actual')} | {rule.get('threshold')} | {rule.get('passed')} |" for name, rule in rules.items() if isinstance(rule, Mapping))
    return "\n".join(lines) + "\n"


def _resolve_under_root(root: Path, uri: str) -> Path:
    candidate = PurePosixPath(uri)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise TraceArtifactError("trace_artifact_uri must be a safe relative path")
    resolved_root = root.resolve()
    resolved = (resolved_root / Path(*candidate.parts)).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise TraceArtifactError("trace_artifact_uri escapes artifact_root") from error
    return resolved


def _safe_trace_id(value: str) -> bool:
    return not any(char in value for char in "\\\\/:") and value not in {".", ".."}


def _evidence_digest(evidence: Mapping[str, Any]) -> str:
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_path_segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or any(char in value for char in "\\\\/:"):
        raise ValueError(f"{name} must be a safe path segment")
    return value


def _string_set(value: Any) -> set[str]:
    return {str(item) for item in value} if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else set()


def _edge_set(value: Any) -> set[tuple[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return set()
    return {(str(item[0]), str(item[1])) for item in value if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2}


def _precision_recall_f1(actual: set[Any], expected: set[Any]) -> dict[str, float]:
    matched = len(actual & expected)
    precision = matched / len(actual) if actual else (1.0 if not expected else 0.0)
    recall = matched / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def _multiset_precision_recall_f1(actual: Counter[str], expected: Counter[str]) -> dict[str, float]:
    matched = sum((actual & expected).values())
    actual_count = sum(actual.values())
    expected_count = sum(expected.values())
    precision = matched / actual_count if actual_count else (1.0 if not expected_count else 0.0)
    recall = matched / expected_count if expected_count else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def _mapping_contains(actual: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(actual, Mapping):
        return False
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, Mapping):
            if not _mapping_contains(actual_value, expected_value):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _first_sequence(primary: Mapping[str, Any], secondary: Mapping[str, Any], *keys: str) -> list[Any] | None:
    for source in (primary, secondary):
        for key in keys:
            value = source.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return list(value)
    return None


def _first_mapping(primary: Mapping[str, Any], secondary: Mapping[str, Any], *keys: str) -> dict[str, Any] | None:
    for source in (primary, secondary):
        for key in keys:
            value = source.get(key)
            if isinstance(value, Mapping):
                return dict(value)
    return None


def _trace_errors(trace_row: Mapping[str, Any], payload: Mapping[str, Any]) -> list[str]:
    values = _first_sequence(trace_row, payload, "errors") or []
    result = [str(value) for value in values]
    summary = payload.get("summary")
    if isinstance(summary, Mapping) and summary.get("trace_error"):
        result.append(str(summary["trace_error"]))
    return result


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _numeric(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def _hash_json_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps([dict(row) for row in rows], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"


def _build_evaluation_service(
    artifact_root: Path | str,
    *,
    session_factory: Any | None = None,
    service_factory: Any | None = None,
) -> Any:
    """Build persistence lazily so offline evaluation can run without a database."""
    if session_factory is None:
        from metro_agent.storage.history.database import SessionLocal

        session_factory = SessionLocal
    if service_factory is None:
        from metro_agent.observability.evaluation_service import EvaluationService

        service_factory = EvaluationService
    return service_factory(session_factory, artifact_root=artifact_root)


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local DeepEval agent evaluation")
    parser.add_argument("--golden", required=True, help="Golden JSONL with optional agent expectations")
    parser.add_argument("--traces", required=True, help="Local JSONL mapping case_id to immutable trace_id")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-root", default="artifacts")
    parser.add_argument("--judge-model")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--allow-degraded", action="store_true")
    args = parser.parse_args(argv)
    backend: DeepEvalBackend | None = None
    if args.judge_model:
        backend = NativeDeepEvalBackend(QwenJudge(model=args.judge_model))
    result = run_deepeval_suite(
        golden_rows=load_jsonl(args.golden),
        trace_rows=load_jsonl(args.traces),
        artifact_root=args.artifact_root,
        suite=args.suite,
        run_id=args.run_id,
        evaluator=backend,
    )
    recorded = None
    if args.persist:
        from evaluation.deepeval_persistence import persist_deepeval_artifact

        recorded = persist_deepeval_artifact(
            artifact_root=args.artifact_root,
            artifact_uri=result.artifact_uri,
            evaluation_service=_build_evaluation_service(args.artifact_root),
        )
    print(json.dumps({"artifact_uri": result.artifact_uri, "summary": result.summary, "recorded_eval_run_id": getattr(recorded, "id", None)}, ensure_ascii=False, indent=2))
    accepted = result.summary.get("acceptance_gate", {}).get("passed") is True
    return 0 if args.allow_degraded or (result.summary["status"] == "completed" and accepted) else 1


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
