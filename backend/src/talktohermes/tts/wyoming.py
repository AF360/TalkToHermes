from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .base import MAX_WAV_BYTES, TTSTechnicalError, validate_text

WYOMING_PIPER_URL = "tcp://127.0.0.1:10200"
WYOMING_PIPER_VOICE = "de_DE-thorsten-medium"
WYOMING_VERSION = "1.8.0"
MAX_HEADER_BYTES = 65_536
MAX_EVENT_DATA_BYTES = 65_536


def _open_output(path: Path) -> tuple[int, tuple[int, int]]:
    if not path.is_absolute():
        raise TTSTechnicalError("invalid_output_path")
    descriptor: int | None = None
    try:
        parent_info = path.parent.lstat()
        info = path.lstat()
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_info = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise TTSTechnicalError("invalid_output_path") from exc
    if (
        path.parent.is_symlink()
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) & 0o077
        or path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or not stat.S_ISREG(opened_info.st_mode)
        or opened_info.st_uid != os.getuid()
        or stat.S_IMODE(opened_info.st_mode) != 0o600
        or (info.st_dev, info.st_ino) != (opened_info.st_dev, opened_info.st_ino)
    ):
        os.close(descriptor)
        raise TTSTechnicalError("invalid_output_path")
    return descriptor, (opened_info.st_dev, opened_info.st_ino)


def _require_same_output(path: Path, identity: tuple[int, int]) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TTSTechnicalError("output_path_changed") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or (info.st_dev, info.st_ino) != identity
    ):
        raise TTSTechnicalError("output_path_changed")


class WyomingPiperTTS:
    name = "wyoming-piper"
    __slots__ = ("_host", "_port", "_voice", "_timeout", "_max_pcm_bytes")

    def __init__(
        self,
        *,
        url: str = WYOMING_PIPER_URL,
        voice: str = WYOMING_PIPER_VOICE,
        timeout: float = 30.0,
        max_pcm_bytes: int = MAX_WAV_BYTES - 44,
    ) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "tcp"
            or not parsed.hostname
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid Wyoming Piper endpoint")
        if re.fullmatch(
            r"[a-z]{2,3}_[A-Z]{2,3}-[a-z0-9_]+-(?:x_)?(?:low|medium|high)",
            voice,
        ) is None:
            raise ValueError("invalid fallback voice server Piper voice")
        if timeout <= 0 or max_pcm_bytes <= 0 or max_pcm_bytes > MAX_WAV_BYTES - 44:
            raise ValueError("invalid fallback voice server Piper bounds")
        self._host = parsed.hostname
        self._port = parsed.port
        self._voice = voice
        self._timeout = timeout
        self._max_pcm_bytes = max_pcm_bytes

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def voice(self) -> str:
        return self._voice

    async def synthesize(self, text: str, output_path: Path) -> Path:
        validated_text = validate_text(text)
        path = Path(output_path)
        descriptor, identity = _open_output(path)
        try:
            audio_format, pcm = await asyncio.wait_for(
                self._receive_audio(validated_text), timeout=self._timeout
            )
            _require_same_output(path, identity)
            _write_wav(descriptor, audio_format, pcm)
            _require_same_output(path, identity)
            return path
        except asyncio.TimeoutError:
            raise
        except TTSTechnicalError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise TTSTechnicalError("wyoming_protocol_failure") from exc
        finally:
            os.close(descriptor)

    async def _receive_audio(
        self, text: str
    ) -> tuple[tuple[int, int, int], bytes]:
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        try:
            reader, writer = await asyncio.open_connection(self._host, self._port)
        except OSError as exc:
            raise TTSTechnicalError("wyoming_unavailable") from exc

        pcm = bytearray()
        audio_format: tuple[int, int, int] | None = None
        try:
            await _write_event(
                writer,
                "synthesize",
                {"text": text, "voice": {"name": self.voice}},
            )
            while True:
                event_type, data, payload = await _read_event(
                    reader, max_payload_bytes=self._max_pcm_bytes - len(pcm)
                )
                if event_type == "audio-start":
                    if audio_format is not None or payload:
                        raise TTSTechnicalError("wyoming_invalid_audio")
                    audio_format = _audio_format(data)
                elif event_type == "audio-chunk":
                    current_format = _audio_format(data, audio_format)
                    if audio_format is None or current_format != audio_format or not payload:
                        raise TTSTechnicalError("wyoming_invalid_audio")
                    pcm.extend(payload)
                elif event_type == "audio-stop":
                    if audio_format is None or not pcm or payload:
                        raise TTSTechnicalError("wyoming_invalid_audio")
                    break
                elif event_type == "error":
                    raise TTSTechnicalError("wyoming_provider_error")
                else:
                    raise TTSTechnicalError("wyoming_unexpected_event")
        finally:
            writer.close()

        rate, width, channels = audio_format
        if len(pcm) % (width * channels):
            raise TTSTechnicalError("wyoming_invalid_audio")
        return audio_format, bytes(pcm)


