import pytest

from src.retry import RetryDecision, retry_with_backoff


class Transient(Exception):
    pass


def flaky(failures: int, result: str = "ok"):
    state = {"calls": 0}

    def operation() -> str:
        state["calls"] += 1
        if state["calls"] <= failures:
            raise Transient(f"failure {state['calls']}")
        return result

    return operation, state


def always_retry(_: Exception) -> RetryDecision:
    return RetryDecision(True)


def test_returns_immediately_on_success():
    sleeps: list[float] = []
    operation, state = flaky(0)

    result = retry_with_backoff(
        operation, decide=always_retry, max_retries=3, sleep=sleeps.append
    )

    assert result == "ok"
    assert state["calls"] == 1
    assert sleeps == []


def test_exponential_backoff_without_jitter_noise():
    sleeps: list[float] = []
    operation, state = flaky(3)

    retry_with_backoff(
        operation,
        decide=always_retry,
        max_retries=3,
        sleep=sleeps.append,
        rand=lambda: 0.5,  # centre of the jitter range -> exactly the base delay
    )

    assert sleeps == [1.0, 2.0, 4.0]
    assert state["calls"] == 4


def test_delay_is_capped():
    sleeps: list[float] = []
    operation, _ = flaky(6)

    retry_with_backoff(
        operation,
        decide=always_retry,
        max_retries=6,
        cap_s=30.0,
        sleep=sleeps.append,
        rand=lambda: 0.5,
    )

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


@pytest.mark.parametrize(("rand_value", "expected"), [(0.0, 0.8), (1.0, 1.2)])
def test_jitter_stays_within_plus_minus_twenty_percent(rand_value, expected):
    sleeps: list[float] = []
    operation, _ = flaky(1)

    retry_with_backoff(
        operation,
        decide=always_retry,
        max_retries=1,
        sleep=sleeps.append,
        rand=lambda: rand_value,
    )

    assert sleeps == [pytest.approx(expected)]


def test_retry_after_replaces_computed_delay_and_is_capped():
    sleeps: list[float] = []
    operation, _ = flaky(2)
    hints = iter([7.0, 500.0])

    retry_with_backoff(
        operation,
        decide=lambda _: RetryDecision(True, next(hints)),
        max_retries=2,
        sleep=sleeps.append,
    )

    assert sleeps == [7.0, 60.0]


def test_non_retryable_error_is_raised_immediately():
    sleeps: list[float] = []
    operation, state = flaky(5)

    with pytest.raises(Transient, match="failure 1"):
        retry_with_backoff(
            operation,
            decide=lambda _: RetryDecision(False),
            max_retries=3,
            sleep=sleeps.append,
        )

    assert state["calls"] == 1
    assert sleeps == []


def test_exhaustion_reraises_the_last_exception_unchanged():
    operation, state = flaky(10)

    with pytest.raises(Transient, match="failure 3"):
        retry_with_backoff(
            operation, decide=always_retry, max_retries=2, sleep=lambda _: None
        )

    assert state["calls"] == 3


def test_zero_retries_means_a_single_attempt():
    operation, state = flaky(10)

    with pytest.raises(Transient):
        retry_with_backoff(
            operation, decide=always_retry, max_retries=0, sleep=lambda _: None
        )

    assert state["calls"] == 1
