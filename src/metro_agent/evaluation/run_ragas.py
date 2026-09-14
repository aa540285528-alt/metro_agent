"""RAGAS evaluation runner backed by Task 5 deterministic artifacts.

The runner never invokes the application RAG path.  It evaluates the exact
answer/source-document snapshot saved by the deterministic suite and obtains
context text only from the immutable, published chunk bundle it references.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Any, Callable, Protocol
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

from metro_agent.tools.chunk_artifacts import read_published_chunk_manifest
from metro_agent.tools.rag_evaluation import TOKEN_USAGE_FIELDS, load_jsonl
from evaluation.qwen_judge import JudgeConfigurationError, JudgeRequestError, QwenJudge
from evaluation.qwen_semantic_judge import (
    METRIC_NAMES as SEMANTIC_METRIC_NAMES,
    QwenSemanticJudge,
    SEMANTIC_JUDGE_PROVIDER,
    SEMANTIC_RUBRIC_VERSION,
)
from metro_agent.observability.artifacts import write_json, write_jsonl, write_markdown
from metro_agent.observability.llm_usage import extract_llm_usage


RAGAS_RUN_SCHEMA_VERSION = "ragas-run-v2"
RAGAS_RUBRIC_VERSION = "ragas-metrics-v1"
ANSWER_CORRECTNESS_WEIGHTS = [1.0, 0.0]
METRIC_NAMES = SEMANTIC_METRIC_NAMES
METRIC_DEFINITIONS = {
    "faithfulness": "Qwen semantic compatibility score for answer support by retrieved chunks.",
    "answer_correctness": "Qwen semantic compatibility score against the golden evidence.",
    "answer_relevancy": "Qwen semantic compatibility score for answering the query.",
    "context_precision": "Qwen semantic compatibility score for retrieved-context relevance.",
    "context_recall": "Qwen semantic compatibility score for retrieved-context evidence coverage.",
}


class RagasDependencyError(RuntimeError):
    """Raised when the optional RAGAS runtime cannot be loaded."""


class RagasExecutionError(RuntimeError):
    """Raised when RAGAS or its configured Qwen providers cannot evaluate."""


class RetrievedChunkInputError(ValueError):
    """Raised when deterministic artifacts cannot reproduce the retrieved order."""


class CaseResultsInputError(ValueError):
    """Raised before parsing a deterministic case-results artifact."""


class RagasBackend(Protocol):
    provider: str
    version: str

    def evaluate(self, samples: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class RagasRunResult:
    run_id: str
    artifact_uri: str
    summary: dict[str, Any]


class QwenSemanticCompatBackend:
    """One Qwen request per answerable sample, without importing native RAGAS."""

    provider = SEMANTIC_JUDGE_PROVIDER
    version = SEMANTIC_RUBRIC_VERSION

    def __init__(self, judge: QwenSemanticJudge) -> None:
        self._judge = judge

    @property
    def judge_config(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self._judge.model,
            "base_url": self._judge.base_url,
            "temperature": self._judge.temperature,
            "timeout_seconds": self._judge.timeout_seconds,
            "max_retries": self._judge.max_retries,
            "prompt_version": self._judge.prompt_version,
            "rubric_version": SEMANTIC_RUBRIC_VERSION,
        }

    def evaluate(self, samples: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for sample in samples:
            result = self._judge.judge(sample)
            outputs.append(
                {
                    "metrics": result.metrics,
                    "judge": {
                        "provider": result.provider,
                        "model": result.model,
                        "prompt_version": result.prompt_version,
                        "rubric_version": result.rubric_version,
                        "pass": result.passed,
                        "reason": result.reason,
                        "evidence": result.evidence,
                        "token_usage": result.token_usage,
                        "latency_ms": result.latency_ms,
                        "estimated_cost": result.estimated_cost,
                    },
                }
            )
        return outputs


class NativeRagasBackend:
    """Lazy RAGAS adapter using the configured Qwen model as its LLM judge."""

    provider = "ragas"

    def __init__(self, judge: QwenJudge) -> None:
        self._judge = judge
        try:
            self.version = importlib.metadata.version("ragas")
        except importlib.metadata.PackageNotFoundError:
            self.version = "unavailable"

    @property
    def judge_config(self) -> dict[str, Any]:
        return {
            "model": self._judge.model,
            "base_url": self._judge.base_url,
            "temperature": self._judge.temperature,
            "timeout_seconds": self._judge.timeout_seconds,
            "max_retries": self._judge.max_retries,
            "prompt_version": self._judge.prompt_version,
            "rubric_version": RAGAS_RUBRIC_VERSION,
        }

    def evaluate(self, samples: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
        try:
            from datasets import Dataset
            from ragas import evaluate
            from ragas.metrics import (
                AnswerCorrectness,
                ContextEntityRecall,
                ContextRecall,
                Faithfulness,
                LLMContextPrecisionWithReference,
                ResponseRelevancy,
            )
        except Exception as error:
            raise RagasDependencyError(f"RAGAS runtime unavailable: {type(error).__name__}") from error

        try:
            metrics: list[Any] = [
                LLMContextPrecisionWithReference(name="context_precision"),
                ContextRecall(name="context_recall"),
                ContextEntityRecall(name="context_entity_recall"),
                Faithfulness(name="faithfulness"),

                AnswerCorrectness(
                    name="answer_correctness", weights=ANSWER_CORRECTNESS_WEIGHTS
                ),
            ]
            embeddings = _build_qwen_embeddings(self._judge)
            if embeddings is not None:
                metrics.append(ResponseRelevancy(name="answer_relevancy"))
            records: list[dict[str, Any]] = []
            usages: list[dict[str, float | int | None]] = []
            latencies: list[float] = []
            for sample in samples:
                usage_collector = _QwenUsageCollector()
                started_at = perf_counter()
                result = evaluate(
                    Dataset.from_list([dict(sample)]),
                    metrics=metrics,
                    llm=self._judge._build_llm(),
                    embeddings=embeddings,
                    callbacks=[usage_collector],
                    raise_exceptions=False,
                    show_progress=False,
                )
                records.extend(result.to_pandas().to_dict(orient="records"))
                usages.append(usage_collector.usage)
                latencies.append(round((perf_counter() - started_at) * 1000, 3))
        except (RagasDependencyError, JudgeConfigurationError):
            raise
        except Exception as error:
            raise RagasExecutionError(f"RAGAS evaluation failed: {type(error).__name__}") from error

        normalized: list[dict[str, Any]] = []
        for record, usage, latency_ms in zip(records, usages, latencies, strict=True):
            scores = {
                name: _numeric_score(record.get(name))
                for name in METRIC_NAMES
                if _numeric_score(record.get(name)) is not None
            }
            normalized.append(
                {
                    "metrics": scores,
                    "judge": {
                        "model": self._judge.model,
                        "prompt_version": self._judge.prompt_version,
                        "token_usage": usage,
                        "latency_ms": latency_ms,
                        "estimated_cost": usage.get("estimated_cost"),
                    },
                }
            )
        return normalized


class _QwenUsageCollector(BaseCallbackHandler):
    """Collect provider usage emitted by RAGAS's LangChain callbacks per case."""

    def __init__(self) -> None:
        self.usage = _unknown_token_usage()

    def on_llm_end(self, response: Any, **_: Any) -> None:
        candidates = [response, getattr(response, "llm_output", None)]
        for generation_group in getattr(response, "generations", []) or []:
            for generation in generation_group:
                message = getattr(generation, "message", None)
                candidates.extend([message, getattr(message, "response_metadata", None)])
        for candidate in candidates:
            extracted = extract_llm_usage(candidate)
            if extracted is not None:
                _accumulate_usage(self.usage, extracted)


