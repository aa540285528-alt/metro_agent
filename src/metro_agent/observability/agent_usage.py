from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from metro_agent.observability.llm_usage import extract_llm_usage


LLM_USAGE_RECORDS_KEY = "llm_usage_records"


def capture_response_usages(
    response: object,
    *,
    fallback_model: str,
    call_name: str,
) -> list[dict[str, Any]]:
    """Extract every provider-backed model response from an agent invocation result."""
    records: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(value: object) -> None:
        value_id = id(value)
        if value_id in seen:
            return
        seen.add(value_id)
        usage = extract_llm_usage(value, fallback_model=fallback_model)
        if usage is not None:
            records.append(
                {
                    **usage,
                    "usage_source": "provider_response",
                    "usage_summary": {"call": call_name},
                }
            )
            return
        if isinstance(value, Mapping):
            for child in value.values():
                visit(child)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                visit(child)
            return
        messages = getattr(value, "messages", None)
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes, bytearray)):
            for child in messages:
                visit(child)

    visit(response)
    return [
        {**record, "usage_summary": {"call": f"{call_name}[{index}]"}}
        if len(records) > 1
        else record
        for index, record in enumerate(records, start=1)
    ]
