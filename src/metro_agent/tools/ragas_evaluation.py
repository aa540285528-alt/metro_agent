"""Versioned, local-artifact runner for RAGAS-compatible RAG evaluation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from metro_agent.tools.rag_evaluation import evaluate_rows, load_jsonl
from evaluation.qwen_judge import QwenJudge
from metro_agent.observability.artifacts import write_json, write_jsonl, write_markdown


class RagasEvaluator(Protocol):
    def evaluate(self, sample: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RagasRunResult:
    run_id: str
    artifact_uri: str
    summary: dict[str, float | int | None]


class QwenRagasEvaluator:
    """Qwen-backed semantic scorer with the RAGAS metric names and contracts.

    The runner keeps framework orchestration injectable so production can replace
    this adapter with a native RAGAS evaluator without changing artifacts.
    """

    METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")

    def __init__(self, judge: QwenJudge) -> None:
        self._judge = judge

    def evaluate(self, sample: Mapping[str, Any]) -> Mapping[str, Any]:
        scores: dict[str, float] = {}
        provenance: dict[str, Any] = {}
        for metric in self.METRICS:
            prompt = json.dumps(
                {
                    "task": "Score one RAG evaluation metric from 0 to 1.",
                    "metric": metric,
                    "sample": dict(sample),
                    "response_schema": {"score": 0.0, "pass": True, "reason": "", "evidence": []},
                },
                ensure_ascii=False,
            )
            result = self._judge.judge(prompt)
            scores[metric] = result.score
            provenance = {
                "model": result.model,
                "prompt_version": result.prompt_version,
                "total_tokens": result.token_usage.get("total_tokens"),
                "latency_ms": result.latency_ms,
                "estimated_cost": result.estimated_cost,
            }
        return {**scores, "judge": provenance}


def run_ragas_suite(
    *,
    golden_rows: list[dict[str, Any]],
    result_rows: list[dict[str, Any]],
    evaluator: RagasEvaluator,
    artifact_root: Path | str,
    suite: str,
    run_id: str | None = None,
) -> RagasRunResult:
    safe_suite = _safe_path_segment(suite, "suite")
    resolved_run_id = _safe_path_segment(run_id or _new_run_id(), "run_id")
    results_by_id = {str(row.get("id")): row for row in result_rows}
    rows: list[dict[str, Any]] = []

    for golden in golden_rows:
        case_id = str(golden.get("id", ""))
        result = results_by_id.get(case_id)
        if result is None:
            rows.append({"case_id": case_id, "status": "evaluation_error", "error": "missing result"})
            continue
        deterministic = evaluate_rows([_complete_golden(golden)], [_complete_result(result)])["rag"]
        sample = {
            "case_id": case_id,
            "query": golden.get("query", ""),
            "reference_answer": golden.get("reference_answer", ""),
            "answer": result.get("answer", ""),
            "retrieved_docs": result.get("source_docs", []),
            "expected_docs": golden.get("expected_docs", []),
            "expected_evidence": golden.get("expected_evidence", []),
        }
        try:
            semantic = dict(evaluator.evaluate(sample))
            judge = semantic.pop("judge", {})
            rows.append(
                {
                    "case_id": case_id,
                    "status": "completed",
                    "query": sample["query"],
                    "retrieved_docs": sample["retrieved_docs"],
                    "answer": sample["answer"],
                    "deterministic_metrics": deterministic,
                    "ragas_metrics": semantic,
                    "judge": judge,
                }
            )
        except Exception as error:
            rows.append(
                {
                    "case_id": case_id,
                    "status": "evaluation_error",
                    "query": sample["query"],
                    "deterministic_metrics": deterministic,
                    "error": type(error).__name__,
                }
            )

    uri = f"evals/rag/{safe_suite}/{resolved_run_id}"
    summary = _summarize(rows)
    root = Path(artifact_root)
    write_json(root, f"{uri}/input_manifest.json", {"suite": safe_suite, "run_id": resolved_run_id, "case_ids": [row.get("id") for row in golden_rows]})
    write_jsonl(root, f"{uri}/case_results.jsonl", rows)
    write_json(root, f"{uri}/summary.json", summary)
    write_markdown(root, f"{uri}/report.md", _report(safe_suite, summary, rows))
    return RagasRunResult(run_id=resolved_run_id, artifact_uri=uri, summary=summary)


def _complete_golden(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"), "scope": row.get("scope", "rag_retrieval"),
        "category": row.get("category", "rag"), "expected_docs": row.get("expected_docs", []),
        "expected_evidence": row.get("expected_evidence", []), "unanswerable": row.get("unanswerable", False),
    }


def _complete_result(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": row.get("id"), "source_docs": row.get("source_docs", []), "answer": row.get("answer", "")}


def _summarize(rows: list[Mapping[str, Any]]) -> dict[str, float | int | None]:
    values: dict[str, list[float]] = {}
    for row in rows:
        for metric, value in row.get("ragas_metrics", {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.setdefault(metric, []).append(float(value))
    return {
        "case_count": len(rows),
        "completed_count": sum(row.get("status") == "completed" for row in rows),
        "error_count": sum(row.get("status") == "evaluation_error" for row in rows),
        **{name: round(sum(metric_values) / len(metric_values), 4) for name, metric_values in sorted(values.items())},
    }


def _report(suite: str, summary: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> str:
    lines = [f"# RAGAS evaluation: {suite}", "", *[f"- {name}: {value}" for name, value in summary.items()], "", "## Evaluation errors"]
    errors = [row for row in rows if row.get("status") == "evaluation_error"]
    lines.extend(f"- {row.get('case_id')}: {row.get('error')}" for row in errors) or lines.append("- None")
    return "\n".join(lines) + "\n"


def _new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"


def _safe_path_segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\\\\/:") or value in {".", ".."}:
        raise ValueError(f"{name} must be a safe path segment")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline RAGAS-compatible evaluation")
    parser.add_argument("--golden", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-root", default="artifacts")
    parser.add_argument("--judge-model")
    args = parser.parse_args()
    judge = QwenJudge(model=args.judge_model) if args.judge_model else QwenJudge()
    run = run_ragas_suite(golden_rows=load_jsonl(args.golden), result_rows=load_jsonl(args.results), evaluator=QwenRagasEvaluator(judge), artifact_root=args.artifact_root, suite=args.suite, run_id=args.run_id)
    print(json.dumps({"run_id": run.run_id, "artifact_uri": run.artifact_uri, "summary": run.summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
