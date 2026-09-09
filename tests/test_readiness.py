from __future__ import annotations

from contextlib import contextmanager

import pytest

from metro_agent.readiness import DependencyReadinessChecker


class FakeConnection:
    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error

    def execute(self, statement) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(str(statement))


class FakeEngine:
    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error

    @contextmanager
    def connect(self):
        yield FakeConnection(self.calls, self.error)


class FakeRedis:
    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error

    def ping(self) -> bool:
        if self.error is not None:
            raise self.error
        self.calls.append("PING")
        return True


def test_readiness_checks_both_databases_and_redis() -> None:
    auth_calls: list[str] = []
    business_calls: list[str] = []
    redis_calls: list[str] = []
    checker = DependencyReadinessChecker(
        auth_engine=FakeEngine(auth_calls),
        business_engine=FakeEngine(business_calls),
        redis_client=FakeRedis(redis_calls),
    )

    checker()

    assert auth_calls == ["SELECT 1"]
    assert business_calls == ["SELECT 1"]
    assert redis_calls == ["PING"]


def test_readiness_checks_knowledge_after_datastores() -> None:
    calls: list[str] = []

    class KnowledgeChecker:
        def __call__(self) -> None:
            calls.append("knowledge")

    checker = DependencyReadinessChecker(
        auth_engine=FakeEngine([]),
        business_engine=FakeEngine([]),
        redis_client=FakeRedis([]),
        knowledge_checker=KnowledgeChecker(),
    )

    checker()

    assert calls == ["knowledge"]


@pytest.mark.parametrize("failing_dependency", ["auth", "business", "redis"])
def test_readiness_propagates_each_dependency_failure(failing_dependency: str) -> None:
    failure = ConnectionError(f"{failing_dependency} unavailable")
    checker = DependencyReadinessChecker(
        auth_engine=FakeEngine([], failure if failing_dependency == "auth" else None),
        business_engine=FakeEngine(
            [], failure if failing_dependency == "business" else None
        ),
        redis_client=FakeRedis([], failure if failing_dependency == "redis" else None),
    )

    with pytest.raises(ConnectionError, match="unavailable"):
        checker()
