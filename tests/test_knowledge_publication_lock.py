from __future__ import annotations

import pytest

from metro_agent.knowledge.publication_lock import (
    PublicationLock,
    PublicationLockError,
    PublicationLockedError,
)


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int]] = {}
        self.eval_result: int | None = None
        self.calls: list[tuple[object, ...]] = []

    def set(self, key: str, value: str, *, nx: bool, px: int) -> bool:
        self.calls.append(("set", key, value, nx, px))
        if key in self.values:
            return False
        self.values[key] = (value, px)
        return True

    def eval(self, script: str, count: int, key: str, *args: object) -> int:
        self.calls.append(("eval", script, count, key, *args))
        if self.eval_result is not None:
            return self.eval_result
        token = str(args[0])
        if self.values.get(key, (None, 0))[0] != token:
            return 0
        if "PEXPIRE" in script:
            self.values[key] = (token, int(args[1]))
        else:
            del self.values[key]
        return 1


def test_second_operator_cannot_acquire_publication_lock() -> None:
    redis = _Redis()
    with PublicationLock(redis, "knowledge:publication", ttl_seconds=900):
        with pytest.raises(PublicationLockedError):
            PublicationLock(redis, "knowledge:publication", ttl_seconds=900).acquire()

    assert redis.calls[0][0:2] == ("set", "knowledge:publication")
    assert redis.calls[0][-1] == 900_000


def test_renew_and_release_fail_closed_when_token_cannot_be_compared() -> None:
    redis = _Redis()
    lock = PublicationLock(redis, "knowledge:publication")
    lock.acquire()
    redis.eval_result = 0

    with pytest.raises(PublicationLockError, match="renew"):
        lock.renew()
    with pytest.raises(PublicationLockError, match="release"):
        lock.release()


def test_lock_rejects_a_ttl_longer_than_fifteen_minutes() -> None:
    with pytest.raises(ValueError, match="900"):
        PublicationLock(_Redis(), "knowledge:publication", ttl_seconds=901)
