from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
MAX_AUDIO_BYTES = 10 * 1024 * 1024


class STTError(RuntimeError):
    """Base class for controlled STT failures with a non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class STTValidationError(STTError):
    pass


class STTTechnicalError(STTError):
    pass


class STTProviderUnavailable(STTTechnicalError):
    def __init__(self, code: str, circuit_state: str) -> None:
        self.circuit_state = circuit_state
        super().__init__(code)


class STTTranscript(str):
    circuit_state: str

    def __new__(cls, value: str, circuit_state: str) -> "STTTranscript":
        transcript = super().__new__(cls, value)
        transcript.circuit_state = circuit_state
        return transcript


AttemptOutcome = Literal["success", "technical_failure", "timeout", "empty", "unavailable"]


@dataclass(frozen=True, slots=True)
class STTAttempt:
    provider: str
    elapsed_ms: float
    outcome: AttemptOutcome
    error_code: str | None = None
    circuit_state: str = "closed"


@dataclass(frozen=True, slots=True)
class STTResult:
    text: str
    provider: str
    attempts: tuple[STTAttempt, ...]


class STTProvider(Protocol):
    name: str

    async def transcribe(self, audio_path: Path, language: str) -> str: ...


def validate_language(language: str) -> str:
    value = language.strip().replace("_", "-") if isinstance(language, str) else ""
    if LANGUAGE_TAG.fullmatch(value) is None:
        raise STTValidationError("invalid_language")
    parts = value.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def validate_audio_input(audio_path: Path | str, *, max_bytes: int = MAX_AUDIO_BYTES) -> Path:
    path = Path(audio_path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise STTValidationError("invalid_audio_input") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise STTValidationError("invalid_audio_input")
    if info.st_size <= 0:
        raise STTValidationError("empty_audio_input")
    if info.st_size > max_bytes:
        raise STTValidationError("audio_too_large")
    if info.st_uid != os.getuid():
        raise STTValidationError("invalid_audio_owner")
    return path.resolve(strict=True)
