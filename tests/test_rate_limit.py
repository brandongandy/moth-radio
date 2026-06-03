import pytest

from scrobble_recs.apis.rate_limit import RateLimiter


def test_rate_limiter_waits_until_next_allowed_request() -> None:
    times = iter([10.0, 10.1])
    slept: list[float] = []
    limiter = RateLimiter(
        rate_limit_per_second=2.0,
        monotonic=lambda: next(times),
        sleep=slept.append,
    )

    limiter.wait()
    limiter.wait()

    assert slept == pytest.approx([0.4])


def test_rate_limiter_does_not_wait_when_interval_has_elapsed() -> None:
    times = iter([10.0, 10.6])
    slept: list[float] = []
    limiter = RateLimiter(
        rate_limit_per_second=2.0,
        monotonic=lambda: next(times),
        sleep=slept.append,
    )

    limiter.wait()
    limiter.wait()

    assert slept == []


def test_rate_limiter_tracks_next_allowed_time_after_sleep() -> None:
    times = iter([10.0, 10.1, 10.7])
    slept: list[float] = []
    limiter = RateLimiter(
        rate_limit_per_second=2.0,
        monotonic=lambda: next(times),
        sleep=slept.append,
    )

    limiter.wait()
    limiter.wait()
    limiter.wait()

    assert slept == pytest.approx([0.4, 0.3])


@pytest.mark.parametrize(
    "rate_limit_per_second",
    [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
)
def test_rate_limiter_rejects_non_positive_and_non_finite_rates(
    rate_limit_per_second: float,
) -> None:
    with pytest.raises(ValueError, match="rate_limit_per_second"):
        RateLimiter(rate_limit_per_second=rate_limit_per_second)
