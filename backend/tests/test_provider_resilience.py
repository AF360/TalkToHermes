from __future__ import annotations

import asyncio

import pytest

from talktohermes.provider_resilience import EndpointCircuitBreaker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_unavailable_opens_and_cooldown_skips_until_one_half_open_probe() -> None:
    clock = Clock()
    breaker = EndpointCircuitBreaker(cooldown_seconds=45, clock=clock)

    first = await breaker.acquire()
    assert first.allowed and first.state == "closed"
    await first.unavailable()

    skipped = await breaker.acquire()
    assert not skipped.allowed and skipped.state == "open"
    clock.now = 45
    probe, concurrent = await asyncio.gather(breaker.acquire(), breaker.acquire())
    assert [permit.allowed for permit in (probe, concurrent)].count(True) == 1
    assert {permit.state for permit in (probe, concurrent)} == {"half_open"}


@pytest.mark.asyncio
async def test_half_open_reachable_closes_and_unavailable_reopens() -> None:
    clock = Clock()
    breaker = EndpointCircuitBreaker(cooldown_seconds=45, clock=clock)
    initial = await breaker.acquire()
    await initial.unavailable()
    clock.now = 45
    probe = await breaker.acquire()
    await probe.reachable()
    assert (await breaker.acquire()).state == "closed"

    await probe.unavailable()  # completion is idempotent
    closed = await breaker.acquire()
    await closed.unavailable()
    clock.now = 90
    failed_probe = await breaker.acquire()
    await failed_probe.unavailable()
    assert not (await breaker.acquire()).allowed


@pytest.mark.asyncio
async def test_stale_closed_completion_cannot_end_active_half_open_probe() -> None:
    clock = Clock()
    breaker = EndpointCircuitBreaker(cooldown_seconds=45, clock=clock)
    stale = await breaker.acquire()
    failing = await breaker.acquire()
    await failing.unavailable()
    clock.now = 45
    probe = await breaker.acquire()
    assert probe.allowed and probe.state == "half_open"

    await stale.reachable()

    blocked = await breaker.acquire()
    assert not blocked.allowed and blocked.state == "half_open"
    await probe.reachable()
    restored = await breaker.acquire()
    assert restored.allowed and restored.state == "closed"


@pytest.mark.asyncio
async def test_stale_closed_unavailable_cannot_override_successful_half_open_probe() -> None:
    clock = Clock()
    breaker = EndpointCircuitBreaker(cooldown_seconds=45, clock=clock)
    stale = await breaker.acquire()
    failing = await breaker.acquire()
    await failing.unavailable()
    clock.now = 45
    probe = await breaker.acquire()
    assert probe.allowed and probe.state == "half_open"

    await stale.unavailable()
    await probe.reachable()

    restored = await breaker.acquire()
    assert restored.allowed and restored.state == "closed"


@pytest.mark.asyncio
async def test_cancel_during_half_open_completion_cannot_strand_circuit() -> None:
    clock = Clock()
    breaker = EndpointCircuitBreaker(cooldown_seconds=5, clock=clock)
    initial = await breaker.acquire()
    await initial.unavailable()
    clock.now = 5
    probe = await breaker.acquire()
    assert probe.allowed and probe.state == "half_open"

    await breaker._lock.acquire()
    completion = asyncio.create_task(probe.reachable())
    await asyncio.sleep(0)
    completion.cancel()
    breaker._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await completion

    restored = await breaker.acquire()
    assert restored.allowed and restored.state == "closed"


@pytest.mark.asyncio
async def test_repeated_cancel_during_half_open_completion_cannot_strand_circuit() -> None:
    clock = Clock()
    breaker = EndpointCircuitBreaker(cooldown_seconds=5, clock=clock)
    initial = await breaker.acquire()
    await initial.unavailable()
    clock.now = 5
    probe = await breaker.acquire()

    await breaker._lock.acquire()
    completion = asyncio.create_task(probe.reachable())
    await asyncio.sleep(0)
    completion.cancel()
    await asyncio.sleep(0)
    completion.cancel()
    breaker._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await completion

    restored = await breaker.acquire()
    assert restored.allowed and restored.state == "closed"


@pytest.mark.asyncio
async def test_cancelled_half_open_permit_cannot_strand_circuit() -> None:
    clock = Clock()
    breaker = EndpointCircuitBreaker(cooldown_seconds=5, clock=clock)
    permit = await breaker.acquire()
    await permit.unavailable()
    clock.now = 5
    probe = await breaker.acquire()
    await probe.cancelled()
    clock.now = 10
    assert (await breaker.acquire()).allowed
