from __future__ import annotations

import re

from pwdlib import PasswordHash


_USERNAME_PATTERN = re.compile(r"[a-z0-9._-]{3,64}")
_PASSWORD_HASH = PasswordHash.recommended()


def normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("username must be a string")
    normalized = username.strip().lower()
    if _USERNAME_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            "username must be 3-64 characters using a-z, 0-9, '.', '_', or '-'"
        )
    return normalized


def password_hash(password: str) -> str:
    _validate_password(password)
    return _PASSWORD_HASH.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return _PASSWORD_HASH.verify(password, encoded)
    except (TypeError, ValueError):
        return False


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if len(password) < 12 or not any(char.isalpha() for char in password):
        raise ValueError(
            "password must be at least 12 characters with letters and digits"
        )
    if not any(char.isdigit() for char in password):
        raise ValueError(
            "password must be at least 12 characters with letters and digits"
        )