def run_ragas_suite(
    *,
    deterministic_artifact_uri: str,
    artifact_root: Path | str = "artifacts",
    suite: str,
    run_id: str | None = None,
    evaluator: RagasBackend | None = None,
) -> RagasRunResult:
    """Run semantic RAGAS metrics from an immutable deterministic-suite input."""
    root = Path(artifact_root)
    source_uri = _safe_relative_uri(deterministic_artifact_uri, "deterministic_artifact_uri")
    source_dir = _resolve_under_root(root, source_uri)
    input_manifest = _read_json(source_dir / "input_manifest.json")
    safe_suite = _safe_path_segment(suite, "suite")
    resolved_run_id = _safe_path_segment(run_id or _new_run_id(), "run_id")
    selected_backend = evaluator or _default_backend()
    case_results_state, case_results_error, case_results_path = _verify_case_results_hash(
        root, input_manifest
    )
    if case_results_error is not None:
        return _write_suite_input_error(
            root=root,
            source_uri=source_uri,
            source_dir=source_dir,
            input_manifest=input_manifest,
            case_results_state=case_results_state,
            backend=selected_backend,
            suite=safe_suite,
            run_id=resolved_run_id,
            error=case_results_error,
        )

    assert case_results_path is not None
    deterministic_rows = load_jsonl(case_results_path)
    snapshot, chunks = _load_published_snapshot(root, input_manifest)
    golden_rows = _load_golden_rows(input_manifest)
    golden_by_id = {str(row["id"]): row for row in golden_rows}

    prepared: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    rows: list[dict[str, Any]] = []
    for deterministic in deterministic_rows:
        case_id = str(deterministic.get("case_id", ""))
        golden = golden_by_id.get(case_id)
        if golden is None:
            rows.append(
                _failure_row(
                    deterministic,
                    ValueError("case is absent from the golden set"),
                    status="input_error",
                    error_type="GoldenSetMismatchError",
                )
            )
            continue
        skip_reason = _ragas_skip_reason(golden)
        if skip_reason is not None:
            rows.append(_skipped_row(deterministic, skip_reason))
            continue
        try:
            sample, retrieval = _to_ragas_sample(deterministic, golden, chunks)
        except RetrievedChunkInputError as error:
            rows.append(_failure_row(deterministic, error, status="input_error"))
            continue
        prepared.append((deterministic, golden, {**sample, **retrieval}))

    if prepared:
        try:
            outputs = list(selected_backend.evaluate([item[2] for item in prepared]))
            if len(outputs) != len(prepared):
                raise RagasExecutionError("RAGAS backend returned a different number of case results")
            for (deterministic, _, sample), output in zip(prepared, outputs, strict=True):
                rows.append(_completed_row(deterministic, sample, output))
        except Exception as error:
            for deterministic, _, sample in prepared:
                rows.append(_failure_row(deterministic, error, sample=sample))

    rows.sort(key=lambda row: row["case_id"])
    artifact_uri = f"evals/rag/{safe_suite}/{resolved_run_id}"
    manifest = _output_manifest(
        source_uri=source_uri,
        source_dir=source_dir,
        input_manifest=input_manifest,
        snapshot=snapshot,
        case_results_state=case_results_state,
        backend=selected_backend,
    )
    summary = _summarize(rows, manifest)
    write_json(root, f"{artifact_uri}/input_manifest.json", manifest)
    write_jsonl(root, f"{artifact_uri}/case_results.jsonl", rows)
    write_json(root, f"{artifact_uri}/summary.json", summary)
    write_markdown(root, f"{artifact_uri}/report.md", _report(safe_suite, summary))
    return RagasRunResult(run_id=resolved_run_id, artifact_uri=artifact_uri, summary=summary)


