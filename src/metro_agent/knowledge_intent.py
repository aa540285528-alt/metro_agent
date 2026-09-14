"""Deterministic routing for requests that require the published knowledge index."""

from metro_agent.memory.long_term.memory_intent import is_memory_query
from metro_agent.safety_policy import (
    is_diagnosis_request,
    is_high_risk_safety_request,
)





_EXPLICIT_KNOWLEDGE_TERMS = (
    "知识库",
    "规程",
    "sop",
    "标准作业",
    "系统架构",
    "设备原理",
    "技术参数",
    "通信原理",
)


def requires_published_knowledge(user_input: str) -> bool:
    """Return whether the deterministic graph route will run ``knowledge_agent``.

    Safety refusals, diagnosis, and memory recall each have their own graph route;
    they must remain available when the published index is unavailable.
    """

    normalized = user_input.strip()
    if not normalized:
        return False
    if (
        is_high_risk_safety_request(normalized)
        or is_diagnosis_request(normalized)
        or is_memory_query(normalized)
    ):
        return False
    lowered = normalized.lower()
    return any(term in lowered for term in _EXPLICIT_KNOWLEDGE_TERMS)
