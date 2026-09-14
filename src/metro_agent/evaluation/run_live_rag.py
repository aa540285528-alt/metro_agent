"""Execute reviewed RAG cases against the published index, then score the captured output."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from metro_agent.tools.rag_evaluation import load_jsonl, run_deterministic_rag_suite
from langchain_core.messages import AIMessage, HumanMessage
from metro_agent.observability.artifacts import write_jsonl


RAG_SCOPES = frozenset({"rag_e2e", "rag_retrieval"})
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class LiveRagRun:
    run_id: str
    artifact_uri: str
    raw_results_uri: str
    golden_snapshot_uri: str
    summary: dict[str, Any]


def run_live_rag_suite(
    *,
    golden_path: Path | str,
    artifact_root: Path | str = "artifacts",
    suite: str,
    run_id: str | None = None,
    index_build_id: str | None = None,
    search: Callable[[str], Mapping[str, Any]] | None = None,
    knowledge_agent: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> LiveRagRun:
    """Capture live RAG output for reviewed RAG scopes and score that immutable snapshot."""
    safe_suite = _safe_segment(suite, "suite")
    resolved_run_id = _safe_segment(run_id or _new_run_id(), "run_id")
    root = Path(artifact_root)
    selected_goldens = [
        row for row in load_jsonl(golden_path) if row.get("scope") in RAG_SCOPES
    ]
    if not selected_goldens:
        raise ValueError("golden set has no RAG cases")
    selected_search = search or _load_live_search()
    selected_knowledge_agent = knowledge_agent or _load_knowledge_agent()

    input_uri = f"eval_inputs/rag/{safe_suite}/{resolved_run_id}"
    golden_snapshot_ref = write_jsonl(root, f"{input_uri}/golden.jsonl", selected_goldens)
    result_rows: list[dict[str, Any]] = []
    resolved_build_id = index_build_id
    for golden in selected_goldens:
        if golden["scope"] == "rag_e2e":
            result = _run_e2e_case(golden, selected_knowledge_agent)
        else:
            result = _run_case(golden, selected_search)
        result_rows.append(result)
        response_build_id = result.get("index_build_id")
        if response_build_id is None:
            continue
        if not isinstance(response_build_id, str) or not response_build_id:
            raise ValueError(f"RAG case {golden['id']} returned an invalid index_build_id")
        if resolved_build_id is None:
            resolved_build_id = response_build_id
        elif resolved_build_id != response_build_id:
            raise ValueError("RAG cases used multiple index build ids")

    if not resolved_build_id:
        raise ValueError("no RAG case returned an index_build_id; pass index_build_id explicitly")

    raw_results_ref = write_jsonl(root, f"{input_uri}/results.jsonl", result_rows)
    deterministic = run_deterministic_rag_suite(
        golden_path=root / golden_snapshot_ref.uri,
        result_rows=result_rows,
        artifact_root=root,
        index_build_id=resolved_build_id,
        suite=safe_suite,
        run_id=resolved_run_id,
    )
    return LiveRagRun(
        run_id=resolved_run_id,
        artifact_uri=deterministic.artifact_uri,
        raw_results_uri=raw_results_ref.uri,
        golden_snapshot_uri=golden_snapshot_ref.uri,
        summary=deterministic.summary,
    )


def _run_case(
    golden: Mapping[str, Any], search: Callable[[str], Mapping[str, Any]]
) -> dict[str, Any]:
    case_id = golden.get("id")
    query = golden.get("query")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("RAG golden case has no id")
    if not isinstance(query, str):
        raise ValueError(f"RAG golden case {case_id} query must be a string")

    started_at = perf_counter()
    try:
        response = search(query)
        if not isinstance(response, Mapping):
            raise ValueError("RAG search must return a mapping")
        source_docs = response.get("source_docs", [])
        sources = response.get("sources", [])
        answer = response.get("answer", "")
        if not isinstance(source_docs, list) or not all(isinstance(doc, str) for doc in source_docs):
            raise ValueError("RAG search source_docs must be a list of strings")
        if not isinstance(sources, list):
            raise ValueError("RAG search sources must be a list")
        if not isinstance(answer, str):
            raise ValueError("RAG search answer must be a string")
        return {
            "id": case_id,
            "source_docs": source_docs,
            "answer": answer,
            "sources": sources,
            "index_build_id": response.get("index_build_id"),
            "top_k": response.get("top_k"),
            "retrieved_count": response.get("retrieved_count", len(sources)),
            "latency_ms": round((perf_counter() - started_at) * 1000, 3),
            "execution_error": None,
        }

    except Exception as error:
        return {
            "id": case_id,
            "source_docs": [],
            "answer": f"RAG execution failed: {type(error).__name__}: {error}",
            "sources": [],
            "index_build_id": None,
            "top_k": None,
            "retrieved_count": 0,
            "latency_ms": round((perf_counter() - started_at) * 1000, 3),
            "execution_error": {"type": type(error).__name__, "message": str(error)},
        }


def _load_live_search() -> Callable[[str], Mapping[str, Any]]:
    """Delay model imports until a caller actually starts a live RAG run."""

    import config  # noqa: F401
    from metro_agent.tools.Knowledge_RAGtools import build_rag_search

    return build_rag_search


def _load_knowledge_agent() -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Delay the production answer agent and its model setup until it is needed."""
    import config  # noqa: F401
    from metro_agent.agents.KnowledgeAgent import knowledge_agent

    return knowledge_agent


