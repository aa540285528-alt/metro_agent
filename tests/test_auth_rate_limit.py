from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_five_failures_are_allowed_and_sixth_request_is_blocked() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()

    for _ in range(5):
        assert limiter.is_blocked("operator", "10.0.0.1") is False
        limiter.record_failure("operator", "10.0.0.1")

    assert limiter.is_blocked("operator", "10.0.0.1") is True
    assert limiter.retry_after("operator", "10.0.0.1") == 60


def test_window_expiry_allows_another_attempt() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(5):
        limiter.record_failure("operator", "10.0.0.1")

    clock.advance(60)

    assert limiter.is_blocked("operator", "10.0.0.1") is False
    assert limiter.tracked_key_count == 0


def test_success_clears_only_the_matching_bucket() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    for _ in range(4):
        limiter.record_failure(" operator ", "10.0.0.1")
        limiter.record_failure("other", "10.0.0.1")

    limiter.clear("OPERATOR", "10.0.0.1")

    assert limiter.failure_count("operator", "10.0.0.1") == 0
    assert limiter.failure_count("other", "10.0.0.1") == 4


def test_username_and_ip_are_independent_buckets() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    for _ in range(5):
        limiter.record_failure("operator", "10.0.0.1")

    assert limiter.is_blocked("other", "10.0.0.1") is False
    assert limiter.is_blocked("operator", "10.0.0.2") is False


def test_missing_and_empty_client_ip_share_stable_unknown_bucket() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    for _ in range(4):
        limiter.record_failure("operator", None)
    limiter.record_failure("operator", "")

    assert limiter.failure_count("operator", None) == 5
    assert limiter.failure_count("operator", "") == 5
    assert limiter.is_blocked("operator", None) is True
    assert limiter.is_blocked("operator", "") is True


def test_concurrent_failure_counting_is_thread_safe() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter(max_failures=1000)
    with ThreadPoolExecutor(max_workers=16) as executor:
        list(
            executor.map(
                lambda _index: limiter.record_failure("operator", "10.0.0.1"),
                range(500),
            )
        )

    assert limiter.failure_count("operator", "10.0.0.1") == 500


def test_invalid_username_is_stored_only_as_a_bounded_digest() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    sensitive = "secret-value-" * 1000
    limiter = LoginRateLimiter()

    limiter.record_failure(sensitive, "unknown")

    keys = limiter.tracked_keys()
    assert len(keys) == 1
    assert sensitive not in keys[0]
    assert len(keys[0]) < 160


def test_expired_buckets_are_opportunistically_removed() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for index in range(20):
        limiter.record_failure(f"user{index}", "10.0.0.1")
    assert limiter.tracked_key_count == 20

    clock.advance(60)
    limiter.record_failure("current", "10.0.0.1")

    assert limiter.tracked_key_count == 1
