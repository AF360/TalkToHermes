from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Literal

CircuitState = Literal["closed", "open", "half_open"]


class CircuitPermit:
    def __init__(
        self,
        breaker: "EndpointCircuitBreaker",
        *,
        allowed: bool,
        state: CircuitState,
        generation: int,
    ) -> None:
        self._breaker = breaker
        self.allowed = allowed
        self.state = state
        self.generation = generation
        self._completed = not allowed

    async def reachable(self) -> None:
        await self._complete(unavailable=False)

    async def unavailable(self) -> None:
        await self._complete(unavailable=True)

    async def cancelled(self) -> None:
        if self._completed:
            return
        transition = asyncio.create_task(self._breaker._cancel(self))
        await self._await_transition(transition)

    async def _complete(self, *, unavailable: bool) -> None:
        if self._completed:
            return
        transition = asyncio.create_task(
            self._breaker._complete(self, unavailable=unavailable)
        )
        await self._await_transition(transition)

    async def _await_transition(self, transition: asyncio.Task[None]) -> None:
        cancelled = False
        while True:
            try:
                await asyncio.shield(transition)
            except asyncio.CancelledError:
                cancelled = True
                continue
            break
        self._completed = True
        if cancelled:
            raise asyncio.CancelledError


class EndpointCircuitBreaker:
    """Process-local connectivity breaker with one real half-open request."""

    def __init__(
        self,
        *,
        cooldown_seconds: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state: CircuitState = "closed"
        self._opened_at = 0.0
        self._probe: CircuitPermit | None = None
        self._generation = 0

    async def acquire(self) -> CircuitPermit:
        async with self._lock:
            if self._state == "open":
                if self._clock() - self._opened_at < self.cooldown_seconds:
                    return CircuitPermit(
                        self, allowed=False, state="open", generation=self._generation
                    )
                self._state = "half_open"
            if self._state == "half_open":
                if self._probe is not None:
                    return CircuitPermit(
                        self, allowed=False, state="half_open", generation=self._generation
                    )
                permit = CircuitPermit(
                    self, allowed=True, state="half_open", generation=self._generation
                )
                self._probe = permit
                return permit
            return CircuitPermit(
                self, allowed=True, state="closed", generation=self._generation
            )

    async def _complete(self, permit: CircuitPermit, *, unavailable: bool) -> None:
        async with self._lock:
            if permit.generation != self._generation:
                return
            if unavailable:
                self._state = "open"
                self._opened_at = self._clock()
                self._probe = None
                self._generation += 1
            elif permit is self._probe:
                self._state = "closed"
                self._probe = None
                self._generation += 1

    async def _cancel(self, permit: CircuitPermit) -> None:
        async with self._lock:
            if permit is self._probe and permit.generation == self._generation:
                self._state = "open"
                self._opened_at = self._clock()
                self._probe = None
                self._generation += 1
