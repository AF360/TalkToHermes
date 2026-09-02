#!/usr/bin/env python3
"""Bounded command adapter from one audio file to Wyoming STT."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient

PCM_RATE = 16_000
PCM_WIDTH = 2
PCM_CHANNELS = 1
PCM_CHUNK_BYTES = 3_200
CONNECT_TIMEOUT_SECONDS = 3
RESPONSE_TIMEOUT_SECONDS = 120


class AdapterError(RuntimeError):
    pass


async def _terminate(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def transcribe(input_path: Path, language: str, uri: str) -> str:
    client = AsyncClient.from_uri(uri)
    process: asyncio.subprocess.Process | None = None
    primary_error: BaseException | None = None
    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT_SECONDS)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(input_path), "-ar", str(PCM_RATE), "-ac", "1",
            "-f", "s16le", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await client.write_event(Transcribe(language=language).event())
        await client.write_event(
            AudioStart(rate=PCM_RATE, width=PCM_WIDTH, channels=PCM_CHANNELS).event()
        )
        if process.stdout is None:
            raise AdapterError("ffmpeg stdout unavailable")
        while chunk := await process.stdout.read(PCM_CHUNK_BYTES):
            await client.write_event(
                AudioChunk(
                    rate=PCM_RATE,
                    width=PCM_WIDTH,
                    channels=PCM_CHANNELS,
                    audio=chunk,
                ).event()
            )
        if await process.wait() != 0:
            raise AdapterError("ffmpeg failed")
        await client.write_event(AudioStop().event())
        while True:
            event = await asyncio.wait_for(
                client.read_event(), timeout=RESPONSE_TIMEOUT_SECONDS
            )
            if event is None:
                raise AdapterError("server closed without transcript")
            if Transcript.is_type(event.type):
                text = Transcript.from_event(event).text.strip()
                if not text:
                    raise AdapterError("empty transcript")
                return text
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            await _terminate(process)
        except BaseException as exc:
            cleanup_error = exc
        try:
            await client.disconnect()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


async def run(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.is_file() or not output_path.parent.is_dir():
        raise AdapterError("invalid path")
    text = await transcribe(input_path, args.language, args.uri)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        temporary.write_text(text + "\n", encoding="utf-8")
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", required=True)
    result.add_argument("--output", required=True)
    result.add_argument("--language", default="de")
    result.add_argument("--uri", required=True)
    return result


def main() -> int:
    try:
        asyncio.run(run(parser().parse_args()))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
