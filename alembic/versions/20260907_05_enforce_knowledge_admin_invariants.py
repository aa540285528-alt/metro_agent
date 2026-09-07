"""Enforce knowledge-admin release and audit invariants.

Revision ID: 20260907_05
Revises: 20260907_04
Create Date: 2026-09-07 00:15:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260907_05"
down_revision: Union[str, Sequence[str], None] = "20260907_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_knowledge_releases_one_current",
        "knowledge_releases",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'current'"),
    )
    op.execute(
        """
        CREATE FUNCTION prevent_knowledge_admin_audit_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'knowledge_admin_audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_knowledge_admin_audit_events_append_only
        BEFORE UPDATE OR DELETE ON knowledge_admin_audit_events
        FOR EACH ROW EXECUTE FUNCTION prevent_knowledge_admin_audit_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_knowledge_admin_audit_events_append_only "
        "ON knowledge_admin_audit_events"
    )
    op.execute("DROP FUNCTION prevent_knowledge_admin_audit_event_mutation()")
    op.drop_index("uq_knowledge_releases_one_current", table_name="knowledge_releases")
