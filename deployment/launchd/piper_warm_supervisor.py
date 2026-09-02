#!/usr/bin/env python3
"""Run one Wyoming-Piper process and warm its configured voice."""

from __future__ import annotations

import argparse
import asyncio
import signal

from wyoming.audio import AudioChunk, AudioStop
from wyoming.client import AsyncClient
from wyoming.tts import Synthesize, SynthesizeVoice

MAX_WARMUP_AUDIO_BYTES = 4 * 1024 * 1024
STARTUP_TIMEOUT_SECONDS = 60.0
WARMUP_TIMEOUT_SECONDS = 30.0


async def warm(uri: str, voice: str) -> None:
    deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_SECONDS
    while True:
        client = AsyncClient.from_uri(uri)
        try:
            await asyncio.wait_for(client.connect(), timeout=1.0)
            break
        except (OSError, asyncio.TimeoutError):
            await client.disconnect()
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Piper listener did not become ready")
            await asyncio.sleep(0.25)

    received = 0
    try:
        await client.write_event(
            Synthesize(
                text="Der Sprachdienst ist bereit.",
                voice=SynthesizeVoice(name=voice),
            ).event()
        )
        while True:
            event = await asyncio.wait_for(
                client.read_event(), timeout=WARMUP_TIMEOUT_SECONDS
            )
            if event is None:
                raise RuntimeError("Piper closed during warm-up")
            if AudioChunk.is_type(event.type):
                received += len(event.payload or b"")
                if received > MAX_WARMUP_AUDIO_BYTES:
                    raise RuntimeError("Piper warm-up audio exceeded bound")
            elif AudioStop.is_type(event.type):
                if received == 0:
                    raise RuntimeError("Piper warm-up returned no audio")
                return
    finally:
        await client.disconnect()


async def run(args: argparse.Namespace) -> int:
    process = await asyncio.create_subprocess_exec(
        args.piper,
        "--voice",
        args.voice,
        "--uri",
        args.uri,
        "--data-dir",
        args.data_dir,
        "--download-dir",
        args.data_dir,
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    try:
        warm_task = asyncio.create_task(warm(args.uri, args.voice))
        exit_task = asyncio.create_task(process.wait())
        done, _ = await asyncio.wait(
            {warm_task, exit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if exit_task in done:
            warm_task.cancel()
            await asyncio.gather(warm_task, return_exceptions=True)
            return process.returncode or 1
        await warm_task
        print(f"warm voice={args.voice} uri={args.uri}", flush=True)

        stop_task = asyncio.create_task(stopping.wait())
        done, _ = await asyncio.wait(
            {exit_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if exit_task in done:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            return process.returncode or 1
        return 0
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--piper", required=True)
    result.add_argument("--voice", required=True)
    result.add_argument("--uri", required=True)
    result.add_argument("--data-dir", required=True)
    return result


def main() -> int:
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