def _write_wav(
    descriptor: int, audio_format: tuple[int, int, int], pcm: bytes
) -> None:
    rate, width, channels = audio_format
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "wb") as raw_output:
            with wave.open(raw_output, "wb") as output:
                output.setframerate(rate)
                output.setsampwidth(width)
                output.setnchannels(channels)
                output.writeframes(pcm)
            raw_output.flush()
            os.fsync(raw_output.fileno())
    except (OSError, OverflowError, ValueError, wave.Error) as exc:
        raise TTSTechnicalError("invalid_wav") from exc


def _audio_format(
    data: dict[str, Any], expected: tuple[int, int, int] | None = None
) -> tuple[int, int, int]:
    try:
        rate = data["rate"]
        width = data["width"]
        channels = data["channels"]
    except (KeyError, TypeError) as exc:
        raise TTSTechnicalError("wyoming_invalid_audio") from exc
    if any(type(value) is not int for value in (rate, width, channels)):
        raise TTSTechnicalError("wyoming_invalid_audio")
    current = (rate, width, channels)
    if (
        width != 2
        or channels != 1
        or not 8_000 <= rate <= 48_000
        or (expected is not None and current != expected)
    ):
        raise TTSTechnicalError("wyoming_invalid_audio")
    return current


async def _write_event(
    writer: asyncio.StreamWriter, event_type: str, data: dict[str, Any]
) -> None:
    encoded_data = json.dumps(
        data, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    header = json.dumps(
        {
            "type": event_type,
            "version": WYOMING_VERSION,
            "data_length": len(encoded_data),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    writer.write(header + b"\n" + encoded_data)
    await writer.drain()


async def _read_event(
    reader: asyncio.StreamReader, *, max_payload_bytes: int
) -> tuple[str, dict[str, Any], bytes]:
    try:
        line = await reader.readline()
    except (OSError, asyncio.LimitOverrunError) as exc:
        raise TTSTechnicalError("wyoming_protocol_failure") from exc
    if not line:
        raise TTSTechnicalError("wyoming_unexpected_eof")
    if len(line) > MAX_HEADER_BYTES:
        raise TTSTechnicalError("wyoming_header_too_large")
    try:
        header = json.loads(line)
        event_type = header["type"]
        version = header.get("version")
        data_length = int(header.get("data_length", 0))
        payload_length = int(header.get("payload_length", 0))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TTSTechnicalError("wyoming_protocol_failure") from exc
    if version != WYOMING_VERSION:
        raise TTSTechnicalError("wyoming_protocol_version")
    if not isinstance(event_type, str):
        raise TTSTechnicalError("wyoming_protocol_failure")
    if data_length < 0 or data_length > MAX_EVENT_DATA_BYTES:
        raise TTSTechnicalError("wyoming_event_too_large")
    if payload_length < 0 or payload_length > max_payload_bytes:
        raise TTSTechnicalError("wyoming_audio_too_large")
    try:
        raw_data = await reader.readexactly(data_length) if data_length else b""
        payload = await reader.readexactly(payload_length) if payload_length else b""
        data = json.loads(raw_data) if raw_data else {}
    except (asyncio.IncompleteReadError, UnicodeError, json.JSONDecodeError) as exc:
        raise TTSTechnicalError("wyoming_protocol_failure") from exc
    if not isinstance(data, dict):
        raise TTSTechnicalError("wyoming_protocol_failure")
    return event_type, data, payload
