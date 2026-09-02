from __future__ import annotations

import hashlib
import math
import os
import stat
import subprocess
import tempfile
import wave
from pathlib import Path

from fastapi import UploadFile

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DURATION_SECONDS = 120.0
ALLOWED_MIME_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/m4a", "audio/mp4", "audio/x-caf", "audio/ogg"})
_SAFE_SUFFIXES = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/x-caf": ".caf",
    "audio/ogg": ".ogg",
}


class AudioValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def private_audio_root(state_dir: Path) -> Path:
    root = state_dir / "audio"
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
    except OSError as exc:
        raise AudioValidationError("invalid_audio_root") from exc
    if root.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AudioValidationError("invalid_audio_root")
    return root.resolve(strict=True)


async def store_upload(upload: UploadFile, root: Path) -> tuple[Path, str, int]:
    mime = (upload.content_type or "").lower()
    if mime not in ALLOWED_MIME_TYPES:
        raise AudioValidationError("unsupported_audio_type")
    descriptor, raw = tempfile.mkstemp(prefix="upload-", suffix=_SAFE_SUFFIXES[mime], dir=root)
    path = Path(raw)
    digest = hashlib.sha256()
    size = 0
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            while chunk := await upload.read(64 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise AudioValidationError("audio_too_large")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0:
            raise AudioValidationError("empty_audio")
        validate_actual_audio(path, mime)
        return path, digest.hexdigest(), size
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def validate_actual_audio(path: Path, mime: str) -> None:
    data = path.read_bytes()[:16]
    if mime in {"audio/wav", "audio/x-wav"}:
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise AudioValidationError("audio_format_mismatch")
        try:
            with wave.open(str(path), "rb") as audio:
                if audio.getframerate() <= 0 or audio.getnframes() <= 0:
                    raise AudioValidationError("invalid_audio")
                duration = audio.getnframes() / audio.getframerate()
        except (wave.Error, EOFError, OSError) as exc:
            raise AudioValidationError("invalid_audio") from exc
        if duration > MAX_DURATION_SECONDS:
            raise AudioValidationError("audio_too_long")
        return
    elif mime in {"audio/m4a", "audio/mp4"}:
        if len(data) < 12 or data[4:8] != b"ftyp":
            raise AudioValidationError("audio_format_mismatch")
    elif mime == "audio/x-caf":
        if data[:4] != b"caff":
            raise AudioValidationError("audio_format_mismatch")
    elif mime == "audio/ogg":
        if data[:4] != b"OggS":
            raise AudioValidationError("audio_format_mismatch")
    else:
        return

    duration = _probe_duration(path)
    if duration <= 0:
        raise AudioValidationError("audio_duration_unavailable")
    if duration > MAX_DURATION_SECONDS:
        raise AudioValidationError("audio_too_long")


def _probe_duration(path: Path) -> float:
    """Read duration through the fixed local ffprobe binary, never a shell."""
    try:
        completed = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "--",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioValidationError("audio_duration_unavailable") from exc
    if completed.returncode != 0 or len(completed.stdout) > 128:
        raise AudioValidationError("audio_duration_unavailable")
    try:
        duration = float(completed.stdout.decode("ascii").strip())
        if not math.isfinite(duration):
            raise ValueError("non-finite duration")
        return duration
    except (UnicodeError, ValueError) as exc:
        raise AudioValidationError("audio_duration_unavailable") from exc
