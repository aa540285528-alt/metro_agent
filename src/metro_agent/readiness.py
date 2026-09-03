from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from redis import Redis
from sqlalchemy import Engine, text

from metro_agent.auth.database import create_auth_engine
from metro_agent.knowledge.chroma_client import get_knowledge_read_proxy_client
from metro_agent.knowledge.releases import ReleaseValidationError, read_release_pointer


class KnowledgeReadinessChecker:
    """Fail closed unless the read proxy exposes a nonempty published release."""

    def __init__(self, *, client_factory=get_knowledge_read_proxy_client, pointer_reader=read_release_pointer) -> None:
        self._client_factory = client_factory
        self._pointer_reader = pointer_reader

    def __call__(self) -> None:
        try:
            client = self._client_factory()
            pointer = self._pointer_reader(client)
            if pointer is None:
                raise ReleaseValidationError("published release pointer is unavailable")
            if client.get_collection(pointer.current_collection_name).count() <= 0:
                raise ReleaseValidationError("published knowledge collection is empty")
        except ReleaseValidationError:
            raise
        except Exception as exc:
            raise ReleaseValidationError(
                "published knowledge service is unavailable"
            ) from exc


class DependencyReadinessChecker:
    def __init__(
        self,
        *,
        auth_engine: Engine | Any,
        business_engine: Engine | Any,
        redis_client: Redis | Any,
        knowledge_checker: Any | None = None,
    ) -> None:
        self._auth_engine = auth_engine
        self._business_engine = business_engine
        self._redis_client = redis_client
        self._knowledge_checker = knowledge_checker or (lambda: None)

    def __call__(self) -> None:
        self._check_database(self._auth_engine)
        self._check_database(self._business_engine)
        self._redis_client.ping()
        self._knowledge_checker()

    @staticmethod
    def _check_database(engine: Engine | Any) -> None:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))


@lru_cache(maxsize=1)
def build_default_readiness_checker() -> DependencyReadinessChecker:
    from metro_agent.storage.history.database import engine as business_engine

    redis_url = os.environ["SHORT_TERM_MEMORY_REDIS_URL"]
    return DependencyReadinessChecker(
        auth_engine=create_auth_engine(),
        business_engine=business_engine,
        redis_client=Redis.from_url(redis_url),
        knowledge_checker=KnowledgeReadinessChecker(),
    )


def check_default_readiness() -> None:
    build_default_readiness_checker()()
