from metro_agent.auth.models import AuthAuditEvent, AuthSession, AuthUser


def test_auth_models_are_separate_and_complete() -> None:
    assert set(AuthUser.metadata.tables) == {"users", "auth_sessions", "auth_audit_events"}
    assert AuthSession.__table__.c.token_hash.unique is True
    assert AuthAuditEvent.__table__.c.metadata_json.nullable is False
