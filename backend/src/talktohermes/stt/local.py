from __future__ import annotations

import asyncio
import json
import re
import stat
from pathlib import Path
from typing import Any

from ..local_audio import serialized_local_audio
from .base import STTTechnicalError, validate_audio_input, validate_language

MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536


class LocalSTT:
    name = "local"

    def __init__(
        self,
        python: Path | str,
        script: Path | str,
        hermes_root: Path | str,
        *,
        model: str = "small",
        timeout: float = 180.0,
    ) -> None:
        self._python = _validated_path(python, "python", directory=False)
        self._script = _validated_path(script, "script", directory=False)
        self._hermes_root = _validated_path(hermes_root, "hermes_root", directory=True)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", model) is None:
            raise ValueError("invalid local STT model")
        self.model = model
        self._timeout = timeout

    @serialized_local_audio
    async def transcribe(self, audio_path: Path, language: str) -> str:
        path = validate_audio_input(audio_path)
        normalized_language = validate_language(language)
        request = json.dumps(
            {
                "operation": "stt-local", "input_path": str(path),
                "model": self.model, "language": normalized_language,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request) > MAX_REQUEST_BYTES:
            raise STTTechnicalError("local_request_too_large")

        try:
            process = await asyncio.create_subprocess_exec(
                str(self._python),
                str(self._script),
                cwd=str(self._hermes_root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise STTTechnicalError("local_start_failed") from exc

        try:
            stdout = await asyncio.wait_for(
                _bounded_exchange(process, request), timeout=self._timeout
            )
        except BaseException:
            await _terminate_and_reap(process)
            raise

        if process.returncode != 0:
            raise STTTechnicalError("local_process_failed")
        if len(stdout) > MAX_RESPONSE_BYTES:
            raise STTTechnicalError("local_response_too_large")
        try:
            payload: Any = json.loads(stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise STTTechnicalError("local_invalid_response") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise STTTechnicalError("local_worker_failed")
        text = payload.get("text")
        if not isinstance(text, str):
            raise STTTechnicalError("local_invalid_response")
        return text.strip()


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


async def _bounded_exchange(
    process: asyncio.subprocess.Process, request: bytes
) -> bytes:
    """Write one bounded request and read at most one bounded JSON response."""
    if process.stdin is None or process.stdout is None:
        raise STTTechnicalError("local_stdio_unavailable")
    process.stdin.write(request)
    try:
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise STTTechnicalError("local_process_failed") from exc
    finally:
        process.stdin.close()

    response = bytearray()
    while True:
        remaining = MAX_RESPONSE_BYTES + 1 - len(response)
        chunk = await process.stdout.read(min(8192, remaining))
        if not chunk:
            break
        response.extend(chunk)
        if len(response) > MAX_RESPONSE_BYTES:
            process.kill()
            await process.wait()
            raise STTTechnicalError("local_response_too_large")
    await process.wait()
    return bytes(response)


def _validated_path(raw_path: Path | str, name: str, *, directory: bool) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"invalid {name} path") from exc
    expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if path.is_symlink() or not expected_type:
        raise ValueError(f"invalid {name} path")
    return path.resolve(strict=True)
