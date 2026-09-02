"""Fail-closed Redis lease used to serialize knowledge publication."""

from __future__ import annotations

import secrets
from threading import Event, Thread
from typing import Any


MAX_TTL_SECONDS = 15 * 60
RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class PublicationLockError(RuntimeError):
    """Redis did not prove that this operator still owns the lease."""


class PublicationLockedError(PublicationLockError):
    """Another publication holds the serializing lease."""


class PublicationLock:
    def __init__(self, redis_client: Any, key: str, *, ttl_seconds: int = MAX_TTL_SECONDS) -> None:
        if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError("ttl_seconds must be between 1 and 900")
        self._redis = redis_client
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.token = secrets.token_urlsafe(32)
        self._held = False
        self._stop_heartbeat = Event()
        self._heartbeat: Thread | None = None
        self._renewal_error: PublicationLockError | None = None

    def __enter__(self) -> PublicationLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()

    def acquire(self) -> PublicationLock:
        try:
            acquired = self._redis.set(self.key, self.token, nx=True, px=self.ttl_seconds * 1000)
        except Exception as exc:
            raise PublicationLockError("publication lock could not be acquired") from exc
        if not acquired:
            raise PublicationLockedError("publication lock is already held")
        self._held = True
        self._heartbeat = Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat.start()
        return self

    def renew(self) -> None:
        self._compare_and_apply(RENEW_SCRIPT, self.ttl_seconds * 1000, "renew")

    def assert_held(self) -> None:
        """Fail closed before the visibility-changing pointer upsert."""
        if self._renewal_error is not None:
            raise self._renewal_error
        if not self._held:
            raise PublicationLockError("publication lock is not held")

    def release(self) -> None:
        if not self._held:
            return
        self._stop_heartbeat.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1)
        self._compare_and_apply(RELEASE_SCRIPT, None, "release")
        self._held = False

    def _compare_and_apply(self, script: str, ttl_ms: int | None, action: str) -> None:
        args: tuple[object, ...] = (self.token,) if ttl_ms is None else (self.token, ttl_ms)
        try:
            result = self._redis.eval(script, 1, self.key, *args)
        except Exception as exc:
            raise PublicationLockError(f"publication lock {action} could not be verified") from exc
        if result != 1:
            raise PublicationLockError(f"publication lock {action} lost ownership")

    def _heartbeat_loop(self) -> None:
        while not self._stop_heartbeat.wait(self.ttl_seconds / 3):
            try:
                self.renew()
            except PublicationLockError as error:
                self._renewal_error = error
                return
