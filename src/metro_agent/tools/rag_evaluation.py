"""Offline scoring for reviewed Metro Agent RAG evaluation fixtures."""

import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from metro_agent.tools.chunk_artifacts import read_published_chunk_manifest
from metro_agent.observability.artifacts import write_json, write_jsonl, write_markdown


DETERMINISTIC_RAG_SCHEMA_VERSION = "deterministic-rag-v1"
METRIC_DEFINITIONS = {
    "precision_at_k": "Relevant retrieved documents divided by all retrieved documents.",
    "recall_at_k": "Relevant expected documents retrieved divided by expected documents.",
    "f1_at_k": "Harmonic mean of document precision_at_k and recall_at_k.",
    "mrr": "Mean reciprocal rank of the first expected document.",
    "ndcg_at_k": "Discounted cumulative gain normalised by the ideal ranking at retrieved K.",
    "evidence_pass_rate": "Share of answers containing every expected evidence phrase.",
    "overall_pass_rate": "Share of all cases whose scope-specific checks pass.",
    "refusal_pass_rate": "Share of unanswerable cases that refuse without retrieved sources.",
    "tool_selection_pass_rate": "Share of tool cases selecting the expected tool.",
    "tool_parameter_pass_rate": "Share of tool cases with an exact expected query.",
    "tool_payload_evidence_pass_rate": "Share of tool cases whose payload contains expected evidence.",
}
DEFAULT_GOLDEN_PATH = Path("tests/rag_eval/golden_set_full.jsonl")
TOKEN_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "estimated_cost",
)


