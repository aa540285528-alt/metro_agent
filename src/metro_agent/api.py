import json
import logging
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from metro_agent.agent_service import AgentRunError, run_chat
from metro_agent.auth.database import create_auth_session_factory
from metro_agent.auth.dependencies import (
    CurrentUser,
    auth_checkpoint_thread_id,
    auth_owner_subject,
    get_current_user,
    get_session_token,
    require_admin,
    require_same_origin,
)
from metro_agent.auth.router import (
    create_auth_router,
    get_auth_cookie_secure,
    get_auth_session_ttl_seconds,
)
from metro_agent.auth.service import AuthService
from metro_agent.storage.history.service import ConversationNotFound
from metro_agent.observability.query_service import (
    MonitoringFilter,
    MonitoringQueryService,
    TraceNotFound,
)


logger = logging.getLogger(__name__)

PAGE_PATH = Path(__file__).resolve().parent / "static" / "preview.html"
SAFE_ERROR_MESSAGE = "暂时无法完成本次请求，请稍后重试。"


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(..., max_length=128, strict=True)
    message: str = Field(..., max_length=10000, strict=True)

    @field_validator("thread_id", "message")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ConversationUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=40, strict=True)
    is_pinned: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def require_change(self) -> "ConversationUpdateRequest":
        if self.title is None and self.is_pinned is None:
            raise ValueError("title or is_pinned is required")
        return self


def build_default_graph() -> Any:
    from metro_agent.graph import build_graph

    return build_graph()


def build_default_history_service() -> Any:
    from metro_agent.storage.history.database import SessionLocal
    from metro_agent.storage.history.service import ConversationHistoryService

    return ConversationHistoryService(SessionLocal)


def build_default_monitoring_service() -> Any:
    from metro_agent.storage.history.database import SessionLocal

    return MonitoringQueryService(SessionLocal)


def build_default_auth_service() -> AuthService:
    return AuthService(create_auth_session_factory())


def encode_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def serialize_history(value: Any) -> Any:
    return jsonable_encoder(asdict(value) if is_dataclass(value) else value)


