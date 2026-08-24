from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: str


def get_current_user(
    request: Request,
    metro_session: Annotated[str | None, Cookie()] = None,
) -> CurrentUser:
    user = (
        request.app.state.auth_service.resolve_session(metro_session)
        if metro_session
        else None
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return CurrentUser(id=user.id, username=user.username, role=user.role)


def require_admin(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return current_user
