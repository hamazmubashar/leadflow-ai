"""The per-address login rate limiter (M11), with a fake clock."""

import pytest

from leadflow_ai.api.login_limit import (
    LOGIN_ATTEMPTS_PER_CLIENT,
    LOGIN_WINDOW_SECONDS,
    MAX_TRACKED_CLIENTS,
    LoginRateLimitedError,
    LoginRateLimiter,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def limiter(clock: Clock, **options: int) -> LoginRateLimiter:
    settings = {"max_attempts": 3, "window_seconds": 60, "max_clients": 100} | options
    return LoginRateLimiter(clock=clock, **settings)


def test_defaults() -> None:
    assert (LOGIN_ATTEMPTS_PER_CLIENT, LOGIN_WINDOW_SECONDS, MAX_TRACKED_CLIENTS) == (
        10,
        900,
        10_000,
    )
    default = LoginRateLimiter()
    for _ in range(LOGIN_ATTEMPTS_PER_CLIENT):
        default.reserve("a")
    with pytest.raises(LoginRateLimitedError):
        default.reserve("a")


def test_attempts_past_the_limit_are_refused_with_a_retry_time(clock: Clock) -> None:
    limit = limiter(clock)
    limit.reserve("a")
    clock.now += 10.2
    limit.reserve("a")
    limit.reserve("a")
    with pytest.raises(LoginRateLimitedError) as refused:
        limit.reserve("a")
    assert refused.value.retry_after_seconds == 50  # the first attempt leaves the window
    limit.reserve("b")  # other addresses are unaffected


def test_the_window_slides(clock: Clock) -> None:
    limit = limiter(clock)
    for _ in range(3):
        limit.reserve("a")
    clock.now += 60  # exactly one window later, the old attempts no longer count
    for _ in range(3):
        limit.reserve("a")


def test_retry_after_is_at_least_one_second(clock: Clock) -> None:
    limit = limiter(clock)
    for _ in range(3):
        limit.reserve("a")
    clock.now += 59.9999
    with pytest.raises(LoginRateLimitedError) as refused:
        limit.reserve("a")
    assert refused.value.retry_after_seconds == 1


def test_a_refused_attempt_does_not_extend_the_block(clock: Clock) -> None:
    limit = limiter(clock)
    for _ in range(3):
        limit.reserve("a")
    for _ in range(5):
        with pytest.raises(LoginRateLimitedError):
            limit.reserve("a")
    clock.now += 60
    limit.reserve("a")


def test_released_attempts_free_their_slot(clock: Clock) -> None:
    limit = limiter(clock)
    stamps = [limit.reserve("a") for _ in range(3)]
    limit.release("a", stamps[1])
    limit.reserve("a")
    with pytest.raises(LoginRateLimitedError):
        limit.reserve("a")


def test_releasing_unknown_attempts_is_harmless(clock: Clock) -> None:
    limit = limiter(clock)
    limit.release("nobody", 1.0)
    stamp = limit.reserve("a")
    limit.release("a", stamp + 1)  # not a stamp of "a"
    limit.reserve("a")
    limit.reserve("a")
    with pytest.raises(LoginRateLimitedError):
        limit.reserve("a")


def test_idle_addresses_are_forgotten_first_when_full(clock: Clock) -> None:
    limit = limiter(clock, max_clients=3)
    for _ in range(3):
        limit.reserve("blocked")
    limit.reserve("idle-1")
    clock.now += 61  # "blocked" and "idle-1" are now outside the window
    limit.reserve("recent")
    limit.reserve("new")  # full: expired addresses are dropped, nothing recent is
    assert limit.tracked_clients() == 2
    limit.reserve("blocked")  # starts afresh


def test_when_full_of_active_addresses_the_least_recent_goes(clock: Clock) -> None:
    limit = limiter(clock, max_clients=2)
    for _ in range(3):
        limit.reserve("first")
    limit.reserve("second")
    for _ in range(2):
        with pytest.raises(LoginRateLimitedError):
            limit.reserve("first")  # seen again: now more recent than "second"
    limit.reserve("third")  # evicts "second", the least recently seen
    assert limit.tracked_clients() == 2
    with pytest.raises(LoginRateLimitedError):
        limit.reserve("first")  # still tracked and still blocked
