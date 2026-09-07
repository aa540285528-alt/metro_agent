"""Governed, offline knowledge-index publication command."""

from __future__ import annotations

import argparse
import json
import os
import platform
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from metro_agent.knowledge import source_validation as _source_validation
from metro_agent.knowledge.chroma_client import get_chroma_client
from metro_agent.knowledge.config import (
    KnowledgeSettings,
    require_knowledge_source_root,
    require_knowledge_upload_staging_root,
)
from metro_agent.knowledge.operator_identity import trusted_operator_identity
from metro_agent.knowledge.publication_lock import PublicationLock
from metro_agent.knowledge.releases import (
    ReleaseValidationError,
    create_validated_release,
    publish_validated_release,
    read_release_pointer,
    read_validated_release,
    rollback_release_pointer,
)
from metro_agent.knowledge.source_validation import validate_source_root


FORCE_REBUILD_REASONS = (
    "indexer-upgrade",
    "embedding-model-change",
    "reranker-model-change",
    "chunker-change",
    "recovery",
)
PUBLICATION_LOCK_KEY = "knowledge:publication"
CHUNKER_PROVENANCE = {
    "parser": "MarkdownNodeParser",
    "overlap_char_count": 0,
    "overlap_strategy": "none",
}


class KnowledgeIndexerError(RuntimeError):
    """A governed indexing operation cannot be safely completed."""


