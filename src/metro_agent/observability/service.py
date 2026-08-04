from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Condition, Event, RLock
from time import monotonic, perf_counter_ns
from typing import Any, Callable, Iterator, Mapping, Protocol
from uuid import uuid4

from sqlalchemy.orm import Session

from metro_agent.observability.models import AgentSpan, AgentTrace, LlmUsage, RagRetrieval, ToolCall
from metro_agent.observability.trace_types import TraceEvent


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TraceStore(Protocol):
    def create_trace(
        self,
        trace_id: str,
        request_attributes: Mapping[str, Any],
        started_at: datetime,
    ) -> bool: ...

    def append_span(self, event: TraceEvent) -> bool: ...

    def append_llm_usage(self, values: Mapping[str, Any]) -> bool: ...

    def append_tool_call(self, values: Mapping[str, Any]) -> bool: ...

    def append_rag_retrieval(self, values: Mapping[str, Any]) -> bool: ...

    def finish_trace(
        self, trace_id: str, status: str, summary: Mapping[str, Any], finished_at: datetime
    ) -> bool: ...

    def update_trace_summary(self, trace_id: str, summary: Mapping[str, Any]) -> bool: ...

    def update_span_artifact(
        self,
        trace_id: str,
        span_id: str,
        artifact_uri: str,
        attributes: Mapping[str, Any],
    ) -> bool: ...


