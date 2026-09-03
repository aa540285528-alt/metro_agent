from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from metro_agent.knowledge.releases import create_validated_release
from metro_agent.knowledge_read_proxy import (
    DEFAULT_DATABASE,
    DEFAULT_TENANT,
    INDEX_REGISTRY_COLLECTION_NAME,
    MAX_BODY_BYTES,
    UpstreamResponse,
    create_knowledge_read_proxy,
)


class RecordingUpstream:
    def __init__(self, release_sha: str, collection_name: str) -> None:
        self.release_sha = release_sha
        self.collection_name = collection_name
        self.requests: list[tuple[str, str, bytes]] = []
        self.error: Exception | None = None

    async def request(
        self, method: str, path: str, body: bytes, headers: list[tuple[bytes, bytes]]
    ) -> UpstreamResponse:
        del headers
        self.requests.append((method, path, body))
        if self.error is not None:
            raise self.error
        collection_base = (
            f"/api/v2/tenants/{DEFAULT_TENANT}/databases/{DEFAULT_DATABASE}/collections"
        )
        if path == f"{collection_base}/{INDEX_REGISTRY_COLLECTION_NAME}":
            return _json_response({"id": "registry-uuid", "name": INDEX_REGISTRY_COLLECTION_NAME})
        if path == f"{collection_base}/registry-uuid/get":
            return _json_response(
                {
                    "ids": ["published"],
                    "metadatas": [
                        {
                            "current_build_id": "build-1",
                            "current_collection_name": self.collection_name,
                            "current_artifact_sha256": self.release_sha,
                        }
                    ]
                }
            )
        if path == f"{collection_base}/{self.collection_name}":
            return _json_response({"id": "current-uuid", "name": self.collection_name})
        return _json_response({"forwarded": path}, status=200)


def _json_response(value: object, status: int = 200) -> UpstreamResponse:
    return UpstreamResponse(
        status=status,
        headers=[(b"content-type", b"application/json")],
        body=json.dumps(value).encode("utf-8"),
    )


def _descriptor() -> dict[str, object]:
    return {
        "index_build_id": "build-1",
        "collection_name": "metro__build_build-1",
        "source_tree_sha256": "a" * 64,
        "source_revision": "a" * 64,
        "provenance": {
            "image_version": "image",
            "code_version": "test",
            "python_version": "3.12",
            "chroma_client_version": "1.5.9",
            "chroma_server_version": "1.5.9",
            "embedding_model_id": "embedding",
            "reranker_model_id": "reranker",
            "chunker_config": {"parser": "markdown"},
        },
    }


@pytest.fixture
def proxy(tmp_path: Path) -> tuple[Any, RecordingUpstream]:
    release = create_validated_release(tmp_path, _descriptor())
    upstream = RecordingUpstream(release.sha256, release.collection_name)
    return create_knowledge_read_proxy(tmp_path, upstream=upstream), upstream


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v2/auth/identity", b""),
        ("GET", "/api/v2/heartbeat", b""),
        ("GET", f"/api/v2/tenants/{DEFAULT_TENANT}", b""),
        (
            "GET",
            f"/api/v2/tenants/{DEFAULT_TENANT}/databases/{DEFAULT_DATABASE}",
            b"",
        ),
        (
            "GET",
            f"/api/v2/tenants/{DEFAULT_TENANT}/databases/{DEFAULT_DATABASE}/collections/{INDEX_REGISTRY_COLLECTION_NAME}",
            b"",
        ),
        (
            "POST",
            f"/api/v2/tenants/{DEFAULT_TENANT}/databases/{DEFAULT_DATABASE}/collections/registry-uuid/get",
            b'{"ids":["published"]}',
        ),
        (
            "GET",
            "/api/v2/tenants/default_tenant/databases/default_database/collections/metro__build_build-1",
            b"",
        ),
        (
            "GET",
            "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/count",
            b"",
        ),
        (
            "POST",
            "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/get",
            b'{"ids":["chunk-1"]}',
        ),
        (
            "POST",
            "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/query",
            b'{"query_embeddings":[[0.1]]}',
        ),
    ],
)
def test_only_required_chroma_1_5_9_reads_are_forwarded(
    proxy: tuple[Any, RecordingUpstream], method: str, path: str, body: bytes
) -> None:
    app, upstream = proxy

    response = _request(app, method, path, body)

    assert response["status"] == 200
    expected_body = (
        b'{"ids":["published"],"include":["metadatas"]}'
        if path.endswith("/registry-uuid/get")
        else body
    )
    assert upstream.requests[-1] == (method, path, expected_body)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections"),
        ("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/add"),
        ("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/update"),
        ("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/upsert"),
        ("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/delete"),
        ("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/fork"),
        ("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/reset"),
        ("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/search"),
        ("PUT", "/api/v2/heartbeat"),
        ("DELETE", "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid"),
        ("GET", "/api/v2/tenants/default_tenant/databases/default_database/collections"),
        ("GET", "/api/v2/tenants/other"),
    ],
)
def test_writes_historical_and_unlisted_routes_are_rejected_before_upstream(
    proxy: tuple[Any, RecordingUpstream], method: str, path: str
) -> None:
    app, upstream = proxy

    response = _request(app, method, path, b'{"secret":"do not log"}')

    assert response["status"] == 403
    assert upstream.requests == []


