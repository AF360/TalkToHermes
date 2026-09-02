from __future__ import annotations

import os
import stat
import struct
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

MAX_TEXT_CHARS = 2_000
MAX_WAV_BYTES = 32 * 1024 * 1024


class TTSError(RuntimeError):
    """Base class for TTS failures carrying only a non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TTSValidationError(TTSError):
    pass


class TTSTechnicalError(TTSError):
    pass


class TTSProviderUnavailable(TTSTechnicalError):
    def __init__(self, code: str, circuit_state: str) -> None:
        self.circuit_state = circuit_state
        super().__init__(code)


class SynthesizedAudio(type(Path())):
    __slots__ = ("circuit_state",)

    def __new__(
        cls, *path_segments: os.PathLike[str] | str, circuit_state: str = "closed"
    ) -> "SynthesizedAudio":
        path = super().__new__(cls, *path_segments)
        path.circuit_state = circuit_state
        return path

    def __init__(
        self, *path_segments: os.PathLike[str] | str, circuit_state: str = "closed"
    ) -> None:
        del circuit_state
        super().__init__(*path_segments)


AttemptOutcome = Literal[
    "success", "technical_failure", "timeout", "empty", "invalid", "oversized"
]


@dataclass(frozen=True, slots=True)
class TTSAttempt:
    provider: str
    voice: str
    elapsed_ms: float
    outcome: AttemptOutcome
    error_code: str | None = None
    circuit_state: str = "closed"


@dataclass(frozen=True, slots=True)
class TTSResult:
    audio_path: Path
    provider: str
    voice: str
    attempts: tuple[TTSAttempt, ...]


class TTSProvider(Protocol):
    name: str
    voice: str

    async def synthesize(
        self, text: str, output_path: Path
    ) -> Path | SynthesizedAudio: ...


def validate_text(text: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(text, str) or not text.strip():
        raise TTSValidationError("text_required")
    if len(text) > max_chars:
        raise TTSValidationError("text_too_large")
    for character in text:
        if character in "\t\n\r":
            continue
        if unicodedata.category(character).startswith("C"):
            raise TTSValidationError("invalid_text")
    return text


def validate_output_directory(raw_path: Path | str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise TTSValidationError("invalid_output_directory")
    try:
        info = path.lstat()
    except OSError as exc:
        raise TTSValidationError("invalid_output_directory") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise TTSValidationError("invalid_output_directory")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TTSValidationError("insecure_output_directory")
    return path.resolve(strict=True)


def validate_wav_output(path: Path, *, max_bytes: int = MAX_WAV_BYTES) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TTSTechnicalError("invalid_wav") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise TTSTechnicalError("invalid_wav")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise TTSTechnicalError("insecure_wav")
    if info.st_size == 0:
        raise TTSTechnicalError("empty_wav")
    if info.st_size > max_bytes:
        raise TTSTechnicalError("wav_too_large")
    try:
        with path.open("rb") as raw_audio:
            header = raw_audio.read(12)
        if (
            len(header) != 12
            or header[:4] != b"RIFF"
            or header[8:12] != b"WAVE"
            or struct.unpack("<I", header[4:8])[0] + 8 != info.st_size
        ):
            raise TTSTechnicalError("invalid_wav")
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            frame_count = audio.getnframes()
            if (
                channels <= 0
                or sample_width <= 0
                or audio.getframerate() <= 0
                or frame_count <= 0
                or audio.getcomptype() != "NONE"
            ):
                raise TTSTechnicalError("invalid_wav")
            frames = audio.readframes(frame_count)
            if len(frames) != frame_count * channels * sample_width:
                raise TTSTechnicalError("invalid_wav")
    except TTSTechnicalError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise TTSTechnicalError("invalid_wav") from exc
    return path
