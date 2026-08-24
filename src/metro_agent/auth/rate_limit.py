from __future__ import annotations

import hashlib
import itertools
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from metro_agent.auth.passwords import normalize_username


@dataclass(frozen=True)
class LoginAttemptLease:
    lease_id: int
    pair_bucket: str
    username_bucket: str
    ip_bucket: str
    started_at: float


@dataclass(frozen=True)
class LoginAttemptBlocked:
    retry_after: int


@dataclass(frozen=True)
class _AttemptEvent:
    lease_id: int
    occurred_at: float


class LoginRateLimiter:
    """Single-process pilot limiter; multi-replica deployments must use Redis."""

    def __init__(
        self,
        *,
        pair_max_attempts: int = 5,
        username_max_attempts: int = 5,
        ip_max_attempts: int = 25,
        window_seconds: int = 60,
        max_tracked_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
        max_failures: int | None = None,
    ) -> None:
        if max_failures is not None:
            pair_max_attempts = max_failures
            username_max_attempts = max_failures
        limits = (pair_max_attempts, username_max_attempts, ip_max_attempts)
        if any(limit <= 0 for limit in limits) or window_seconds <= 0:
            raise ValueError("rate limit values must be positive")
        if max_tracked_keys < 3:
            raise ValueError("max_tracked_keys must be at least 3")
        self._limits = {
            "pair": pair_max_attempts,
            "username": username_max_attempts,
            "ip": ip_max_attempts,
        }
        self._window_seconds = window_seconds
        self._max_tracked_keys = max_tracked_keys
        self._clock = clock
        self._buckets: OrderedDict[tuple[str, str], deque[_AttemptEvent]] = (
            OrderedDict()
        )
        self._lease_ids = itertools.count(1)
        self._lock = Lock()

    def begin_attempt(
        self, username: object, client_ip: str | None
    ) -> LoginAttemptLease | LoginAttemptBlocked:
        pair_bucket, username_bucket, ip_bucket = self._scope_keys(
            username, client_ip
        )
        scoped_buckets = (
            ("pair", pair_bucket),
            ("username", username_bucket),
            ("ip", ip_bucket),
        )
        with self._lock:
            now = self._clock()
            blocked_retry_after = 0
            for scope, bucket in scoped_buckets:
                events = self._current_events((scope, bucket), now)
                if len(events) >= self._limits[scope]:
                    retry_after = math.ceil(
                        events[0].occurred_at + self._window_seconds - now
                    )
                    blocked_retry_after = max(blocked_retry_after, retry_after, 1)
            if blocked_retry_after:
                return LoginAttemptBlocked(retry_after=blocked_retry_after)

            lease = LoginAttemptLease(
                lease_id=next(self._lease_ids),
                pair_bucket=pair_bucket,
                username_bucket=username_bucket,
                ip_bucket=ip_bucket,
                started_at=now,
            )
            event = _AttemptEvent(lease.lease_id, now)
            for scope, bucket in scoped_buckets:
                self._append_event((scope, bucket), event, self._limits[scope])
            return lease

    def finalize_failure(self, _lease: LoginAttemptLease) -> None:
        # The reservation already consumes all three budgets.
        return None

    def finalize_success(self, lease: LoginAttemptLease) -> None:
        with self._lock:
            self._buckets.pop(("pair", lease.pair_bucket), None)
            self._buckets.pop(("username", lease.username_bucket), None)
            self._remove_event(("ip", lease.ip_bucket), lease.lease_id)

    def cancel(self, lease: LoginAttemptLease) -> None:
        with self._lock:
            self._remove_event(("pair", lease.pair_bucket), lease.lease_id)
            self._remove_event(("username", lease.username_bucket), lease.lease_id)
            self._remove_event(("ip", lease.ip_bucket), lease.lease_id)

    # Compatibility helpers for direct callers; HTTP login uses the lease API above.
    def is_blocked(self, username: object, client_ip: str | None) -> bool:
        return self.retry_after(username, client_ip) > 0

    def record_failure(self, username: object, client_ip: str | None) -> None:
        pair_bucket, username_bucket, ip_bucket = self._scope_keys(
            username, client_ip
        )
        with self._lock:
            now = self._clock()
            event = _AttemptEvent(next(self._lease_ids), now)
            for scope, bucket in (
                ("pair", pair_bucket),
                ("username", username_bucket),
                ("ip", ip_bucket),
            ):
                self._current_events((scope, bucket), now)
                self._append_event((scope, bucket), event, self._limits[scope])

    def clear(self, username: object, client_ip: str | None) -> None:
        pair_bucket, username_bucket, _ip_bucket = self._scope_keys(
            username, client_ip
        )
        with self._lock:
            self._buckets.pop(("pair", pair_bucket), None)
            self._buckets.pop(("username", username_bucket), None)

    def retry_after(self, username: object, client_ip: str | None) -> int:
        pair_bucket, username_bucket, ip_bucket = self._scope_keys(
            username, client_ip
        )
        with self._lock:
            now = self._clock()
            retry_after = 0
            for scope, bucket in (
                ("pair", pair_bucket),
                ("username", username_bucket),
                ("ip", ip_bucket),
            ):
                events = self._current_events((scope, bucket), now)
                if len(events) >= self._limits[scope]:
                    retry_after = max(
                        retry_after,
                        math.ceil(
                            events[0].occurred_at + self._window_seconds - now
                        ),
                        1,
                    )
            return retry_after

    def failure_count(self, username: object, client_ip: str | None) -> int:
        pair_bucket, _username_bucket, _ip_bucket = self._scope_keys(
            username, client_ip
        )
        with self._lock:
            return len(self._current_events(("pair", pair_bucket), self._clock()))

    @property
    def tracked_key_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    @property
    def max_events_in_any_key(self) -> int:
        with self._lock:
            return max((len(events) for events in self._buckets.values()), default=0)

    def tracked_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(f"{scope}:{bucket}" for scope, bucket in self._buckets)

    def _current_events(
        self, key: tuple[str, str], now: float
    ) -> deque[_AttemptEvent]:
        events = self._buckets.get(key)
        if events is None:
            return deque()
        cutoff = now - self._window_seconds
        while events and events[0].occurred_at <= cutoff:
            events.popleft()
        if not events:
            del self._buckets[key]
            return deque()
        self._buckets.move_to_end(key)
        return events

    def _append_event(
        self,
        key: tuple[str, str],
        event: _AttemptEvent,
        event_limit: int,
    ) -> None:
        events = self._buckets.get(key)
        if events is None:
            events = deque()
            self._buckets[key] = events
        while len(events) >= event_limit:
            events.popleft()
        events.append(event)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_tracked_keys:
            self._buckets.popitem(last=False)

    def _remove_event(self, key: tuple[str, str], lease_id: int) -> None:
        events = self._buckets.get(key)
        if events is None:
            return
        retained = deque(event for event in events if event.lease_id != lease_id)
        if retained:
            self._buckets[key] = retained
            self._buckets.move_to_end(key)
        else:
            del self._buckets[key]

    @classmethod
    def _scope_keys(
        cls, username: object, client_ip: str | None
    ) -> tuple[str, str, str]:
        username_key = cls._username_key(username)
        ip_value = client_ip if client_ip else "<unknown>"
        ip_key = hashlib.sha256(str(ip_value).encode()).hexdigest()
        return f"{username_key}:{ip_key}", username_key, ip_key

    @staticmethod
    def _username_key(username: object) -> str:
        try:
            return normalize_username(username)  # type: ignore[arg-type]
        except ValueError:
            raw_username = str(username).strip().lower().encode()
            return "invalid:" + hashlib.sha256(raw_username).hexdigest()