def test_current_collection_is_resolved_per_request_and_historical_uuid_is_rejected(
    proxy: tuple[Any, RecordingUpstream],
) -> None:
    app, upstream = proxy

    response = _request(
        app,
        "POST",
        "/api/v2/tenants/default_tenant/databases/default_database/collections/old-uuid/query",
        b"{}",
    )

    assert response["status"] == 403
    assert upstream.requests[:3] == [
        (
            "GET",
            "/api/v2/tenants/default_tenant/databases/default_database/collections/Metro_Knowledge_Index_Registry_v1",
            b"",
        ),
        (
            "POST",
            "/api/v2/tenants/default_tenant/databases/default_database/collections/registry-uuid/get",
            b'{"ids":["published"],"include":["metadatas"]}',
        ),
        (
            "GET",
            "/api/v2/tenants/default_tenant/databases/default_database/collections/metro__build_build-1",
            b"",
        ),
    ]


def test_stale_descriptor_or_pointer_is_unavailable_and_never_forwards(tmp_path: Path) -> None:
    release = create_validated_release(tmp_path, _descriptor())
    upstream = RecordingUpstream("f" * 64, release.collection_name)
    app = create_knowledge_read_proxy(tmp_path, upstream=upstream)

    response = _request(
        app,
        "POST",
        "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/query",
        b"{}",
    )

    assert response["status"] == 503
    assert all(not request[1].endswith("/query") for request in upstream.requests)


def test_body_limit_is_enforced_before_upstream(proxy: tuple[Any, RecordingUpstream]) -> None:
    app, upstream = proxy

    response = _request(app, "GET", "/api/v2/heartbeat", b"x" * (MAX_BODY_BYTES + 1))

    assert response["status"] == 413
    assert upstream.requests == []


def test_timeout_is_mapped_to_unavailable(proxy: tuple[Any, RecordingUpstream]) -> None:
    app, upstream = proxy
    upstream.error = TimeoutError("read timeout")

    response = _request(app, "GET", "/api/v2/heartbeat", b"")

    assert response["status"] == 503


def test_registry_get_is_limited_to_the_published_pointer(
    proxy: tuple[Any, RecordingUpstream],
) -> None:
    app, upstream = proxy

    response = _request(
        app,
        "POST",
        "/api/v2/tenants/default_tenant/databases/default_database/collections/registry-uuid/get",
        b'{"ids":["another-registry-record"],"include":["documents"]}',
    )

    assert response["status"] == 200
    assert upstream.requests[-1] == (
        "POST",
        "/api/v2/tenants/default_tenant/databases/default_database/collections/registry-uuid/get",
        b'{"ids":["published"],"include":["metadatas"]}',
    )


def test_registry_uuid_is_resolved_at_runtime_and_never_hard_coded(
    proxy: tuple[Any, RecordingUpstream],
) -> None:
    app, upstream = proxy

    response = _request(
        app,
        "POST",
        "/api/v2/tenants/default_tenant/databases/default_database/collections/registry-uuid/get",
        b"{}",
    )

    assert response["status"] == 200
    assert any(request[1].endswith("/registry-uuid/get") for request in upstream.requests)
    response = _request(
        app,
        "POST",
        "/api/v2/tenants/default_tenant/databases/default_database/collections/guessed-registry-uuid/get",
        b"{}",
    )
    assert response["status"] == 403


def test_current_uuid_cannot_be_used_as_a_collection_metadata_name(
    proxy: tuple[Any, RecordingUpstream],
) -> None:
    app, _ = proxy

    response = _request(
        app,
        "GET",
        "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid",
        b"",
    )

    assert response["status"] == 403


def test_structured_logs_redact_body_headers_and_query(
    proxy: tuple[Any, RecordingUpstream], caplog: pytest.LogCaptureFixture
) -> None:
    app, _ = proxy
    secret = "top-secret-vector-and-cookie"

    with caplog.at_level(logging.INFO):
        response = _request(
            app,
            "POST",
            "/api/v2/tenants/default_tenant/databases/default_database/collections/current-uuid/query?query="
            + secret,
            ("{\"query_embeddings\":[\"" + secret + "\"]}").encode(),
            headers=[(b"authorization", secret.encode()), (b"cookie", secret.encode())],
        )

    assert response["status"] == 200
    messages = [record.getMessage() for record in caplog.records]
    assert all(secret not in message for message in messages)
    payload = json.loads(messages[-1])
    assert set(payload) == {"request_id", "route", "status", "elapsed_ms", "error_type"}


def _request(
    app: Any,
    method: str,
    path: str,
    body: bytes,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    async def invoke() -> dict[str, Any]:
        raw_path, _, query_string = path.partition("?")
        events = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return events.pop(0) if events else {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "method": method,
                "path": raw_path,
                "query_string": query_string.encode(),
                "headers": headers or [],
            },
            receive,
            send,
        )
        start = next(message for message in sent if message["type"] == "http.response.start")
        return {"status": start["status"], "messages": sent}

    return asyncio.run(invoke())
