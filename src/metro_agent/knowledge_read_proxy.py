"""Fail-closed ASGI read proxy for the published Chroma 1.5.9 collection."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from metro_agent.knowledge.config import KnowledgeSettings
from metro_agent.knowledge.releases import (
    INDEX_REGISTRY_COLLECTION_NAME,
    PUBLISHED_INDEX_ID,
    ReleaseValidationError,
    read_validated_release,
)


CHROMA_API_PREFIX = "/api/v2"
CHROMA_UPSTREAM_BASE_URL = "http://chroma:8000"
CHROMA_CONNECT_TIMEOUT_SECONDS = 2.0
CHROMA_READ_TIMEOUT_SECONDS = 10.0
DEFAULT_TENANT = "default_tenant"
DEFAULT_DATABASE = "default_database"
MAX_BODY_BYTES = 1024 * 1024

_LOGGER = logging.getLogger(__name__)
_COLLECTION_BASE = (
    f"{CHROMA_API_PREFIX}/tenants/{DEFAULT_TENANT}/databases/{DEFAULT_DATABASE}/collections"
)
_REGISTRY_RECORD_BODY = json.dumps(
    {"ids": [PUBLISHED_INDEX_ID], "include": ["metadatas"]}, separators=(",", ":")
).encode("utf-8")


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes


class UpstreamTransport(Protocol):
    async def request(
        self, method: str, path: str, body: bytes, headers: list[tuple[bytes, bytes]]
    ) -> UpstreamResponse: ...


class _UpstreamFailure(RuntimeError):
    def __init__(self, error_type: str) -> None:
        self.error_type = error_type
        super().__init__(error_type)


class _HttpxChromaUpstream:
    """The deployment has one fixed, internal-only Chroma endpoint."""

    async def request(
        self, method: str, path: str, body: bytes, headers: list[tuple[bytes, bytes]]
    ) -> UpstreamResponse:
        import httpx

        timeout = httpx.Timeout(
            connect=CHROMA_CONNECT_TIMEOUT_SECONDS,
            read=CHROMA_READ_TIMEOUT_SECONDS,
            write=CHROMA_READ_TIMEOUT_SECONDS,
            pool=CHROMA_CONNECT_TIMEOUT_SECONDS,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    f"{CHROMA_UPSTREAM_BASE_URL}{path}",
                    content=body,
                    headers={key.decode("latin-1"): value.decode("latin-1") for key, value in headers},
                )
        except httpx.TimeoutException as exc:
            raise _UpstreamFailure("timeout") from exc
        except httpx.HTTPError as exc:
            raise _UpstreamFailure("unavailable") from exc
        return UpstreamResponse(
            status=response.status_code,
            headers=[(key.encode("latin-1"), value.encode("latin-1")) for key, value in response.headers.items()],
            body=response.content,
        )


class KnowledgeReadProxy:
    """Permit exactly the published registry and current collection read routes."""

    def __init__(self, artifact_root: Path | str, *, upstream: UpstreamTransport | None = None) -> None:
        self._artifact_root = Path(artifact_root)
        self._upstream = upstream or _HttpxChromaUpstream()

    async def __call__(
        self,
        scope: dict[str, object],
        receive: Callable[[], Awaitable[dict[str, object]]],
        send: Callable[[dict[str, object]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            return
        started = time.monotonic()
        request_id = str(uuid.uuid4())
        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        route = self._route_category(method, path)
        status = 500
        error_type: str | None = None
        try:
            if _is_statically_denied_route(method, path):
                status, error_type = 403, "route_denied"
                await self._send_error(send, status, request_id)
                return
            body = await _read_body(receive)
            if len(body) > MAX_BODY_BYTES:
                status, error_type = 413, "body_too_large"
                await self._send_error(send, status, request_id)
                return
            if method in {"PUT", "DELETE"}:
                status, error_type = 403, "route_denied"
                await self._send_error(send, status, request_id)
                return
            response = await self._handle(method, path, body, _safe_request_headers(scope))
            status = response.status
            await _send_response(send, response, request_id)
        except _UpstreamFailure as exc:
            status, error_type = 503, exc.error_type
            await self._send_error(send, status, request_id)
        except ReleaseValidationError:
            status, error_type = 503, "release_validation"
            await self._send_error(send, status, request_id)
        except _RouteDenied:
            status, error_type = 403, "route_denied"
            await self._send_error(send, status, request_id)
        except _BodyReadError:
            status, error_type = 400, "invalid_request"
            await self._send_error(send, status, request_id)
        finally:
            _LOGGER.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "route": route,
                        "status": status,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "error_type": error_type,
                    },
                    separators=(",", ":"),
                )
            )

    async def _handle(
        self, method: str, path: str, body: bytes, headers: list[tuple[bytes, bytes]]
    ) -> UpstreamResponse:
        if (method, path) in {
            ("GET", f"{CHROMA_API_PREFIX}/auth/identity"),
            ("GET", f"{CHROMA_API_PREFIX}/heartbeat"),
            ("GET", f"{CHROMA_API_PREFIX}/tenants/{DEFAULT_TENANT}"),
            (
                "GET",
                f"{CHROMA_API_PREFIX}/tenants/{DEFAULT_TENANT}/databases/{DEFAULT_DATABASE}",
            ),
        }:
            return await self._forward(method, path, body, headers)

        collection_ref, action = _collection_route(path)
        if collection_ref is None:
            raise _RouteDenied()

        if (method, action) not in {
            ("GET", None),
            ("GET", "count"),
            ("POST", "get"),
            ("POST", "query"),
        }:
            raise _RouteDenied()

        if collection_ref == INDEX_REGISTRY_COLLECTION_NAME:
            if (method, action) != ("GET", None):
                raise _RouteDenied()
            await self._current_collection()
            return await self._forward(method, path, body, headers)

        registry_uuid = await self._registry_uuid()
        if collection_ref == registry_uuid:
            if (method, action) != ("POST", "get"):
                raise _RouteDenied()
            await self._current_collection(registry_uuid=registry_uuid)
            # The registry is a control-plane collection.  Its sole readable
            # document through this proxy is the single published pointer;
            # never let callers enumerate any other registry records.
            body = _REGISTRY_RECORD_BODY
            return await self._forward(method, path, body, headers)

        current_name, current_uuid = await self._current_collection(registry_uuid=registry_uuid)
        allowed_current_route = (
            (method, action) == ("GET", None) and collection_ref == current_name
        ) or (
            (method, action) in {("GET", "count"), ("POST", "get"), ("POST", "query")}
            and collection_ref == current_uuid
        )
        if not allowed_current_route:
            raise _RouteDenied()
        return await self._forward(method, path, body, headers)

    async def _registry_uuid(self) -> str:
        response = await self._forward(
            "GET", f"{_COLLECTION_BASE}/{INDEX_REGISTRY_COLLECTION_NAME}", b"", []
        )
        if response.status != 200:
            raise _UpstreamFailure("registry_unavailable")
        return _collection_id(response, INDEX_REGISTRY_COLLECTION_NAME)

    async def _current_collection(self, *, registry_uuid: str | None = None) -> tuple[str, str]:
        registry_uuid = registry_uuid or await self._registry_uuid()
        pointer_response = await self._forward(
            "POST", f"{_COLLECTION_BASE}/{registry_uuid}/get", _REGISTRY_RECORD_BODY, []
        )
        if pointer_response.status != 200:
            raise _UpstreamFailure("registry_unavailable")
        build_id, collection_name, artifact_sha256 = _published_pointer(pointer_response)
        release = read_validated_release(self._artifact_root, build_id)
        if release.sha256 != artifact_sha256 or release.collection_name != collection_name:
            raise ReleaseValidationError("published pointer does not match validated release")
        collection_response = await self._forward(
            "GET", f"{_COLLECTION_BASE}/{collection_name}", b"", []
        )
        if collection_response.status != 200:
            raise _UpstreamFailure("current_collection_unavailable")
        return collection_name, _collection_id(collection_response, collection_name)

    async def _forward(
        self, method: str, path: str, body: bytes, headers: list[tuple[bytes, bytes]]
    ) -> UpstreamResponse:
        try:
            return await self._upstream.request(method, path, body, headers)
        except _UpstreamFailure:
            raise
        except TimeoutError as exc:
            raise _UpstreamFailure("timeout") from exc
        except Exception as exc:
            raise _UpstreamFailure("unavailable") from exc

    async def _send_error(
        self,
        send: Callable[[dict[str, object]], Awaitable[None]],
        status: int,
        request_id: str,
    ) -> None:
        await _send_response(
            send,
            UpstreamResponse(
                status=status,
                headers=[(b"content-type", b"application/json")],
                body=b'{"detail":"published knowledge service unavailable"}',
            ),
            request_id,
        )

    @staticmethod
    def _route_category(method: str, path: str) -> str:
        if path in {f"{CHROMA_API_PREFIX}/heartbeat", f"{CHROMA_API_PREFIX}/auth/identity"}:
            return "service"
        collection_ref, action = _collection_route(path)
        if collection_ref == INDEX_REGISTRY_COLLECTION_NAME:
            return "registry"
        if collection_ref is not None:
            return f"collection:{action or 'metadata'}"
        return f"other:{method.lower()}"


def create_knowledge_read_proxy(
    artifact_root: Path | str, *, upstream: UpstreamTransport | None = None
) -> KnowledgeReadProxy:
    """Create the ASGI application without exposing a configurable upstream URL."""
    return KnowledgeReadProxy(artifact_root, upstream=upstream)


def _configured_artifact_root() -> Path:
    artifact_root = KnowledgeSettings.from_environment().artifact_root
    if artifact_root is None:
        raise RuntimeError("KNOWLEDGE_ARTIFACT_ROOT must be configured for knowledge-read-proxy")
    return artifact_root


# Uvicorn imports this fixed deployment entry point.  The upstream remains the
# internal Chroma service constant above and cannot be supplied by callers.
app = create_knowledge_read_proxy(_configured_artifact_root())


class _RouteDenied(RuntimeError):
    pass


class _BodyReadError(RuntimeError):
    pass


async def _read_body(
    receive: Callable[[], Awaitable[dict[str, object]]]
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            raise _BodyReadError()
        if message_type != "http.request":
            raise _BodyReadError()
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes):
            raise _BodyReadError()
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return b"x" * (MAX_BODY_BYTES + 1)
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _collection_route(path: str) -> tuple[str | None, str | None]:
    if not path.startswith(f"{_COLLECTION_BASE}/"):
        return None, None
    segments = path[len(_COLLECTION_BASE) + 1 :].split("/")
    if len(segments) == 1 and segments[0]:
        return segments[0], None
    if len(segments) == 2 and all(segments):
        return segments[0], segments[1]
    return None, None


def _is_statically_denied_route(method: str, path: str) -> bool:
    """Reject known writes before consuming an attacker-controlled request body."""
    if method in {"PUT", "DELETE"}:
        return True
    if (method, path) in {
        ("GET", f"{CHROMA_API_PREFIX}/auth/identity"),
        ("GET", f"{CHROMA_API_PREFIX}/heartbeat"),
        ("GET", f"{CHROMA_API_PREFIX}/tenants/{DEFAULT_TENANT}"),
        ("GET", f"{CHROMA_API_PREFIX}/tenants/{DEFAULT_TENANT}/databases/{DEFAULT_DATABASE}"),
    }:
        return False
    collection_ref, action = _collection_route(path)
    if collection_ref is None:
        return True
    if (method, action) not in {
        ("GET", None),
        ("GET", "count"),
        ("POST", "get"),
        ("POST", "query"),
    }:
        return True
    return collection_ref == INDEX_REGISTRY_COLLECTION_NAME and (method, action) != ("GET", None)


def _collection_id(response: UpstreamResponse, expected_name: str) -> str:
    payload = _response_json(response)
    collection_id = payload.get("id")
    if payload.get("name") != expected_name or not isinstance(collection_id, str) or not collection_id:
        raise ReleaseValidationError("collection resolution is invalid")
    return collection_id


def _published_pointer(response: UpstreamResponse) -> tuple[str, str, str]:
    payload = _response_json(response)
    if payload.get("ids") != [PUBLISHED_INDEX_ID]:
        raise ReleaseValidationError("published pointer is invalid")
    metadatas = payload.get("metadatas")
    if not isinstance(metadatas, list) or len(metadatas) != 1 or not isinstance(metadatas[0], dict):
        raise ReleaseValidationError("published pointer is invalid")
    metadata = metadatas[0]
    build_id = metadata.get("current_build_id")
    collection_name = metadata.get("current_collection_name")
    artifact_sha256 = metadata.get("current_artifact_sha256")
    if not all(isinstance(value, str) and value for value in (build_id, collection_name, artifact_sha256)):
        raise ReleaseValidationError("published pointer is invalid")
    return build_id, collection_name, artifact_sha256


def _response_json(response: UpstreamResponse) -> dict[str, object]:
    try:
        payload = json.loads(response.body)
    except (TypeError, ValueError) as exc:
        raise ReleaseValidationError("upstream response is invalid") from exc
    if not isinstance(payload, dict):
        raise ReleaseValidationError("upstream response is invalid")
    return payload


def _safe_request_headers(scope: dict[str, object]) -> list[tuple[bytes, bytes]]:
    headers = scope.get("headers", [])
    if not isinstance(headers, list):
        return []
    return [
        (key, value)
        for key, value in headers
        if isinstance(key, bytes)
        and isinstance(value, bytes)
        and key.lower() in {b"accept", b"content-type"}
    ]


async def _send_response(
    send: Callable[[dict[str, object]], Awaitable[None]],
    response: UpstreamResponse,
    request_id: str,
) -> None:
    headers = [
        (key, value)
        for key, value in response.headers
        if key.lower() not in {b"content-length", b"set-cookie", b"connection"}
    ]
    headers.extend([(b"content-length", str(len(response.body)).encode()), (b"x-request-id", request_id.encode())])
    await send({"type": "http.response.start", "status": response.status, "headers": headers})
    await send({"type": "http.response.body", "body": response.body, "more_body": False})
