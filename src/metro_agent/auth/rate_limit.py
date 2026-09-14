from __future__ import annotations

import hashlib
import itertools
import math
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal

from metro_agent.auth.passwords import normalize_username


BucketKey = tuple[str, str]


@dataclass(frozen=True)
class LoginAttemptLease:
    token: str
    sequence: int
    pair_bucket: str
    username_bucket: str
    ip_bucket: str
    started_at: float


@dataclass(frozen=True)
class LoginAttemptBlocked:
    retry_after: int


@dataclass
class _AttemptEvent:
    token: str
    sequence: int
    occurred_at: float
    expires_at: float
    state: Literal["pending", "failed"] = "pending"


class _IndexedExpiryHeap:
    """A bounded min-heap with O(log N) update and removal by bucket key."""

    def __init__(self) -> None:
        self._heap: list[tuple[float, BucketKey]] = []
        self._positions: dict[BucketKey, int] = {}

    def set(self, key: BucketKey, expires_at: float) -> None:
        position = self._positions.get(key)
        if position is None:
            self._positions[key] = len(self._heap)
            self._heap.append((expires_at, key))
            self._sift_up(len(self._heap) - 1)
            return
        previous = self._heap[position]
        self._heap[position] = (expires_at, key)
        if self._heap[position] < previous:
            self._sift_up(position)
        else:
            self._sift_down(position)

    def remove(self, key: BucketKey) -> None:
        position = self._positions.pop(key, None)
        if position is None:
            return
        last = self._heap.pop()
        if position == len(self._heap):
            return
        self._heap[position] = last
        self._positions[last[1]] = position
        parent = (position - 1) // 2
        if position > 0 and self._heap[position] < self._heap[parent]:
            self._sift_up(position)
        else:
            self._sift_down(position)

    def peek(self) -> tuple[float, BucketKey] | None:
        return self._heap[0] if self._heap else None

    def pop_min(self) -> tuple[float, BucketKey] | None:
        minimum = self.peek()
        if minimum is not None:
            self.remove(minimum[1])
        return minimum

    def _sift_up(self, position: int) -> None:
        while position > 0:
            parent = (position - 1) // 2
            if self._heap[parent] <= self._heap[position]:
                break
            self._swap(parent, position)
            position = parent

    def _sift_down(self, position: int) -> None:
        size = len(self._heap)
        while True:
            left = 2 * position + 1
            right = left + 1
            smallest = position
            if left < size and self._heap[left] < self._heap[smallest]:
                smallest = left
            if right < size and self._heap[right] < self._heap[smallest]:
                smallest = right
            if smallest == position:
                return
            self._swap(position, smallest)
            position = smallest

    def _swap(self, first: int, second: int) -> None:
        self._heap[first], self._heap[second] = (
            self._heap[second],
            self._heap[first],
        )
        self._positions[self._heap[first][1]] = first
        self._positions[self._heap[second][1]] = second


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
            ip_max_attempts = max_failures
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
        self._buckets: OrderedDict[BucketKey, deque[_AttemptEvent]] = OrderedDict()
        self._expirations = _IndexedExpiryHeap()
        self._sequences = itertools.count(1)
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
            retry_after = self._retry_after_for(scoped_buckets, now)
            if retry_after:
                return LoginAttemptBlocked(retry_after=retry_after)

            sequence = next(self._sequences)
            lease = LoginAttemptLease(
                token=f"{sequence}:{secrets.token_urlsafe(18)}",
                sequence=sequence,
                pair_bucket=pair_bucket,
                username_bucket=username_bucket,
                ip_bucket=ip_bucket,
                started_at=now,
            )
            event = _AttemptEvent(
                token=lease.token,
                sequence=sequence,
                occurred_at=now,
                expires_at=now + self._window_seconds,
            )
            for scope, bucket in scoped_buckets:
                self._append_event((scope, bucket), event)
            return lease

    def finalize_failure(self, lease: LoginAttemptLease) -> None:
        with self._lock:
            event = self._find_lease_event(lease)
            if event is not None and event.state == "pending":
                event.state = "failed"

    def finalize_success(self, lease: LoginAttemptLease) -> None:
        with self._lock:
            event = self._find_lease_event(lease)
            if event is None or event.state != "pending":
                return
            self._filter_bucket(
                ("pair", lease.pair_bucket),
                lambda event: not self._cleared_by_success(event, lease),
            )
            self._filter_bucket(
                ("username", lease.username_bucket),
                lambda event: not self._cleared_by_success(event, lease),
            )
            self._remove_token(("ip", lease.ip_bucket), lease.token)

    def cancel(self, lease: LoginAttemptLease) -> None:
        with self._lock:
            event = self._find_lease_event(lease)
            if event is None or event.state != "pending":
                return
            self._remove_token(("pair", lease.pair_bucket), lease.token)
            self._remove_token(("username", lease.username_bucket), lease.token)
            self._remove_token(("ip", lease.ip_bucket), lease.token)


    def is_blocked(self, username: object, client_ip: str | None) -> bool:
        return self.retry_after(username, client_ip) > 0

    def record_failure(self, username: object, client_ip: str | None) -> None:
        attempt = self.begin_attempt(username, client_ip)
        if isinstance(attempt, LoginAttemptLease):
            self.finalize_failure(attempt)

    def clear(self, username: object, client_ip: str | None) -> None:
        pair_bucket, username_bucket, _ip_bucket = self._scope_keys(
            username, client_ip
        )
        with self._lock:
            self._filter_bucket(
                ("pair", pair_bucket), lambda event: event.state != "failed"
            )
            self._filter_bucket(
                ("username", username_bucket), lambda event: event.state != "failed"
            )

    def retry_after(self, username: object, client_ip: str | None) -> int:
        pair_bucket, username_bucket, ip_bucket = self._scope_keys(
            username, client_ip
        )
        with self._lock:
            return self._retry_after_for(
                (
                    ("pair", pair_bucket),
                    ("username", username_bucket),
                    ("ip", ip_bucket),
                ),
                self._clock(),
            )

    def failure_count(self, username: object, client_ip: str | None) -> int:
        pair_bucket, _username_bucket, _ip_bucket = self._scope_keys(
            username, client_ip
        )
        with self._lock:
            events = self._current_events(("pair", pair_bucket), self._clock())
            return sum(event.state == "failed" for event in events)

    @property
    def tracked_key_count(self) -> int:
        with self._lock:
            self._expire_due(self._clock())
            return len(self._buckets)

    @property
    def max_events_in_any_key(self) -> int:
        with self._lock:
            return max((len(events) for events in self._buckets.values()), default=0)

    def tracked_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(f"{scope}:{bucket}" for scope, bucket in self._buckets)

    def _retry_after_for(
        self, scoped_buckets: tuple[BucketKey, BucketKey, BucketKey], now: float
    ) -> int:
        self._expire_due(now)
        retry_after = 0
        missing_keys = 0
        for scope, bucket in scoped_buckets:
            key = (scope, bucket)
            events = self._current_events(key, now)
            if not events:
                missing_keys += 1
            elif len(events) >= self._limits[scope]:
                retry_after = max(
                    retry_after,
                    math.ceil(events[0].expires_at - now),
                    1,
                )
        if len(self._buckets) + missing_keys > self._max_tracked_keys:
            retry_after = max(retry_after, self._capacity_retry_after())
        return retry_after

    def _capacity_retry_after(self) -> int:
        return self._window_seconds

    def _expire_due(self, now: float) -> None:
        while True:
            earliest = self._expirations.peek()
            if earliest is None or earliest[0] > now:
                return
            expired = self._expirations.pop_min()
            assert expired is not None
            _expires_at, key = expired
            self._buckets.pop(key, None)

    def _current_events(
        self, key: BucketKey, now: float
    ) -> deque[_AttemptEvent]:
        events = self._buckets.get(key)
        if events is None:
            return deque()
        while events and events[0].expires_at <= now:
            events.popleft()
        if not events:
            self._drop_bucket(key)
            return deque()
        return events

    def _append_event(self, key: BucketKey, event: _AttemptEvent) -> None:
        events = self._buckets.get(key)
        if events is None:
            if len(self._buckets) >= self._max_tracked_keys:
                raise RuntimeError("login limiter capacity reservation failed")
            events = deque()
            self._buckets[key] = events
        limit = self._limits[key[0]]
        if len(events) >= limit:
            raise RuntimeError("login limiter budget reservation failed")
        events.append(event)
        self._expirations.set(key, events[-1].expires_at)

    def _find_lease_event(self, lease: LoginAttemptLease) -> _AttemptEvent | None:
        for key in (
            ("pair", lease.pair_bucket),
            ("username", lease.username_bucket),
            ("ip", lease.ip_bucket),
        ):
            events = self._buckets.get(key)
            if events is None:
                continue
            for event in events:
                if event.token == lease.token and event.sequence == lease.sequence:
                    return event
        return None

    @staticmethod
    def _cleared_by_success(
        event: _AttemptEvent, success: LoginAttemptLease
    ) -> bool:
        if event.token == success.token and event.sequence == success.sequence:
            return True
        return event.state == "failed" and event.sequence < success.sequence

    def _remove_token(self, key: BucketKey, token: str) -> None:
        self._filter_bucket(key, lambda event: event.token != token)

    def _filter_bucket(
        self, key: BucketKey, keep: Callable[[_AttemptEvent], bool]
    ) -> None:
        events = self._buckets.get(key)
        if events is None:
            return
        retained = deque(event for event in events if keep(event))
        if not retained:
            self._drop_bucket(key)
            return
        self._buckets[key] = retained
        self._expirations.set(key, retained[-1].expires_at)

    def _drop_bucket(self, key: BucketKey) -> None:
        self._buckets.pop(key, None)
        self._expirations.remove(key)

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