REFUSAL_PHRASES = ("没有", "未提供", "无法", "不包含")


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load non-empty UTF-8 JSONL rows and report malformed line numbers."""
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            text = raw_line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 第 {line_number} 行格式错误: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL 第 {line_number} 行必须是对象")
            rows.append(row)
    return rows


def _duplicate_ids(rows: list[dict[str, Any]]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in rows:
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise ValueError("样本缺少有效 id")
        if row_id in seen:
            duplicates.add(row_id)
        seen.add(row_id)
    return duplicates


def _require_list(row: dict[str, Any], field: str) -> list[Any]:
    value = row.get(field)
    if not isinstance(value, list):
        raise ValueError(f"样本 {row['id']} 的 {field} 必须是列表")
    return value


def validate_rows(
    golden_rows: list[dict[str, Any]], result_rows: list[dict[str, Any]]
) -> None:
    """Validate schema essentials and require a one-to-one ID alignment."""
    golden_duplicates = _duplicate_ids(golden_rows)
    result_duplicates = _duplicate_ids(result_rows)
    if golden_duplicates:
        raise ValueError(f"金标集存在重复 id: {sorted(golden_duplicates)}")
    if result_duplicates:
        raise ValueError(f"结果集存在重复 id: {sorted(result_duplicates)}")

    for row in golden_rows:
        for field in ("scope", "category", "expected_docs", "expected_evidence", "unanswerable"):
            if field not in row:
                raise ValueError(f"金标样本 {row['id']} 缺少字段 {field}")
        _require_list(row, "expected_docs")
        _require_list(row, "expected_evidence")
        if not isinstance(row["unanswerable"], bool):
            raise ValueError(f"金标样本 {row['id']} 的 unanswerable 必须是布尔值")
        _validate_optional_expectations(row)

    for row in result_rows:
        for field in ("source_docs", "answer"):
            if field not in row:
                raise ValueError(f"结果样本 {row['id']} 缺少字段 {field}")
        _require_list(row, "source_docs")
        if not isinstance(row["answer"], str):
            raise ValueError(f"结果样本 {row['id']} 的 answer 必须是字符串")

    golden_ids = {row["id"] for row in golden_rows}
    result_ids = {row["id"] for row in result_rows}
    missing_ids = golden_ids - result_ids
    unknown_ids = result_ids - golden_ids
    if missing_ids:
        raise ValueError(f"结果集缺少 id: {sorted(missing_ids)}")
    if unknown_ids:
        raise ValueError(f"结果集包含未知 id: {sorted(unknown_ids)}")


def evidence_passes(answer: str, expected_evidence: list[str]) -> bool:
    normalized_answer = answer.casefold()
    return all(term.casefold() in normalized_answer for term in expected_evidence)


def document_rank(source_docs: list[str], expected_docs: list[str]) -> int | None:
    ranks = [source_docs.index(name) + 1 for name in expected_docs if name in source_docs]
    return min(ranks) if ranks else None


def _is_refusal(answer: str) -> bool:
    return any(phrase in answer for phrase in REFUSAL_PHRASES)


def _rate(passed: int, total: int) -> float | None:
    if total == 0:
        return None
    return round(passed / total, 4)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _validate_optional_expectations(row: dict[str, Any]) -> None:
    list_fields = ("expected_agents", "expected_tools")
    text_fields = ("reference_answer", "answer_rubric")
    number_fields = ("latency_budget_ms", "token_budget")
    for field in list_fields:
        if field in row and not isinstance(row[field], list):
            raise ValueError(f"金标样本 {row['id']} 的 {field} 必须是列表")
    if "expected_plan" in row and not isinstance(row["expected_plan"], dict):
        raise ValueError(f"金标样本 {row['id']} 的 expected_plan 必须是对象")
    for field in text_fields:
        if field in row and not isinstance(row[field], str):
            raise ValueError(f"金标样本 {row['id']} 的 {field} 必须是字符串")
    for field in number_fields:
        value = row.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f"金标样本 {row['id']} 的 {field} 必须是数字")


def _retrieval_metrics(source_docs: list[str], expected_docs: list[str]) -> tuple[float, float, float, float]:
    expected = set(expected_docs)
    relevant = [doc for doc in source_docs if doc in expected]
    precision = len(relevant) / len(source_docs) if source_docs else 0.0
    recall = len(set(relevant)) / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    dcg = sum(1 / math.log2(index + 2) for index, doc in enumerate(source_docs) if doc in expected)
    ideal_count = min(len(expected), len(source_docs))
    ideal_dcg = sum(1 / math.log2(index + 2) for index in range(ideal_count))
    ndcg = dcg / ideal_dcg if ideal_dcg else 0.0
    return precision, recall, f1, ndcg


def evaluate_rows(
    golden_rows: list[dict[str, Any]], result_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Score RAG, refusal, and tool fixtures without external dependencies."""
    validate_rows(golden_rows, result_rows)
    results_by_id = {row["id"]: row for row in result_rows}
    category_totals: dict[str, int] = defaultdict(int)
    category_passes: dict[str, int] = defaultdict(int)
    rag_ranks: list[int | None] = []
    rag_evidence: list[bool] = []
    rag_precision: list[float] = []
    rag_recall: list[float] = []
    rag_f1: list[float] = []
    rag_ndcg: list[float] = []
    refusal_passes: list[bool] = []
    tool_selection: list[bool] = []
    tool_parameters: list[bool] = []
    tool_payloads: list[bool] = []
    overall_passes: list[bool] = []

    for golden in golden_rows:
        result = results_by_id[golden["id"]]
        scope = golden["scope"]
        category = golden["category"]
        category_totals[category] += 1
        answer = result["answer"]
        source_docs = result["source_docs"]

        if scope == "agent_tool_e2e":
            selected = result.get("tool_name") == golden.get("expected_tool")
            parameters = result.get("tool_query") == golden.get("expected_query")
            payload_text = json.dumps(result.get("tool_result", {}), ensure_ascii=False)
            payload = evidence_passes(payload_text, golden["expected_evidence"])
            row_passed = selected and parameters and payload
            tool_selection.append(selected)
            tool_parameters.append(parameters)
            tool_payloads.append(payload)
        elif golden["unanswerable"]:
            row_passed = not source_docs and _is_refusal(answer)
            refusal_passes.append(row_passed)
        else:
            rank = document_rank(source_docs, golden["expected_docs"])
            evidence = evidence_passes(answer, golden["expected_evidence"])
            precision, recall, f1, ndcg = _retrieval_metrics(source_docs, golden["expected_docs"])
            rag_ranks.append(rank)
            rag_evidence.append(evidence)
            rag_precision.append(precision)
            rag_recall.append(recall)
            rag_f1.append(f1)
            rag_ndcg.append(ndcg)
            row_passed = rank is not None and evidence

        if row_passed:
            category_passes[category] += 1
        overall_passes.append(row_passed)

    category_report = {
        category: {
            "count": total,
            "pass_count": category_passes[category],
            "pass_rate": _rate(category_passes[category], total),
        }
        for category, total in sorted(category_totals.items())
    }
    hit_count = sum(rank is not None for rank in rag_ranks)
    reciprocal_ranks = [1 / rank if rank is not None else 0.0 for rank in rag_ranks]
    return {
        "total_rows": len(golden_rows),
        "overall_pass_rate": _rate(sum(overall_passes), len(overall_passes)),
        "overall": {
            "count": len(overall_passes),
            "pass_count": sum(overall_passes),
            "pass_rate": _rate(sum(overall_passes), len(overall_passes)),
        },
        "categories": category_report,
        "rag": {
            "count": len(rag_ranks),
            "precision_at_k": _mean(rag_precision),
            "recall_at_k": _mean(rag_recall),
            "f1_at_k": _mean(rag_f1),
            "mrr": _mean(reciprocal_ranks),
            "ndcg_at_k": _mean(rag_ndcg),
            "evidence_pass_rate": _rate(sum(rag_evidence), len(rag_evidence)),
        },
        "refusal": {
            "count": len(refusal_passes),
            "pass_rate": _rate(sum(refusal_passes), len(refusal_passes)),
        },
        "tool": {
            "count": len(tool_selection),
            "selection_pass_rate": _rate(sum(tool_selection), len(tool_selection)),
            "parameter_pass_rate": _rate(sum(tool_parameters), len(tool_parameters)),
            "payload_evidence_pass_rate": _rate(sum(tool_payloads), len(tool_payloads)),
        },
    }


