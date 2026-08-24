from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyCookie


SESSION_COOKIE_NAME = "metro_session"
session_cookie_scheme = APIKeyCookie(
    name=SESSION_COOKIE_NAME,
    scheme_name="MetroSessionCookie",
    auto_error=False,
)


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: str


def auth_owner_subject(user_id: int) -> str:
    return f"auth:{user_id}"


def auth_checkpoint_thread_id(owner_subject: str, client_thread_id: str) -> str:
    return f"{owner_subject}:{client_thread_id}"


def get_current_user(
    request: Request,
    metro_session: Annotated[str | None, Security(session_cookie_scheme)],
) -> CurrentUser:
    user = (
        request.app.state.auth_service.resolve_session(metro_session)
        if metro_session
        else None
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return CurrentUser(id=user.id, username=user.username, role=user.role)


def get_session_token(
    metro_session: Annotated[str | None, Security(session_cookie_scheme)],
) -> str | None:
    return metro_session


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return
    supplied_origin = _normalized_http_origin(origin, allow_path=False)
    request_origin = _normalized_http_origin(str(request.base_url), allow_path=True)
    if supplied_origin is None or supplied_origin != request_origin:
        raise HTTPException(status_code=403, detail="Cross-origin request forbidden")


def _normalized_http_origin(
    value: str,
    *,
    allow_path: bool,
) -> tuple[str, str, int] | None:
    if value != value.strip():
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in {"http", "https"} or hostname is None:
        return None
    if parsed.netloc.endswith(":"):
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
        return None
    if not allow_path and parsed.path:
        return None
    effective_port = port if port is not None else (80 if scheme == "http" else 443)
    return scheme, hostname.lower(), effective_port


def require_admin(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return current_user
