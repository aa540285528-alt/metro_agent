from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


class DeepEvalArtifactPersistenceError(ValueError):
    pass


def persist_deepeval_artifact(*, artifact_root: Path | str, artifact_uri: str, evaluation_service: Any) -> Any:
    root = Path(artifact_root).resolve()
    uri = PurePosixPath(artifact_uri)
    if not artifact_uri or uri.is_absolute() or any(part in {"", ".", ".."} for part in uri.parts):
        raise DeepEvalArtifactPersistenceError("artifact_uri must be a safe relative URI")
    target = (root / Path(*uri.parts)).resolve()
    if root not in target.parents:
        raise DeepEvalArtifactPersistenceError("artifact_uri escapes artifact_root")
    manifest = _read_json(target / "input_manifest.json")
    summary = _read_json(target / "summary.json")
    rows = _read_jsonl(target / "case_results.jsonl")
    if not isinstance(manifest, Mapping) or not isinstance(summary, Mapping) or not isinstance(summary.get("acceptance_gate"), Mapping):
        raise DeepEvalArtifactPersistenceError("agent artifact is missing acceptance_gate")
    run_id = manifest.get("run_id") if isinstance(manifest.get("run_id"), str) else target.name
    suite = manifest.get("suite") if isinstance(manifest.get("suite"), str) else target.parent.name
    cases = [{"case_id": str(row.get("case_id")), "status": str(row.get("status", "unknown")), "passed": row.get("status") == "completed", "metrics": _metrics(row), "reason": None} for row in rows if isinstance(row.get("case_id"), str)]
    return evaluation_service.record_existing_run(run_id=run_id, suite=suite, category="agent", artifact_uri=artifact_uri, artifact_sha256=_sha(target / "summary.json"), case_artifact_sha256=_sha(target / "case_results.jsonl"), cases=cases, summary={"acceptance_gate": dict(summary["acceptance_gate"])}, dataset_version=manifest.get("golden_sha256"), judge_config=dict(manifest.get("deepeval", {})) if isinstance(manifest.get("deepeval"), Mapping) else {})


def _read_json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error: raise DeepEvalArtifactPersistenceError(f"cannot read {path.name}") from error


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try: rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as error: raise DeepEvalArtifactPersistenceError(f"cannot read {path.name}") from error
    if not all(isinstance(row, Mapping) for row in rows): raise DeepEvalArtifactPersistenceError("case results must be objects")
    return rows


def _sha(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()


def _metrics(row: Mapping[str, Any]) -> dict[str, float]:
    values = row.get("deepeval_metrics")
    return {str(key): float(value) for key, value in values.items() if isinstance(value, (int, float)) and not isinstance(value, bool)} if isinstance(values, Mapping) else {}
