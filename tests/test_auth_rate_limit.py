from __future__ import annotations

from collections import OrderedDict
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


def test_username_budget_spans_ips_while_other_username_remains_available() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    for _ in range(5):
        limiter.record_failure("operator", "10.0.0.1")

    assert limiter.is_blocked("other", "10.0.0.1") is False
    assert limiter.is_blocked("operator", "10.0.0.2") is True


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
    assert len(keys) == 3
    assert all(sensitive not in key for key in keys)
    assert all(len(key) < 160 for key in keys)


def test_expired_active_keys_are_reclaimed_by_expiry_index() -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for index in range(20):
        limiter.record_failure(f"user{index}", "10.0.0.1")
    assert limiter.tracked_key_count == 41

    clock.advance(60)
    limiter.record_failure("current", "10.0.0.1")

    assert limiter.failure_count("current", "10.0.0.1") == 1
    assert limiter.tracked_key_count == 3


def test_begin_attempt_atomically_blocks_the_sixth_concurrent_attempt() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptBlocked, LoginAttemptLease
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    with ThreadPoolExecutor(max_workers=6) as executor:
        decisions = list(
            executor.map(
                lambda _index: limiter.begin_attempt("operator", "10.0.0.1"),
                range(6),
            )
        )

    leases = [decision for decision in decisions if isinstance(decision, LoginAttemptLease)]
    blocked = [
        decision for decision in decisions if isinstance(decision, LoginAttemptBlocked)
    ]
    assert len(leases) == 5
    assert len(blocked) == 1
    assert blocked[0].retry_after > 0


def test_cancel_removes_reserved_attempt_from_every_budget() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptLease, LoginRateLimiter

    limiter = LoginRateLimiter()
    lease = limiter.begin_attempt("operator", "10.0.0.1")
    assert isinstance(lease, LoginAttemptLease)

    limiter.cancel(lease)

    assert limiter.failure_count("operator", "10.0.0.1") == 0
    assert isinstance(
        limiter.begin_attempt("operator", "10.0.0.1"), LoginAttemptLease
    )


def test_username_budget_blocks_rotating_client_ips() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptBlocked, LoginAttemptLease
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    for index in range(5):
        lease = limiter.begin_attempt("operator", f"10.0.0.{index}")
        assert isinstance(lease, LoginAttemptLease)
        limiter.finalize_failure(lease)

    decision = limiter.begin_attempt("operator", "10.0.1.1")

    assert isinstance(decision, LoginAttemptBlocked)
    assert decision.retry_after > 0


def test_ip_budget_blocks_rotating_usernames() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptBlocked, LoginAttemptLease
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    for index in range(25):
        lease = limiter.begin_attempt(f"user{index}", "10.0.0.1")
        assert isinstance(lease, LoginAttemptLease)
        limiter.finalize_failure(lease)

    decision = limiter.begin_attempt("user25", "10.0.0.1")

    assert isinstance(decision, LoginAttemptBlocked)
    assert decision.retry_after > 0


def test_success_clears_username_and_pair_budgets_but_keeps_other_ip_failures() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptLease, LoginRateLimiter

    limiter = LoginRateLimiter()
    for _ in range(4):
        lease = limiter.begin_attempt("operator", "10.0.0.1")
        assert isinstance(lease, LoginAttemptLease)
        limiter.finalize_failure(lease)
    success = limiter.begin_attempt("operator", "10.0.0.1")
    assert isinstance(success, LoginAttemptLease)

    limiter.finalize_success(success)

    assert limiter.failure_count("operator", "10.0.0.1") == 0
    for _ in range(5):
        lease = limiter.begin_attempt("operator", "10.0.0.1")
        assert isinstance(lease, LoginAttemptLease)
        limiter.finalize_failure(lease)


def test_success_preserves_older_pending_attempts_that_fail_later() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptBlocked, LoginAttemptLease
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    wrong_leases = []
    for _ in range(4):
        lease = limiter.begin_attempt("operator", "10.0.0.1")
        assert isinstance(lease, LoginAttemptLease)
        wrong_leases.append(lease)
    success = limiter.begin_attempt("operator", "10.0.0.1")
    assert isinstance(success, LoginAttemptLease)

    limiter.finalize_success(success)
    for lease in wrong_leases:
        limiter.finalize_failure(lease)

    assert limiter.failure_count("operator", "10.0.0.1") == 4
    fifth = limiter.begin_attempt("operator", "10.0.0.1")
    assert isinstance(fifth, LoginAttemptLease)
    limiter.finalize_failure(fifth)
    assert isinstance(
        limiter.begin_attempt("operator", "10.0.0.1"), LoginAttemptBlocked
    )


def test_finalizing_an_old_token_does_not_change_rebuilt_bucket_event() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptLease, LoginRateLimiter

    limiter = LoginRateLimiter()
    old = limiter.begin_attempt("operator", "10.0.0.1")
    assert isinstance(old, LoginAttemptLease)
    limiter.cancel(old)
    current = limiter.begin_attempt("operator", "10.0.0.1")
    assert isinstance(current, LoginAttemptLease)

    limiter.finalize_failure(old)
    limiter.finalize_success(old)

    pair_events = limiter._buckets[("pair", current.pair_bucket)]
    assert len(pair_events) == 1
    assert pair_events[0].token == current.token
    assert pair_events[0].state == "pending"


