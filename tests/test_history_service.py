from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from metro_agent.storage.history.models import Base, Conversation
from metro_agent.storage.history.service import (
    ConversationHistoryService,
    ConversationNotFound,
)


@pytest.fixture
def history_service():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield ConversationHistoryService(factory), factory
    finally:
        engine.dispose()


def test_claim_thread_creates_owned_placeholder_before_agent_runs(
    history_service,
) -> None:
    service, factory = history_service

    service.claim_thread("thread-1", "auth:1", "  first   question  ")

    with factory() as session:
        conversation = session.get(Conversation, "thread-1")
        assert conversation is not None
        assert conversation.owner_id == "auth:1"
        assert conversation.title == "first question"
        assert conversation.messages == []

    service.claim_thread("thread-1", "auth:1", "ignored replacement title")


def test_claim_thread_rejects_other_owner(history_service) -> None:
    service, _factory = history_service
    service.claim_thread("thread-1", "auth:1", "question")

    with pytest.raises(ConversationNotFound):
        service.claim_thread("thread-1", "auth:2", "other question")


def test_prefixed_owner_does_not_inherit_legacy_numeric_owner(history_service) -> None:
    service, factory = history_service
    with factory.begin() as session:
        session.add(
            Conversation(
                id="legacy-thread",
                owner_id="1",
                title="legacy",
                updated_at=datetime.now(UTC),
            )
        )

    with pytest.raises(ConversationNotFound):
        service.claim_thread("legacy-thread", "auth:1", "new question")


def test_claim_thread_rechecks_owner_after_unique_key_race() -> None:
    competing = Conversation(
        id="raced-thread",
        owner_id="auth:2",
        title="winner",
    )

    class InsertSession:
        def get(self, _model, _thread_id):
            return None

        def add(self, _conversation) -> None:
            pass

        def flush(self) -> None:
            raise IntegrityError("insert", {}, Exception("duplicate"))

    class ReadSession:
        def get(self, _model, _thread_id):
            return competing

    class SessionContext:
        def __init__(self, session) -> None:
            self.session = session

        def __enter__(self):
            return self.session

        def __exit__(self, *_args) -> None:
            return None

    class RaceSessionFactory:
        def begin(self):
            return SessionContext(InsertSession())

        def __call__(self):
            return SessionContext(ReadSession())

    service = ConversationHistoryService(RaceSessionFactory())

    with pytest.raises(ConversationNotFound):
        service.claim_thread("raced-thread", "auth:1", "loser")
