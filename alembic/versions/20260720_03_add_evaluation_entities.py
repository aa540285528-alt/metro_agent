"""Add evaluation and chunk-build metadata tables.

Revision ID: 20260720_03
Revises: 20260720_02
Create Date: 2026-07-20 00:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_03"
down_revision: Union[str, Sequence[str], None] = "20260720_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("suite", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=True),
        sa.Column("code_version", sa.String(length=128), nullable=True),
        sa.Column("judge_config", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("artifact_uri", sa.String(length=512), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_eval_runs_suite_category", "eval_runs", ["suite", "category"], unique=False)
    op.create_index("ix_eval_runs_created_at", "eval_runs", ["created_at"], unique=False)

    op.create_table(
        "eval_case_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("case_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("reason_summary", sa.String(length=512), nullable=True),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "case_id", name="uq_eval_case_results_run_case"),
    )
    op.create_index("ix_eval_case_results_run_id", "eval_case_results", ["run_id"], unique=False)

    op.create_table(
        "chunk_builds",
        sa.Column("index_build_id", sa.String(length=128), nullable=False),
        sa.Column("chunker_config", sa.JSON(), nullable=False),
        sa.Column("embedding_config", sa.JSON(), nullable=False),
        sa.Column("source_manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("artifact_uri", sa.String(length=512), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("index_build_id"),
    )
    op.create_index("ix_chunk_builds_created_at", "chunk_builds", ["created_at"], unique=False)

    op.create_table(
        "chunk_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("index_build_id", sa.String(length=128), nullable=False),
        sa.Column("chunk_id", sa.String(length=256), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=True),
        sa.Column("source_path", sa.String(length=512), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("heading_path", sa.JSON(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("overlap_char_count", sa.Integer(), nullable=True),
        sa.Column("content_summary", sa.String(length=512), nullable=True),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["index_build_id"], ["chunk_builds.index_build_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_build_id", "chunk_id", name="uq_chunk_items_build_chunk"),
    )
    op.create_index("ix_chunk_items_build_id", "chunk_items", ["index_build_id"], unique=False)
    op.create_index("ix_chunk_items_document_id", "chunk_items", ["document_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chunk_items_document_id", table_name="chunk_items")
    op.drop_index("ix_chunk_items_build_id", table_name="chunk_items")
    op.drop_table("chunk_items")
    op.drop_index("ix_chunk_builds_created_at", table_name="chunk_builds")
    op.drop_table("chunk_builds")
    op.drop_index("ix_eval_case_results_run_id", table_name="eval_case_results")
    op.drop_table("eval_case_results")
    op.drop_index("ix_eval_runs_created_at", table_name="eval_runs")
    op.drop_index("ix_eval_runs_suite_category", table_name="eval_runs")
    op.drop_table("eval_runs")