def _run_e2e_case(
    golden: Mapping[str, Any],
    knowledge_agent: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    case_id = golden.get("id")
    query = golden.get("query")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("RAG golden case has no id")
    if not isinstance(query, str):
        raise ValueError(f"RAG golden case {case_id} query must be a string")

    round_id = f"eval-{case_id}"
    started_at = perf_counter()
    try:
        history_messages = _history_messages(golden, case_id)
        state = {
            "user_input": query,
            "messages": history_messages + [
                HumanMessage(content=query, additional_kwargs={"round_id": round_id})
            ],
            "current_round_id": round_id,
            "conversation_summaries": [],
            "agents_output": {},
            "tool_results": {},
        }
        result = knowledge_agent(state)
        if not isinstance(result, Mapping):
            raise ValueError("knowledge agent must return a mapping")
        outputs = result.get("agents_output", {})
        if not isinstance(outputs, Mapping):
            raise ValueError("knowledge agent response has no agents_output mapping")
        answer = outputs.get("knowledge", "")
        sources = outputs.get("sources", "")
        retrieved_sources = outputs.get("retrieved_sources", [])
        if not isinstance(answer, str):
            raise ValueError("knowledge agent answer must be a string")
        if not isinstance(retrieved_sources, list):
            raise ValueError("knowledge agent retrieved_sources must be a list")
        return {
            "id": case_id,
            "source_docs": _source_docs(sources),
            "answer": answer,
            "sources": retrieved_sources,
            "index_build_id": outputs.get("index_build_id"),
            "top_k": outputs.get("top_k"),
            "retrieved_count": outputs.get("retrieved_count", len(retrieved_sources)),
            "latency_ms": round((perf_counter() - started_at) * 1000, 3),
            "execution_error": None,
        }
    except Exception as error:
        return {
            "id": case_id,
            "source_docs": [],
            "answer": f"RAG execution failed: {type(error).__name__}: {error}",
            "sources": [],
            "index_build_id": None,
            "top_k": None,
            "retrieved_count": 0,
            "latency_ms": round((perf_counter() - started_at) * 1000, 3),
            "execution_error": {"type": type(error).__name__, "message": str(error)},
        }


def _source_docs(value: Any) -> list[str]:
    if not isinstance(value, str) or value == "未检索到明确文档来源":
        return []
    return list(dict.fromkeys(item.strip() for item in value.split("、") if item.strip()))


def _history_messages(golden: Mapping[str, Any], case_id: str) -> list[HumanMessage | AIMessage]:
    history = golden.get("history", [])
    if history is None:
        return []
    if not isinstance(history, list):
        raise ValueError(f"RAG golden case {case_id} has invalid history")

    messages: list[HumanMessage | AIMessage] = []
    for index, item in enumerate(history):
        if not isinstance(item, Mapping):
            raise ValueError(f"RAG golden case {case_id} has invalid history entry")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"RAG golden case {case_id} has empty history content")
        message_type = AIMessage if item.get("role") in {"assistant", "ai"} else HumanMessage
        messages.append(
            message_type(
                content=content,
                additional_kwargs={"round_id": f"eval-{case_id}-history-{index}"},
            )
        )
    return messages


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run reviewed RAG cases against the published knowledge index")
    parser.add_argument("--golden", required=True)
    parser.add_argument("--artifact-root", default="artifacts")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--index-build-id")
    args = parser.parse_args(argv)
    run = run_live_rag_suite(
        golden_path=args.golden,
        artifact_root=args.artifact_root,
        suite=args.suite,
        run_id=args.run_id,
        index_build_id=args.index_build_id,
    )
    print(json.dumps({
        "run_id": run.run_id,
        "artifact_uri": run.artifact_uri,
        "raw_results_uri": run.raw_results_uri,
        "golden_snapshot_uri": run.golden_snapshot_uri,
        "summary": run.summary,
    }, ensure_ascii=False))
    return 0


def _safe_segment(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{field} must contain only letters, numbers, dots, underscores, or hyphens")
    return value


def _new_run_id() -> str:
    return uuid4().hex


if __name__ == "__main__":
    raise SystemExit(main())
