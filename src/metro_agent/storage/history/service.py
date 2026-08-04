from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from metro_agent.storage.history.models import Conversation, ConversationMessage


class ConversationNotFound(Exception):
    pass


@dataclass(frozen=True)
class ConversationSummary:
    id: str
    title: str
    is_pinned: bool
    updated_at: datetime


@dataclass(frozen=True)
class StoredMessage:
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


@dataclass(frozen=True)
class ConversationDetail:
    id: str
    title: str
    is_pinned: bool
    messages: list[StoredMessage]


class ConversationHistoryService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _to_summary(conversation: Conversation) -> ConversationSummary:
        return ConversationSummary(
            id=conversation.id,
            title=conversation.title,
            is_pinned=conversation.is_pinned,
            updated_at=ConversationHistoryService._as_utc(conversation.updated_at),
        )

    @staticmethod
    def _get_owned_conversation(
        session: Session, thread_id: str, owner_id: str
    ) -> Conversation:
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.id == thread_id,
                Conversation.owner_id == owner_id,
            )
        )
        if conversation is None:
            raise ConversationNotFound(thread_id)
        return conversation

    def record_turn(
        self,
        thread_id: str,
        owner_id: str,
        user_content: str,
        assistant_content: str,
    ) -> ConversationSummary:
        user_content = user_content.strip()
        assistant_content = assistant_content.strip()
        if not user_content or not assistant_content:
            raise ValueError("conversation messages must not be blank")

        with self._session_factory.begin() as session:
            conversation = session.get(Conversation, thread_id)
            if conversation is None:
                conversation = Conversation(
                    id=thread_id,
                    owner_id=owner_id,
                    title=" ".join(user_content.split())[:40],
                )
                session.add(conversation)
            elif conversation.owner_id != owner_id:
                raise ConversationNotFound(thread_id)

            conversation.updated_at = datetime.now(timezone.utc)
            session.add_all(
                [
                    ConversationMessage(
                        conversation=conversation,
                        role="user",
                        content=user_content,
                    ),
                    ConversationMessage(
                        conversation=conversation,
                        role="assistant",
                        content=assistant_content,
                    ),
                ]
            )
            session.flush()
            return self._to_summary(conversation)

    def list_conversations(self, owner_id: str) -> list[ConversationSummary]:
        with self._session_factory() as session:
            conversations = session.scalars(
                select(Conversation)
                .where(Conversation.owner_id == owner_id)
                .order_by(
                    Conversation.is_pinned.desc(),
                    Conversation.pinned_at.desc(),
                    Conversation.updated_at.desc(),
                    Conversation.id.asc(),
                )
            ).all()
            return [self._to_summary(conversation) for conversation in conversations]

    def get_conversation(
        self, thread_id: str, owner_id: str
    ) -> ConversationDetail:
        with self._session_factory() as session:
            conversation = session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == thread_id,
                    Conversation.owner_id == owner_id,
                )
                .options(selectinload(Conversation.messages))
            )
            if conversation is None:
                raise ConversationNotFound(thread_id)
            return ConversationDetail(
                id=conversation.id,
                title=conversation.title,
                is_pinned=conversation.is_pinned,
                messages=[
                    StoredMessage(
                        role=message.role,
                        content=message.content,
                        created_at=self._as_utc(message.created_at),
                    )
                    for message in conversation.messages
                ],
            )

    def update_conversation(
        self,
        thread_id: str,
        owner_id: str,
        *,
        title: str | None = None,
        is_pinned: bool | None = None,
    ) -> ConversationSummary:
        with self._session_factory.begin() as session:
            conversation = self._get_owned_conversation(session, thread_id, owner_id)
            if title is not None:
                normalized_title = title.strip()
                if not normalized_title or len(normalized_title) > 40:
                    raise ValueError("title must contain 1 to 40 characters")
                conversation.title = normalized_title
            if is_pinned is not None:
                conversation.is_pinned = is_pinned
                conversation.pinned_at = datetime.now(timezone.utc) if is_pinned else None
            conversation.updated_at = datetime.now(timezone.utc)
            session.flush()
            return self._to_summary(conversation)

    def delete_conversation(self, thread_id: str, owner_id: str) -> None:
        with self._session_factory.begin() as session:
            session.delete(self._get_owned_conversation(session, thread_id, owner_id))
