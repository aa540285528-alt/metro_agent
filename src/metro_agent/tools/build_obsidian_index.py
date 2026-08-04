"""Build and publish an Obsidian knowledge index outside the request path."""

import argparse
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import chromadb
from llama_index.core import StorageContext, VectorStoreIndex
from llama_index.core.node_parser import MarkdownNodeParser
from llama_index.vector_stores.chroma import ChromaVectorStore

from metro_agent.tools.knowledge_index_registry import (
    KnowledgeIndexUnavailableError,
    clear_published_collection_name,
    publish_collection_name,
    read_published_collection_name,
)
from metro_agent.tools.chunk_artifacts import (
    assign_deterministic_chunk_ids,
    mark_chunk_manifest_failed,
    mark_chunk_manifest_published,
    mark_chunk_manifest_publish_uncertain,
    read_chunk_manifest,
    write_chunk_artifacts,
)
from metro_agent.tools.obsidian_loader import load_obsidian_documents
from metro_agent.llama_config import (
    CHROMA_DB_DIR,
    COLLECTION_NAME,
    INDEX_REGISTRY_COLLECTION_NAME,
    KNOWLEDGE_PATH,
    init_llama_index_components,
)


DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parent.parent / "artifacts"
MARKDOWN_CHUNKER_CONFIG = {
    "parser": "MarkdownNodeParser",
    "overlap_char_count": 0,
    "overlap_strategy": "none",
    "overlap_definition": "MarkdownNodeParser emits semantic chunks without configured overlap.",
}


@dataclass(frozen=True)
class IndexBuildResult:
    collection_name: str
    document_count: int
    node_count: int


class IndexBuildError(RuntimeError):
    """Raised when an offline index cannot be safely published."""


@dataclass(frozen=True)
class IndexReconciliationResult:
    index_build_id: str
    status: str
    collection_name: str | None


def build_nodes():
    documents = load_obsidian_documents(KNOWLEDGE_PATH)
    nodes = MarkdownNodeParser().get_nodes_from_documents(documents)
    return documents, nodes


def write_nodes(nodes, collection_name: str, client) -> int:
    init_llama_index_components()
    collection = client.create_collection(name=collection_name)
    vector_store = ChromaVectorStore(
        chroma_collection=collection,
        hybrid_search=True,
    )
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    VectorStoreIndex(nodes, storage_context=storage_context)
    return collection.count()


def build_index(
    *,
    rebuild: bool,
    client=None,
    artifact_root: Path | str = DEFAULT_ARTIFACT_ROOT,
) -> IndexBuildResult:
    client = client or chromadb.PersistentClient(path=str(CHROMA_DB_DIR))

    previous_collection_name: str | None = None
    try:
        previous_collection_name = read_published_collection_name(
            client, INDEX_REGISTRY_COLLECTION_NAME
        )
        has_published_index = True
    except KnowledgeIndexUnavailableError:
        has_published_index = False

    if has_published_index and not rebuild:
        raise IndexBuildError("已有已发布索引；如需替换请使用 --rebuild。")

    documents, nodes = build_nodes()
    if not nodes:
        raise IndexBuildError("Obsidian 文档未生成任何节点，拒绝发布空索引。")

    nodes = assign_deterministic_chunk_ids(nodes)
    collection_name = f"{COLLECTION_NAME}__build_{uuid4().hex}"
    index_build_id = collection_name.rsplit("__build_", 1)[-1]
    try:
        write_chunk_artifacts(
            artifact_root=artifact_root,
            index_build_id=index_build_id,
            nodes=nodes,
            document_count=len(documents),
            chunker_config=MARKDOWN_CHUNKER_CONFIG,
        )
    except Exception as exc:
        raise IndexBuildError("chunk artifacts could not be written; index was not published") from exc
    publish_attempted = False
    registry_verified_new = False
    try:
        written_count = write_nodes(nodes, collection_name, client)
        if written_count <= 0:
            raise IndexBuildError("temporary index is empty; index was not published")
        publish_attempted = True
        publish_collection_name(
            client,
            INDEX_REGISTRY_COLLECTION_NAME,
            collection_name,
        )
        _verify_registry_pointer(client, collection_name)
        registry_verified_new = True
        mark_chunk_manifest_published(
            artifact_root,
            index_build_id,
            collection_name,
        )
    except Exception as exc:
        if publish_attempted and not registry_verified_new:
            try:
                registry_verified_new = _registry_points_to(client, collection_name)
            except Exception as verification_error:
                _mark_publish_uncertain_or_raise(
                    artifact_root=artifact_root,
                    index_build_id=index_build_id,
                    primary_error=exc,
                    previous_collection_name=previous_collection_name,
                    new_collection_name=collection_name,
                    verification_error=verification_error,
                )
                raise AssertionError("unreachable")
        if registry_verified_new:
            try:
                _restore_registry_pointer(client, previous_collection_name)
                _verify_registry_pointer_or_absent(client, previous_collection_name)
            except Exception as rollback_error:
                _mark_publish_uncertain_or_raise(
                    artifact_root=artifact_root,
                    index_build_id=index_build_id,
                    primary_error=exc,
                    rollback_error=rollback_error,
                    previous_collection_name=previous_collection_name,
                    new_collection_name=collection_name,
                )
        try:
            mark_chunk_manifest_failed(artifact_root, index_build_id, exc)
        except Exception as manifest_error:
            raise IndexBuildError(
                "index build failed and the failed manifest could not be persisted"
            ) from manifest_error
        if isinstance(exc, IndexBuildError):
            raise
        raise IndexBuildError("index build or publication failed") from exc
    return IndexBuildResult(
        collection_name=collection_name,
        document_count=len(documents),
        node_count=len(nodes),
    )


