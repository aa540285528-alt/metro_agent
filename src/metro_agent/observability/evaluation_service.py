from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from metro_agent.observability.artifacts import ArtifactRef, write_json, write_jsonl, write_markdown
from metro_agent.observability.models import EvalCaseResult, EvalRun


@dataclass(frozen=True, slots=True)
class RecordedEvalRun:
    id: str
    suite: str
    category: str
    summary: dict[str, Any]
    artifact_uri: str
    artifact_sha256: str


class EvaluationService:
    """Persists compact evaluation metadata alongside reviewable local artifacts."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        artifact_root: Path | str,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = Path(artifact_root)

    def record_run(
        self,
        *,
        suite: str,
        category: str,
        cases: Iterable[Mapping[str, Any]],
        dataset_version: str | None = None,
        code_version: str | None = None,
        judge_config: Mapping[str, Any] | None = None,
    ) -> RecordedEvalRun:
        _require_text("suite", suite)
        _require_text("category", category)
        normalized_cases = [_normalize_case(case) for case in cases]
        run_id = uuid4().hex
        artifact_uri = f"evals/{category}/{suite}/{run_id}"
        summary = _summarize_cases(normalized_cases)
        manifest = {
            "run_id": run_id,
            "suite": suite,
            "category": category,
            "dataset_version": dataset_version,
            "code_version": code_version,
            "judge_config": dict(judge_config or {}),
            "case_ids": [case["case_id"] for case in normalized_cases],
        }

        write_json(self._artifact_root, f"{artifact_uri}/input_manifest.json", manifest)
        case_results_ref = write_jsonl(
            self._artifact_root,
            f"{artifact_uri}/case_results.jsonl",
            normalized_cases,
        )
        summary_ref = write_json(
            self._artifact_root,
            f"{artifact_uri}/summary.json",
            summary,
        )
        write_markdown(
            self._artifact_root,
            f"{artifact_uri}/report.md",
            _build_report(suite=suite, category=category, summary=summary, cases=normalized_cases),
        )

        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            session.add(
                EvalRun(
                    id=run_id,
                    suite=suite,
                    category=category,
                    dataset_version=dataset_version,
                    code_version=code_version,
                    judge_config=dict(judge_config or {}),
                    summary=summary,
                    artifact_uri=artifact_uri,
                    artifact_sha256=summary_ref.sha256,
                    started_at=now,
                    finished_at=now,
                )
            )
            session.add_all(
                EvalCaseResult(
                    run_id=run_id,
                    case_id=case["case_id"],
                    status=case["status"],
                    passed=case["passed"],
                    metrics=case["metrics"],
                    reason_summary=case["reason_summary"],
                    artifact_uri=f"{artifact_uri}/case_results.jsonl",
                    artifact_sha256=case_results_ref.sha256,
                )
                for case in normalized_cases
            )
            session.commit()

        return RecordedEvalRun(
            id=run_id,
            suite=suite,
            category=category,
            summary=summary,
            artifact_uri=artifact_uri,
            artifact_sha256=summary_ref.sha256,
        )

    def record_existing_run(
        self,
        *,
        run_id: str,
        suite: str,
        category: str,
        artifact_uri: str,
        artifact_sha256: str | None,
        case_artifact_sha256: str | None = None,
        cases: Iterable[Mapping[str, Any]],
        summary: Mapping[str, Any],
        dataset_version: str | None = None,
        code_version: str | None = None,
        judge_config: Mapping[str, Any] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> RecordedEvalRun:
        """Register an immutable evaluation artifact without creating another copy."""
        _require_text("run_id", run_id)
        _require_text("suite", suite)
        _require_text("category", category)
        _require_text("artifact_uri", artifact_uri)
        if artifact_sha256 is not None and (
            not isinstance(artifact_sha256, str) or not artifact_sha256
        ):
            raise ValueError("artifact_sha256 must be a non-empty string or None")
        if case_artifact_sha256 is not None and (
            not isinstance(case_artifact_sha256, str) or not case_artifact_sha256
        ):
            raise ValueError("case_artifact_sha256 must be a non-empty string or None")
        if not isinstance(summary, Mapping):
            raise ValueError("summary must be a mapping")

        normalized_cases = [_normalize_case(case) for case in cases]
        normalized_summary = dict(summary)
        normalized_judge_config = dict(judge_config or {})
        _require_json_compatible(normalized_summary, "summary")
        _require_json_compatible(normalized_judge_config, "judge_config")
        now = datetime.now(timezone.utc)
        recorded_started_at = started_at or now
        recorded_finished_at = finished_at or now

        with self._session_factory() as session:
            existing = session.get(EvalRun, run_id)
            if existing is not None:
                return RecordedEvalRun(
                    id=existing.id,
                    suite=existing.suite,
                    category=existing.category,
                    summary=dict(existing.summary or {}),
                    artifact_uri=existing.artifact_uri,
                    artifact_sha256=existing.artifact_sha256 or "",
                )

            session.add(
                EvalRun(
                    id=run_id,
                    suite=suite,
                    category=category,
                    dataset_version=dataset_version,
                    code_version=code_version,
                    judge_config=normalized_judge_config,
                    summary=normalized_summary,
                    artifact_uri=artifact_uri,
                    artifact_sha256=artifact_sha256,
                    started_at=recorded_started_at,
                    finished_at=recorded_finished_at,
                )
            )
            session.add_all(
                EvalCaseResult(
                    run_id=run_id,
                    case_id=case["case_id"],
                    status=case["status"],
                    passed=case["passed"],
                    metrics=case["metrics"],
                    reason_summary=case["reason_summary"],
                    artifact_uri=f"{artifact_uri}/case_results.jsonl",
                    artifact_sha256=case_artifact_sha256 or artifact_sha256,
                )
                for case in normalized_cases
            )
            session.commit()

        return RecordedEvalRun(
            id=run_id,
            suite=suite,
            category=category,
            summary=normalized_summary,
            artifact_uri=artifact_uri,
            artifact_sha256=artifact_sha256 or "",
        )


def _normalize_case(case: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(case, Mapping):
        raise ValueError("evaluation case must be a mapping")
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("evaluation case requires a non-empty string case_id")
    metrics = case.get("metrics", {})
    if not isinstance(metrics, Mapping):
        raise ValueError("evaluation case metrics must be a mapping")
    _require_json_compatible(metrics, "metrics")
    passed = case.get("passed")
    if passed is not None and not isinstance(passed, bool):
        raise ValueError("evaluation case passed must be a boolean or null")
    status = case.get("status", "completed")
    if not isinstance(status, str) or not status:
        raise ValueError("evaluation case status must be a non-empty string")
    reason = case.get("reason", case.get("reason_summary"))
    if reason is not None and not isinstance(reason, str):
        raise ValueError("evaluation case reason must be a string when provided")
    return {
        "case_id": case_id,
        "status": status,
        "passed": passed,
        "metrics": dict(metrics),
        "reason_summary": reason[:512] if reason else None,
    }


def _summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    passed_count = sum(case["passed"] is True for case in cases)
    failed_count = sum(case["passed"] is False for case in cases)
    metric_values: dict[str, list[float]] = {}
    for case in cases:
        for name, value in case["metrics"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metric_values.setdefault(str(name), []).append(float(value))
    return {
        "case_count": len(cases),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "pass_rate": round(passed_count / len(cases), 6) if cases else None,
        "metrics": {
            name: round(sum(values) / len(values), 6)
            for name, values in sorted(metric_values.items())
        },
    }


def _build_report(
    *, suite: str, category: str, summary: Mapping[str, Any], cases: list[dict[str, Any]]
) -> str:
    lines = [
        f"# {category} evaluation: {suite}",
        "",
        f"- Cases: {summary['case_count']}",
        f"- Passed: {summary['passed_count']}",
        f"- Failed: {summary['failed_count']}",
        f"- Pass rate: {summary['pass_rate']}",
        "",
        "## Failed cases",
    ]
    failures = [case for case in cases if case["passed"] is False]
    if not failures:
        lines.append("None.")
    else:
        lines.extend(
            f"- {case['case_id']}: {case['reason_summary'] or 'No reason recorded.'}"
            for case in failures
        )
    return "\n".join(lines) + "\n"


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_json_compatible(value: object, name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"evaluation case {name} must be JSON-compatible") from error
