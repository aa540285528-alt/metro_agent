"""Trusted operating-system identities for immutable publication audit records."""

from __future__ import annotations

import ctypes
import os


class OperatorIdentityError(RuntimeError):
    """The current process identity cannot be resolved safely."""


def trusted_operator_identity() -> str:
    """Return the account bound to the current effective process token."""
    if os.name == "nt":
        return _windows_operator_identity()
    return _posix_operator_identity()


def _posix_operator_identity() -> str:
    import pwd

    try:
        identity = pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError) as exc:
        raise OperatorIdentityError("effective Unix operator identity is unavailable") from exc
    if not identity:
        raise OperatorIdentityError("effective Unix operator identity is unavailable")
    return identity


def _windows_operator_identity() -> str:
    size = ctypes.c_ulong(257)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
        raise OperatorIdentityError("Windows operator identity is unavailable")
    identity = buffer.value
    if not identity:
        raise OperatorIdentityError("Windows operator identity is unavailable")
    return identity