def _restore_registry_pointer(client, previous_collection_name: str | None) -> None:
    if previous_collection_name:
        publish_collection_name(
            client,
            INDEX_REGISTRY_COLLECTION_NAME,
            previous_collection_name,
        )
        return
    clear_published_collection_name(client, INDEX_REGISTRY_COLLECTION_NAME)


def _registry_points_to(client, collection_name: str) -> bool:
    return read_published_collection_name(client, INDEX_REGISTRY_COLLECTION_NAME) == collection_name


def _verify_registry_pointer(client, collection_name: str) -> None:
    if not _registry_points_to(client, collection_name):
        raise IndexBuildError("registry pointer did not match the newly published collection")


def _mark_publish_uncertain_or_raise(
    *,
    artifact_root: Path | str,
    index_build_id: str,
    primary_error: Exception,
    previous_collection_name: str | None,
    new_collection_name: str,
    rollback_error: Exception | None = None,
    verification_error: Exception | None = None,
) -> None:
    try:
        marker_kwargs = (
            {"verification_error": verification_error}
            if verification_error is not None
            else {}
        )
        mark_chunk_manifest_publish_uncertain(
            artifact_root,
            index_build_id,
            primary_error,
            rollback_error,
            previous_collection_name,
            new_collection_name,
            **marker_kwargs,
        )
    except Exception as manifest_error:
        raise IndexBuildError(
            "registry publication state is uncertain and the manifest could not be persisted"
        ) from manifest_error
    if rollback_error is not None:
        raise IndexBuildError(
            "registry pointer recovery failed; manifest marked publish_uncertain"
        ) from rollback_error
    raise IndexBuildError(
        "registry publication state could not be verified; manifest marked publish_uncertain"
    ) from verification_error


def reconcile_publish_uncertain(
    *,
    index_build_id: str,
    client=None,
    artifact_root: Path | str = DEFAULT_ARTIFACT_ROOT,
    target: str = "rollback",
) -> IndexReconciliationResult:
    """Reconcile a failed publication only after checking the live registry pointer."""
    if target not in {"rollback", "publish"}:
        raise ValueError("target must be 'rollback' or 'publish'")
    client = client or chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
    manifest = read_chunk_manifest(artifact_root, index_build_id)
    lifecycle = manifest.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get("status") != "publish_uncertain":
        raise IndexBuildError("only publish_uncertain manifests can be reconciled")
    new_collection_name = lifecycle.get("new_collection_name")
    previous_collection_name = lifecycle.get("previous_collection_name")
    if not isinstance(new_collection_name, str) or not new_collection_name:
        raise IndexBuildError("publish_uncertain manifest has no new collection binding")
    if previous_collection_name is not None and not isinstance(previous_collection_name, str):
        raise IndexBuildError("publish_uncertain manifest has an invalid previous collection binding")

    current_collection_name = _read_registry_pointer_or_none(client)
    if target == "publish":
        if current_collection_name != new_collection_name:
            raise IndexBuildError("registry pointer does not verify the new collection for publication")
        mark_chunk_manifest_published(artifact_root, index_build_id, new_collection_name)
        return IndexReconciliationResult(index_build_id, "published", new_collection_name)

    expected_after_rollback = previous_collection_name
    if current_collection_name == new_collection_name:
        try:
            _restore_registry_pointer(client, previous_collection_name)
            _verify_registry_pointer_or_absent(client, expected_after_rollback)
        except Exception as rollback_error:
            _mark_publish_uncertain_or_raise(
                artifact_root=artifact_root,
                index_build_id=index_build_id,
                primary_error=IndexBuildError("operator reconciliation rollback failed"),
                rollback_error=rollback_error,
                previous_collection_name=previous_collection_name,
                new_collection_name=new_collection_name,
            )
    elif current_collection_name != expected_after_rollback:
        raise IndexBuildError("registry pointer references an unrelated collection; refusing reconciliation")

    original_error_type = lifecycle.get("error_type")
    mark_chunk_manifest_failed(
        artifact_root,
        index_build_id,
        original_error_type if isinstance(original_error_type, str) else "IndexBuildError",
    )
    return IndexReconciliationResult(index_build_id, "failed", None)


def _read_registry_pointer_or_none(client) -> str | None:
    try:
        return read_published_collection_name(client, INDEX_REGISTRY_COLLECTION_NAME)
    except KnowledgeIndexUnavailableError:
        return None


def _verify_registry_pointer_or_absent(client, expected_collection_name: str | None) -> None:
    actual_collection_name = _read_registry_pointer_or_none(client)
    if actual_collection_name != expected_collection_name:
        raise IndexBuildError("registry pointer did not match the recovered collection state")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Obsidian knowledge index.")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Replace the currently published index after a new build completes.",
    )
    parser.add_argument(
        "--reconcile-build",
        help="Reconcile a publish_uncertain chunk manifest by build id.",
    )
    parser.add_argument(
        "--reconcile-target",
        choices=("rollback", "publish"),
        default="rollback",
        help="Converge the verified registry state to failed or published.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.reconcile_build:
            result = reconcile_publish_uncertain(
                index_build_id=args.reconcile_build,
                target=args.reconcile_target,
            )
            print(
                "Index reconciliation complete: "
                f"build={result.index_build_id}, status={result.status}, "
                f"collection={result.collection_name or 'none'}"
            )
            return 0
        result = build_index(rebuild=args.rebuild)
    except IndexBuildError as exc:
        print(f"Index build failed: {exc}")
        return 1

    print(
        "Index published: "
        f"collection={result.collection_name}, "
        f"documents={result.document_count}, nodes={result.node_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