class _UnavailableTraceStore:
    """No-op store used when the optional default database cannot initialize."""

    def create_trace(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def append_span(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def append_llm_usage(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def append_tool_call(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def append_rag_retrieval(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def finish_trace(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def update_trace_summary(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def update_span_artifact(self, *args: Any, **kwargs: Any) -> bool:
        return False


class InMemoryTraceStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self.traces: dict[str, AgentTrace] = {}
        self.spans: list[AgentSpan] = []
        self.usages: list[LlmUsage] = []
        self.tool_calls: list[ToolCall] = []
        self.rag_retrievals: list[RagRetrieval] = []

    def create_trace(
        self,
        trace_id: str,
        request_attributes: Mapping[str, Any],
        started_at: datetime,
    ) -> bool:
        with self._lock:
            if trace_id in self.traces:
                return True
            self.traces[trace_id] = AgentTrace(
                id=trace_id,
                thread_id=_optional_string(request_attributes.get("thread_id")),
                user_id=_optional_string(request_attributes.get("user_id")),
                round_id=_optional_string(request_attributes.get("round_id")),
                request_summary=dict(request_attributes),
                status="running",
                started_at=started_at,
            )
        return True

    def append_span(self, event: TraceEvent) -> bool:
        with self._lock:
            self.spans.append(
                AgentSpan(
                    trace_id=event.trace_id,
                    span_id=event.span_id,
                    parent_span_id=event.parent_span_id,
                    event_type=event.event_type,
                    status=event.status,
                    started_at=_iso_to_datetime(event.started_at),
                    finished_at=_iso_to_datetime(event.finished_at)
                    if event.finished_at
                    else None,
                    duration_ms=event.duration_ms,
                    agent=event.agent,
                    step_id=event.step_id,
                    attempt=event.attempt,
                    error_type=_optional_string(event.attributes.get("error_type")),
                    attributes=event.to_dict()["attributes"],
                    artifact_uri=event.artifact_uri,
                )
            )
        return True

    def append_llm_usage(self, values: Mapping[str, Any]) -> bool:
        with self._lock:
            self.usages.append(LlmUsage(**dict(values)))
        return True

    def append_tool_call(self, values: Mapping[str, Any]) -> bool:
        with self._lock:
            self.tool_calls.append(ToolCall(**dict(values)))
        return True

    def append_rag_retrieval(self, values: Mapping[str, Any]) -> bool:
        with self._lock:
            self.rag_retrievals.append(RagRetrieval(**dict(values)))
        return True

    def finish_trace(
        self, trace_id: str, status: str, summary: Mapping[str, Any], finished_at: datetime
    ) -> bool:
        with self._lock:
            trace = self.traces.get(trace_id)
            if trace is None:
                return False
            trace.status = status
            trace.finished_at = finished_at
            trace.duration_ms = _summary_duration_ms(summary, trace.started_at, finished_at)
            trace.critical_path_latency_ms = _optional_float(
                summary.get("critical_path_latency_ms")
            )
            trace.total_tokens = _optional_int(summary.get("total_tokens"))
            trace.estimated_cost = _optional_float(summary.get("estimated_cost"))
            trace.artifact_uri = _optional_string(summary.get("artifact_uri"))
            trace.request_summary = {**(trace.request_summary or {}), "trace_summary": dict(summary)}
        return True

    def update_trace_summary(self, trace_id: str, summary: Mapping[str, Any]) -> bool:
        with self._lock:
            trace = self.traces.get(trace_id)
            if trace is None:
                return False
            trace.critical_path_latency_ms = _optional_float(
                summary.get("critical_path_latency_ms")
            )
            trace.total_tokens = _optional_int(summary.get("total_tokens"))
            trace.estimated_cost = _optional_float(summary.get("estimated_cost"))
            trace.artifact_uri = _optional_string(summary.get("artifact_uri"))
            trace.request_summary = {**(trace.request_summary or {}), "trace_summary": dict(summary)}
        return True

    def update_span_artifact(
        self,
        trace_id: str,
        span_id: str,
        artifact_uri: str,
        attributes: Mapping[str, Any],
    ) -> bool:
        with self._lock:
            for span in self.spans:
                if span.trace_id == trace_id and span.span_id == span_id:
                    span.artifact_uri = artifact_uri
                    span.attributes = {**(span.attributes or {}), **dict(attributes)}
                    return True
        return False


class SqlAlchemyTraceStore:
    """Best-effort SQLAlchemy writer. Database configuration is imported lazily."""

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        if session_factory is None:
            from metro_agent.storage.history.database import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory
        self.last_error: str | None = None
        self.cleanup_errors: list[str] = []

    def _write(self, operation: Callable[[Session], None]) -> bool:
        session: Session | None = None
        primary_error: Exception | None = None
        cleanup_errors: list[Exception] = []
        try:
            session = self._session_factory()
            operation(session)
            session.commit()
        except Exception as error:
            primary_error = error
        finally:
            if primary_error is not None and session is not None:
                try:
                    session.rollback()
                except Exception as rollback_error:
                    cleanup_errors.append(rollback_error)
            if session is not None:
                try:
                    session.close()
                except Exception as close_error:
                    cleanup_errors.append(close_error)

        self.last_error = type(primary_error).__name__ if primary_error is not None else None
        self.cleanup_errors = [type(error).__name__ for error in cleanup_errors]
        return primary_error is None

    def create_trace(
        self,
        trace_id: str,
        request_attributes: Mapping[str, Any],
        started_at: datetime,
    ) -> bool:
        return self._write(
            lambda session: session.add(
                AgentTrace(
                    id=trace_id,
                    thread_id=_optional_string(request_attributes.get("thread_id")),
                    user_id=_optional_string(request_attributes.get("user_id")),
                    round_id=_optional_string(request_attributes.get("round_id")),
                    request_summary=dict(request_attributes),
                    status="running",
                    started_at=started_at,
                )
            )
        )

    def append_span(self, event: TraceEvent) -> bool:
        return self._write(
            lambda session: session.add(
                AgentSpan(
                    trace_id=event.trace_id,
                    span_id=event.span_id,
                    parent_span_id=event.parent_span_id,
                    event_type=event.event_type,
                    status=event.status,
                    started_at=_iso_to_datetime(event.started_at),
                    finished_at=_iso_to_datetime(event.finished_at)
                    if event.finished_at
                    else None,
                    duration_ms=event.duration_ms,
                    agent=event.agent,
                    step_id=event.step_id,
                    attempt=event.attempt,
                    error_type=_optional_string(event.attributes.get("error_type")),
                    attributes=event.to_dict()["attributes"],
                    artifact_uri=event.artifact_uri,
                )
            )
        )

    def append_llm_usage(self, values: Mapping[str, Any]) -> bool:
        return self._write(lambda session: session.add(LlmUsage(**dict(values))))

    def append_tool_call(self, values: Mapping[str, Any]) -> bool:
        return self._write(lambda session: session.add(ToolCall(**dict(values))))

    def append_rag_retrieval(self, values: Mapping[str, Any]) -> bool:
        return self._write(lambda session: session.add(RagRetrieval(**dict(values))))

    def finish_trace(
        self, trace_id: str, status: str, summary: Mapping[str, Any], finished_at: datetime
    ) -> bool:
        def finish(session: Session) -> None:
            trace = session.get(AgentTrace, trace_id)
            if trace is None:
                raise LookupError("trace not found")
            trace.status = status
            trace.finished_at = finished_at
            trace.duration_ms = _summary_duration_ms(summary, trace.started_at, finished_at)
            trace.critical_path_latency_ms = _optional_float(
                summary.get("critical_path_latency_ms")
            )
            trace.total_tokens = _optional_int(summary.get("total_tokens"))
            trace.estimated_cost = _optional_float(summary.get("estimated_cost"))
            trace.artifact_uri = _optional_string(summary.get("artifact_uri"))
            trace.request_summary = {
                **(trace.request_summary or {}),
                "trace_summary": dict(summary),
            }

        return self._write(finish)

    def update_trace_summary(self, trace_id: str, summary: Mapping[str, Any]) -> bool:
        def update(session: Session) -> None:
            trace = session.get(AgentTrace, trace_id)
            if trace is None:
                raise LookupError("trace not found")
            trace.critical_path_latency_ms = _optional_float(
                summary.get("critical_path_latency_ms")
            )
            trace.total_tokens = _optional_int(summary.get("total_tokens"))
            trace.estimated_cost = _optional_float(summary.get("estimated_cost"))
            trace.artifact_uri = _optional_string(summary.get("artifact_uri"))
            trace.request_summary = {
                **(trace.request_summary or {}),
                "trace_summary": dict(summary),
            }

        return self._write(update)

    def update_span_artifact(
        self,
        trace_id: str,
        span_id: str,
        artifact_uri: str,
        attributes: Mapping[str, Any],
    ) -> bool:
        def update(session: Session) -> None:
            span = session.get(
                AgentSpan,
                {"trace_id": trace_id, "span_id": span_id},
            )
            if span is None:
                raise LookupError("span not found")
            span.artifact_uri = artifact_uri
            span.attributes = {**(span.attributes or {}), **dict(attributes)}

        return self._write(update)


@dataclass
class _DeferredObservation:
    operation: str
    values: dict[str, Any]


@dataclass
class TraceSpan:
    recorder: TraceRecorder
    event: TraceEvent | None
    observations: list[_DeferredObservation] = field(default_factory=list)
    result_status: str = "success"
    result_attributes: Mapping[str, Any] | None = None

    @property
    def span_id(self) -> str | None:
        return self.event.span_id if self.event else None

    def record_llm_usage(self, **values: Any) -> bool:
        return self.recorder._prepare_llm_usage(self, values)

    def record_tool_call(self, **values: Any) -> bool:
        return self.recorder._prepare_observation(self, "append_tool_call", values)

    def record_rag_retrieval(self, **values: Any) -> bool:
        return self.recorder._prepare_observation(self, "append_rag_retrieval", values)

    def set_result(
        self, status: str, attributes: Mapping[str, Any] | None = None
    ) -> None:
        if not isinstance(status, str) or not status:
            raise ValueError("span result status must be a non-empty string")
        self.result_status = status
        self.result_attributes = attributes


class TraceRecorder:
    def __init__(
        self,
        trace_id: str,
        store: TraceStore,
        request_attributes: Mapping[str, Any] | None = None,
        finish_wait_timeout_seconds: float = 1.0,
    ) -> None:
        if finish_wait_timeout_seconds < 0:
            raise ValueError("finish_wait_timeout_seconds cannot be negative")
        self.trace_id = trace_id
        self.store = store
        self.errors: list[str] = []
        self._lock = RLock()
        self._close_condition = Condition(self._lock)
        self._started_at = _utc_now()
        self._started_perf_ns = perf_counter_ns()
        self._total_tokens = 0
        self._estimated_cost = 0.0
        self._final_summary: dict[str, Any] = {}
        self._finished_at: datetime | None = None
        self._finish_persisted = False
        self._finished = False
        self._closing = False
        self._finish_wait_timeout_seconds = finish_wait_timeout_seconds
        self._active_span_ids: set[str] = set()
        self._known_span_ids: set[str] = set()
        self._completed_events: list[dict[str, Any]] = []
        self._recorded_llm_usages: list[dict[str, Any]] = []
        self._recorded_tool_calls: list[dict[str, Any]] = []
        self._call_store(
            "create_trace",
            self.store.create_trace,
            trace_id,
            dict(request_attributes or {}),
            self._started_at,
        )

    @contextmanager
    def span(
        self,
        event_type: str,
        agent: str | None = None,
        step_id: str | None = None,
        parent_span_id: str | None = None,
        attempt: int | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[TraceSpan]:
        with self._close_condition:
            unavailable = self._finished or self._closing
        if unavailable:
            yield TraceSpan(recorder=self, event=None)
            return
        try:
            event = TraceEvent.start(
                trace_id=self.trace_id,
                span_id=uuid4().hex,
                event_type=event_type,
                agent=agent,
                step_id=step_id,
                parent_span_id=parent_span_id,
                attempt=attempt,
                attributes=attributes,
            )
        except Exception as error:
            self._capture_error("start_span", error)
            span = TraceSpan(recorder=self, event=None)
            yield span
            return

        with self._close_condition:
            unavailable = self._finished or self._closing
            if not unavailable:
                self._active_span_ids.add(event.span_id)
        if unavailable:
            yield TraceSpan(recorder=self, event=None)
            return
        span = TraceSpan(recorder=self, event=event)
        try:
            yield span
        except Exception as error:
            attributes = {"error_type": type(error).__name__}
            if span.result_attributes:
                attributes.update(span.result_attributes)
            self._complete_span(span, "failed", attributes)
            raise
        else:
            self._complete_span(span, span.result_status, span.result_attributes)

    def record_llm_usage(self, span_id: str, **values: Any) -> bool:
        return self._prepare_llm_usage(None, {"span_id": span_id, **values})

    def record_tool_call(self, span_id: str, **values: Any) -> bool:
        return self._prepare_observation(None, "append_tool_call", {"span_id": span_id, **values})

    def record_rag_retrieval(self, span_id: str, **values: Any) -> bool:
        return self._prepare_observation(
            None, "append_rag_retrieval", {"span_id": span_id, **values}
        )

    def finish(self, status: str, summary: Mapping[str, Any] | None = None) -> bool:
        with self._close_condition:
            if self._finished or self._closing:
                return False
            self._closing = True
            deadline = monotonic() + self._finish_wait_timeout_seconds
            timed_out = False
            while self._active_span_ids:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                self._close_condition.wait(timeout=remaining)
            self._finished = True
            self._closing = False
            finished_at = _utc_now()
            values = {
                **dict(summary or {}),
                "total_tokens": self._total_tokens,
                "estimated_cost": self._estimated_cost,
                "e2e_latency_ms": (perf_counter_ns() - self._started_perf_ns) / 1_000_000,
            }
            if timed_out:
                values["trace_close_timeout"] = True
                values["unfinished_span_count"] = len(self._active_span_ids)
            self._final_summary = dict(values)
            self._finished_at = finished_at
        persisted = self._call_store(
            "finish_trace", self.store.finish_trace, self.trace_id, status, values, finished_at
        )
        with self._close_condition:
            self._finish_persisted = persisted
        return persisted

    @property
    def final_summary(self) -> dict[str, Any]:
        with self._close_condition:
            return dict(self._final_summary)

    def evidence_snapshot(self) -> dict[str, Any]:
        """Return local, structured observations for the immutable trace artifact."""
        with self._close_condition:
            return {
                "trace_id": self.trace_id,
                "spans": [dict(event) for event in self._completed_events],
                "llm_usages": [dict(usage) for usage in self._recorded_llm_usages],
                "tool_calls": [dict(call) for call in self._recorded_tool_calls],
                "errors": list(self.errors),
            }

    def update_final_summary(self, summary: Mapping[str, Any]) -> bool:
        with self._close_condition:
            if not self._finished or not self._finish_persisted:
                return False
            previous = dict(self._final_summary)
            values = {**previous, **dict(summary)}
            for field in ("e2e_latency_ms", "total_tokens", "estimated_cost"):
                values[field] = previous[field]

        persisted = self._call_store(
            "update_trace_summary", self.store.update_trace_summary, self.trace_id, values
        )
        if persisted:
            with self._close_condition:
                self._final_summary = values
        return persisted

    def update_span_artifact(
        self,
        span_id: str,
        artifact_uri: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> bool:
        if not isinstance(span_id, str) or not span_id:
            return False
        if not isinstance(artifact_uri, str) or not artifact_uri:
            return False
        with self._close_condition:
            if (
                not self._finished
                or not self._finish_persisted
                or span_id not in self._known_span_ids
            ):
                return False
        return self._call_store(
            "update_span_artifact",
            self.store.update_span_artifact,
            self.trace_id,
            span_id,
            artifact_uri,
            dict(attributes or {}),
        )

    def _complete_span(
        self,
        span: TraceSpan,
        status: str,
        attributes: Mapping[str, Any] | None,
    ) -> None:
        if span.event is None:
            return
        event = span.event.finish(status=status, attributes=attributes)
        with self._close_condition:
            try:
                if self._finished or event.span_id not in self._active_span_ids:
                    return
                self._completed_events.append(event.to_dict())
                span_persisted = self._call_store("append_span", self.store.append_span, event)
                if span_persisted:
                    self._known_span_ids.add(event.span_id)
                for observation in span.observations:
                    if span_persisted:
                        self._call_store(
                            observation.operation,
                            getattr(self.store, observation.operation),
                            observation.values,
                        )
                    if observation.operation == "append_llm_usage":
                        self._recorded_llm_usages.append(dict(observation.values))
                        self._total_tokens += observation.values["total_tokens"]
                        estimated_cost = observation.values.get("estimated_cost")
                        if isinstance(estimated_cost, (int, float)) and not isinstance(estimated_cost, bool):
                            self._estimated_cost += max(0.0, float(estimated_cost))
                    elif observation.operation == "append_tool_call":
                        self._recorded_tool_calls.append(dict(observation.values))
            finally:
                self._active_span_ids.discard(event.span_id)
                self._close_condition.notify_all()

    def _prepare_llm_usage(
        self, span: TraceSpan | None, values: Mapping[str, Any]
    ) -> bool:
        if span is not None and span.event is None:
            return False
        try:
            prompt_tokens = _required_token_count(values, "prompt_tokens")
            completion_tokens = _required_token_count(values, "completion_tokens")
            reasoning_tokens = _optional_token_count(values.get("reasoning_tokens", 0))
            cached_tokens = _optional_token_count(values.get("cached_tokens", 0))
            provided_total_tokens = _optional_int(values.get("total_tokens"))
            if provided_total_tokens is None:
                total_tokens = prompt_tokens + completion_tokens
            elif provided_total_tokens < 0:
                raise ValueError("total_tokens must be non-negative")
            else:
                total_tokens = provided_total_tokens
            span_id = span.span_id if span else values.get("span_id")
            if not isinstance(span_id, str) or not span_id:
                raise ValueError("span_id is required")
            model = values.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError("model is required")
            payload = {
                "trace_id": self.trace_id,
                "span_id": span_id,
                "provider": _optional_string(values.get("provider")),
                "model": model,
                "prompt_version": _optional_string(values.get("prompt_version")),
                "usage_category": values.get("usage_category", "production"),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": total_tokens,
                "estimated_cost": _optional_float(values.get("estimated_cost")),
                "latency_ms": _optional_float(values.get("latency_ms")),
                "usage_source": _optional_string(values.get("usage_source")),
                "usage_summary": dict(values.get("usage_summary") or {}),
                "artifact_uri": _optional_string(values.get("artifact_uri")),
            }
        except Exception as error:
            self._capture_error("append_llm_usage", error)
            return False
        with self._close_condition:
            if span is not None:
                if self._finished or span_id not in self._active_span_ids:
                    return False
                span.observations.append(_DeferredObservation("append_llm_usage", payload))
                return True
            if self._finished or self._closing or span_id not in self._known_span_ids:
                return False
            if self._call_store("append_llm_usage", self.store.append_llm_usage, payload):
                self._recorded_llm_usages.append(dict(payload))
                self._total_tokens += total_tokens
                estimated_cost = payload.get("estimated_cost")
                if isinstance(estimated_cost, (int, float)) and not isinstance(
                    estimated_cost, bool
                ):
                    self._estimated_cost += max(0.0, float(estimated_cost))
                return True
            return False

    def _prepare_observation(
        self,
        span: TraceSpan | None,
        operation: str,
        values: Mapping[str, Any],
    ) -> bool:
        if span is not None and span.event is None:
            return False
        try:
            span_id = span.span_id if span else values.get("span_id")
            if not isinstance(span_id, str) or not span_id:
                raise ValueError("span_id is required")
            payload = self._observation_payload(operation, span_id, values)
        except Exception as error:
            self._capture_error(operation, error)
            return False
        with self._close_condition:
            if span is not None:
                if self._finished or span_id not in self._active_span_ids:
                    return False
                span.observations.append(_DeferredObservation(operation, payload))
                return True
            if self._finished or self._closing or span_id not in self._known_span_ids:
                return False
            persisted = self._call_store(operation, getattr(self.store, operation), payload)
            if operation == "append_tool_call":
                self._recorded_tool_calls.append(dict(payload))
            return persisted

    def _observation_payload(
        self, operation: str, span_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        if operation == "append_tool_call":
            tool_name = values.get("tool_name")
            status = values.get("status")
            if not isinstance(tool_name, str) or not tool_name:
                raise ValueError("tool_name is required")
            if not isinstance(status, str) or not status:
                raise ValueError("status is required")
            return {
                "trace_id": self.trace_id,
                "span_id": span_id,
                "tool_name": tool_name,
                "status": status,
                "attempt": _optional_int(values.get("attempt")),
                "duration_ms": _optional_float(values.get("duration_ms")),
                "arguments_summary": dict(values.get("arguments_summary") or {}),
                "arguments_hash": _optional_string(values.get("arguments_hash")),
                "result_summary": dict(values.get("result_summary") or {}),
                "result_hash": _optional_string(values.get("result_hash")),
                "artifact_uri": _optional_string(values.get("artifact_uri")),
            }
        return {
            "trace_id": self.trace_id,
            "span_id": span_id,
            "query_summary": _optional_string(values.get("query_summary")),
            "index_build_id": _optional_string(values.get("index_build_id")),
            "chunk_id": _optional_string(values.get("chunk_id")),
            "document_id": _optional_string(values.get("document_id")),
            "rank": _optional_int(values.get("rank")),
            "raw_score": _optional_float(values.get("raw_score")),
            "rerank_score": _optional_float(values.get("rerank_score")),
            "cited": bool(values.get("cited", False)),
            "result_summary": dict(values.get("result_summary") or {}),
            "artifact_uri": _optional_string(values.get("artifact_uri")),
        }

    def _call_store(self, operation: str, method: Callable[..., bool], *args: Any) -> bool:
        try:
            success = method(*args)
        except Exception as error:
            self._capture_error(operation, error)
            return False
        if not success:
            self._capture_error(operation, RuntimeError("store returned false"))
            return False
        return True

    def _capture_error(self, operation: str, error: Exception) -> None:
        with self._lock:
            self.errors.append(f"{operation}:{type(error).__name__}")


class TraceRegistry:
    def __init__(self, store_factory: Callable[[], TraceStore] | None = None) -> None:
        self._store_factory = store_factory or SqlAlchemyTraceStore
        self._recorders: dict[str, TraceRecorder] = {}
        self._starting: dict[str, Event] = {}
        self._lock = RLock()

    def start(
        self, trace_id: str, request_attributes: Mapping[str, Any] | None = None
    ) -> TraceRecorder:
        while True:
            with self._lock:
                recorder = self._recorders.get(trace_id)
                if recorder is not None:
                    return recorder
                started = self._starting.get(trace_id)
                if started is None:
                    started = Event()
                    self._starting[trace_id] = started
                    break
            started.wait()

        initialization_error: Exception | None = None
        try:
            store = self._store_factory()
        except Exception as error:
            store = _UnavailableTraceStore()
            initialization_error = error
        try:
            recorder = TraceRecorder(
                trace_id=trace_id,
                store=store,
                request_attributes=request_attributes,
            )
        except Exception as error:
            recorder = TraceRecorder(trace_id=trace_id, store=_UnavailableTraceStore())
            initialization_error = initialization_error or error
        if initialization_error is not None:
            recorder._capture_error("store_initialization", initialization_error)

        with self._lock:
            existing = self._recorders.get(trace_id)
            if existing is None:
                self._recorders[trace_id] = recorder
                existing = recorder
            self._starting.pop(trace_id).set()
            return existing

    def get(self, trace_id: str) -> TraceRecorder | None:
        with self._lock:
            return self._recorders.get(trace_id)

    def finish(
        self, trace_id: str, status: str, summary: Mapping[str, Any] | None = None
    ) -> bool:
        with self._lock:
            recorder = self._recorders.get(trace_id)
        if recorder is None:
            return False
        result = recorder.finish(status, summary)
        with self._lock:
            if self._recorders.get(trace_id) is recorder:
                self._recorders.pop(trace_id, None)
        return result


def _duration_ms(started_at: datetime, finished_at: datetime) -> float:
    normalized_started_at = _as_utc(started_at)
    normalized_finished_at = _as_utc(finished_at)
    return max(0.0, (normalized_finished_at - normalized_started_at).total_seconds() * 1000)


def _summary_duration_ms(
    summary: Mapping[str, Any], started_at: datetime, finished_at: datetime
) -> float:
    e2e_latency_ms = _optional_float(summary.get("e2e_latency_ms"))
    return e2e_latency_ms if e2e_latency_ms is not None else _duration_ms(started_at, finished_at)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("value must be an integer")
    return value


def _required_token_count(values: Mapping[str, Any], name: str) -> int:
    value = _optional_int(values.get(name))
    if value is None or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_token_count(value: Any) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        return 0
    if parsed < 0:
        raise ValueError("token count must be non-negative")
    return parsed


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be numeric")
    return float(value)
