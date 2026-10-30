"""Circuit breaker for the LLM provider (A5).

closed    -> calls pass; consecutive failures are counted.
open      -> after `fail_threshold` consecutive failures; calls are refused
             immediately for `reset_s` seconds (no load on a dead provider).
half_open -> after `reset_s`, one trial call is let through. Success closes
             the breaker; failure opens it again.
"""
from __future__ import annotations

import threading
import time

CLOSED, HALF_OPEN, OPEN = "closed", "half_open", "open"
STATE_VALUE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, name: str, fail_threshold: int = 5, reset_s: float = 30.0, clock=time.monotonic,
                 on_change=None):
        self.name = name
        self.fail_threshold = fail_threshold
        self.reset_s = reset_s
        self._clock = clock
        self._on_change = on_change
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _set(self, new: str) -> None:
        if new != self._state:
            old, self._state = self._state, new
            if self._on_change:
                self._on_change(self.name, old, new)

    def _maybe_half_open(self) -> None:
        if self._state == OPEN and self._clock() - self._opened_at >= self.reset_s:
            self._set(HALF_OPEN)
            self._trial_in_flight = False

    def before_call(self) -> None:
        """Raise CircuitOpenError if the call must not go to the provider."""
        with self._lock:
            self._maybe_half_open()
            if self._state == OPEN:
                raise CircuitOpenError(f"breaker {self.name} open")
            if self._state == HALF_OPEN:
                if self._trial_in_flight:
                    raise CircuitOpenError(f"breaker {self.name} half-open, trial in flight")
                self._trial_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._trial_in_flight = False
            self._set(CLOSED)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._trial_in_flight = False
            if self._state == HALF_OPEN or self._failures >= self.fail_threshold:
                self._opened_at = self._clock()
                self._set(OPEN)

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state, "consecutive_failures": self._failures,
                "fail_threshold": self.fail_threshold, "reset_s": self.reset_s}
