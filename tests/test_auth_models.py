from sqlalchemy import DateTime

from metro_agent.auth.models import AuthAuditEvent, AuthSession, AuthUser, utc_now_naive


def test_auth_models_are_separate_and_complete() -> None:
    assert set(AuthUser.metadata.tables) == {"users", "auth_sessions", "auth_audit_events"}
    assert AuthSession.__table__.c.token_hash.unique is True
    assert AuthAuditEvent.__table__.c.metadata_json.nullable is False


def test_auth_models_declare_migration_indexes() -> None:
    index_names = {
        index.name
        for table in AuthUser.metadata.tables.values()
        for index in table.indexes
    }

    assert index_names == {
        "ix_auth_sessions_user_id",
        "ix_auth_sessions_expires_at",
        "ix_auth_audit_events_occurred_at",
    }


def test_auth_datetimes_are_naive_utc() -> None:
    datetime_columns = [
        column
        for table in AuthUser.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime)
    ]
    default_columns = {
        AuthUser.__table__.c.created_at,
        AuthUser.__table__.c.updated_at,
        AuthSession.__table__.c.created_at,
        AuthAuditEvent.__table__.c.occurred_at,
    }

    assert utc_now_naive().tzinfo is None
    assert all(column.type.timezone is False for column in datetime_columns)
    assert {
        str(column.server_default.arg) for column in default_columns
    } == {"UTC_TIMESTAMP()"}