def _default_backend() -> RagasBackend:
    try:
        return QwenSemanticCompatBackend(QwenSemanticJudge())
    except Exception as error:
        return _UnavailableBackend(error, judge_config=_environment_judge_config())


class _UnavailableBackend:
    provider = SEMANTIC_JUDGE_PROVIDER
    version = "unavailable"

    def __init__(self, error: Exception, *, judge_config: Mapping[str, Any]) -> None:
        self._error = error
        self.judge_config = dict(judge_config)

    def evaluate(self, samples: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
        raise self._error


def _environment_judge_config() -> dict[str, Any]:
    return {
        "provider": SEMANTIC_JUDGE_PROVIDER,
        "model": os.getenv("EVAL_JUDGE_MODEL"),
        "base_url": os.getenv("EVAL_JUDGE_BASE_URL"),
        "temperature": os.getenv("EVAL_JUDGE_TEMPERATURE", "0"),
        "timeout_seconds": os.getenv("EVAL_JUDGE_TIMEOUT_SECONDS"),
        "max_retries": os.getenv("EVAL_JUDGE_MAX_RETRIES"),
        "prompt_version": "semantic-v1",
        "rubric_version": SEMANTIC_RUBRIC_VERSION,
    }


def _verify_case_results_hash(
    root: Path, input_manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], CaseResultsInputError | None, Path | None]:
    metadata = input_manifest.get("case_results")
    if not isinstance(metadata, Mapping):
        return (
            {"uri": None, "expected_sha256": None, "actual_sha256": None, "verified": False},
            CaseResultsInputError(
                "MissingCaseResultsMetadataError: Task 5 input_manifest lacks case_results"
            ),
            None,
        )
    uri = metadata.get("uri")
    expected = metadata.get("sha256")
    state = {
        "uri": uri if isinstance(uri, str) else None,
        "expected_sha256": expected if isinstance(expected, str) else None,
        "actual_sha256": None,
        "verified": False,
    }
    if not isinstance(uri, str) or not uri:
        return state, CaseResultsInputError(
            "MissingCaseResultsUriError: Task 5 case_results is missing uri"
        ), None
    if not isinstance(expected, str) or not expected:
        return state, CaseResultsInputError(
            "MissingCaseResultsHashError: Task 5 case_results is missing sha256"
        ), None
    try:
        case_path = _resolve_under_root(root, _safe_relative_uri(uri, "case_results.uri"))
        actual = _sha256_file(case_path)
    except Exception as error:
        return state, CaseResultsInputError(
            f"CaseResultsArtifactError: unable to read Task 5 case_results ({type(error).__name__})"
        ), None
    state["actual_sha256"] = actual
    if actual != expected:
        return state, CaseResultsInputError(
            "CaseResultsHashMismatchError: Task 5 case_results.jsonl hash does not match input_manifest"
        ), None
    state["verified"] = True
    return state, None, case_path