class DeterministicRagRun:
    def __init__(self, *, run_id: str, artifact_uri: str, summary: dict[str, Any]) -> None:
        self.run_id = run_id
        self.artifact_uri = artifact_uri
        self.summary = summary


def run_deterministic_rag_suite(
    *,
    golden_path: Path | str = DEFAULT_GOLDEN_PATH,
    result_rows: list[dict[str, Any]],
    artifact_root: Path | str,
    index_build_id: str,
    suite: str,
    run_id: str | None = None,
    judge_error: Exception | str | None = None,
) -> DeterministicRagRun:
    """Score a reviewed golden set against one published Chunk snapshot.

    This deterministic runner never converts a judge request failure into a
    numeric zero. Semantic judge calls stay in later evaluation runners.
    """
    safe_suite = _safe_path_segment(suite, "suite")
    resolved_run_id = _safe_path_segment(run_id or _new_run_id(), "run_id")
    golden_file = Path(golden_path)
    golden_rows = load_jsonl(golden_file)
    validate_rows(golden_rows, result_rows)
    snapshot = _read_published_snapshot(Path(artifact_root), index_build_id)
    results_by_id = {row["id"]: row for row in result_rows}
    rows: list[dict[str, Any]] = []
    for golden in golden_rows:
        result = results_by_id[golden["id"]]
        deterministic = evaluate_rows([golden], [result])
        case_metrics = _with_rag_metric_aliases(deterministic)
        judge = _judge_artifact(judge_error)
        retrieved_chunk_ids, retrieved_chunks = _retrieved_chunk_payload(result)
        rows.append(
            {
                "case_id": golden["id"],
                "scope": golden["scope"],
                "category": golden["category"],
                "query": golden.get("query", ""),
                "source_docs": result["source_docs"],
                "answer": result["answer"],
                "retrieved_chunk_ids": retrieved_chunk_ids,
                "retrieved_chunks": retrieved_chunks,
                "deterministic_metrics": case_metrics,
                "overall_passed": deterministic["overall"]["pass_count"] == 1,
                "metric_definitions": _case_metric_definitions(golden),
                "judge": judge,
                "token_usage": _token_usage_payload(result.get("token_usage")),
            }
        )

    root = Path(artifact_root)
    artifact_uri = f"evals/rag/{safe_suite}/{resolved_run_id}"
    case_results_ref = write_jsonl(root, f"{artifact_uri}/case_results.jsonl", rows)
    case_results = {
        "uri": case_results_ref.uri,
        "sha256": case_results_ref.sha256,
    }
    summary = _suite_summary(
        golden_rows,
        result_rows,
        rows,
        snapshot,
        judge_error,
        case_results,
    )
    write_json(
        root,
        f"{artifact_uri}/input_manifest.json",
        {
            "schema_version": DETERMINISTIC_RAG_SCHEMA_VERSION,
            "suite": safe_suite,
            "run_id": resolved_run_id,
            "golden_set": {
                "uri": str(golden_file),
                "sha256": hashlib.sha256(golden_file.read_bytes()).hexdigest(),
                "case_count": len(golden_rows),
            },
            "knowledge_snapshot": snapshot,
            "case_results": case_results,
            "metric_definitions": METRIC_DEFINITIONS,
        },
    )
    write_json(root, f"{artifact_uri}/summary.json", summary)
    write_markdown(root, f"{artifact_uri}/report.md", _suite_report(safe_suite, summary))
    return DeterministicRagRun(
        run_id=resolved_run_id, artifact_uri=artifact_uri, summary=summary
    )


