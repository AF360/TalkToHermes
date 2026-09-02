from __future__ import annotations

import asyncio
import os
import signal
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from .base import STTTechnicalError, validate_audio_input, validate_language

WYOMING_STT_PYTHON = "/opt/hermes-stt-wyoming/venv/bin/python"
WYOMING_STT_SCRIPT = "/opt/hermes-stt-wyoming/app/wyoming_stt.py"
MAX_TRANSCRIPT_BYTES = 1_048_576


async def _terminate_process_group(
    process: asyncio.subprocess.Process, *, grace_seconds: float = 2.0
) -> None:
    pid = getattr(process, "pid", None)
    if pid is None:
        if process.returncode is None:
            process.kill()
            await process.wait()
        return
    process_group = pid  # start_new_session=True makes the leader PID the PGID.
    if process_group == os.getpgrp():
        if process.returncode is None:
            process.kill()
            await process.wait()
        return

    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        if process.returncode is None:
            await process.wait()
        return
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            pass

    # The leader may already have exited while a descendant ignores TERM.
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        await process.wait()


class WyomingSTT:
    name = "wyoming"

    def __init__(
        self,
        *,
        uri: str = "tcp://127.0.0.1:10300",
        timeout: float = 120.0,
        python_path: Path | str | None = None,
        script_path: Path | str | None = None,
        path_validator: Callable[[], object] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        parsed = urlparse(uri)
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
            raise ValueError("invalid Wyoming STT endpoint")
        self.uri = uri
        self._timeout = timeout
        self._python_path = Path(python_path or WYOMING_STT_PYTHON)
        self._script_path = Path(script_path or WYOMING_STT_SCRIPT)
        self._path_validator = path_validator

    async def transcribe(self, audio_path: Path, language: str) -> str:
        path = validate_audio_input(audio_path)
        normalized_language = validate_language(language)
        if self._path_validator is not None:
            try:
                self._path_validator()
            except (OSError, ValueError) as exc:
                raise STTTechnicalError("wyoming_path_changed") from exc

        with tempfile.TemporaryDirectory(prefix="talktohermes-wyoming-") as temp_dir:
            output_path = Path(temp_dir) / "transcript.txt"
            try:
                process = await asyncio.create_subprocess_exec(
                    str(self._python_path),
                    str(self._script_path),
                    "--input",
                    str(path),
                    "--output",
                    str(output_path),
                    "--language",
                    normalized_language,
                    "--uri",
                    self.uri,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                raise STTTechnicalError("wyoming_start_failed") from exc

            try:
                await asyncio.wait_for(process.communicate(), timeout=self._timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await _terminate_process_group(process)
                raise

            if process.returncode != 0:
                raise STTTechnicalError("wyoming_process_failed")
            try:
                info = output_path.lstat()
                if output_path.is_symlink() or not stat.S_ISREG(info.st_mode):
                    raise STTTechnicalError("wyoming_invalid_output")
                if info.st_size > MAX_TRANSCRIPT_BYTES:
                    raise STTTechnicalError("wyoming_output_too_large")
                raw = output_path.read_bytes()
                return raw.decode("utf-8").strip()
            except STTTechnicalError:
                raise
            except (OSError, UnicodeError) as exc:
                raise STTTechnicalError("wyoming_invalid_output") from exc
