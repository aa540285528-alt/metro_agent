from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Mapping, TypeVar

from metro_agent.observability.finalize_node import get_trace_registry
from metro_agent.observability.llm_usage import record_llm_usage


F = TypeVar("F", bound=Callable[..., Any])
_current_span: ContextVar[object | None] = ContextVar("current_trace_span", default=None)


def _trace_recorder(state: Mapping[str, Any]) -> object | None:
    trace_id = state.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id:
        return None
    try:
        return get_trace_registry().get(trace_id)
    except Exception:
        return None


def traced_node(event_type: str) -> Callable[[F], F]:
    """Add a best-effort span to a graph node without altering its business result."""

    def decorate(node: F) -> F:
        @wraps(node)
        def wrapped(state: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
            recorder = _trace_recorder(state)
            if recorder is None:
                return node(state, *args, **kwargs)
            try:
                span_context = recorder.span(event_type)
                span = span_context.__enter__()
            except Exception:
                return node(state, *args, **kwargs)
            token = _current_span.set(span)
            try:
                result = node(state, *args, **kwargs)
            except BaseException as error:
                try:
                    span_context.__exit__(type(error), error, error.__traceback__)
                except Exception:
                    pass
                raise
            else:
                try:
                    span_context.__exit__(None, None, None)
                except Exception:
                    pass
                return result
            finally:
                _current_span.reset(token)

        return wrapped  # type: ignore[return-value]

    return decorate


def record_current_llm_usage(
    response: object,
    *,
    fallback_model: str | None = None,
    usage_category: str = "production",
) -> bool:
    return record_llm_usage(
        _current_span.get(),
        response,
        fallback_model=fallback_model,
        usage_category=usage_category,
    )
