from __future__ import annotations

import json
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable, Mapping

from metro_agent.observability.llm_usage import extract_llm_usage


DEFAULT_JUDGE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RETRIES = 3
EXPECTED_RESPONSE_KEYS = frozenset({"score", "pass", "reason", "evidence"})


class JudgeResponseError(ValueError):
    """Raised when a judge response cannot be trusted as a typed evaluation."""


class JudgeConfigurationError(ValueError):
    """Raised before a judge request when required local configuration is absent."""


class JudgeRequestError(RuntimeError):
    """Raised when the remote judge cannot return a usable response."""


def normalize_json_response(raw_response: str) -> str:
    """Unwrap a single Markdown JSON fence without accepting surrounding prose."""
    normalized = raw_response.strip()
    if not normalized.startswith("```"):
        return normalized
    lines = normalized.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        opening = lines[0].strip().lower()
        if opening in {"```", "```json", "```jsonc"}:
            return "\n".join(lines[1:-1]).strip()
    return normalized


@dataclass(frozen=True, slots=True)
class JudgeResult:
    score: float
    passed: bool
    reason: str
    evidence: list[str]
    model: str
    prompt_version: str
    raw_response: str
    token_usage: dict[str, Any]
    latency_ms: float
    estimated_cost: float | None = None


def parse_judge_response(
    raw_response: str,
    *,
    model: str,
    prompt_version: str,
    token_usage: Mapping[str, Any] | None = None,
    latency_ms: float = 0.0,
) -> JudgeResult:
    if not isinstance(raw_response, str):
        raise JudgeResponseError("judge response must be text")
    try:
        payload = json.loads(normalize_json_response(raw_response))
    except json.JSONDecodeError as error:
        raise JudgeResponseError("judge response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise JudgeResponseError("judge response must be a JSON object")
    if set(payload) != EXPECTED_RESPONSE_KEYS:
        raise JudgeResponseError(
            "judge response must contain exactly score, pass, reason, and evidence"
        )

    score = payload.get("score")
    passed = payload.get("pass")
    reason = payload.get("reason")
    evidence = payload.get("evidence")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise JudgeResponseError("judge score must be a number between 0 and 1")
    if not isinstance(passed, bool):
        raise JudgeResponseError("judge pass must be a boolean")
    if not isinstance(reason, str):
        raise JudgeResponseError("judge reason must be a string")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise JudgeResponseError("judge evidence must be a list of strings")
    if not isinstance(model, str) or not model:
        raise JudgeResponseError("judge model is required")
    if not isinstance(prompt_version, str) or not prompt_version:
        raise JudgeResponseError("judge prompt version is required")

    usage = dict(token_usage or {})
    cost = usage.get("estimated_cost")
    return JudgeResult(
        score=float(score),
        passed=passed,
        reason=reason,
        evidence=list(evidence),
        model=model,
        prompt_version=prompt_version,
        raw_response=raw_response,
        token_usage=usage,
        latency_ms=float(latency_ms),
        estimated_cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
    )


class QwenJudge:
    """Explicitly invoked Qwen judge; importing it never creates a network client."""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        prompt_version: str = "v1",
        llm_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model or os.getenv("EVAL_JUDGE_MODEL")
        self.base_url = base_url or os.getenv("EVAL_JUDGE_BASE_URL", DEFAULT_JUDGE_BASE_URL)
        self.api_key = api_key if api_key is not None else os.getenv("DASHSCOPE_API_KEY")
        self.temperature = (
            temperature
            if temperature is not None
            else float(os.getenv("EVAL_JUDGE_TEMPERATURE", "0"))
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

    def judge(self, prompt: str) -> JudgeResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("judge prompt must be non-empty")
        started = perf_counter()
        request_prompt = (
            f"{prompt}\n\nReturn only one JSON object with exactly these fields: "
            '"score" (number 0 to 1), "pass" (boolean), "reason" (string), '
            'and "evidence" (array of strings).'
        )
        try:
            response = self._build_llm().invoke(request_prompt)
        except JudgeConfigurationError:
            raise
        except Exception as error:
            raise JudgeRequestError(f"judge request failed: {type(error).__name__}") from error
        latency_ms = (perf_counter() - started) * 1000
        raw_response = getattr(response, "content", response)
        if not isinstance(raw_response, str):
            raise JudgeResponseError("judge response content must be text")
        usage = extract_llm_usage(response, fallback_model=self.model) or {}
        return parse_judge_response(
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
            )
        if not self.api_key:
            raise JudgeConfigurationError("DASHSCOPE_API_KEY is required")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=self.timeout_seconds,
            max_retries=self.max_retries,
        )
