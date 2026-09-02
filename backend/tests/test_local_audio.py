from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from talktohermes.local_audio import serialized_local_audio


def test_local_audio_serialization_is_process_wide_and_event_loop_neutral() -> None:
    active = 0
    maximum = 0
    guard = threading.Lock()
    start = threading.Barrier(2)

    @serialized_local_audio
    async def operation() -> None:
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        await asyncio.sleep(0.03)
        with guard:
            active -= 1

    def run() -> None:
        start.wait(timeout=1)
        asyncio.run(operation())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        for future in futures:
            future.result(timeout=2)

    assert maximum == 1


def test_cancelled_waiter_does_not_consume_local_audio_slot_later() -> None:
    entered = threading.Event()
    release = threading.Event()

    @serialized_local_audio
    async def blocking() -> None:
        entered.set()
        await asyncio.to_thread(release.wait)

    async def scenario() -> None:
        first = asyncio.create_task(blocking())
        await asyncio.to_thread(entered.wait)
        waiter = asyncio.create_task(blocking())
        await asyncio.sleep(0.01)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        release.set()
        await first
        await asyncio.wait_for(blocking(), timeout=0.2)

    asyncio.run(scenario())
