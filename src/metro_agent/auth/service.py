from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from metro_agent.auth.models import (
    AuthAuditEvent,
    AuthSession,
    AuthUser,
    utc_now_naive,
)
from metro_agent.auth.passwords import (
    normalize_username,
    password_hash,
    verify_password,
)


_ALLOWED_ROLES = frozenset({"admin", "user"})
_MAX_SESSION_TTL = timedelta(hours=8)
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$DWOPKfDPXCaXtDpklUZG8w$"
    "VwwEezJKpT2x7NLBEE6om5LoxH4jvf8/+tNm9V0hUiY"
)


class AuthService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        session_pepper: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session_pepper = session_pepper or os.environ.get("AUTH_SESSION_PEPPER")
        if not self._session_pepper:
            raise ValueError("AUTH_SESSION_PEPPER must be set")

    def create_user(
        self,
        username: str,
        password: str,
        role: str,
        actor_user_id: int | None,
    ) -> AuthUser:
        normalized = normalize_username(username)
        encoded = password_hash(password)
        self._validate_role(role)
        with self._session_factory.begin() as session:
            if session.scalar(
                select(AuthUser.id).where(AuthUser.username == normalized)
            ):
                raise ValueError("username already exists")
            user = AuthUser(
                username=normalized,
                password_hash=encoded,
                role=role,
                is_active=True,
            )
            session.add(user)
            try:
                session.flush()
            except IntegrityError:
                raise ValueError("username already exists") from None
            self._add_audit(
                session,
                "user_created",
                actor_user_id,
                user.id,
                {"username": normalized, "role": role},
            )
            return user

    def authenticate(self, username: str, password: str) -> AuthUser | None:
        """Verify credentials only; interactive login callers must use login()."""
        try:
            normalized = normalize_username(username)
        except ValueError:
            verify_password(password, _DUMMY_PASSWORD_HASH)
            self._record_failed_authentication(
                self._malformed_username_metadata(username)
            )
            return None

        with self._session_factory.begin() as session:
            user = session.scalar(
                select(AuthUser).where(AuthUser.username == normalized)
            )
            encoded = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
            password_matches = verify_password(password, encoded)
            if user is None or not password_matches or not user.is_active:
                self._add_audit(
                    session,
                    "authentication_failed",
                    None,
                    user.id if user is not None else None,
                    {"username": normalized},
                )
                return None

            user.last_login_at = utc_now_naive()
            user.updated_at = utc_now_naive()
            self._add_audit(
                session,
                "authentication_succeeded",
                user.id,
                user.id,
                {"username": normalized},
            )
            return user

    def login(
        self,
        username: str,
        password: str,
        ttl: timedelta,
    ) -> tuple[AuthUser, str] | None:
        """Verify credentials and issue a session in one transaction."""
        self._validate_session_ttl(ttl)
        try:
            normalized = normalize_username(username)
        except ValueError:
            verify_password(password, _DUMMY_PASSWORD_HASH)
            self._record_failed_authentication(
                self._malformed_username_metadata(username)
            )
            return None

        with self._session_factory.begin() as session:
            user = session.scalar(
                select(AuthUser)
                .where(AuthUser.username == normalized)
                .with_for_update()
            )
            encoded = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
            password_matches = verify_password(password, encoded)
            if user is None or not password_matches or not user.is_active:
                self._add_audit(
                    session,
                    "authentication_failed",
                    None,
                    user.id if user is not None else None,
                    {"username": normalized},
                )
                return None

            raw_token = secrets.token_urlsafe(32)
            now = utc_now_naive()
            expires_at = now + ttl
            user.last_login_at = now
            user.updated_at = now
            session.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=self._hash_token(raw_token),
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            self._add_audit(
                session,
                "authentication_succeeded",
                user.id,
                user.id,
                {"username": normalized},
            )
            self._add_audit(
                session,
                "session_created",
                user.id,
                user.id,
                {"expires_at": expires_at.isoformat()},
            )
            return user, raw_token

    def create_session(
        self,
        user_id: int,
        ttl: timedelta,
        actor_user_id: int | None,
    ) -> str:
        """Issue a session for a trusted admin flow, never from raw credentials."""
        self._validate_session_ttl(ttl)
        raw_token = secrets.token_urlsafe(32)
        now = utc_now_naive()
        with self._session_factory.begin() as session:
            users = self._lock_users_for_update(session, user_id, actor_user_id)
            user = users[user_id]
            if not user.is_active:
                raise ValueError("active user not found")
            session.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=self._hash_token(raw_token),
                    created_at=now,
                    expires_at=now + ttl,
                )
            )
            self._add_audit(
                session,
                "session_created",
                actor_user_id,
                user.id,
                {"expires_at": (now + ttl).isoformat()},
            )
        return raw_token

    def resolve_session(self, raw_token: str) -> AuthUser | None:
        token_hash = self._hash_token(raw_token)
        statement = (
            select(AuthUser)
            .join(AuthSession, AuthSession.user_id == AuthUser.id)
            .where(
                AuthSession.token_hash == token_hash,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > utc_now_naive(),
                AuthUser.is_active.is_(True),
            )
        )
        with self._session_factory() as session:
            return session.scalar(statement)

    def revoke_session(
        self,
        raw_token: str,
        user_id: int,
        actor_user_id: int | None,
    ) -> None:
        with self._session_factory.begin() as session:
            self._lock_users_for_update(session, user_id, actor_user_id)
            auth_session = session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.token_hash == self._hash_token(raw_token),
                    AuthSession.user_id == user_id,
                )
                .with_for_update()
            )
            if auth_session is None:
                raise ValueError("session not found")
            if auth_session.revoked_at is not None:
                return
            auth_session.revoked_at = utc_now_naive()
            self._add_audit(session, "session_revoked", actor_user_id, user_id, {})

    def set_user_active(
        self,
        user_id: int,
        is_active: bool,
        actor_user_id: int | None,
    ) -> AuthUser:
        now = utc_now_naive()
        with self._session_factory.begin() as session:
            users = self._lock_users_for_update(session, user_id, actor_user_id)
            user = users[user_id]
            if user.is_active is is_active:
                return user
            user.is_active = is_active
            user.updated_at = now
            if not is_active:
                self._revoke_active_sessions(session, user_id, now)
            self._add_audit(
                session,
                "user_enabled" if is_active else "user_disabled",
                actor_user_id,
                user_id,
                {},
            )
            return user

    def update_user(
        self,
        user_id: int,
        actor_user_id: int | None,
        *,
        role: str | None = None,
        password: str | None = None,
        is_active: bool | None = None,
    ) -> AuthUser:
        if role is not None:
            self._validate_role(role)
        encoded_password = password_hash(password) if password is not None else None
        now = utc_now_naive()

        with self._session_factory.begin() as session:
            users = self._lock_users_for_update(session, user_id, actor_user_id)
            user = users[user_id]

            if role is not None and user.role != role:
                previous_role = user.role
                user.role = role
                self._add_audit(
                    session,
                    "user_role_changed",
                    actor_user_id,
                    user_id,
                    {"previous_role": previous_role, "role": role},
                )

            if encoded_password is not None:
                user.password_hash = encoded_password
                self._revoke_active_sessions(session, user_id, now)
                self._add_audit(
                    session,
                    "password_reset",
                    actor_user_id,
                    user_id,
                    {},
                )

            if is_active is not None and user.is_active is not is_active:
                user.is_active = is_active
                if not is_active:
                    self._revoke_active_sessions(session, user_id, now)
                self._add_audit(
                    session,
                    "user_enabled" if is_active else "user_disabled",
                    actor_user_id,
                    user_id,
                    {},
                )

            user.updated_at = now
            return user

    def reset_password(
        self,
        user_id: int,
        new_password: str,
        actor_user_id: int | None,
    ) -> None:
        encoded = password_hash(new_password)
        now = utc_now_naive()
        with self._session_factory.begin() as session:
            users = self._lock_users_for_update(session, user_id, actor_user_id)
            user = users[user_id]
            user.password_hash = encoded
            user.updated_at = now
            self._revoke_active_sessions(session, user_id, now)
            self._add_audit(session, "password_reset", actor_user_id, user_id, {})

    def list_users(self) -> list[AuthUser]:
        with self._session_factory() as session:
            return list(session.scalars(select(AuthUser).order_by(AuthUser.id)))

    def list_audit_events(self) -> list[AuthAuditEvent]:
        with self._session_factory() as session:
            return list(
                session.scalars(select(AuthAuditEvent).order_by(AuthAuditEvent.id))
            )

    def _record_failed_authentication(self, metadata: dict[str, str]) -> None:
        with self._session_factory.begin() as session:
            self._add_audit(
                session,
                "authentication_failed",
                None,
                None,
                metadata,
            )

    def _hash_token(self, raw_token: str) -> str:
        if not isinstance(raw_token, str):
            return ""
        value = f"{self._session_pepper}:{raw_token}".encode()
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _add_audit(
        session: Session,
        event_type: str,
        actor_user_id: int | None,
        subject_user_id: int | None,
        metadata: dict[str, Any],
    ) -> None:
        session.add(
            AuthAuditEvent(
                actor_user_id=actor_user_id,
                subject_user_id=subject_user_id,
                event_type=event_type,
                metadata_json=metadata,
            )
        )

    @staticmethod
    def _lock_users_for_update(
        session: Session,
        *user_ids: int | None,
    ) -> dict[int, AuthUser]:
        ordered_ids = sorted({user_id for user_id in user_ids if user_id is not None})
        if not ordered_ids:
            return {}
        users = list(
            session.scalars(
                select(AuthUser)
                .where(AuthUser.id.in_(ordered_ids))
                .order_by(AuthUser.id)
                .with_for_update()
            )
        )
        users_by_id = {user.id: user for user in users}
        if len(users_by_id) != len(ordered_ids):
            raise ValueError("user not found")
        return users_by_id

    @staticmethod
    def _validate_session_ttl(ttl: timedelta) -> None:
        if not isinstance(ttl, timedelta) or not timedelta(0) < ttl <= _MAX_SESSION_TTL:
            raise ValueError(
                "session ttl must be greater than zero and at most 8 hours"
            )

    @staticmethod
    def _malformed_username_metadata(username: object) -> dict[str, str]:
        normalized = str(username).strip().lower()
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        return {"username_sha256": digest}

    @staticmethod
    def _revoke_active_sessions(session: Session, user_id: int, now: datetime) -> None:
        session.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    @staticmethod
    def _validate_role(role: str) -> None:
        if role not in _ALLOWED_ROLES:
            raise ValueError("role must be 'admin' or 'user'")