def monitoring_filter(
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    status: str | None = Query(default=None, max_length=32),
    agent: str | None = Query(default=None, max_length=128),
    model: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> MonitoringFilter:
    return MonitoringFilter(
        started_after=started_after,
        started_before=started_before,
        status=status,
        agent=agent,
        model=model,
        limit=limit,
        offset=offset,
    )


def reject_legacy_monitoring_user_id(request: Request) -> None:
    if "user_id" in request.query_params:
        raise HTTPException(status_code=422, detail="user_id is no longer supported")


def create_app(
    graph_factory: Callable[[], Any] = build_default_graph,
    history_service_factory: Callable[[], Any] = build_default_history_service,
    monitoring_service_factory: Callable[[], Any] | None = None,
    auth_service_factory: Callable[[], Any] = build_default_auth_service,
    chat_runner: Callable[..., str] = run_chat,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.graph = graph_factory()
        app.state.history_service = history_service_factory()
        app.state.auth_service = auth_service_factory()
        app.state.monitoring_service_factory = (
            monitoring_service_factory or build_default_monitoring_service
        )
        app.state.monitoring_service = (
            monitoring_service_factory()
            if monitoring_service_factory is not None
            else None
        )
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(
        create_auth_router(
            cookie_secure=get_auth_cookie_secure(),
            session_ttl_seconds=get_auth_session_ttl_seconds(),
        )
    )

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(PAGE_PATH, media_type="text/html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/monitoring/summary")
    def monitoring_summary(
        request: Request,
        filters: MonitoringFilter = Depends(monitoring_filter),
        _legacy_user_id: None = Depends(reject_legacy_monitoring_user_id),
        _admin: CurrentUser = Depends(require_admin),
    ) -> dict[str, Any]:
        return serialize_history(_monitoring_service(request).summary(filters))

    @app.get("/api/monitoring/traces")
    def monitoring_traces(
        request: Request,
        filters: MonitoringFilter = Depends(monitoring_filter),
        _legacy_user_id: None = Depends(reject_legacy_monitoring_user_id),
        _admin: CurrentUser = Depends(require_admin),
    ) -> dict[str, Any]:
        return serialize_history(_monitoring_service(request).list_traces(filters))

    @app.get("/api/monitoring/traces/{trace_id}")
    def monitoring_trace_detail(
        request: Request,
        trace_id: str,
        _legacy_user_id: None = Depends(reject_legacy_monitoring_user_id),
        _admin: CurrentUser = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            detail = _monitoring_service(request).get_trace(trace_id)
        except TraceNotFound as exc:
            raise HTTPException(status_code=404, detail="Trace not found") from exc
        return serialize_history(detail)

    @app.get("/api/monitoring/evaluations")
    def monitoring_evaluations(
        request: Request,
        category: str | None = Query(default=None, max_length=32),
        _legacy_user_id: None = Depends(reject_legacy_monitoring_user_id),
        _admin: CurrentUser = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        values = _monitoring_service(request).list_evaluations(category=category)
        return [serialize_history(value) for value in values]

    @app.get("/api/conversations")
    def list_conversations(
        request: Request,
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> list[dict[str, Any]]:
        conversations = request.app.state.history_service.list_conversations(
            auth_owner_subject(current_user.id)
        )
        return [serialize_history(conversation) for conversation in conversations]

    @app.get("/api/conversations/{thread_id}")
    def get_conversation(
        request: Request,
        thread_id: str,
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> dict[str, Any]:
        try:
            conversation = request.app.state.history_service.get_conversation(
                thread_id, auth_owner_subject(current_user.id)
            )
        except ConversationNotFound as exc:
            raise HTTPException(
                status_code=404, detail="Conversation not found"
            ) from exc
        return serialize_history(conversation)

    @app.patch(
        "/api/conversations/{thread_id}",
        dependencies=[Depends(require_same_origin)],
    )
    def update_conversation(
        request: Request,
        thread_id: str,
        payload: ConversationUpdateRequest,
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> dict[str, Any]:
        try:
            conversation = request.app.state.history_service.update_conversation(
                thread_id,
                auth_owner_subject(current_user.id),
                title=payload.title,
                is_pinned=payload.is_pinned,
            )
        except ConversationNotFound as exc:
            raise HTTPException(
                status_code=404, detail="Conversation not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid conversation update"
            ) from exc
        return serialize_history(conversation)

    @app.delete(
        "/api/conversations/{thread_id}",
        status_code=204,
        dependencies=[Depends(require_same_origin)],
    )
    def delete_conversation(
        request: Request,
        thread_id: str,
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> Response:
        try:
            request.app.state.history_service.delete_conversation(
                thread_id, auth_owner_subject(current_user.id)
            )
        except ConversationNotFound as exc:
            raise HTTPException(
                status_code=404, detail="Conversation not found"
            ) from exc
        return Response(status_code=204)

    @app.post(
        "/api/chat/stream",
        response_class=StreamingResponse,
        dependencies=[Depends(require_same_origin)],
        responses={
            200: {
                "description": "Server-sent chat events",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            401: {"description": "Not authenticated"},
            403: {"description": "Cross-origin request forbidden"},
        },
    )
    def chat_stream(
        request: Request,
        payload: ChatRequest,
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
        _raw_session_token: Annotated[str | None, Depends(get_session_token)],
    ) -> StreamingResponse:
        owner_subject = auth_owner_subject(current_user.id)
        try:
            request.app.state.history_service.ensure_thread_available(
                payload.thread_id, owner_subject
            )
        except ConversationNotFound as exc:
            raise HTTPException(
                status_code=404, detail="Conversation not found"
            ) from exc

        def session_is_current() -> bool:
            if _raw_session_token is None:
                return False
            try:
                resolved = request.app.state.auth_service.resolve_session(
                    _raw_session_token
                )
            except Exception:
                return False
            return resolved is not None and resolved.id == current_user.id

        def events() -> Generator[str, None, None]:
            if not session_is_current():
                yield encode_sse("error", {"message": SAFE_ERROR_MESSAGE})
                yield encode_sse("done", {})
                return
            try:
                answer = chat_runner(
                    request.app.state.graph,
                    thread_id=auth_checkpoint_thread_id(
                        owner_subject, payload.thread_id
                    ),
                    user_id=owner_subject,
                    message=payload.message,
                )
            except AgentRunError:
                logger.warning("Agent chat did not produce a usable answer")
                yield encode_sse("error", {"message": SAFE_ERROR_MESSAGE})
            except Exception as exc:
                logger.error(
                    "operation=run_chat error_type=%s",
                    type(exc).__name__,
                )
                yield encode_sse("error", {"message": SAFE_ERROR_MESSAGE})
            else:
                if not session_is_current():
                    yield encode_sse("error", {"message": SAFE_ERROR_MESSAGE})
                else:
                    try:
                        request.app.state.history_service.record_turn(
                            payload.thread_id,
                            owner_subject,
                            payload.message,
                            answer,
                        )
                    except Exception as exc:
                        logger.error(
                            "operation=record_turn error_type=%s",
                            type(exc).__name__,
                        )
                        yield encode_sse("error", {"message": SAFE_ERROR_MESSAGE})
                    else:
                        yield encode_sse(
                            "final",
                            {"thread_id": payload.thread_id, "content": answer},
                        )
            yield encode_sse("done", {})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _monitoring_service(request: Request) -> Any:
    service = request.app.state.monitoring_service
    if service is None:
        service = request.app.state.monitoring_service_factory()
        request.app.state.monitoring_service = service
    return service


app = create_app()
