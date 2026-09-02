from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

# One process-wide slot shared by CPU-heavy local STT and Piper synthesis.
# A threading lock is event-loop neutral; nonblocking polling keeps cancelled
# waiters from acquiring the slot later in a detached worker thread.
_LOCAL_AUDIO_LOCK = threading.Lock()


def serialized_local_audio(
    function: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    @functools.wraps(function)
    async def serialized(*args: P.args, **kwargs: P.kwargs) -> R:
        while not _LOCAL_AUDIO_LOCK.acquire(blocking=False):
            await asyncio.sleep(0.005)
        try:
            return await function(*args, **kwargs)
        finally:
            _LOCAL_AUDIO_LOCK.release()

    return serialized
