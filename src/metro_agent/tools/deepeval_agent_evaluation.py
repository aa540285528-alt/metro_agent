"""Compatibility exports for the former Task 7 module location."""

from evaluation.run_deepeval import (
    DeepEvalRunResult,
    NativeDeepEvalBackend,
    main,
    run_cli,
    run_deepeval_suite,
    score_budget,
    score_plan_graph,
    score_tool_calls,
)

__all__ = [
    "DeepEvalRunResult",
    "NativeDeepEvalBackend",
    "main",
    "run_cli",
    "run_deepeval_suite",
    "score_budget",
    "score_plan_graph",
    "score_tool_calls",
]


if __name__ == "__main__":
    raise SystemExit(main())
