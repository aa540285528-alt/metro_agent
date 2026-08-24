from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from collections.abc import Callable
from threading import Lock

from metro_agent.auth.passwords import normalize_username


class LoginRateLimiter:
    """Single-process pilot limiter; multi-replica deployments must use Redis."""

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_failures <= 0 or window_seconds <= 0:
            raise ValueError("rate limit values must be positive")
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = Lock()

    def is_blocked(self, username: object, client_ip: str | None) -> bool:
        key = self._key(username, client_ip)
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            return len(self._failures.get(key, ())) >= self._max_failures

    def record_failure(self, username: object, client_ip: str | None) -> None:
        key = self._key(username, client_ip)
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            self._failures.setdefault(key, deque()).append(now)

    def clear(self, username: object, client_ip: str | None) -> None:
        key = self._key(username, client_ip)
        with self._lock:
            self._failures.pop(key, None)

    def retry_after(self, username: object, client_ip: str | None) -> int:
        key = self._key(username, client_ip)
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            failures = self._failures.get(key)
            if failures is None or len(failures) < self._max_failures:
                return 0
            return max(1, math.ceil(failures[0] + self._window_seconds - now))

    def failure_count(self, username: object, client_ip: str | None) -> int:
        key = self._key(username, client_ip)
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            return len(self._failures.get(key, ()))

    @property
    def tracked_key_count(self) -> int:
        with self._lock:
            self._cleanup(self._clock())
            return len(self._failures)

    def tracked_keys(self) -> tuple[str, ...]:
        with self._lock:
            self._cleanup(self._clock())
            return tuple(self._failures)

    def _cleanup(self, now: float) -> None:
        cutoff = now - self._window_seconds
        empty_keys: list[str] = []
        for key, failures in self._failures.items():
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                empty_keys.append(key)
        for key in empty_keys:
            del self._failures[key]

    @staticmethod
    def _key(username: object, client_ip: str | None) -> str:
        try:
            username_key = normalize_username(username)  # type: ignore[arg-type]
        except ValueError:
            raw_username = str(username).strip().lower().encode()
            username_key = "invalid:" + hashlib.sha256(raw_username).hexdigest()
        ip_value = client_ip if client_ip else "<unknown>"
        ip_digest = hashlib.sha256(str(ip_value).encode()).hexdigest()
        return f"{username_key}:{ip_digest}"
