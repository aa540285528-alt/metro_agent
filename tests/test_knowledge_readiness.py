from __future__ import annotations

import pytest

from metro_agent.knowledge.releases import ReleasePointer, ReleaseValidationError
from metro_agent.readiness import KnowledgeReadinessChecker


class FakeKnowledgeCollection:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class FakeKnowledgeClient:
    def __init__(self, count: int = 1) -> None:
        self.collection_names: list[str] = []
        self.count = count

    def get_collection(self, name: str) -> FakeKnowledgeCollection:
        self.collection_names.append(name)
        return FakeKnowledgeCollection(self.count)


def test_knowledge_readiness_requires_a_nonempty_published_collection() -> None:
    client = FakeKnowledgeClient()
    checker = KnowledgeReadinessChecker(
        client_factory=lambda: client,
        pointer_reader=lambda _client: ReleasePointer(
            current_build_id="build-1",
            current_collection_name="metro__build_build-1",
            current_artifact_sha256="a" * 64,
        ),
    )

    checker()

    assert client.collection_names == ["metro__build_build-1"]


@pytest.mark.parametrize("count", [0, -1])
def test_knowledge_readiness_rejects_empty_published_collection(count: int) -> None:
    checker = KnowledgeReadinessChecker(
        client_factory=lambda: FakeKnowledgeClient(count=count),
        pointer_reader=lambda _client: ReleasePointer(
            current_build_id="build-1",
            current_collection_name="metro__build_build-1",
            current_artifact_sha256="a" * 64,
        ),
    )

    with pytest.raises(ReleaseValidationError, match="empty"):
        checker()


def test_knowledge_readiness_fails_closed_without_a_published_release() -> None:
    checker = KnowledgeReadinessChecker(
        client_factory=FakeKnowledgeClient,
        pointer_reader=lambda _client: (_ for _ in ()).throw(
            ReleaseValidationError("published release pointer is unavailable")
        ),
    )

    with pytest.raises(ReleaseValidationError, match="unavailable"):
        checker()