def _write_suite_input_error(
    *,
    root: Path,
    source_uri: PurePosixPath,
    source_dir: Path,
    input_manifest: Mapping[str, Any],
    case_results_state: Mapping[str, Any],
    backend: RagasBackend,
    suite: str,
    run_id: str,
    error: CaseResultsInputError,
) -> RagasRunResult:
    artifact_uri = f"evals/rag/{suite}/{run_id}"
    manifest = _output_manifest(
        source_uri=source_uri,
        source_dir=source_dir,
        input_manifest=input_manifest,
        snapshot=input_manifest.get("knowledge_snapshot"),
        case_results_state=case_results_state,
        backend=backend,
    )
    row = {
        "case_id": "__suite_input__",
        "status": "input_error",
        "token_usage": {"system": _unknown_token_usage(), "judge": _unknown_token_usage()},
        "error": {"type": _input_error_type(error) or type(error).__name__, "message": str(error)},
    }
    summary = _summarize([row], manifest)
    write_json(root, f"{artifact_uri}/input_manifest.json", manifest)
    write_jsonl(root, f"{artifact_uri}/case_results.jsonl", [row])
    write_json(root, f"{artifact_uri}/summary.json", summary)
    write_markdown(root, f"{artifact_uri}/report.md", _report(suite, summary))
    return RagasRunResult(run_id=run_id, artifact_uri=artifact_uri, summary=summary)


def _output_manifest(
    *,
    source_uri: PurePosixPath,
    source_dir: Path,
    input_manifest: Mapping[str, Any],
    snapshot: Any,
    case_results_state: Mapping[str, Any],
    backend: RagasBackend,
) -> dict[str, Any]:
    return {
        "schema_version": RAGAS_RUN_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "deterministic_artifact_uri": source_uri.as_posix(),
        "deterministic_input_manifest_sha256": _sha256_file(source_dir / "input_manifest.json"),
        "deterministic_case_results": dict(case_results_state),
        "knowledge_snapshot": snapshot,
        "ragas": {"provider": backend.provider, "version": backend.version},
        "judge": _judge_reproducibility(backend),
        "embedding": {
            "model": os.getenv("EVAL_JUDGE_EMBEDDING_MODEL"),
            "configured": bool(os.getenv("EVAL_JUDGE_EMBEDDING_MODEL")),
        },
        "answer_correctness": {"weights": ANSWER_CORRECTNESS_WEIGHTS},
        "packages": _package_versions(),
        "metrics": METRIC_DEFINITIONS,
    }


