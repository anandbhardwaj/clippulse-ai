import random
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retryable: bool
    retry_after_s: float | None = None


def retry_with_backoff[T](
    operation: Callable[[], T],
    *,
    decide: Callable[[Exception], RetryDecision],
    max_retries: int,
    base_delay_s: float = 1.0,
    factor: float = 2.0,
    cap_s: float = 30.0,
    jitter: float = 0.2,
    retry_after_cap_s: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Run ``operation``, retrying transient failures with exponential backoff.

    ``decide`` classifies each exception. Retries stop after ``max_retries``
    extra attempts or on the first non-retryable failure; the last exception is
    re-raised unchanged so callers can map it to their own typed error. A server
    supplied ``retry_after_s`` replaces the computed delay (capped at
    ``retry_after_cap_s``); computed delays get +/- ``jitter`` and cap at ``cap_s``.
    """
    attempt = 0
    while True:
        try:
            return operation()
        except Exception as exc:
            decision = decide(exc)
            if not decision.retryable or attempt >= max_retries:
                raise
            if decision.retry_after_s is not None:
                delay = min(max(decision.retry_after_s, 0.0), retry_after_cap_s)
            else:
                delay = min(base_delay_s * factor**attempt, cap_s)
                delay *= 1 + jitter * (2 * rand() - 1)
            sleep(delay)
            attempt += 1
