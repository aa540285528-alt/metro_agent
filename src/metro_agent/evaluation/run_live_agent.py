from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from metro_agent.tools.rag_evaluation import load_jsonl
from metro_agent.observability.artifacts import write_json, write_jsonl


TOOL_CATEGORIES = frozenset({"realtime_alarm", "tool_failure"})


class AgentEvaluationConfigurationError(ValueError):
    pass


def run_live_agent_suite(
    *, golden_rows: Sequence[Mapping[str, Any]], artifact_root: Path | str = "artifacts", suite: str, run_id: str,
    alarm_base_url: str | None = None, graph_factory: Callable[[], Any] | None = None,
    chat_runner: Callable[..., Any] | None = None, environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    safe_suite, safe_run_id = _safe_segment(suite, "suite"), _safe_segment(run_id, "run_id")
    env = dict(os.environ if environment is None else environment)
    endpoint = (alarm_base_url or env.get("AGENT_EVAL_ALARM_BASE_URL") or "").strip()
    if any(str(row.get("category", "")) in TOOL_CATEGORIES for row in golden_rows) and not endpoint:
        raise AgentEvaluationConfigurationError("AGENT_EVAL_ALARM_BASE_URL is required for tool evaluation cases")
    root = Path(artifact_root)
    uri = f"eval_inputs/agent/{safe_suite}/{safe_run_id}"
    golden_ref = write_jsonl(root, f"{uri}/golden_snapshot.jsonl", golden_rows)
    factory = graph_factory or _default_graph_factory
    runner = chat_runner or _default_chat_runner
    previous = os.environ.get("AGENT_EVAL_ALARM_BASE_URL")
    if endpoint:
        os.environ["AGENT_EVAL_ALARM_BASE_URL"] = endpoint
    try:
        mappings = []
        for row in golden_rows:
            case_id = _case_id(row)
            result = runner(factory(), thread_id=f"agent-eval-{safe_suite}-{safe_run_id}-{case_id}", user_id="agent-eval", message=str(row.get("query", "")))
            trace_id = getattr(result, "trace_id", None)
            if not isinstance(trace_id, str) or not trace_id:
                raise RuntimeError(f"case {case_id} did not return a trace_id")
            mappings.append({"case_id": case_id, "trace_id": trace_id})
    finally:
        if endpoint:
            if previous is None:
                os.environ.pop("AGENT_EVAL_ALARM_BASE_URL", None)
            else:
                os.environ["AGENT_EVAL_ALARM_BASE_URL"] = previous
    traces_ref = write_jsonl(root, f"{uri}/trace_results.jsonl", mappings)
    summary = {
        "schema_version": "live-agent-eval-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(mappings),
        "golden_snapshot": {"uri": golden_ref.uri, "sha256": golden_ref.sha256},
        "trace_results": {"uri": traces_ref.uri, "sha256": traces_ref.sha256},
    }
    write_json(root, f"{uri}/summary.json", summary)
    return {"golden_snapshot_uri": golden_ref.uri, "trace_results_uri": traces_ref.uri, "summary_uri": f"{uri}/summary.json", "case_count": len(mappings), "golden_sha256": hashlib.sha256(json.dumps([dict(row) for row in golden_rows], ensure_ascii=False, sort_keys=True).encode()).hexdigest()}


def _default_graph_factory() -> Any:
    from metro_agent.api import build_default_graph
    return build_default_graph()


def _default_chat_runner(graph: Any, **kwargs: Any) -> Any:
    from metro_agent.agent_service import run_chat_with_trace
    return run_chat_with_trace(graph, **kwargs)


def _safe_segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or any(char in value for char in "\\/:"):
        raise ValueError(f"{name} must be a safe path segment")
    return value


def _case_id(row: Mapping[str, Any]) -> str:
    value = row.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("golden case id is required")
    return value


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run reviewed cases through the real Metro Agent")
    parser.add_argument("--golden", required=True)
    parser.add_argument("--artifact-root", default="artifacts")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--alarm-base-url")
    args = parser.parse_args(argv)
    print(json.dumps(run_live_agent_suite(golden_rows=load_jsonl(args.golden), artifact_root=args.artifact_root, suite=args.suite, run_id=args.run_id, alarm_base_url=args.alarm_base_url), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
