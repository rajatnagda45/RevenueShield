"""Shared resilience primitives: exponential backoff with jitter and a circuit breaker.

Used for every external dependency (Razorpay, Twilio, SMTP, the LLM) and by the job worker.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional, Tuple, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def backoff_delay(attempt: int, *, base_seconds: float = 0.5, max_seconds: float = 30.0, jitter: bool = True) -> float:
    """Delay before retry number `attempt` (1-based): base * 2^(attempt-1), capped, with full jitter."""
    raw = min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))
    return random.uniform(0, raw) if jitter else raw


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    retries: int = 3,
    base_seconds: float = 0.5,
    max_seconds: float = 30.0,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
    should_retry: Optional[Callable[[BaseException], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    jitter: bool = True,
) -> T:
    """Call `fn` up to `retries + 1` times. Re-raises the last exception when exhausted."""
    attempt = 0
    while True:
        try:
            return fn()
        except retry_on as exc:  # type: ignore[misc]
            attempt += 1
            if attempt > retries or (should_retry is not None and not should_retry(exc)):
                raise
            delay = backoff_delay(attempt, base_seconds=base_seconds, max_seconds=max_seconds, jitter=jitter)
            if on_retry:
                on_retry(attempt, exc, delay)
            else:
                logger.warning(f"[RETRY] attempt {attempt}/{retries} failed with {type(exc).__name__}: {exc}; sleeping {delay:.2f}s")
            sleep(delay)


class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures; half-opens after `cooldown_seconds`."""

    def __init__(self, cooldown_seconds: int = 60, failure_threshold: int = 1, clock: Callable[[], float] = time.monotonic):
        self.cooldown = cooldown_seconds
        self.failure_threshold = failure_threshold
        self._clock = clock
        self._opened_at: Optional[float] = None
        self.failures = 0

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if self._clock() - self._opened_at >= self.cooldown:
            return False  # half-open: let one call through
        return True

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "open" if self.is_open else "half_open"

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self._opened_at = self._clock()

    def record_success(self) -> None:
        self.failures = 0
        self._opened_at = None

    def reset(self) -> None:
        self.record_success()

    def call(self, fn: Callable[[], T], *, on_open: Optional[Callable[[], T]] = None) -> T:
        if self.is_open:
            if on_open is not None:
                return on_open()
            raise RuntimeError("circuit breaker is open")
        try:
            result = fn()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result
