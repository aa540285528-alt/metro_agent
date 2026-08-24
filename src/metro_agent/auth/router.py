from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from metro_agent.auth.dependencies import (
    SESSION_COOKIE_NAME,
    CurrentUser,
    get_current_user,
    get_session_token,
    require_admin,
    require_same_origin,
)


logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=1024)


class CurrentUserResponse(BaseModel):
    id: int
    username: str
    role: str


class AdminUserResponse(CurrentUserResponse):
    is_active: bool


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=1024)
    role: str = Field(default="user", pattern="^(admin|user)$")


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    password: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def require_change(self) -> "UpdateUserRequest":
        if self.is_active is None and self.role is None and self.password is None:
            raise ValueError("at least one user change is required")
        return self


def get_auth_cookie_secure() -> bool:
    value = os.environ.get("AUTH_COOKIE_SECURE", "true").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("AUTH_COOKIE_SECURE must be true or false")
    return value == "true"


def get_auth_session_ttl_seconds() -> int:
    raw_value = os.environ.get("AUTH_SESSION_TTL_SECONDS", "28800")
    try:
        ttl_seconds = int(raw_value)
    except ValueError as exc:
        raise ValueError("AUTH_SESSION_TTL_SECONDS must be an integer") from exc
    if not 0 < ttl_seconds <= 28800:
        raise ValueError("AUTH_SESSION_TTL_SECONDS must be between 1 and 28800")
    return ttl_seconds


def create_auth_router(
    *,
    cookie_secure: bool,
    session_ttl_seconds: int,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post(
        "/auth/login",
        response_model=CurrentUserResponse,
        dependencies=[Depends(require_same_origin)],
    )
    def login(payload: LoginRequest, request: Request, response: Response):
        result = request.app.state.auth_service.login(
            payload.username,
            payload.password,
            timedelta(seconds=session_ttl_seconds),
        )
        if result is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid username or password",
            )
        user, raw_token = result
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=raw_token,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            max_age=session_ttl_seconds,
            path="/",
        )
        return CurrentUserResponse(id=user.id, username=user.username, role=user.role)

    @router.post(
        "/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_same_origin)],
    )
    def logout(
        request: Request,
        metro_session: Annotated[str | None, Depends(get_session_token)],
    ) -> Response:
        if metro_session is not None:
            try:
                user = request.app.state.auth_service.resolve_session(metro_session)
                if user is not None:
                    request.app.state.auth_service.revoke_session(
                        metro_session,
                        user.id,
                        user.id,
                    )
            except Exception as exc:
                logger.warning(
                    "operation=logout_revoke status=ignored error_type=%s",
                    type(exc).__name__,
                )
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path="/",
            secure=cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.get("/auth/me", response_model=CurrentUserResponse)
    def me(
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUserResponse:
        return CurrentUserResponse(**current_user.__dict__)

    @router.post(
        "/admin/users",
        response_model=AdminUserResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_same_origin)],
    )
    def create_user(
        payload: CreateUserRequest,
        request: Request,
        admin: Annotated[CurrentUser, Depends(require_admin)],
    ) -> AdminUserResponse:
        try:
            user = request.app.state.auth_service.create_user(
                payload.username,
                payload.password,
                payload.role,
                admin.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid user") from exc
        return _admin_user_response(user)

    @router.get("/admin/users", response_model=list[AdminUserResponse])
    def list_users(
        request: Request,
        _admin: Annotated[CurrentUser, Depends(require_admin)],
    ) -> list[AdminUserResponse]:
        return [
            _admin_user_response(user)
            for user in request.app.state.auth_service.list_users()
        ]

    @router.patch(
        "/admin/users/{user_id}",
        response_model=AdminUserResponse,
        dependencies=[Depends(require_same_origin)],
    )
    def update_user(
        user_id: int,
        payload: UpdateUserRequest,
        request: Request,
        admin: Annotated[CurrentUser, Depends(require_admin)],
    ) -> AdminUserResponse:
        service = request.app.state.auth_service
        try:
            user = service.update_user(
                user_id,
                admin.id,
                role=payload.role,
                password=payload.password,
                is_active=payload.is_active,
            )
        except ValueError as exc:
            if str(exc) == "user not found":
                raise HTTPException(status_code=404, detail="User not found") from exc
            raise HTTPException(status_code=422, detail="Invalid user update") from exc
        return _admin_user_response(user)

    return router


def _admin_user_response(user) -> AdminUserResponse:
    return AdminUserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
    )
