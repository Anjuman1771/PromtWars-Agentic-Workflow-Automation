"""FlowPilot AI Reusable Asynchronous Circuit Breaker.

Provides stateful protection against cascading failures across external tools,
services, and network endpoints with thread/coroutine-safe concurrency locks.

States:
- CLOSED: Normal operation. Failures increment counter until threshold.
- OPEN: Tripped. Requests rejected immediately with CircuitBreakerOpenException.
- HALF_OPEN: Recovery period elapsed. Limited trial requests allowed to probe health.
"""

import asyncio
from enum import Enum
import time
from typing import Any, Callable, Coroutine, Dict, Optional

from ..config import settings
from ..observability.logger import get_logger
from .exceptions import CircuitBreakerOpenException

logger = get_logger("flowpilot.safety.circuit_breaker")


class CircuitBreakerState(str, Enum):
    """Lifecycle states of the circuit breaker."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Asynchronous circuit breaker enforcing failure thresholds and recovery timeouts."""

    def __init__(
        self,
        name: str = "default",
        failure_threshold: Optional[int] = None,
        recovery_timeout: Optional[float] = None,
        half_open_max_trials: Optional[int] = None,
    ):
        self.name = name
        self.failure_threshold = (
            failure_threshold or settings.circuit_breaker_failure_threshold
        )
        self.recovery_timeout = (
            recovery_timeout or settings.circuit_breaker_recovery_timeout_seconds
        )
        self.half_open_max_trials = (
            half_open_max_trials or settings.circuit_breaker_half_open_max_trials
        )

        self._state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self._failure_count: int = 0
        self._trial_count: int = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitBreakerState:
        """Current state of the breaker, accounting for recovery timeout expiration."""
        if self._state == CircuitBreakerState.OPEN:
            if (
                self._last_failure_time is not None
                and (time.time() - self._last_failure_time >= self.recovery_timeout)
            ):
                return CircuitBreakerState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    async def execute(
        self,
        coro_func: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute an asynchronous callable protected by circuit breaker bounds."""
        async with self._lock:
            # Check state and handle automatic transition from OPEN to HALF_OPEN
            if self._state == CircuitBreakerState.OPEN:
                if (
                    self._last_failure_time is not None
                    and (time.time() - self._last_failure_time >= self.recovery_timeout)
                ):
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._trial_count = 0
                    logger.info(
                        f"Circuit breaker '{self.name}' entered HALF_OPEN state after recovery timeout.",
                        extra={"data": {"name": self.name, "state": "HALF_OPEN"}},
                    )
                else:
                    time_remaining = (
                        (self._last_failure_time + self.recovery_timeout) - time.time()
                        if self._last_failure_time
                        else self.recovery_timeout
                    )
                    logger.warning(
                        f"Circuit breaker '{self.name}' is OPEN. Blocked request. Cooldown: {time_remaining:.1f}s remaining.",
                        extra={"data": {"name": self.name, "state": "OPEN", "remaining": time_remaining}},
                    )
                    raise CircuitBreakerOpenException(
                        f"Circuit breaker '{self.name}' is OPEN. Service unavailable.",
                        details={"name": self.name, "cooldown_seconds_remaining": max(0.0, time_remaining)},
                    )

            if self._state == CircuitBreakerState.HALF_OPEN:
                if self._trial_count >= self.half_open_max_trials:
                    raise CircuitBreakerOpenException(
                        f"Circuit breaker '{self.name}' is HALF_OPEN and trial quota is exhausted. Awaiting probe result.",
                        details={"name": self.name, "trial_count": self._trial_count},
                    )
                self._trial_count += 1

        # Execute target coroutine outside the entry lock to allow concurrency in CLOSED mode
        try:
            result = await coro_func(*args, **kwargs)
        except Exception as exc:
            async with self._lock:
                self._record_failure(exc)
            raise
        else:
            async with self._lock:
                self._record_success()
            return result

    def _record_failure(self, exc: Exception) -> None:
        """Internal failure handler under lock."""
        self._last_failure_time = time.time()

        if self._state == CircuitBreakerState.HALF_OPEN:
            # Probe failed in trial mode -> immediate return to OPEN
            self._state = CircuitBreakerState.OPEN
            self._trial_count = 0
            logger.warning(
                f"Circuit breaker '{self.name}' trial probe failed. Returned to OPEN state.",
                extra={
                    "data": {
                        "name": self.name,
                        "error_type": type(exc).__name__,
                    }
                },
            )
        elif self._state == CircuitBreakerState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitBreakerState.OPEN
                logger.warning(
                    f"Circuit breaker '{self.name}' tripped to OPEN after {self._failure_count} consecutive failures.",
                    extra={"data": {"name": self.name, "failures": self._failure_count}},
                )

    def _record_success(self) -> None:
        """Internal success handler under lock."""
        if self._state == CircuitBreakerState.HALF_OPEN:
            # Probe succeeded -> full recovery to CLOSED
            self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._trial_count = 0
            logger.info(
                f"Circuit breaker '{self.name}' probe succeeded. Fully recovered to CLOSED state.",
                extra={"data": {"name": self.name, "state": "CLOSED"}},
            )
        elif self._state == CircuitBreakerState.CLOSED:
            self._failure_count = 0

    async def trip(self) -> None:
        """Force the circuit breaker into the OPEN state."""
        async with self._lock:
            self._state = CircuitBreakerState.OPEN
            self._last_failure_time = time.time()
            self._trial_count = 0

    async def reset(self) -> None:
        """Force the circuit breaker into the CLOSED state."""
        async with self._lock:
            self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._trial_count = 0
            self._last_failure_time = None

    def get_metrics(self) -> Dict[str, Any]:
        """Return diagnostic metrics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "last_failure_time": self._last_failure_time,
        }
