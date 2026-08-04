from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Callable

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from metro_agent.observability.models import AgentSpan, AgentTrace, EvalRun, LlmUsage, RagRetrieval, ToolCall


class TraceNotFound(LookupError):
    """Raised when a trace does not exist in the requesting owner's scope."""


@dataclass(frozen=True)
class MonitoringFilter:
    user_id: str
    started_after: datetime | None = None
    started_before: datetime | None = None
    status: str | None = None
    agent: str | None = None
    model: str | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class TraceListItem:
    id: str
    status: str
    request_summary: str
    path: list[str]
    started_at: datetime
    duration_ms: float | None
    critical_path_latency_ms: float | None
    total_tokens: int | None
    estimated_cost: float | None


@dataclass(frozen=True)
class TracePage:
    items: list[TraceListItem]
    total: int


@dataclass(frozen=True)
class SpanDetail:
    span_id: str
    parent_span_id: str | None
    event_type: str
    status: str
    started_at: datetime
    duration_ms: float | None
    agent: str | None
    step_id: str | None
    attempt: int | None
    error_type: str | None
    attributes: dict
    artifact_uri: str | None


@dataclass(frozen=True)
class LlmUsageDetail:
    span_id: str
    provider: str | None
    model: str
    usage_category: str
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None
    latency_ms: float | None
    usage_source: str | None


@dataclass(frozen=True)
class ToolCallDetail:
    span_id: str
    tool_name: str
    status: str
    attempt: int | None
    duration_ms: float | None
    arguments_summary: dict
    result_summary: dict
    artifact_uri: str | None


@dataclass(frozen=True)
class RagRetrievalDetail:
    span_id: str
    query_summary: str | None
    index_build_id: str | None
    chunk_id: str | None
    document_id: str | None
    rank: int | None
    raw_score: float | None
    rerank_score: float | None
    cited: bool
    result_summary: dict
    artifact_uri: str | None


@dataclass(frozen=True)
class TraceDetail:
    id: str
    status: str
    request_summary: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None
    critical_path_latency_ms: float | None
    total_tokens: int | None
    estimated_cost: float | None
    artifact_uri: str | None
    spans: list[SpanDetail]
    dag_edges: list[tuple[str, str]]
    llm_usages: list[LlmUsageDetail]
    tool_calls: list[ToolCallDetail]
    rag_retrievals: list[RagRetrievalDetail]


@dataclass(frozen=True)
class MonitoringSummary:
    trace_count: int
    success_rate: float | None
    average_e2e_latency_ms: float | None
    p50_e2e_latency_ms: float | None
    p95_e2e_latency_ms: float | None
    average_critical_path_latency_ms: float | None
    total_tokens: int
    estimated_cost: float
    tool_call_count: int
    tool_failure_rate: float | None


@dataclass(frozen=True)
class EvaluationRunListItem:
    id: str
    suite: str
    category: str
    dataset_version: str | None
    code_version: str | None
    judge_config: dict
    summary: dict
    artifact_uri: str
    started_at: datetime
    finished_at: datetime | None


