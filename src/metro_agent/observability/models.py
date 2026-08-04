from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from metro_agent.storage.history.models import Base


class AgentTrace(Base):
    __tablename__ = "agent_traces"
    __table_args__ = (
        Index("ix_agent_traces_created_at", "created_at"),
        Index("ix_agent_traces_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[str | None] = mapped_column(String(128))
    round_id: Mapped[str | None] = mapped_column(String(128))
    request_summary: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    critical_path_latency_ms: Mapped[float | None] = mapped_column(Float)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    artifact_uri: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    spans: Mapped[list[AgentSpan]] = relationship(
        back_populates="trace", cascade="all, delete-orphan"
    )


class AgentSpan(Base):
    __tablename__ = "agent_spans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trace_id", "parent_span_id"],
            ["agent_spans.trace_id", "agent_spans.span_id"],
            name="fk_agent_spans_parent_same_trace",
        ),
        PrimaryKeyConstraint("trace_id", "span_id"),
        Index("ix_agent_spans_trace_id", "trace_id"),
        Index("ix_agent_spans_step_id", "step_id"),
    )

    trace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_traces.id", ondelete="CASCADE"), nullable=False
    )
    span_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    agent: Mapped[str | None] = mapped_column(String(128))
    step_id: Mapped[str | None] = mapped_column(String(128))
    attempt: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(128))
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    artifact_uri: Mapped[str | None] = mapped_column(String(512))
    trace: Mapped[AgentTrace] = relationship(back_populates="spans")


class LlmUsage(Base):
    __tablename__ = "llm_usages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["agent_spans.trace_id", "agent_spans.span_id"],
            name="fk_llm_usages_span_same_trace",
            ondelete="CASCADE",
        ),
        Index("ix_llm_usages_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    span_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(128))
    usage_category: Mapped[str] = mapped_column(String(32), nullable=False, default="production")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    usage_source: Mapped[str | None] = mapped_column(String(32))
    usage_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    artifact_uri: Mapped[str | None] = mapped_column(String(512))


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["agent_spans.trace_id", "agent_spans.span_id"],
            name="fk_tool_calls_span_same_trace",
            ondelete="CASCADE",
        ),
        Index("ix_tool_calls_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    span_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    arguments_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    arguments_hash: Mapped[str | None] = mapped_column(String(64))
    result_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    artifact_uri: Mapped[str | None] = mapped_column(String(512))


class RagRetrieval(Base):
    __tablename__ = "rag_retrievals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["agent_spans.trace_id", "agent_spans.span_id"],
            name="fk_rag_retrievals_span_same_trace",
            ondelete="CASCADE",
        ),
        Index("ix_rag_retrievals_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    span_id: Mapped[str] = mapped_column(String(128), nullable=False)
    query_summary: Mapped[str | None] = mapped_column(String(512))
    index_build_id: Mapped[str | None] = mapped_column(String(128))
    chunk_id: Mapped[str | None] = mapped_column(String(256))
    document_id: Mapped[str | None] = mapped_column(String(256))
    rank: Mapped[int | None] = mapped_column(Integer)
    raw_score: Mapped[float | None] = mapped_column(Float)
    rerank_score: Mapped[float | None] = mapped_column(Float)
    cited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    artifact_uri: Mapped[str | None] = mapped_column(String(512))


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_eval_runs_suite_category", "suite", "category"),
        Index("ix_eval_runs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    suite: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_version: Mapped[str | None] = mapped_column(String(128))
    code_version: Mapped[str | None] = mapped_column(String(128))
    judge_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    artifact_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    case_results: Mapped[list[EvalCaseResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class EvalCaseResult(Base):
    __tablename__ = "eval_case_results"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", name="uq_eval_case_results_run_case"),
        Index("ix_eval_case_results_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    passed: Mapped[bool | None] = mapped_column(Boolean)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reason_summary: Mapped[str | None] = mapped_column(String(512))
    artifact_uri: Mapped[str | None] = mapped_column(String(512))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    run: Mapped[EvalRun] = relationship(back_populates="case_results")


class ChunkBuild(Base):
    __tablename__ = "chunk_builds"
    __table_args__ = (Index("ix_chunk_builds_created_at", "created_at"),)

    index_build_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    chunker_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    embedding_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    artifact_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    items: Mapped[list[ChunkItem]] = relationship(
        back_populates="build", cascade="all, delete-orphan"
    )


class ChunkItem(Base):
    __tablename__ = "chunk_items"
    __table_args__ = (
        UniqueConstraint("index_build_id", "chunk_id", name="uq_chunk_items_build_chunk"),
        Index("ix_chunk_items_build_id", "index_build_id"),
        Index("ix_chunk_items_document_id", "document_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_build_id: Mapped[str] = mapped_column(
        ForeignKey("chunk_builds.index_build_id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[str] = mapped_column(String(256), nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(256))
    source_path: Mapped[str | None] = mapped_column(String(512))
    source_hash: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(512))
    heading_path: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    char_count: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int | None] = mapped_column(Integer)
    overlap_char_count: Mapped[int | None] = mapped_column(Integer)
    content_summary: Mapped[str | None] = mapped_column(String(512))
    artifact_uri: Mapped[str | None] = mapped_column(String(512))
    build: Mapped[ChunkBuild] = relationship(back_populates="items")
