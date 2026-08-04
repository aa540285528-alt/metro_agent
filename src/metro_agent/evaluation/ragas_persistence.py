"""Persist compact RAGAS artifact metadata without duplicating artifact files."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from metro_agent.observability.evaluation_service import EvaluationService, RecordedEvalRun


FAITHFULNESS_THRESHOLD = 0.80
ANSWER_CORRECTNESS_THRESHOLD = 0.80
THRESHOLD_VERSION = "faithfulness-correctness-v1"


class RagasArtifactPersistenceError(ValueError):
    """Raised when a semantic artifact cannot be safely registered."""


def persist_ragas_artifact(
    *,
    artifact_root: Path | str,
    artifact_uri: str,
    evaluation_service: EvaluationService,
) -> RecordedEvalRun:
    """Read a completed RAGAS artifact and register its compact metadata."""
    root = Path(artifact_root)
    artifact_dir = _resolve_under_root(root, artifact_uri)
    manifest = _read_json(artifact_dir / "input_manifest.json")
    if not isinstance(manifest, Mapping):
        raise RagasArtifactPersistenceError("semantic input_manifest must be a JSON object")
    verification = manifest.get("deterministic_case_results")
    if not isinstance(verification, Mapping) or verification.get("verified") is not True:
        raise RagasArtifactPersistenceError("semantic artifact has no verified deterministic input")

    summary_path = artifact_dir / "summary.json"
    case_results_path = artifact_dir / "case_results.jsonl"
    summary = _read_json(summary_path)
    if not isinstance(summary, Mapping):
        raise RagasArtifactPersistenceError("semantic summary must be a JSON object")
    rows = _read_jsonl(case_results_path)
    run_id = _run_id(manifest, artifact_dir)
    dataset_version = _dataset_version(root, manifest)
    cases = [_case_for_persistence(row) for row in rows]
    compact_summary = _summary_for_persistence(
        summary=summary,
        cases=cases,
        deterministic_artifact_uri=manifest.get("deterministic_artifact_uri"),
        semantic_artifact_uri=artifact_uri,
    )

    return evaluation_service.record_existing_run(
        run_id=run_id,
        suite=_suite_name(artifact_dir),
        category="rag",
        artifact_uri=artifact_uri,
        artifact_sha256=_sha256(summary_path),
        case_artifact_sha256=_sha256(case_results_path),
        cases=cases,
        summary=compact_summary,
        dataset_version=dataset_version,
        judge_config=_judge_config(manifest),
    )


def _resolve_under_root(root: Path, artifact_uri: str) -> Path:
    if not isinstance(artifact_uri, str) or not artifact_uri:
        raise RagasArtifactPersistenceError("artifact_uri must be a non-empty relative URI")
    uri = PurePosixPath(artifact_uri)
    if uri.is_absolute() or any(part in {"", ".", ".."} for part in uri.parts):
        raise RagasArtifactPersistenceError("artifact_uri must be a safe relative URI")
    resolved_root = root.resolve()
    resolved_path = (resolved_root / Path(*uri.parts)).resolve()
    if resolved_root not in resolved_path.parents:
        raise RagasArtifactPersistenceError("artifact_uri escapes artifact_root")
    return resolved_path


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RagasArtifactPersistenceError(
            f"cannot read JSON artifact {path.name}: {type(error).__name__}"
        ) from error


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as error:
        raise RagasArtifactPersistenceError(
            f"cannot read JSONL artifact {path.name}: {type(error).__name__}"
        ) from error
    if not all(isinstance(row, Mapping) for row in rows):
        raise RagasArtifactPersistenceError("semantic case results must contain JSON objects")
    return rows


def _run_id(manifest: Mapping[str, Any], artifact_dir: Path) -> str:
    value = manifest.get("run_id")
    if isinstance(value, str) and value:
        return value
    if artifact_dir.name:
        return artifact_dir.name
    raise RagasArtifactPersistenceError("semantic artifact has no run id")


def _suite_name(artifact_dir: Path) -> str:
    try:
        return artifact_dir.parent.name
    except AttributeError as error:
        raise RagasArtifactPersistenceError("semantic artifact has no suite name") from error


def _dataset_version(root: Path, manifest: Mapping[str, Any]) -> str | None:
    uri = manifest.get("deterministic_artifact_uri")
    if not isinstance(uri, str) or not uri:
        return None
    deterministic_manifest = _read_json(_resolve_under_root(root, uri) / "input_manifest.json")
    if not isinstance(deterministic_manifest, Mapping):
        raise RagasArtifactPersistenceError("deterministic input_manifest must be a JSON object")
    golden = deterministic_manifest.get("golden_set")
    if not isinstance(golden, Mapping):
        return None
    digest = golden.get("sha256")
    return digest if isinstance(digest, str) and digest else None


def _case_for_persistence(row: Mapping[str, Any]) -> dict[str, Any]:
    case_id = row.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise RagasArtifactPersistenceError("semantic case result has no case_id")
    source_status = row.get("status")
    metrics = _numeric_metrics(row.get("ragas_metrics"))
    if source_status == "completed":
        faithfulness = metrics.get("faithfulness")
        correctness = metrics.get("answer_correctness")
        if faithfulness is None or correctness is None:
            return {
                "case_id": case_id,
                "status": "unknown",
                "passed": None,
                "metrics": metrics,
                "reason": "faithfulness 或 answer_correctness 指标缺失。",
            }
        passed = faithfulness >= FAITHFULNESS_THRESHOLD and correctness >= ANSWER_CORRECTNESS_THRESHOLD
        return {
            "case_id": case_id,
            "status": "completed",
            "passed": passed,
            "metrics": metrics,
            "reason": (
                f"faithfulness={faithfulness:.4f}（阈值 {FAITHFULNESS_THRESHOLD:.2f}），"
                f"answer_correctness={correctness:.4f}（阈值 {ANSWER_CORRECTNESS_THRESHOLD:.2f}）。"
            ),
        }
    if source_status == "skipped_not_applicable":
        return {
            "case_id": case_id,
            "status": "skipped",
            "passed": None,
            "metrics": metrics,
            "reason": "该样本不适用于语义 RAGAS 评测。",
        }
    error = row.get("error")
    message = error.get("message") if isinstance(error, Mapping) else None
    return {
        "case_id": case_id,
        "status": "input_error" if source_status == "input_error" else "error",
        "passed": None,
        "metrics": metrics,
        "reason": str(message or f"RAGAS 样本状态：{source_status or 'unknown'}")[:512],
    }


def _numeric_metrics(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(name): float(score)
        for name, score in value.items()
        if isinstance(score, (int, float)) and not isinstance(score, bool)
    }


def _summary_for_persistence(
    *,
    summary: Mapping[str, Any],
    cases: list[Mapping[str, Any]],
    deterministic_artifact_uri: Any,
    semantic_artifact_uri: str,
) -> dict[str, Any]:
    completed = [case for case in cases if case["status"] == "completed"]
    passed = sum(case["passed"] is True for case in completed)
    failed = sum(case["passed"] is False for case in completed)
    return {
        "ragas": dict(summary),
        "semantic_completed_count": len(completed),
        "semantic_passed_count": passed,
        "semantic_failed_count": failed,
        "semantic_unknown_count": sum(case["status"] == "unknown" for case in cases),
        "semantic_skipped_count": sum(case["status"] == "skipped" for case in cases),
        "semantic_pass_rate": round(passed / len(completed), 6) if completed else None,
        "deterministic_artifact_uri": (
            deterministic_artifact_uri if isinstance(deterministic_artifact_uri, str) else None
        ),
        "semantic_artifact_uri": semantic_artifact_uri,
    }


def _judge_config(manifest: Mapping[str, Any]) -> dict[str, Any]:
    judge = manifest.get("judge")
    config = dict(judge) if isinstance(judge, Mapping) else {}
    config["threshold_version"] = THRESHOLD_VERSION
    config["faithfulness_threshold"] = FAITHFULNESS_THRESHOLD
    config["answer_correctness_threshold"] = ANSWER_CORRECTNESS_THRESHOLD
    return config


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise RagasArtifactPersistenceError(
            f"cannot hash artifact {path.name}: {type(error).__name__}"
        ) from error
