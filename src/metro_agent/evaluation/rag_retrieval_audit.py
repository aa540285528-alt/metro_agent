"""Read-only local audit for RAG retrieval, reranking, and cutoff behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from metro_agent.tools.chunk_artifacts import read_published_chunk_manifest
from metro_agent.tools.rag_evaluation import load_jsonl
from metro_agent.observability.artifacts import write_json, write_jsonl, write_markdown


AUDIT_SCHEMA_VERSION = "rag-retrieval-audit-v1"
AUDIT_THRESHOLDS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)


@dataclass(frozen=True, slots=True)
class AuditRun:
    run_id: str
    artifact_uri: str
    summary: dict[str, Any]


def run_retrieval_audit(
    *,
    deterministic_artifact: str | Path,
    artifact_root: str | Path,
    suite: str,
    run_id: str,
    retrieve: Callable[[str], Sequence[Any]] | None = None,
    rerank: Callable[[Sequence[Any], str], Sequence[Any]] | None = None,
    configured_cutoff: float | None = None,
) -> AuditRun:
    """Audit an immutable deterministic artifact without generating answers."""
    root = Path(artifact_root)
    safe_suite = _safe_segment(suite, "suite")
    safe_run_id = _safe_segment(run_id, "run_id")
    source_uri = _safe_relative_uri(str(deterministic_artifact), "deterministic_artifact")
    source_dir = _resolve_under_root(root, source_uri)
    source_manifest = _read_json(source_dir / "input_manifest.json")
    case_state, deterministic_rows = _load_case_results(root, source_manifest)
    snapshot, chunk_state = _load_published_snapshot(root, source_manifest)
    golden_state, golden_rows = _load_golden_rows(source_manifest)
    _validate_case_alignment(golden_rows, deterministic_rows)
    cutoff = _configured_cutoff(configured_cutoff)
    selected_retrieve, selected_rerank = (
        (retrieve, rerank)
        if retrieve is not None and rerank is not None
        else _build_local_adapter()
    )

    rows: list[dict[str, Any]] = []
    for golden in golden_rows:
        case_id = _case_id(golden)
        skip_reason = _skip_reason(golden)
        if skip_reason is not None:
            rows.append(
                {
                    "case_id": case_id,
                    "status": "skipped_not_applicable",
                    "skip_reason": skip_reason,
                }
            )
            continue
        try:
            query = _query(golden, case_id)
            candidates = list(selected_retrieve(query))
            reranked = list(selected_rerank(candidates, query))[:3]
            rows.append(_completed_row(golden, candidates, reranked, cutoff))
        except Exception as error:
            rows.append(
                {
                    "case_id": case_id,
                    "status": "audit_error",
                    "error": {"type": type(error).__name__, "message": str(error)},
                }
            )

    artifact_uri = f"evals/rag-retrieval-audit/{safe_suite}/{safe_run_id}"
    output_manifest = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "deterministic_artifact_uri": source_uri.as_posix(),
        "deterministic_input_manifest_sha256": _sha256_file(
            source_dir / "input_manifest.json"
        ),
        "deterministic_case_results": case_state,
        "golden_set": golden_state,
        "knowledge_snapshot": snapshot,
        "chunk_manifest": chunk_state,
        "query_transform": "original_query_only",
        "configured_similarity_cutoff": cutoff,
        "thresholds": list(AUDIT_THRESHOLDS),
    }
    summary = _summarize(rows, cutoff, output_manifest)
    write_json(root, f"{artifact_uri}/input_manifest.json", output_manifest)
    write_jsonl(root, f"{artifact_uri}/case_results.jsonl", rows)
    write_json(root, f"{artifact_uri}/summary.json", summary)
    write_markdown(root, f"{artifact_uri}/report.md", _report(safe_suite, summary))
    return AuditRun(run_id=safe_run_id, artifact_uri=artifact_uri, summary=summary)


def _completed_row(
    golden: Mapping[str, Any], candidates: Sequence[Any], reranked: Sequence[Any], cutoff: float
) -> dict[str, Any]:
    top_three = [_node_evidence(item) for item in reranked]
    scores = [item["score"] for item in top_three if item["score"] is not None]
    highest_score = max(scores) if scores else None
    expected_docs = {str(value) for value in golden.get("expected_docs", [])}
    gold_hit = any(item["file_name"] in expected_docs for item in top_three)
    if not top_three:
        filter_reason = "no_candidates"
    elif highest_score is not None and highest_score < cutoff:
        filter_reason = "below_similarity_cutoff"
    else:
        filter_reason = "retained"
    return {
        "case_id": _case_id(golden),
        "status": "completed",
        "candidate_count": len(candidates),
        "reranked_top3": top_three,
        "highest_rerank_score": highest_score,
        "gold_doc_in_reranked_top3": gold_hit,
        "filtered_at_configured_cutoff": filter_reason == "below_similarity_cutoff",
        "filter_reason": filter_reason,
    }


def _node_evidence(value: Any) -> dict[str, str | float | None]:
    node = getattr(value, "node", value)
    metadata = getattr(node, "metadata", None)
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    chunk_id = getattr(node, "node_id", None) or getattr(node, "id_", None)
    file_name = (
        metadata.get("file_name")
        or metadata.get("filename")
        or metadata.get("file_path")
        or "unknown"
    )
    score = getattr(value, "score", None)
    if score is None and value is not node:
        score = getattr(node, "score", None)
    return {
        "chunk_id": str(chunk_id or ""),
        "file_name": str(file_name),
        "score": _number(score),
    }


def _summarize(
    rows: Sequence[Mapping[str, Any]], cutoff: float, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    completed = [row for row in rows if row.get("status") == "completed"]
    threshold_summary: dict[str, dict[str, int]] = {}
    for threshold in AUDIT_THRESHOLDS:
        no_candidates = sum(not row.get("reranked_top3") for row in completed)
        retained = sum(
            bool(row.get("reranked_top3"))
            and isinstance(row.get("highest_rerank_score"), (int, float))
            and row["highest_rerank_score"] >= threshold
            for row in completed
        )
        filtered = sum(
            bool(row.get("reranked_top3"))
            and isinstance(row.get("highest_rerank_score"), (int, float))
            and row["highest_rerank_score"] < threshold
            for row in completed
        )
        gold_retained = sum(
            row.get("gold_doc_in_reranked_top3") is True
            and isinstance(row.get("highest_rerank_score"), (int, float))
            and row["highest_rerank_score"] >= threshold
            for row in completed
        )
        threshold_summary[f"{threshold:.1f}"] = {
            "retained_count": retained,
            "filtered_count": filtered,
            "no_candidate_count": no_candidates,
            "gold_top3_retained_count": gold_retained,
        }
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "status": "degraded" if any(row.get("status") == "audit_error" for row in rows) else "completed",
        "case_count": len(rows),
        "completed_count": len(completed),
        "skipped_count": sum(row.get("status") == "skipped_not_applicable" for row in rows),
        "error_count": sum(row.get("status") == "audit_error" for row in rows),
        "configured_similarity_cutoff": cutoff,
        "thresholds": threshold_summary,
        "knowledge_snapshot": manifest["knowledge_snapshot"],
    }


def _load_case_results(
    root: Path, input_manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata = input_manifest.get("case_results")
    if not isinstance(metadata, Mapping):
        raise ValueError("deterministic input is missing case_results metadata")
    uri = _safe_relative_uri(metadata.get("uri"), "case_results.uri")
    expected_sha = _non_empty_string(metadata.get("sha256"), "case_results.sha256")
    path = _resolve_under_root(root, uri)
    actual_sha = _sha256_file(path)
    if actual_sha != expected_sha:
        raise ValueError("deterministic case_results hash mismatch")
    return (
        {"uri": uri.as_posix(), "sha256": actual_sha, "verified": True},
        load_jsonl(path),
    )


def _load_golden_rows(
    input_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata = input_manifest.get("golden_set")
    if not isinstance(metadata, Mapping):
        raise ValueError("deterministic input is missing golden_set metadata")
    path = Path(_non_empty_string(metadata.get("uri"), "golden_set.uri"))
    if not path.is_file():
        raise ValueError("deterministic golden_set is unavailable")
    expected_sha = _non_empty_string(metadata.get("sha256"), "golden_set.sha256")
    actual_sha = _sha256_file(path)
    if actual_sha != expected_sha:
        raise ValueError("deterministic golden_set hash mismatch")
    return ({"sha256": actual_sha, "case_count": len(load_jsonl(path))}, load_jsonl(path))


def _load_published_snapshot(
    root: Path, input_manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = input_manifest.get("knowledge_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("deterministic input is missing knowledge_snapshot")
    build_id = _non_empty_string(snapshot.get("index_build_id"), "index_build_id")
    collection = _non_empty_string(snapshot.get("collection_name"), "collection_name")
    manifest = read_published_chunk_manifest(root, build_id)
    lifecycle = manifest.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or lifecycle.get("collection_name") != collection:
        raise ValueError("published chunk collection does not match deterministic snapshot")
    artifacts = manifest.get("artifacts")
    chunks = artifacts.get("chunks_jsonl") if isinstance(artifacts, Mapping) else None
    if not isinstance(chunks, Mapping):
        raise ValueError("published chunk manifest is missing chunks_jsonl")
    chunks_uri = _safe_relative_uri(chunks.get("uri"), "chunks_jsonl.uri")
    chunks_path = _resolve_under_root(root, chunks_uri)
    chunks_sha = _sha256_file(chunks_path)
    if chunks_sha != chunks.get("sha256") or chunks_sha != snapshot.get("chunks_sha256"):
        raise ValueError("published chunk hash does not match deterministic snapshot")
    chunk_rows = load_jsonl(chunks_path)
    if any(row.get("index_build_id") != build_id for row in chunk_rows):
        raise ValueError("chunk rows do not match published index_build_id")
    return (
        dict(snapshot),
        {"uri": chunks_uri.as_posix(), "sha256": chunks_sha, "verified": True},
    )


def _validate_case_alignment(
    golden_rows: Sequence[Mapping[str, Any]], deterministic_rows: Sequence[Mapping[str, Any]]
) -> None:
    golden_ids = [_case_id(row) for row in golden_rows]
    deterministic_ids = [_case_id(row) for row in deterministic_rows]
    if len(set(golden_ids)) != len(golden_ids):
        raise ValueError("golden_set contains duplicate case IDs")
    if len(set(deterministic_ids)) != len(deterministic_ids):
        raise ValueError("case_results contains duplicate case IDs")
    if set(golden_ids) != set(deterministic_ids):
        raise ValueError("case_results case IDs do not match golden_set")


def _skip_reason(golden: Mapping[str, Any]) -> str | None:
    if golden.get("scope") == "agent_tool_e2e" or golden.get("expected_tool"):
        return "tool_case"
    if golden.get("unanswerable") is True:
        return "refusal_case"
    expected_docs = golden.get("expected_docs")
    if not isinstance(expected_docs, list) or not expected_docs:
        return "no_rag_documents"
    if not isinstance(golden.get("query"), str) or not golden["query"].strip():
        return "missing_query"
    return None


def _query(golden: Mapping[str, Any], case_id: str) -> str:
    query = golden.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"case {case_id} has no query")
    return query


def _case_id(row: Mapping[str, Any]) -> str:
    return _non_empty_string(row.get("id") or row.get("case_id"), "case_id")


def _configured_cutoff(value: float | None) -> float:
    if value is None:
        from metro_agent.llama_config import SIMILARITY_CUTOFF

        value = SIMILARITY_CUTOFF
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError("configured similarity cutoff must be a number in [0, 1]")
    return float(value)


def _build_local_adapter() -> tuple[Callable[[str], Sequence[Any]], Callable[[Sequence[Any], str], Sequence[Any]]]:
    from metro_agent.tools.Knowledge_RAGtools import (
        build_storage_context,
        get_chroma_client,
        get_published_collection,
        resolve_published_collection_name,
    )
    from metro_agent.llama_config import ALPHA, SIMILARITY_TOP_K, SPARSE_TOP_K, init_llama_index_components
    from llama_index.core import VectorStoreIndex

    client = get_chroma_client()
    collection_name = resolve_published_collection_name(client)
    collection = get_published_collection(client, collection_name)
    storage_context = build_storage_context(collection)
    reranker = init_llama_index_components()
    index = VectorStoreIndex.from_vector_store(
        vector_store=storage_context.vector_store,
        storage_context=storage_context,
    )
    retriever = index.as_retriever(
        vector_store_query_mode="hybrid",
        similarity_top_k=SIMILARITY_TOP_K,
        sparse_top_k=SPARSE_TOP_K,
        alpha=ALPHA,
    )
    return (
        lambda query: retriever.retrieve(query),
        lambda nodes, query: reranker.postprocess_nodes(nodes, query_str=query),
    )


def _report(suite: str, summary: Mapping[str, Any]) -> str:
    lines = [f"# RAG 检索审计：{suite}", "", "- 查询变换：`original_query_only`", "- 不调用外部 LLM，不生成答案。", f"- 当前线上阈值：`{summary['configured_similarity_cutoff']:.1f}`", "", "| 阈值 | 保留 | 过滤 | 无候选 | 金标 Top 3 且保留 |", "| --- | ---: | ---: | ---: | ---: |"]
    for threshold, values in summary["thresholds"].items():
        lines.append(
            f"| {threshold} | {values['retained_count']} | {values['filtered_count']} | {values['no_candidate_count']} | {values['gold_top3_retained_count']} |"
        )
    lines.extend([
        "",
        "该结果用于阈值标定；因未执行线上 HyDE 生成，不能视为与线上问答逐字一致的回放。",
        "",
    ])
    return "\n".join(lines)


def _safe_relative_uri(value: Any, name: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{name} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{name} must be a safe relative path")
    return path


def _resolve_under_root(root: Path, relative: PurePosixPath) -> Path:
    resolved_root = root.resolve()
    path = (resolved_root / Path(relative)).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("artifact path escapes artifact_root") from exc
    return path


def _safe_segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\\/:") or value in {".", ".."}:
        raise ValueError(f"{name} must be a safe path segment")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., AuditRun] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run a local RAG retrieval audit")
    parser.add_argument("--deterministic-artifact", required=True)
    parser.add_argument("--artifact-root", default="artifacts")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    selected_runner = runner or run_retrieval_audit
    run = selected_runner(
        deterministic_artifact=args.deterministic_artifact,
        artifact_root=args.artifact_root,
        suite=args.suite,
        run_id=args.run_id,
    )
    print(json.dumps({"run_id": run.run_id, "artifact_uri": run.artifact_uri, "summary": run.summary}, ensure_ascii=False))
    return 1 if run.summary.get("error_count") else 0


if __name__ == "__main__":
    raise SystemExit(main())
