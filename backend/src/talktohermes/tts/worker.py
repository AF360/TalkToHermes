from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
from pathlib import Path
from typing import Any

from talktohermes.local_audio import serialized_local_audio
from talktohermes.worker_security import (
    WorkerPathError,
    ValidatedWorkerPaths,
    assert_worker_paths_unchanged,
    open_validated_worker_interpreter,
    open_validated_worker_script,
    validate_worker_paths,
)

from .base import MAX_WAV_BYTES, TTSTechnicalError, validate_text, validate_wav_output

PIPER_VOICE_RE = re.compile(
    r"^[a-z]{2,3}_[A-Z]{2,3}-[a-z0-9_]+-(?:x_)?(?:low|medium|high)$"
)
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536
_ISOLATED_LAUNCHER = (
    "import runpy,sys;sys.path.insert(0,sys.argv[1]);script=sys.argv[2];"
    "sys.argv=[script];runpy.run_path(script,run_name='__main__')"
)


class PiperWorkerTTS:
    name = "piper"

    def __init__(
        self,
        python: Path | str,
        script: Path | str,
        hermes_root: Path | str,
        voice: str,
        *,
        timeout: float = 180.0,
        max_wav_bytes: int = MAX_WAV_BYTES,
    ) -> None:
        self._paths = validate_worker_paths(python, script, hermes_root)
        if PIPER_VOICE_RE.fullmatch(voice) is None:
            raise ValueError("unsupported Piper voice")
        if timeout <= 0 or max_wav_bytes <= 0:
            raise ValueError("timeout and max_wav_bytes must be positive")
        self.voice = voice
        self._timeout = timeout
        self._max_wav_bytes = max_wav_bytes

    @serialized_local_audio
    async def synthesize(self, text: str, output_path: Path) -> Path:
        validated_text = validate_text(text)
        path = _validated_output_path(output_path)
        staging_path = _worker_staging_path(path)
        if staging_path.exists() or staging_path.is_symlink():
            raise TTSTechnicalError("worker_staging_collision")
        result: Path
        try:
            payload = await _invoke_worker(
                self._paths,
                {
                    "operation": "tts",
                    "provider": "piper",
                    "voice": self.voice,
                    "text": validated_text,
                    "output_path": str(path),
                },
                timeout=self._timeout,
            )
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise TTSTechnicalError("worker_failed")
            if (
                payload.get("provider") != "piper"
                or payload.get("voice") != self.voice
                or payload.get("file_path") != str(path)
            ):
                raise TTSTechnicalError("worker_invalid_response")
            result = validate_wav_output(path, max_bytes=self._max_wav_bytes)
        finally:
            _cleanup_worker_staging(staging_path)
        return result


class HermesWorkerTextNormalizer:
    """Async callable exposing Hermes prepare_spoken_text through the worker."""

    def __init__(
        self,
        python: Path | str,
        script: Path | str,
        hermes_root: Path | str,
        *,
        timeout: float = 15.0,
    ) -> None:
        self._paths = validate_worker_paths(python, script, hermes_root)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout

    async def __call__(self, text: str) -> str:
        validated_text = validate_text(text)
        payload = await _invoke_worker(
            self._paths,
            {"operation": "normalize", "text": validated_text},
            timeout=self._timeout,
        )
        normalized = payload.get("text") if isinstance(payload, dict) else None
        if payload.get("ok") is not True or not isinstance(normalized, str):
            raise TTSTechnicalError("worker_invalid_response")
        return normalized


async def _invoke_worker(
    paths: ValidatedWorkerPaths,
    request_payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    request = json.dumps(
        request_payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(request) > MAX_REQUEST_BYTES:
        raise TTSTechnicalError("worker_request_too_large")

    interpreter_descriptor: int | None = None
    try:
        assert_worker_paths_unchanged(paths)
        interpreter_descriptor = open_validated_worker_interpreter(paths)
        script_descriptor = open_validated_worker_script(paths)
    except WorkerPathError as exc:
        if interpreter_descriptor is not None:
            os.close(interpreter_descriptor)
        raise TTSTechnicalError("worker_path_changed") from exc
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                str(paths.python),
                "-I",
                "-c",
                _ISOLATED_LAUNCHER,
                str(paths.hermes_root),
                f"/proc/self/fd/{script_descriptor}",
                cwd=str(paths.hermes_root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env=_worker_environment(),
                executable=f"/proc/self/fd/{interpreter_descriptor}",
                pass_fds=(interpreter_descriptor, script_descriptor),
            )
        finally:
            os.close(interpreter_descriptor)
            os.close(script_descriptor)
    except OSError as exc:
        raise TTSTechnicalError("worker_start_failed") from exc

    try:
        stdout = await asyncio.wait_for(
            _bounded_exchange(process, request), timeout=timeout
        )
        if process.returncode != 0:
            raise TTSTechnicalError("worker_process_failed")
        try:
            payload: Any = json.loads(stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TTSTechnicalError("worker_invalid_response") from exc
        if not isinstance(payload, dict):
            raise TTSTechnicalError("worker_invalid_response")
        return payload
    finally:
        await asyncio.shield(_terminate_and_reap(process))


async def _bounded_exchange(process: asyncio.subprocess.Process, request: bytes) -> bytes:
    if process.stdin is None or process.stdout is None:
        raise TTSTechnicalError("worker_stdio_unavailable")
    process.stdin.write(request)
    try:
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise TTSTechnicalError("worker_process_failed") from exc
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
            raise TTSTechnicalError("worker_response_too_large")
    await process.wait()
    return bytes(response)


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    pid = getattr(process, "pid", None)
    if pid is None or pid == os.getpgrp():
        if process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.returncode is None:
            await process.wait()
        return
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass

    # The worker leader may exit while an inference/download descendant is
    # still alive. The known leader PID is also the PGID (start_new_session).
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        await process.wait()


def _worker_environment() -> dict[str, str]:
    return {
        "HOME": os.path.expanduser("~"),
        "PATH": "/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONNOUSERSITE": "1",
        "HERMES_SESSION_PLATFORM": "",
    }


def _worker_staging_path(output_path: Path) -> Path:
    return output_path.with_name(f".tts-worker-{output_path.name}")


def _cleanup_worker_staging(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise TTSTechnicalError("worker_artifact_cleanup_failed") from exc


def _validated_output_path(raw_path: Path | str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise TTSTechnicalError("invalid_output_path") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise TTSTechnicalError("invalid_output_path")
    return path.resolve(strict=True)