def test_failed_lease_cannot_be_reclassified_by_late_success_or_cancel() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptLease, LoginRateLimiter

    limiter = LoginRateLimiter()
    failed = limiter.begin_attempt("operator", "10.0.0.1")
    assert isinstance(failed, LoginAttemptLease)
    limiter.finalize_failure(failed)

    limiter.finalize_success(failed)
    limiter.cancel(failed)

    assert limiter.failure_count("operator", "10.0.0.1") == 1


def test_active_capacity_and_event_counts_remain_bounded_under_username_flood() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptLease, LoginRateLimiter

    limiter = LoginRateLimiter(max_tracked_keys=128)
    for index in range(3000):
        lease = limiter.begin_attempt(f"user{index}", f"10.0.{index // 256}.{index % 256}")
        if isinstance(lease, LoginAttemptLease):
            limiter.finalize_failure(lease)

    assert limiter.tracked_key_count <= 128
    assert limiter.max_events_in_any_key <= 25
    assert len(limiter._expirations._heap) == limiter.tracked_key_count


def test_active_victim_remains_blocked_during_new_key_churn() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptBlocked, LoginAttemptLease
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter(max_tracked_keys=9)
    for _ in range(5):
        lease = limiter.begin_attempt("victim", "10.0.0.1")
        assert isinstance(lease, LoginAttemptLease)
        limiter.finalize_failure(lease)

    for index in range(1000):
        lease = limiter.begin_attempt(f"churn{index}", f"10.1.0.{index}")
        if isinstance(lease, LoginAttemptLease):
            limiter.finalize_failure(lease)

    assert isinstance(
        limiter.begin_attempt("victim", "10.0.0.1"), LoginAttemptBlocked
    )
    assert limiter.tracked_key_count <= 9


def test_full_active_capacity_rejects_new_key_without_eviction() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptBlocked, LoginAttemptLease
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter(max_tracked_keys=3)
    victim = limiter.begin_attempt("victim", "10.0.0.1")
    assert isinstance(victim, LoginAttemptLease)
    limiter.finalize_failure(victim)

    blocked = limiter.begin_attempt("new.user", "10.0.0.2")

    assert isinstance(blocked, LoginAttemptBlocked)
    assert blocked.retry_after == 60
    assert limiter.failure_count("victim", "10.0.0.1") == 1
    assert limiter.tracked_key_count == 3


def test_expired_capacity_is_reclaimed_for_new_key() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptBlocked, LoginAttemptLease
    from metro_agent.auth.rate_limit import LoginRateLimiter

    clock = FakeClock()
    limiter = LoginRateLimiter(max_tracked_keys=3, clock=clock)
    victim = limiter.begin_attempt("victim", "10.0.0.1")
    assert isinstance(victim, LoginAttemptLease)
    limiter.finalize_failure(victim)
    assert isinstance(
        limiter.begin_attempt("new.user", "10.0.0.2"), LoginAttemptBlocked
    )

    clock.advance(60)
    recovered = limiter.begin_attempt("new.user", "10.0.0.2")

    assert isinstance(recovered, LoginAttemptLease)
    assert limiter.tracked_key_count == 3


def test_capacity_retry_after_covers_staggered_scope_expirations() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptBlocked, LoginAttemptLease
    from metro_agent.auth.rate_limit import LoginRateLimiter

    clock = FakeClock()
    limiter = LoginRateLimiter(max_tracked_keys=5, clock=clock)
    first = limiter.begin_attempt("victim", "10.0.0.1")
    assert isinstance(first, LoginAttemptLease)
    limiter.finalize_failure(first)

    clock.advance(10)
    second = limiter.begin_attempt("victim", "10.0.0.2")
    assert isinstance(second, LoginAttemptLease)
    limiter.finalize_failure(second)

    clock.advance(10)
    blocked = limiter.begin_attempt("new.user", "10.0.0.3")

    assert isinstance(blocked, LoginAttemptBlocked)
    assert blocked.retry_after == 60

    clock.advance(blocked.retry_after)
    recovered = limiter.begin_attempt("new.user", "10.0.0.3")

    assert isinstance(recovered, LoginAttemptLease)


class NoFullScanOrderedDict(OrderedDict):
    def __iter__(self):
        raise AssertionError("request path must not scan all limiter keys")

    def items(self):
        raise AssertionError("request path must not scan all limiter keys")

    def values(self):
        raise AssertionError("request path must not scan all limiter keys")


def test_attempt_operations_do_not_scan_the_full_lru() -> None:
    from metro_agent.auth.rate_limit import LoginAttemptLease, LoginRateLimiter

    limiter = LoginRateLimiter(max_tracked_keys=64)
    limiter._buckets = NoFullScanOrderedDict(limiter._buckets)

    for index in range(100):
        lease = limiter.begin_attempt(f"user{index}", f"10.0.0.{index}")
        if isinstance(lease, LoginAttemptLease):
            limiter.finalize_failure(lease)