def _read_published_snapshot(artifact_root: Path, index_build_id: str) -> dict[str, Any]:
    manifest = read_published_chunk_manifest(artifact_root, index_build_id)
    artifacts = manifest.get("artifacts")
    chunks = artifacts.get("chunks_jsonl") if isinstance(artifacts, dict) else None
    chunks_uri = chunks.get("uri") if isinstance(chunks, dict) else None
    if not isinstance(chunks_uri, str):
        raise ValueError("published chunk manifest is missing chunks_jsonl")
    root = artifact_root.resolve()
    chunks_path = (root / chunks_uri).resolve()
    try:
        chunks_path.relative_to(root)
    except ValueError as error:
        raise ValueError("published chunk manifest references an unsafe chunks path") from error
    chunk_rows = load_jsonl(chunks_path)
    if any(row.get("index_build_id") != index_build_id for row in chunk_rows):
        raise ValueError("published chunk manifest rows do not match index_build_id")
    lifecycle = manifest["lifecycle"]
    return {
        "index_build_id": index_build_id,
        "collection_name": lifecycle["collection_name"],
        "manifest_uri": f"chunks/{index_build_id}/manifest.json",
        "chunks_uri": chunks_uri,
        "chunks_sha256": chunks.get("sha256"),
        "chunk_count": len(chunk_rows),
    }


def _token_usage_payload(value: Any) -> dict[str, float | int | None]:
    usage: dict[str, float | int | None] = {
        field: None for field in TOKEN_USAGE_FIELDS
    }
    if not isinstance(value, dict):
        return usage
    for field in TOKEN_USAGE_FIELDS:
        item = value.get(field)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            usage[field] = item
    return usage


def _with_rag_metric_aliases(metrics: dict[str, Any]) -> dict[str, Any]:
    """Keep the full report while preserving the original flat RAG metric API."""
    result = dict(metrics)
    rag = metrics.get("rag")
    if not isinstance(rag, dict):
        return result
    for name in (
        "precision_at_k",
        "recall_at_k",
        "f1_at_k",
        "mrr",
        "ndcg_at_k",
        "evidence_pass_rate",
    ):
        result[name] = rag.get(name)
    return result


