from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from redis import Redis
from sqlalchemy import Engine, text

from metro_agent.auth.database import create_auth_engine


class DependencyReadinessChecker:
    def __init__(
        self,
        *,
        auth_engine: Engine | Any,
        business_engine: Engine | Any,
        redis_client: Redis | Any,
    ) -> None:
        self._auth_engine = auth_engine
        self._business_engine = business_engine
        self._redis_client = redis_client

    def __call__(self) -> None:
        self._check_database(self._auth_engine)
        self._check_database(self._business_engine)
        self._redis_client.ping()

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
    )


def check_default_readiness() -> None:
    build_default_readiness_checker()()