class OperationEvents:
    """Append-only lifecycle evidence for a single operator action."""

    def __init__(self, artifact_root: Path | str, *, host: str | None = None) -> None:
        self._root = Path(artifact_root) / "operations"
        self._operator = trusted_operator_identity()
        self._host = host or platform.node()
        self._contexts: dict[str, dict[str, Any]] = {}

    def started(
        self,
        *,
        operation: str,
        build_id: str | None,
        previous_build_id: str | None,
        source_revision: str | None,
        force_reason: str | None,
        current_build_id: str | None = None,
    ) -> str:
        operation_id = str(uuid4())
        self._contexts[operation_id] = {
            "operation": operation,
            "build_id": build_id,
            "current_build_id": current_build_id,
            "previous_build_id": previous_build_id,
            "source_revision": source_revision,
            "force_reason": force_reason,
            "error_category": None,
        }
        self._write(operation_id, "started", self._contexts[operation_id])
        return operation_id

    def succeeded(
        self,
        operation_id: str,
        *,
        operation: str,
        build_id: str | None,
        current_build_id: str | None = None,
        previous_build_id: str | None = None,
    ) -> None:
        self._write(
            operation_id,
            "succeeded",
            self._terminal_context(
                operation_id,
                operation,
                build_id,
                current_build_id=current_build_id,
                previous_build_id=previous_build_id,
            ),
        )

    def failed(self, operation_id: str, *, operation: str, build_id: str | None, error: Exception) -> None:
        self._write(
            operation_id,
            "failed",
            {
                **self._terminal_context(operation_id, operation, build_id),
                "error_category": type(error).__name__,
            },
        )

    def _terminal_context(
        self,
        operation_id: str,
        operation: str,
        build_id: str | None,
        *,
        current_build_id: str | None = None,
        previous_build_id: str | None = None,
    ) -> dict[str, Any]:
        context = dict(self._contexts.get(operation_id, {}))
        context.update({"operation": operation, "build_id": build_id})
        if current_build_id is not None:
            context["current_build_id"] = current_build_id
        if previous_build_id is not None:
            context["previous_build_id"] = previous_build_id
        for field in (
            "current_build_id",
            "previous_build_id",
            "source_revision",
            "force_reason",
            "error_category",
        ):
            context.setdefault(field, None)
        return context

    def _write(self, operation_id: str, state: str, detail: Mapping[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{operation_id}.{state}.json"
        payload = {
            "operation_id": operation_id,
            "operator_identity": self._operator,
            "host": self._host,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            **detail,
        }
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")


class KnowledgeIndexer:
    def __init__(self, *, client: Any, artifact_root: Path | str, redis_client: Any, source_root: Path | None = None) -> None:
        self.client = client
        self.artifact_root = Path(artifact_root)
        self.redis_client = redis_client
        self.source_root = source_root

    def build_and_publish(
        self,
        *,
        force_reason: str | None = None,
    ) -> dict[str, str]:
        return self._build_and_publish(self._validated_source, force_reason=force_reason)

    def build_and_publish_from_staged_source(
        self,
        staged_source_root: Path,
        *,
        force_reason: str | None = None,
    ) -> dict[str, str]:
        """Build and publish from an already staged, file-system validated source."""
        return self._build_and_publish(
            lambda: self._validated_staged_source(staged_source_root),
            force_reason=force_reason,
        )

    def _build_and_publish(
        self,
        source_provider: Any,
        *,
        force_reason: str | None = None,
    ) -> dict[str, str]:
        if force_reason is not None and force_reason not in FORCE_REBUILD_REASONS:
            raise KnowledgeIndexerError("force_reason is not an approved rebuild reason")
        initial_source = source_provider()
        source_sha = self._source_sha(initial_source)
        provenance = self._provenance()
        current = self._current_release_descriptor()
        current_pointer = read_release_pointer(self.client, required=False)
        if force_reason is None and self._matches_current(current, source_sha, provenance):
            return {"status": "no-op", "build_id": current_pointer.current_build_id if current_pointer else ""}

        events = OperationEvents(self.artifact_root)
        operation_id: str | None = None
        build_id: str | None = None
        try:
            with PublicationLock(self.redis_client, PUBLICATION_LOCK_KEY) as publication_lock:
                # Re-check after taking exclusive ownership: no mixed source tree is publishable.
                source = source_provider()
                if self._source_sha(source) != source_sha:
                    raise KnowledgeIndexerError("knowledge source changed during build preparation")
                current = self._current_release_descriptor()
                current_pointer = read_release_pointer(self.client, required=False)
                if force_reason is None and self._matches_current(current, source_sha, provenance):
                    return {"status": "no-op", "build_id": current_pointer.current_build_id if current_pointer else ""}
                operation_id = events.started(
                    operation="build-and-publish",
                    build_id=None,
                    current_build_id=current_pointer.current_build_id if current_pointer else None,
                    previous_build_id=current_pointer.previous_build_id if current_pointer else None,
                    source_revision=getattr(source, "git_commit", None),
                    force_reason=force_reason,
                )
                build_id, collection_name = self._build_collection(source)
                self._ensure_collection_nonempty(collection_name)
                self._run_smoke_queries(source, collection_name)
                completed_source = source_provider()
                if self._source_sha(completed_source) != source_sha:
                    raise KnowledgeIndexerError("knowledge source changed during build")
                descriptor = {
                    "index_build_id": build_id,
                    "collection_name": collection_name,
                    "source_tree_sha256": source_sha,
                    "source_revision": source_sha,
                    "git_commit": getattr(source, "git_commit", None),
                    "provenance": provenance,
                }
                release = create_validated_release(self.artifact_root, descriptor)
                publication_lock.assert_held()
                publish_validated_release(self.client, release)
            assert operation_id is not None
            events.succeeded(
                operation_id,
                operation="build-and-publish",
                build_id=build_id,
                current_build_id=build_id,
                previous_build_id=current_pointer.current_build_id if current_pointer else None,
            )
            return {"status": "published", "build_id": build_id}
        except Exception as exc:
            if operation_id is not None:
                events.failed(operation_id, operation="build-and-publish", build_id=build_id, error=exc)
            if isinstance(exc, KnowledgeIndexerError):
                raise
            raise KnowledgeIndexerError("knowledge build-and-publish failed") from exc

    def _validated_staged_source(self, staged_source_root: Path) -> Any:
        staging_root = _source_validation.validate_knowledge_source_root(
            require_knowledge_upload_staging_root(
                KnowledgeSettings.from_environment().upload_staging_root
            )
        )
        source_root = _source_validation.validate_knowledge_source_root(staged_source_root)
        try:
            source_root.relative_to(staging_root)
        except ValueError as exc:
            raise KnowledgeIndexerError(
                "staged source root must be inside KNOWLEDGE_UPLOAD_STAGING_ROOT"
            ) from exc
        try:
            documents = _source_validation._discover_documents(
                source_root, _source_validation._shanghai_today()
            )
            smoke_path = source_root / _source_validation.SMOKE_QUERY_FILE
            _source_validation._reject_link_or_reparse(
                smoke_path, _source_validation.SMOKE_QUERY_FILE
            )
            if not smoke_path.is_file():
                raise KnowledgeIndexerError(
                    "staged source is missing required smoke query file"
                )
            smoke_queries = _source_validation._read_jsonl(
                smoke_path, {document.relative_path for document in documents}
            )
        except _source_validation.KnowledgeSourceValidationError as exc:
            raise KnowledgeIndexerError(str(exc)) from exc
        return _source_validation.ValidatedKnowledgeSource(
            root=source_root,
            documents=tuple(documents),
            smoke_queries=tuple(smoke_queries),
            source_tree_sha256=_source_validation.source_tree_sha256(
                list(documents), smoke_path
            ),
            git_commit=None,
        )

    def status(self) -> dict[str, Any]:
        pointer = read_release_pointer(self.client, required=False)
        return {"status": "unpublished"} if pointer is None else {"status": "published", **asdict(pointer)}

    def verify(self) -> dict[str, str]:
        pointer = read_release_pointer(self.client)
        assert pointer is not None
        release = read_validated_release(self.artifact_root, pointer.current_build_id)
        if release.sha256 != pointer.current_artifact_sha256 or release.collection_name != pointer.current_collection_name:
            raise KnowledgeIndexerError("published pointer does not match validated artifact")
        self._ensure_collection_nonempty(pointer.current_collection_name)
        return {"status": "verified", "build_id": pointer.current_build_id}

    def rollback(self) -> dict[str, str]:
        events = OperationEvents(self.artifact_root)
        before = None
        operation_id: str | None = None
        try:
            with PublicationLock(self.redis_client, PUBLICATION_LOCK_KEY) as publication_lock:
                before = read_release_pointer(self.client)
                assert before is not None
                operation_id = events.started(
                    operation="rollback",
                    build_id=before.current_build_id,
                    current_build_id=before.current_build_id,
                    previous_build_id=before.previous_build_id,
                    source_revision=None,
                    force_reason=None,
                )
                pointer = rollback_release_pointer(
                    self.client,
                    self.artifact_root,
                    before_publish=publication_lock.assert_held,
                )
            assert operation_id is not None
            events.succeeded(
                operation_id,
                operation="rollback",
                build_id=pointer.current_build_id,
                current_build_id=pointer.current_build_id,
                previous_build_id=pointer.previous_build_id,
            )
            return {"status": "rolled-back", "build_id": pointer.current_build_id}
        except Exception as exc:
            if operation_id is not None:
                events.failed(
                    operation_id,
                    operation="rollback",
                    build_id=before.current_build_id if before else None,
                    error=exc,
                )
            if isinstance(exc, KnowledgeIndexerError):
                raise
            raise KnowledgeIndexerError("knowledge rollback failed") from exc

    def _validated_source(self) -> Any:
        return validate_source_root(require_knowledge_source_root(self.source_root or KnowledgeSettings.from_environment().source_root))

    @staticmethod
    def _source_sha(source: Any) -> str:
        return str(source.source_tree_sha256)

    @staticmethod
    def _provenance() -> dict[str, Any]:
        try:
            from importlib.metadata import version

            chroma_client_version = version("chromadb")
        except Exception:
            chroma_client_version = "unknown"
        return {
            "indexer": "metro_agent.tools.knowledge_indexer",
            "python_version": platform.python_version(),
            "image_version": os.getenv("METRO_AGENT_IMAGE", "unknown"),
            "code_version": os.getenv("METRO_AGENT_CODE_VERSION", "unknown"),
            "chroma_client_version": chroma_client_version,
            "chroma_server_version": os.getenv("CHROMA_SERVER_VERSION", "unknown"),
            "embedding_model_id": os.getenv("EMBEDDING_MODEL_PATH", "/models/bge-m3"),
            "reranker_model_id": os.getenv("RERANK_MODEL_PATH", "/models/bge-reranker"),
            "chunker_config": dict(CHUNKER_PROVENANCE),
        }

    def _current_release_descriptor(self) -> Mapping[str, Any] | None:
        pointer = read_release_pointer(self.client, required=False)
        if pointer is None:
            return None
        return read_validated_release(self.artifact_root, pointer.current_build_id).descriptor

    @staticmethod
    def _matches_current(current: Mapping[str, Any] | None, source_sha: str, provenance: Mapping[str, Any]) -> bool:
        return bool(current and current.get("source_tree_sha256") == source_sha and current.get("provenance") == dict(provenance))

    def _build_collection(self, source: Any) -> tuple[str, str]:
        from llama_index.core.node_parser import MarkdownNodeParser

        from metro_agent.tools.build_obsidian_index import MARKDOWN_CHUNKER_CONFIG, write_nodes
        from metro_agent.tools.chunk_artifacts import assign_deterministic_chunk_ids, write_chunk_artifacts
        from metro_agent.tools.obsidian_loader import load_obsidian_documents
        from metro_agent.llama_config import COLLECTION_NAME

        build_id = uuid4().hex
        collection_name = f"{COLLECTION_NAME}__build_{build_id}"
        documents = load_obsidian_documents(source.root)
        nodes = assign_deterministic_chunk_ids(MarkdownNodeParser().get_nodes_from_documents(documents))
        if not nodes:
            raise KnowledgeIndexerError("knowledge documents produced no chunks")
        write_chunk_artifacts(artifact_root=self.artifact_root, index_build_id=build_id, nodes=nodes, document_count=len(documents), chunker_config=MARKDOWN_CHUNKER_CONFIG)
        write_nodes(nodes, collection_name, self.client)
        return build_id, collection_name

    def _ensure_collection_nonempty(self, collection_name: str) -> None:
        try:
            if self.client.get_collection(collection_name).count() <= 0:
                raise KnowledgeIndexerError("built collection is empty")
        except KnowledgeIndexerError:
            raise
        except Exception as exc:
            raise KnowledgeIndexerError("built collection cannot be verified") from exc

    def _run_smoke_queries(self, source: Any, collection_name: str) -> None:
        from llama_index.core import Settings

        collection = self.client.get_collection(collection_name)
        for smoke in source.smoke_queries:
            try:
                query_embedding = Settings.embed_model.get_query_embedding(smoke.query)
                response = collection.query(
                    query_embeddings=[query_embedding],
                    n_results=smoke.minimum_matches,
                    include=["metadatas"],
                )
                matches = (response.get("metadatas") or [[]])[0]
            except Exception as exc:
                raise KnowledgeIndexerError("release smoke query failed") from exc
            if len(matches) < smoke.minimum_matches or not any(
                isinstance(metadata, Mapping)
                and (metadata.get("relative_path") == smoke.expected_source or metadata.get("source_path") == smoke.expected_source)
                for metadata in matches[: smoke.minimum_matches]
            ):
                raise KnowledgeIndexerError("release smoke query did not match expected source")


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and govern the published knowledge index.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-and-publish")
    build.add_argument("--force-rebuild", choices=FORCE_REBUILD_REASONS)
    for name in ("status", "rollback", "verify"):
        commands.add_parser(name)
    return parser.parse_args(arguments)


def _default_indexer() -> KnowledgeIndexer:
    settings = KnowledgeSettings.from_environment()
    if settings.artifact_root is None:
        raise KnowledgeIndexerError("KNOWLEDGE_ARTIFACT_ROOT must be configured")
    import redis

    return KnowledgeIndexer(
        client=get_chroma_client(settings),
        artifact_root=settings.artifact_root,
        redis_client=redis.Redis(host=os.getenv("REDIS_HOST", "redis"), port=int(os.getenv("REDIS_PORT", "6379")), decode_responses=True),
        source_root=settings.source_root,
    )


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    indexer = _default_indexer()
    try:
        if args.command == "build-and-publish":
            result = indexer.build_and_publish(force_reason=args.force_rebuild)
        elif args.command == "status":
            result = indexer.status()
        elif args.command == "rollback":
            result = indexer.rollback()
        else:
            result = indexer.verify()
    except (KnowledgeIndexerError, ReleaseValidationError) as exc:
        print(f"knowledge indexer failed: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