def _retrieved_chunk_payload(
    result: dict[str, Any],
) -> tuple[list[str], list[dict[str, float | int | str | None]]]:
    """Preserve the retrieval ordering from the result without saving chunk text."""
    candidates = result.get("sources")
    if not isinstance(candidates, list):
        candidates = result.get("retrieved_chunks")
    if not isinstance(candidates, list):
        candidates = result.get("retrieved_chunk_ids", [])
    if not isinstance(candidates, list):
        return [], []

    chunk_ids: list[str] = []
    chunks: list[dict[str, float | int | str | None]] = []
    for position, candidate in enumerate(candidates, start=1):
        if isinstance(candidate, dict):
            chunk_id = candidate.get("chunk_id")
            rank = candidate.get("rank")
            score = candidate.get("score")
        else:
            chunk_id = candidate
            rank = position
            score = None
        if not isinstance(chunk_id, str) or not chunk_id:
            continue
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            rank = position
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            score = None
        chunk_ids.append(chunk_id)
        chunks.append({"chunk_id": chunk_id, "rank": rank, "score": score})
    return chunk_ids, chunks


def _suite_summary(
    golden_rows: list[dict[str, Any]],
    result_rows: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
    judge_error: Exception | str | None,
    case_results: dict[str, str],
) -> dict[str, Any]:
    report = evaluate_rows(golden_rows, result_rows)
    token_totals: dict[str, float | int] = {
        field: 0 for field in TOKEN_USAGE_FIELDS
    }
    for row in rows:
        for field, value in row["token_usage"].items():
            if value is not None:
                token_totals[field] += value
    return {
        "schema_version": DETERMINISTIC_RAG_SCHEMA_VERSION,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "deterministic_metrics": report,
        "metric_definitions": METRIC_DEFINITIONS,
        "knowledge_snapshot": snapshot,
        "case_results": case_results,
        "token_usage": dict(token_totals),
        "judge": _judge_artifact(judge_error),
    }


def _case_metric_definitions(golden: dict[str, Any]) -> dict[str, str]:
    names = {"overall_pass_rate"}
    if golden["scope"] == "agent_tool_e2e":
        names.update(
            {
                "tool_selection_pass_rate",
                "tool_parameter_pass_rate",
                "tool_payload_evidence_pass_rate",
            }
        )
    elif golden["unanswerable"]:
        names.add("refusal_pass_rate")
    else:
        names.update(
            {
                "precision_at_k",
                "recall_at_k",
                "f1_at_k",
                "mrr",
                "ndcg_at_k",
                "evidence_pass_rate",
            }
        )
    return {name: METRIC_DEFINITIONS[name] for name in sorted(names)}


def _judge_artifact(error: Exception | str | None) -> dict[str, str]:
    if error is None:
        return {"status": "not_requested"}
    error_type = type(error).__name__ if isinstance(error, Exception) else str(error)
    if error_type == "JudgeConfigurationError":
        return {"status": "configuration_error", "error_type": error_type}
    return {"status": "evaluation_error", "error_type": error_type}


def _suite_report(suite: str, summary: dict[str, Any]) -> str:
    metrics = summary["deterministic_metrics"]
    rag = metrics["rag"]
    lines = [f"# Deterministic RAG evaluation: {suite}", "", "## Retrieval", ""]
    lines.extend(f"- {name}: {value}" for name, value in rag.items())
    lines.extend(["", "## Token usage", ""])
    lines.extend(f"- {name}: {value}" for name, value in summary["token_usage"].items())
    lines.extend(["", "## Judge", "", f"- Status: {summary['judge']['status']}", ""])
    return "\n".join(lines)


def _safe_path_segment(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char in value for char in "\\\\/:")
        or value in {".", ".."}
    ):
        raise ValueError(f"{name} must be a safe path segment")
    return value


def _new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="离线评估 Metro Agent RAG 结果")
    parser.add_argument("--golden", required=True, help="金标 JSONL 文件路径")
    parser.add_argument("--results", required=True, help="系统结果 JSONL 文件路径")
    args = parser.parse_args()

    report = evaluate_rows(load_jsonl(args.golden), load_jsonl(args.results))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
