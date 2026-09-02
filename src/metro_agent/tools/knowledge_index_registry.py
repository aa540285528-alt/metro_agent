"""Persistent pointer to the fully built knowledge index collection."""

from metro_agent.knowledge.releases import read_release_pointer

PUBLISHED_INDEX_ID = "published"


class KnowledgeIndexUnavailableError(RuntimeError):
    """Raised when online RAG has no complete index to read."""


def read_published_collection_name(client, registry_name: str) -> str:
    # New publications use a descriptor-bound, atomic pointer.  Retain the
    # legacy record shape only so historical registries remain readable until
    # they are replaced by their next governed publication.
    pointer = read_release_pointer(client, required=False)
    if pointer is not None:
        return pointer.current_collection_name
    try:
        registry = client.get_collection(registry_name)
    except Exception as exc:
        raise KnowledgeIndexUnavailableError(
            "知识库索引尚未构建，请先运行离线构建命令。"
        ) from exc

    record = registry.get(ids=[PUBLISHED_INDEX_ID], include=["metadatas"])
    metadatas = record.get("metadatas") or []
    collection_name = metadatas[0].get("collection_name") if metadatas else None
    if not collection_name:
        raise KnowledgeIndexUnavailableError(
            "知识库索引尚未构建，请先运行离线构建命令。"
        )

    return str(collection_name)


def publish_collection_name(client, registry_name: str, collection_name: str) -> None:
    """Compatibility writer for pre-governance callers.

    The governed indexer publishes through ``publish_validated_release`` so a
    collection can never become visible without its artifact digest.
    """
    registry = client.get_or_create_collection(registry_name)
    registry.upsert(
        ids=[PUBLISHED_INDEX_ID],
        documents=["published index"],
        embeddings=[[0.0]],
        metadatas=[{"collection_name": collection_name, "status": "ready"}],
    )


def clear_published_collection_name(client, registry_name: str) -> None:
    """Remove the published pointer when a failed first publication is rolled back."""
    registry = client.get_collection(registry_name)
    registry.delete(ids=[PUBLISHED_INDEX_ID])