class MonitoringQueryService:
    """Read-only, owner-scoped projections over persisted observability data."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def list_traces(self, filters: MonitoringFilter) -> TracePage:
        with self._session_factory() as session:
            statement = self._filtered_trace_statement(filters)
            traces = list(
                session.scalars(
                    statement.order_by(AgentTrace.started_at.desc(), AgentTrace.id.desc())
                    .offset(max(0, filters.offset))
                    .limit(max(1, min(filters.limit, 100)))
                )
            )
            total = len(list(session.scalars(self._filtered_trace_statement(filters))))
            paths = self._paths_for_traces(session, [trace.id for trace in traces])
            return TracePage(
                items=[self._list_item(trace, paths.get(trace.id, [])) for trace in traces],
                total=total,
            )

    def get_trace(self, trace_id: str, user_id: str) -> TraceDetail:
        with self._session_factory() as session:
            trace = session.scalar(
                select(AgentTrace).where(AgentTrace.id == trace_id, AgentTrace.user_id == user_id)
            )
            if trace is None:
                raise TraceNotFound(trace_id)

            spans = list(
                session.scalars(
                    select(AgentSpan)
                    .where(AgentSpan.trace_id == trace.id)
                    .order_by(AgentSpan.started_at.asc(), AgentSpan.span_id.asc())
                )
            )
            spans = _order_spans_for_display(spans)
            return TraceDetail(
                id=trace.id,
                status=trace.status,
                request_summary=_display_summary(trace.request_summary),
                started_at=trace.started_at,
                finished_at=trace.finished_at,
                duration_ms=trace.duration_ms,
                critical_path_latency_ms=trace.critical_path_latency_ms,
                total_tokens=trace.total_tokens,
                estimated_cost=trace.estimated_cost,
                artifact_uri=trace.artifact_uri,
                spans=[_span_detail(span) for span in spans],
                dag_edges=[
                    (span.parent_span_id, span.span_id)
                    for span in spans
                    if span.parent_span_id is not None
                ],
                llm_usages=[
                    _usage_detail(value)
                    for value in session.scalars(
                        select(LlmUsage)
                        .where(LlmUsage.trace_id == trace.id)
                        .order_by(LlmUsage.id.asc())
                    )
                ],
                tool_calls=[
                    _tool_detail(value)
                    for value in session.scalars(
                        select(ToolCall)
                        .where(ToolCall.trace_id == trace.id)
                        .order_by(ToolCall.id.asc())
                    )
                ],
                rag_retrievals=[
                    _rag_detail(value)
                    for value in session.scalars(
                        select(RagRetrieval)
                        .where(RagRetrieval.trace_id == trace.id)
                        .order_by(RagRetrieval.id.asc())
                    )
                ],
            )

    def summary(self, filters: MonitoringFilter) -> MonitoringSummary:
        with self._session_factory() as session:
            traces = list(session.scalars(self._filtered_trace_statement(filters)))
            trace_ids = [trace.id for trace in traces]
            tool_calls = (
                list(
                    session.scalars(select(ToolCall).where(ToolCall.trace_id.in_(trace_ids)))
                )
                if trace_ids
                else []
            )
            successful = sum(trace.status in {"completed", "success"} for trace in traces)
            durations = [trace.duration_ms for trace in traces if trace.duration_ms is not None]
            critical = [
                trace.critical_path_latency_ms
                for trace in traces
                if trace.critical_path_latency_ms is not None
            ]
            failures = sum(call.status not in {"completed", "success"} for call in tool_calls)
            return MonitoringSummary(
                trace_count=len(traces),
                success_rate=_rate(successful, len(traces)),
                average_e2e_latency_ms=_average(durations),
                p50_e2e_latency_ms=_percentile(durations, 0.50),
                p95_e2e_latency_ms=_percentile(durations, 0.95),
                average_critical_path_latency_ms=_average(critical),
                total_tokens=sum(trace.total_tokens or 0 for trace in traces),
                estimated_cost=round(sum(trace.estimated_cost or 0.0 for trace in traces), 6),
                tool_call_count=len(tool_calls),
                tool_failure_rate=_rate(failures, len(tool_calls)),
            )

    def list_evaluations(self, *, category: str | None = None) -> list[EvaluationRunListItem]:
        with self._session_factory() as session:
            statement = select(EvalRun)
            if category:
                statement = statement.where(EvalRun.category == category)
            runs = session.scalars(
                statement.order_by(EvalRun.started_at.desc(), EvalRun.id.desc()).limit(100)
            )
            return [
                EvaluationRunListItem(
                    id=run.id,
                    suite=run.suite,
                    category=run.category,
                    dataset_version=run.dataset_version,
                    code_version=run.code_version,
                    judge_config=dict(run.judge_config or {}),
                    summary=dict(run.summary or {}),
                    artifact_uri=run.artifact_uri,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
                for run in runs
            ]

    def _filtered_trace_statement(self, filters: MonitoringFilter) -> Select[tuple[AgentTrace]]:
        statement = select(AgentTrace).where(AgentTrace.user_id == filters.user_id)
        if filters.started_after is not None:
            statement = statement.where(AgentTrace.started_at >= filters.started_after)
        if filters.started_before is not None:
            statement = statement.where(AgentTrace.started_at <= filters.started_before)
        if filters.status:
            statement = statement.where(AgentTrace.status == filters.status)
        if filters.agent:
            statement = statement.where(
                AgentTrace.id.in_(select(AgentSpan.trace_id).where(AgentSpan.agent == filters.agent))
            )
        if filters.model:
            statement = statement.where(
                AgentTrace.id.in_(select(LlmUsage.trace_id).where(LlmUsage.model == filters.model))
            )
        return statement

    @staticmethod
    def _paths_for_traces(session: Session, trace_ids: list[str]) -> dict[str, list[str]]:
        if not trace_ids:
            return {}
        paths: dict[str, list[str]] = {trace_id: [] for trace_id in trace_ids}
        spans = session.scalars(
            select(AgentSpan)
            .where(AgentSpan.trace_id.in_(trace_ids))
            .order_by(AgentSpan.trace_id.asc(), AgentSpan.started_at.asc(), AgentSpan.span_id.asc())
        )
        for span in spans:
            label = span.agent or span.event_type
            if label and label not in paths[span.trace_id]:
                paths[span.trace_id].append(label)
        return paths

    @staticmethod
    def _list_item(trace: AgentTrace, path: list[str]) -> TraceListItem:
        return TraceListItem(
            id=trace.id,
            status=trace.status,
            request_summary=_display_summary(trace.request_summary),
            path=path,
            started_at=trace.started_at,
            duration_ms=trace.duration_ms,
            critical_path_latency_ms=trace.critical_path_latency_ms,
            total_tokens=trace.total_tokens,
            estimated_cost=trace.estimated_cost,
        )


def _display_summary(value: dict | None) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("display", "text", "request_summary"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate[:512]
    return ""


def _order_spans_for_display(spans: list[AgentSpan]) -> list[AgentSpan]:
    """Keep parent spans before their children for a readable trace timeline."""
    remaining = {span.span_id: span for span in spans}
    ordered: list[AgentSpan] = []

    while remaining:
        ready = [
            span
            for span in remaining.values()
            if span.parent_span_id is None or span.parent_span_id not in remaining
        ]
        if not ready:
            ready = list(remaining.values())
        ready.sort(key=lambda span: (span.started_at, span.span_id))
        for span in ready:
            ordered.append(span)
            remaining.pop(span.span_id, None)
    return ordered


def _span_detail(value: AgentSpan) -> SpanDetail:
    attributes = dict(value.attributes or {})
    return SpanDetail(
        span_id=value.span_id,
        parent_span_id=value.parent_span_id,
        event_type=value.event_type,
        status=value.status,
        started_at=value.started_at,
        duration_ms=value.duration_ms,
        agent=value.agent,
        step_id=value.step_id,
        attempt=value.attempt,
        error_type=value.error_type or _optional_text(attributes.get("error_type")),
        attributes=attributes,
        artifact_uri=value.artifact_uri,
    )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _usage_detail(value: LlmUsage) -> LlmUsageDetail:
    return LlmUsageDetail(
        span_id=value.span_id,
        provider=value.provider,
        model=value.model,
        usage_category=value.usage_category,
        prompt_tokens=value.prompt_tokens,
        completion_tokens=value.completion_tokens,
        reasoning_tokens=value.reasoning_tokens,
        cached_tokens=value.cached_tokens,
        total_tokens=value.total_tokens,
        estimated_cost=value.estimated_cost,
        latency_ms=value.latency_ms,
        usage_source=value.usage_source,
    )


def _tool_detail(value: ToolCall) -> ToolCallDetail:
    return ToolCallDetail(
        span_id=value.span_id,
        tool_name=value.tool_name,
        status=value.status,
        attempt=value.attempt,
        duration_ms=value.duration_ms,
        arguments_summary=dict(value.arguments_summary or {}),
        result_summary=dict(value.result_summary or {}),
        artifact_uri=value.artifact_uri,
    )


def _rag_detail(value: RagRetrieval) -> RagRetrievalDetail:
    return RagRetrievalDetail(
        span_id=value.span_id,
        query_summary=value.query_summary,
        index_build_id=value.index_build_id,
        chunk_id=value.chunk_id,
        document_id=value.document_id,
        rank=value.rank,
        raw_score=value.raw_score,
        rerank_score=value.rerank_score,
        cited=value.cited,
        result_summary=dict(value.result_summary or {}),
        artifact_uri=value.artifact_uri,
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(len(ordered) * quantile) - 1))
    return ordered[index]
