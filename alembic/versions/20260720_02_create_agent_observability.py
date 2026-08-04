"""Create agent observability tables.

Revision ID: 20260720_02
Revises: 20260717_01
Create Date: 2026-07-20 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_02"
down_revision: Union[str, Sequence[str], None] = "20260717_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_traces",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("round_id", sa.String(length=128), nullable=True),
        sa.Column("request_summary", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("critical_path_latency_ms", sa.Float(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_traces_created_at", "agent_traces", ["created_at"], unique=False
    )
    op.create_index("ix_agent_traces_status", "agent_traces", ["status"], unique=False)

    op.create_table(
        "agent_spans",
        sa.Column(
            "trace_id",
            sa.String(length=128),
            sa.ForeignKey("agent_traces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("span_id", sa.String(length=128), nullable=False),
        sa.Column(
            "parent_span_id",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("agent", sa.String(length=128), nullable=True),
        sa.Column("step_id", sa.String(length=128), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["trace_id", "parent_span_id"],
            ["agent_spans.trace_id", "agent_spans.span_id"],
            name="fk_agent_spans_parent_same_trace",
        ),
        sa.PrimaryKeyConstraint("trace_id", "span_id"),
    )
    op.create_index("ix_agent_spans_trace_id", "agent_spans", ["trace_id"], unique=False)
    op.create_index("ix_agent_spans_step_id", "agent_spans", ["step_id"], unique=False)

    op.create_table(
        "llm_usages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("span_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=True),
        sa.Column("usage_category", sa.String(length=32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("usage_source", sa.String(length=32), nullable=True),
        sa.Column("usage_summary", sa.JSON(), nullable=False),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["agent_spans.trace_id", "agent_spans.span_id"],
            name="fk_llm_usages_span_same_trace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usages_trace_id", "llm_usages", ["trace_id"], unique=False)

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("span_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("arguments_summary", sa.JSON(), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=True),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["agent_spans.trace_id", "agent_spans.span_id"],
            name="fk_tool_calls_span_same_trace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_calls_trace_id", "tool_calls", ["trace_id"], unique=False)

    op.create_table(
        "rag_retrievals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("span_id", sa.String(length=128), nullable=False),
        sa.Column("query_summary", sa.String(length=512), nullable=True),
        sa.Column("index_build_id", sa.String(length=128), nullable=True),
        sa.Column("chunk_id", sa.String(length=256), nullable=True),
        sa.Column("document_id", sa.String(length=256), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("raw_score", sa.Float(), nullable=True),
        sa.Column("rerank_score", sa.Float(), nullable=True),
        sa.Column("cited", sa.Boolean(), nullable=False),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["agent_spans.trace_id", "agent_spans.span_id"],
            name="fk_rag_retrievals_span_same_trace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rag_retrievals_trace_id", "rag_retrievals", ["trace_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_rag_retrievals_trace_id", table_name="rag_retrievals")
    op.drop_table("rag_retrievals")
    op.drop_index("ix_tool_calls_trace_id", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_llm_usages_trace_id", table_name="llm_usages")
    op.drop_table("llm_usages")
    op.drop_index("ix_agent_spans_step_id", table_name="agent_spans")
    op.drop_index("ix_agent_spans_trace_id", table_name="agent_spans")
    op.drop_table("agent_spans")
    op.drop_index("ix_agent_traces_status", table_name="agent_traces")
    op.drop_index("ix_agent_traces_created_at", table_name="agent_traces")
    op.drop_table("agent_traces")
