"""Single-call Qwen-compatible semantic judge for offline RAG evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from evaluation.qwen_judge import (
    DEFAULT_JUDGE_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    JudgeConfigurationError,
    JudgeRequestError,
)
from metro_agent.observability.llm_usage import extract_llm_usage


SEMANTIC_JUDGE_PROVIDER = "qwen_semantic_compat"
SEMANTIC_RUBRIC_VERSION = "qwen-semantic-rubric-v1"
METRIC_NAMES = (
    "faithfulness",
    "answer_correctness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
)
EXPECTED_RESPONSE_KEYS = frozenset({"metrics", "pass", "reason", "evidence"})


class SemanticJudgeResponseError(ValueError):
    """Raised when a semantic judge response fails the strict output contract."""


@dataclass(frozen=True, slots=True)
class SemanticJudgeResult:
    metrics: dict[str, float]
    passed: bool
    reason: str
    evidence: list[str]
    provider: str
    model: str
    prompt_version: str
    rubric_version: str
    raw_response: str
    token_usage: dict[str, Any]
    latency_ms: float
    estimated_cost: float | None = None


def parse_semantic_judge_response(
    raw_response: str,
    *,
    model: str,
    prompt_version: str,
    token_usage: Mapping[str, Any] | None = None,
    latency_ms: float = 0.0,
) -> SemanticJudgeResult:
    if not isinstance(raw_response, str):
        raise SemanticJudgeResponseError("semantic judge response must be text")
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as error:
        raise SemanticJudgeResponseError("semantic judge response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise SemanticJudgeResponseError("semantic judge response must be a JSON object")
    if set(payload) != EXPECTED_RESPONSE_KEYS:
        raise SemanticJudgeResponseError(
            "semantic judge response must contain exactly metrics, pass, reason, and evidence"
        )

    metrics = payload.get("metrics")
    passed = payload.get("pass")
    reason = payload.get("reason")
    evidence = payload.get("evidence")
    if not isinstance(metrics, Mapping) or set(metrics) != set(METRIC_NAMES):
        raise SemanticJudgeResponseError("semantic judge metrics must contain exactly five metrics")
    typed_metrics: dict[str, float] = {}
    for name in METRIC_NAMES:
        score = metrics[name]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise SemanticJudgeResponseError(
                f"semantic judge metric {name} must be a number between 0 and 1"
            )
        typed_metrics[name] = float(score)
    if not isinstance(passed, bool):
        raise SemanticJudgeResponseError("semantic judge pass must be a boolean")
    if not isinstance(reason, str):
        raise SemanticJudgeResponseError("semantic judge reason must be a string")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise SemanticJudgeResponseError("semantic judge evidence must be a list of strings")
    if not isinstance(model, str) or not model:
        raise SemanticJudgeResponseError("semantic judge model is required")
    if not isinstance(prompt_version, str) or not prompt_version:
        raise SemanticJudgeResponseError("semantic judge prompt version is required")

    usage = dict(token_usage or {})
    cost = usage.get("estimated_cost")
    return SemanticJudgeResult(
        metrics=typed_metrics,
        passed=passed,
        reason=reason,
        evidence=list(evidence),
        provider=SEMANTIC_JUDGE_PROVIDER,
        model=model,
        prompt_version=prompt_version,
        rubric_version=SEMANTIC_RUBRIC_VERSION,
        raw_response=raw_response,
        token_usage=usage,
        latency_ms=float(latency_ms),
        estimated_cost=(
            float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None
        ),
    )


class QwenSemanticJudge:
    """Builds one reproducible Qwen request for all semantic RAG metrics."""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        prompt_version: str = "semantic-v1",
        llm_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model or os.getenv("EVAL_JUDGE_MODEL")
        self.base_url = base_url or os.getenv("EVAL_JUDGE_BASE_URL", DEFAULT_JUDGE_BASE_URL)
        self.api_key = api_key if api_key is not None else os.getenv("DASHSCOPE_API_KEY")
        self.temperature = (
            temperature if temperature is not None else float(os.getenv("EVAL_JUDGE_TEMPERATURE", "0"))
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.getenv("EVAL_JUDGE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
        )
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int(os.getenv("EVAL_JUDGE_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)))
        )
        self.prompt_version = prompt_version
        self._llm_factory = llm_factory
        if not isinstance(self.model, str) or not self.model.strip():
            raise JudgeConfigurationError("EVAL_JUDGE_MODEL is required")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise JudgeConfigurationError("DASHSCOPE_API_KEY is required")
        if self.timeout_seconds <= 0:
            raise JudgeConfigurationError("EVAL_JUDGE_TIMEOUT_SECONDS must be positive")
        if self.max_retries < 0:
            raise JudgeConfigurationError("EVAL_JUDGE_MAX_RETRIES must be non-negative")

    def judge(self, sample: Mapping[str, Any]) -> SemanticJudgeResult:
        request_prompt = _semantic_prompt(sample)
        started = perf_counter()
        try:
            response = self._build_llm().invoke(request_prompt)
        except JudgeConfigurationError:
            raise
        except Exception as error:
            raise JudgeRequestError(
                f"semantic judge request failed: {type(error).__name__}"
            ) from error
        latency_ms = (perf_counter() - started) * 1000
        raw_response = getattr(response, "content", response)
        if not isinstance(raw_response, str):
            raise SemanticJudgeResponseError("semantic judge response content must be text")
        usage = extract_llm_usage(response, fallback_model=self.model) or {}
        return parse_semantic_judge_response(
            raw_response,
            model=self.model,
            prompt_version=self.prompt_version,
            token_usage=usage,
            latency_ms=latency_ms,
        )

    def _build_llm(self) -> Any:
        if self._llm_factory is not None:
            return self._llm_factory(
                model=self.model,
                base_url=self.base_url,
                api_key=self.api_key,
                temperature=self.temperature,
                timeout=self.timeout_seconds,
                max_retries=self.max_retries,
                model_kwargs={"response_format": {"type": "json_object"}},
            )
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=self.timeout_seconds,
            max_retries=self.max_retries,
            model_kwargs={"response_format": {"type": "json_object"}},
        )


def _semantic_prompt(sample: Mapping[str, Any]) -> str:
    contexts = sample.get("retrieved_contexts")
    if not isinstance(contexts, Sequence) or isinstance(contexts, (str, bytes)):
        raise ValueError("semantic judge sample retrieved_contexts must be a list of strings")
    normalized_contexts = [str(context) for context in contexts]
    payload = {
        "query": str(sample.get("user_input", "")),
        "answer": str(sample.get("response", "")),
        "reference": str(sample.get("reference", "")),
        "retrieved_contexts": normalized_contexts,
    }
    return (
        "You are evaluating one offline RAG result. Use only the supplied query, answer, "
        "reference, and retrieved contexts. Do not invent facts or missing context. "
        "Score each metric from 0 to 1: faithfulness means the answer is supported by "
        "retrieved contexts; answer_correctness means the answer satisfies the reference; "
        "answer_relevancy means the answer addresses the query; context_precision means "
        "retrieved contexts are relevant to the reference and query; context_recall means "
        "retrieved contexts cover the needed reference evidence.\n\n"
        "Return only one JSON object with exactly this shape: "
        '{"metrics":{"faithfulness":0.0,"answer_correctness":0.0,"answer_relevancy":0.0,'
        '"context_precision":0.0,"context_recall":0.0},"pass":false,"reason":"",'
        '"evidence":[]}.\n\n'
        f"Evaluation input:\n{json.dumps(payload, ensure_ascii=False)}"
    )