def _to_ragas_sample(
    deterministic: Mapping[str, Any], golden: Mapping[str, Any], chunks: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_docs = deterministic.get("source_docs")
    source_docs = [str(item) for item in source_docs] if isinstance(source_docs, list) else []
    ordered_ids = _ordered_retrieved_chunk_ids(deterministic)
    chunks_by_id = {str(chunk.get("chunk_id")): chunk for chunk in chunks}
    missing_ids = [chunk_id for chunk_id, _ in ordered_ids if chunk_id not in chunks_by_id]
    if missing_ids:
        raise RetrievedChunkInputError(
            "UnknownRetrievedChunkIdError: published chunk IDs are missing "
            + ", ".join(missing_ids)
        )
    chosen = [chunks_by_id[chunk_id] for chunk_id, _ in ordered_ids]
    reference = golden.get("reference_answer")
    if not isinstance(reference, str) or not reference.strip():
        evidence = golden.get("expected_evidence")
        reference = "\n".join(str(item) for item in evidence) if isinstance(evidence, list) else ""
    return (
        {
            "user_input": str(deterministic.get("query", "")),
            "response": str(deterministic.get("answer", "")),
            "reference": reference,
            "retrieved_contexts": [str(chunk["text"]) for chunk in chosen],
        },
        {
            "retrieved_chunk_ids": [chunk_id for chunk_id, _ in ordered_ids],
            "retrieved_chunk_ranks": [rank for _, rank in ordered_ids],
            "source_docs": source_docs,
            "reference_source": "reference_answer" if golden.get("reference_answer") else "expected_evidence",
        },
    )


def _ragas_skip_reason(golden: Mapping[str, Any]) -> str | None:
    if golden.get("scope") == "agent_tool_e2e" or golden.get("expected_tool"):
        return "tool_case"
    if golden.get("unanswerable") is True:
        return "refusal_case"
    expected_docs = golden.get("expected_docs")
    expected_evidence = golden.get("expected_evidence")
    if not isinstance(expected_docs, list) or not expected_docs:
        return "no_rag_documents"
    if not isinstance(expected_evidence, list) or not expected_evidence:
        return "no_rag_evidence"
    return None


def _skipped_row(deterministic: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "case_id": str(deterministic.get("case_id", "")),
        "status": "skipped_not_applicable",
        "skip_reason": reason,
        "query": deterministic.get("query", ""),
        "answer": deterministic.get("answer", ""),
        "source_docs": deterministic.get("source_docs", []),
        "deterministic_metrics": deterministic.get("deterministic_metrics", {}),
        "token_usage": {
            "system": _token_usage(deterministic.get("token_usage")),
            "judge": _unknown_token_usage(),
        },
    }


def _ordered_retrieved_chunk_ids(
    deterministic: Mapping[str, Any],
) -> list[tuple[str, int]]:
    entries = deterministic.get("retrieved_chunks")
    if isinstance(entries, list):
        parsed = [_retrieval_entry(entry) for entry in entries]
    else:
        chunk_ids = deterministic.get("retrieved_chunk_ids")
        ranks = deterministic.get("retrieved_chunk_ranks")
        if not isinstance(chunk_ids, list) or not isinstance(ranks, list):
            raise RetrievedChunkInputError(
                "MissingRetrievedChunkIdsError: deterministic artifact requires "
                "retrieved_chunks or retrieved_chunk_ids with retrieved_chunk_ranks"
            )
        if len(chunk_ids) != len(ranks):
            raise RetrievedChunkInputError(
                "RetrievedChunkRankError: retrieved_chunk_ids and retrieved_chunk_ranks differ in length"
            )
        parsed = [_retrieval_entry({"chunk_id": chunk_id, "rank": rank}) for chunk_id, rank in zip(chunk_ids, ranks, strict=True)]
    if not parsed:
        raise RetrievedChunkInputError("MissingRetrievedChunkIdsError: no retrieved chunks were recorded")
    chunk_ids = [chunk_id for chunk_id, _ in parsed]
    ranks = [rank for _, rank in parsed]
    if len(set(chunk_ids)) != len(chunk_ids):
        raise RetrievedChunkInputError("RetrievedChunkIdError: duplicate retrieved chunk_id")
    if len(set(ranks)) != len(ranks):
        raise RetrievedChunkInputError("RetrievedChunkRankError: duplicate retrieved rank")
    return sorted(parsed, key=lambda item: item[1])


def _retrieval_entry(value: Any) -> tuple[str, int]:
    if not isinstance(value, Mapping):
        raise RetrievedChunkInputError("RetrievedChunkEntryError: retrieved chunk entry must be an object")
    chunk_id = value.get("chunk_id")
    rank = value.get("rank")
    if not isinstance(chunk_id, str) or not chunk_id:
        raise RetrievedChunkInputError("RetrievedChunkIdError: chunk_id must be a non-empty string")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise RetrievedChunkInputError("RetrievedChunkRankError: rank must be a positive integer")
    return chunk_id, rank


def _completed_row(deterministic: Mapping[str, Any], sample: Mapping[str, Any], output: Mapping[str, Any]) -> dict[str, Any]:
    metrics = output.get("metrics")
    if not isinstance(metrics, Mapping) or not metrics:
        return _failure_row(
            deterministic,
            RagasExecutionError("backend returned no metrics"),
            sample=sample,
            error_type="RagasResultError",
        )
    normalized = {name: score for name, value in metrics.items() if (score := _numeric_score(value)) is not None}
    if not normalized:
        return _failure_row(
            deterministic,
            RagasExecutionError("backend returned no numeric metrics"),
            sample=sample,
            error_type="RagasResultError",
        )
    judge = output.get("judge") if isinstance(output.get("judge"), Mapping) else {}
    return {
        "case_id": str(deterministic.get("case_id", "")),
        "status": "completed",
        "query": deterministic.get("query", ""),
        "answer": deterministic.get("answer", ""),
        "source_docs": sample["source_docs"],
        "retrieved_chunk_ids": sample["retrieved_chunk_ids"],
        "retrieved_chunk_ranks": sample["retrieved_chunk_ranks"],
        "reference_source": sample["reference_source"],
        "deterministic_metrics": deterministic.get("deterministic_metrics", {}),
        "ragas_metrics": normalized,
        "token_usage": {
            "system": _token_usage(deterministic.get("token_usage")),
            "judge": _token_usage(judge.get("token_usage")),
        },
        "judge": dict(judge),
    }


def _failure_row(
    deterministic: Mapping[str, Any],
    error: Exception,
    sample: Mapping[str, Any] | None = None,
    *,
    status: str | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    resolved_type = error_type or _input_error_type(error) or type(error).__name__
    return {
        "case_id": str(deterministic.get("case_id", "")),
        "status": status or _failure_status(error),
        "query": deterministic.get("query", ""),
        "answer": deterministic.get("answer", ""),
        "source_docs": list(sample.get("source_docs", [])) if sample else deterministic.get("source_docs", []),
        "retrieved_chunk_ids": list(sample.get("retrieved_chunk_ids", [])) if sample else [],
        "deterministic_metrics": deterministic.get("deterministic_metrics", {}),
        "token_usage": {"system": _token_usage(deterministic.get("token_usage")), "judge": _unknown_token_usage()},
        "error": {"type": resolved_type, "message": str(error)},
    }


def _failure_status(error: Exception) -> str:
    if isinstance(error, JudgeConfigurationError):
        return "configuration_error"
    if isinstance(error, RagasDependencyError):
        return "dependency_unavailable"
    if isinstance(error, (JudgeRequestError, RagasExecutionError)):
        return "remote_execution_error"
    return "evaluation_error"


def _input_error_type(error: Exception) -> str | None:
    if not isinstance(error, (RetrievedChunkInputError, CaseResultsInputError)):
        return None
    message = str(error)
    prefix, separator, _ = message.partition(":")
    return prefix if separator and prefix.endswith("Error") else None


def _load_published_snapshot(root: Path, input_manifest: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snapshot = input_manifest.get("knowledge_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("deterministic input is missing knowledge_snapshot")
    build_id = snapshot.get("index_build_id")
    if not isinstance(build_id, str) or not build_id:
        raise ValueError("knowledge_snapshot is missing index_build_id")
    manifest = read_published_chunk_manifest(root, build_id)
    lifecycle = manifest["lifecycle"]
    if lifecycle.get("collection_name") != snapshot.get("collection_name"):
        raise ValueError("published chunk collection does not match deterministic snapshot")
    chunks_info = manifest.get("artifacts", {}).get("chunks_jsonl")
    if not isinstance(chunks_info, Mapping) or not isinstance(chunks_info.get("uri"), str):
        raise ValueError("published chunk manifest is missing chunks_jsonl")
    chunks_path = _resolve_under_root(root, _safe_relative_uri(chunks_info["uri"], "chunks_jsonl"))
    chunks_sha = _sha256_file(chunks_path)
    if chunks_sha != chunks_info.get("sha256") or chunks_sha != snapshot.get("chunks_sha256"):
        raise ValueError("published chunk hash does not match deterministic snapshot")
    chunks = load_jsonl(chunks_path)
    if any(row.get("index_build_id") != build_id for row in chunks):
        raise ValueError("chunk rows do not match published index_build_id")
    return dict(snapshot), chunks


def _load_golden_rows(input_manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    golden = input_manifest.get("golden_set")
    if not isinstance(golden, Mapping) or not isinstance(golden.get("uri"), str):
        raise ValueError("deterministic input is missing golden_set uri")
    path = Path(golden["uri"])
    if not path.is_file():
        raise ValueError("deterministic golden_set is unavailable")
    expected_sha = golden.get("sha256")
    if isinstance(expected_sha, str) and _sha256_file(path) != expected_sha:
        raise ValueError("deterministic golden_set hash mismatch")
    return load_jsonl(path)


def _summarize(rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, list[float]] = {name: [] for name in METRIC_NAMES}
    token_totals = {field: 0 for field in TOKEN_USAGE_FIELDS}
    judge_total_tokens = 0
    judge_latency_ms = 0.0
    judge_estimated_cost = 0.0
    judge_latency_count = 0
    judge_cost_count = 0
    for row in rows:
        for name, value in row.get("ragas_metrics", {}).items():
            if name in values and (score := _numeric_score(value)) is not None:
                values[name].append(score)
        token_usage = row.get("token_usage", {})
        system = token_usage.get("system", {}) if isinstance(token_usage, Mapping) else {}
        judge = token_usage.get("judge", {}) if isinstance(token_usage, Mapping) else {}
        for field in TOKEN_USAGE_FIELDS:
            value = system.get(field) if isinstance(system, Mapping) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                token_totals[field] += value
        judge_tokens = judge.get("total_tokens") if isinstance(judge, Mapping) else None
        if isinstance(judge_tokens, (int, float)) and not isinstance(judge_tokens, bool):
            judge_total_tokens += judge_tokens
        judge_metadata = row.get("judge", {})
        latency = judge_metadata.get("latency_ms") if isinstance(judge_metadata, Mapping) else None
        cost = judge_metadata.get("estimated_cost") if isinstance(judge_metadata, Mapping) else None
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            judge_latency_ms += latency
            judge_latency_count += 1
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            judge_estimated_cost += cost
            judge_cost_count += 1
    completed_count = sum(row.get("status") == "completed" for row in rows)
    skipped_count = sum(row.get("status") == "skipped_not_applicable" for row in rows)
    error_count = len(rows) - completed_count - skipped_count
    failure_counts: dict[str, int] = {}
    for row in rows:
        status = row.get("status")
        if status not in {"completed", "skipped_not_applicable"} and isinstance(status, str):
            failure_counts[status] = failure_counts.get(status, 0) + 1
    return {
        "schema_version": RAGAS_RUN_SCHEMA_VERSION,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if error_count == 0 else "degraded",
        "case_count": len(rows),
        "completed_count": completed_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "failure_counts": failure_counts,
        "metrics": {name: round(sum(scores) / len(scores), 4) if scores else None for name, scores in values.items()},
        "metric_case_counts": {name: len(scores) for name, scores in values.items()},
        "token_usage": {**token_totals, "judge_total_tokens": judge_total_tokens},
        "judge_latency_ms": round(judge_latency_ms, 3) if judge_latency_count else None,
        "judge_estimated_cost": round(judge_estimated_cost, 8) if judge_cost_count else None,
        "ragas": manifest["ragas"],
        "knowledge_snapshot": manifest["knowledge_snapshot"],
    }


def _judge_reproducibility(backend: RagasBackend) -> dict[str, Any]:
    config = getattr(backend, "judge_config", None)
    if isinstance(config, Mapping):
        return dict(config)
    return {
        "model": None,
        "prompt_version": None,
        "rubric_version": SEMANTIC_RUBRIC_VERSION,
    }


def _package_versions() -> dict[str, str]:
    packages = ("ragas", "datasets", "langchain-core", "langchain-openai")
    values = {"python": sys.version.split()[0]}
    for package in packages:
        try:
            values[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            values[package] = "unavailable"
    return values


def _build_qwen_embeddings(judge: QwenJudge) -> Any | None:
    import os

    model = os.getenv("EVAL_JUDGE_EMBEDDING_MODEL")
    if not model:
        return None
    try:
        from langchain_openai import OpenAIEmbeddings
    except Exception as error:
        raise RagasDependencyError("langchain-openai embedding adapter unavailable") from error
    if not judge.api_key:
        raise RagasExecutionError("DASHSCOPE_API_KEY is required for Qwen embeddings")
    return OpenAIEmbeddings(model=model, base_url=judge.base_url, api_key=judge.api_key)


def _token_usage(value: Any) -> dict[str, float | int | None]:
    usage = _unknown_token_usage()
    if isinstance(value, Mapping):
        for field in TOKEN_USAGE_FIELDS:
            item = value.get(field)
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                usage[field] = item
    return usage


def _unknown_token_usage() -> dict[str, float | int | None]:
    return {field: None for field in TOKEN_USAGE_FIELDS}


def _accumulate_usage(
    total: dict[str, float | int | None], value: Mapping[str, Any]
) -> None:
    for field in TOKEN_USAGE_FIELDS:
        item = value.get(field)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            total[field] = (total[field] or 0) + item


def _numeric_score(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
        return float(value)
    return None


def _safe_relative_uri(value: str, name: str) -> PurePosixPath:
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
    except ValueError as error:
        raise ValueError("artifact path escapes artifact_root") from error
    return path


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_path_segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\\/:") or value in {".", ".."}:
        raise ValueError(f"{name} must be a safe path segment")
    return value


def _new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"


def _report(suite: str, summary: Mapping[str, Any]) -> str:
    lines = [f"# RAGAS evaluation: {suite}", "", "## Metrics", ""]
    lines.extend(f"- {name}: {value}" for name, value in summary["metrics"].items())
    lines.extend(["", "## Run", "", f"- Status: {summary['status']}", f"- Errors: {summary['error_count']}", f"- Judge tokens: {summary['token_usage']['judge_total_tokens']}", ""])
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., RagasRunResult] = run_ragas_suite,
    persister: Callable[..., Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run offline RAGAS evaluation from deterministic artifacts")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--deterministic-artifact")
    source.add_argument("--register-artifact")
    parser.add_argument("--artifact-root", default="artifacts")
    parser.add_argument("--suite")
    parser.add_argument("--run-id")
    parser.add_argument("--judge-model")
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args(argv)
    selected_persister = persister or _default_persister

    if args.register_artifact:
        if args.persist:
            parser.error("--persist cannot be used with --register-artifact")
        try:
            recorded = selected_persister(
                artifact_root=args.artifact_root,
                artifact_uri=args.register_artifact,
            )
        except Exception as error:
            print(json.dumps({
                "artifact_uri": args.register_artifact,
                "persistence_error": {"type": type(error).__name__, "message": str(error)},
            }, ensure_ascii=False))
            return 1
        print(json.dumps({
            "artifact_uri": args.register_artifact,
            "recorded_eval_run_id": recorded.id,
            "recorded_eval_artifact_uri": recorded.artifact_uri,
        }, ensure_ascii=False))
        return 0

    if not args.suite:
        parser.error("--suite is required with --deterministic-artifact")
    _load_workspace_env()
    evaluator: RagasBackend | None = None
    if args.judge_model:
        try:
            evaluator = QwenSemanticCompatBackend(QwenSemanticJudge(model=args.judge_model))
        except Exception as error:
            config = _environment_judge_config()
            config["model"] = args.judge_model
            evaluator = _UnavailableBackend(error, judge_config=config)
    run = runner(
        deterministic_artifact_uri=args.deterministic_artifact,
        artifact_root=args.artifact_root,
        suite=args.suite,
        run_id=args.run_id,
        evaluator=evaluator,
    )
    payload = {"run_id": run.run_id, "artifact_uri": run.artifact_uri, "summary": run.summary}
    if args.persist:
        try:
            recorded = selected_persister(
                artifact_root=args.artifact_root,
                artifact_uri=run.artifact_uri,
            )
        except Exception as error:
            payload["persistence_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 1
        payload["recorded_eval_run_id"] = recorded.id
        payload["recorded_eval_artifact_uri"] = recorded.artifact_uri
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if run.summary.get("error_count") else 0


def _default_persister(*, artifact_root: Path | str, artifact_uri: str) -> Any:
    """Load database dependencies only when an offline caller requests persistence."""
    from metro_agent.storage.history.database import SessionLocal
    from evaluation.ragas_persistence import persist_ragas_artifact
    from metro_agent.observability.evaluation_service import EvaluationService

    return persist_ragas_artifact(
        artifact_root=artifact_root,
        artifact_uri=artifact_uri,
        evaluation_service=EvaluationService(SessionLocal, artifact_root=artifact_root),
    )


def _load_workspace_env() -> None:
    """Load the local offline-evaluation configuration without importing model setup."""
    import dotenv

    dotenv.load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


if __name__ == "__main__":
    raise SystemExit(main())
