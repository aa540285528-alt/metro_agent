from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_PROMPT_TOKEN_KEYS = ("prompt_tokens", "input_tokens", "input_token_count")
_COMPLETION_TOKEN_KEYS = (
    "completion_tokens",
    "output_tokens",
    "output_token_count",
)
_TOTAL_TOKEN_KEYS = ("total_tokens", "total_token_count")
_CACHED_TOKEN_KEYS = (
    "cached_tokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
)
_MODEL_KEYS = ("model", "model_name", "model_id")
_COST_KEYS = ("estimated_cost", "total_cost", "cost_usd", "cost")


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _get_mapping(value: object, name: str) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return _as_mapping(value.get(name))
    return _as_mapping(getattr(value, name, None))


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _first_token_count(mappings: list[Mapping[str, Any]], keys: tuple[str, ...]) -> int | None:
    for mapping in mappings:
        for key in keys:
            count = _token_count(mapping.get(key))
            if count is not None:
                return count
    return None


def _first_string(mappings: list[Mapping[str, Any]], keys: tuple[str, ...]) -> str | None:
    for mapping in mappings:
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _first_cost(mappings: list[Mapping[str, Any]]) -> float | None:
    for mapping in mappings:
        for key in _COST_KEYS:
            value = mapping.get(key)
            if isinstance(value, Mapping):
                value = value.get("total", value.get("total_cost", value.get("usd")))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def _detail_token_count(
    mappings: list[Mapping[str, Any]], detail_name: str, token_name: str
) -> int | None:
    for mapping in mappings:
        details = _as_mapping(mapping.get(detail_name))
        if details is None:
            continue
        count = _token_count(details.get(token_name))
        if count is not None:
            return count
    return None


def _response_mappings(response: object) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    metadata: list[Mapping[str, Any]] = []
    usage: list[Mapping[str, Any]] = []
    direct = _as_mapping(response)
    if direct is not None:
        metadata.append(direct)
    for name in ("response_metadata", "additional_kwargs"):
        mapping = _get_mapping(response, name)
        if mapping is not None:
            metadata.append(mapping)
    direct_usage = _get_mapping(response, "usage_metadata")
    if direct_usage is not None:
        usage.append(direct_usage)
    for mapping in metadata:
        for name in ("usage_metadata", "usage", "token_usage"):
            nested = _as_mapping(mapping.get(name))
            if nested is not None:
                usage.append(nested)
    usage.extend(metadata)
    return metadata, usage


def extract_llm_usage(response: object, *, fallback_model: str | None = None) -> dict[str, Any] | None:
    """Normalize common LangChain/provider response usage shapes without raising."""
    try:
        metadata, usage_mappings = _response_mappings(response)
        prompt_tokens = _first_token_count(usage_mappings, _PROMPT_TOKEN_KEYS)
        completion_tokens = _first_token_count(usage_mappings, _COMPLETION_TOKEN_KEYS)
        total_tokens = _first_token_count(usage_mappings, _TOTAL_TOKEN_KEYS)
        if prompt_tokens is None and total_tokens is not None and completion_tokens is not None:
            prompt_tokens = total_tokens - completion_tokens
        if completion_tokens is None and total_tokens is not None and prompt_tokens is not None:
            completion_tokens = total_tokens - prompt_tokens
        if prompt_tokens is None or completion_tokens is None:
            return None
        if prompt_tokens < 0 or completion_tokens < 0:
            return None
        values: dict[str, Any] = {
            "model": _first_string(metadata, _MODEL_KEYS) or fallback_model or "unknown",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        if total_tokens is not None:
            values["total_tokens"] = total_tokens
        cached_tokens = _first_token_count(usage_mappings, _CACHED_TOKEN_KEYS)
        if cached_tokens is None:
            for detail_name in ("prompt_tokens_details", "input_tokens_details"):
                cached_tokens = _detail_token_count(
                    usage_mappings, detail_name, "cached_tokens"
                )
                if cached_tokens is not None:
                    break
        if cached_tokens is not None:
            values["cached_tokens"] = cached_tokens
        reasoning_tokens = None
        for detail_name in ("completion_tokens_details", "output_tokens_details"):
            reasoning_tokens = _detail_token_count(
                usage_mappings, detail_name, "reasoning_tokens"
            )
            if reasoning_tokens is not None:
                break
        if reasoning_tokens is not None:
            values["reasoning_tokens"] = reasoning_tokens
        cost = _first_cost(metadata + usage_mappings)
        if cost is not None:
            values["estimated_cost"] = cost
        return values
    except Exception:
        return None


def record_llm_usage(
    span: object | None,
    response: object,
    *,
    fallback_model: str | None = None,
    usage_category: str = "production",
) -> bool:
    values = extract_llm_usage(response, fallback_model=fallback_model)
    if span is None or values is None:
        return False
    try:
        return bool(span.record_llm_usage(usage_category=usage_category, **values))
    except Exception:
        return False
