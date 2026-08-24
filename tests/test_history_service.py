from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
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


def test_thread_availability_allows_owner_and_unknown_thread(history_service) -> None:
    service, _factory = history_service
    service.record_turn("thread-1", "auth:1", "question", "answer")

    service.ensure_thread_available("thread-1", "auth:1")
    service.ensure_thread_available("new-thread", "auth:2")


def test_thread_availability_rejects_other_owner(history_service) -> None:
    service, _factory = history_service
    service.record_turn("thread-1", "auth:1", "question", "answer")

    with pytest.raises(ConversationNotFound):
        service.ensure_thread_available("thread-1", "auth:2")


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
        service.ensure_thread_available("legacy-thread", "auth:1")
