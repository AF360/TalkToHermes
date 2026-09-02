from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import cast

from .base import (
    AttemptOutcome,
    MAX_WAV_BYTES,
    TTSAttempt,
    TTSProvider,
    TTSResult,
    TTSTechnicalError,
    validate_output_directory,
    validate_text,
    validate_wav_output,
)

class TTSChainError(TTSTechnicalError):
    def __init__(self, attempts: tuple[TTSAttempt, ...]) -> None:
        self.attempts = attempts
        super().__init__("tts_providers_exhausted")


class TTSChain:
    """Strictly sequential configured-provider policy."""

    def __init__(
        self,
        *configured: TTSProvider | tuple[TTSProvider, ...],
        max_wav_bytes: int = MAX_WAV_BYTES,
    ) -> None:
        providers = cast(tuple[TTSProvider, ...], (
            tuple(configured[0])
            if len(configured) == 1 and isinstance(configured[0], tuple)
            else tuple(configured)
        ))
        if not providers or any(
            not getattr(provider, "name", "") or not getattr(provider, "voice", "")
            for provider in providers
        ):
            raise ValueError("at least one named TTS provider with a voice is required")
        if max_wav_bytes <= 0:
            raise ValueError("max_wav_bytes must be positive")
        self._providers = providers
        self._max_wav_bytes = max_wav_bytes

    async def synthesize(self, text: str, output_dir: Path | str) -> TTSResult:
        validated_text = validate_text(text)
        directory = validate_output_directory(output_dir)
        attempts: list[TTSAttempt] = []

        for provider in self._providers:
            attempt_path = _private_attempt_file(directory)
            started = time.perf_counter()
            try:
                returned = await provider.synthesize(validated_text, attempt_path)
                circuit_state = str(getattr(returned, "circuit_state", "closed"))
                if Path(returned) != attempt_path:
                    raise TTSTechnicalError("unexpected_output_path")
                validate_wav_output(attempt_path, max_bytes=self._max_wav_bytes)
            except asyncio.TimeoutError:
                _remove(attempt_path)
                attempts.append(
                    TTSAttempt(provider.name, provider.voice, _elapsed_ms(started), "timeout", "provider_timeout")
                )
                continue
            except TTSTechnicalError as exc:
                _remove(attempt_path)
                outcome = _failure_outcome(exc.code)
                attempts.append(
                    TTSAttempt(provider.name, provider.voice, _elapsed_ms(started), outcome, exc.code)
                )
                continue
            except BaseException:
                _remove(attempt_path)
                raise

            attempts.append(
                TTSAttempt(
                    provider.name, provider.voice, _elapsed_ms(started), "success",
                    circuit_state=circuit_state,
                )
            )
            return TTSResult(attempt_path, provider.name, provider.voice, tuple(attempts))

        raise TTSChainError(tuple(attempts))


def _private_attempt_file(directory: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix="tts-", suffix=".wav", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return Path(raw_path)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)


def _failure_outcome(code: str) -> AttemptOutcome:
    if code == "empty_wav":
        return "empty"
    if code in {"invalid_wav", "insecure_wav", "unexpected_output_path"}:
        return "invalid"
    if code == "wav_too_large":
        return "oversized"
    return "technical_failure"
