from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import cast

from .base import (
    STTAttempt,
    STTProvider,
    STTProviderUnavailable,
    STTResult,
    STTTechnicalError,
    validate_audio_input,
    validate_language,
)


class STTChainError(STTTechnicalError):
    def __init__(self, attempts: tuple[STTAttempt, ...]) -> None:
        self.attempts = attempts
        super().__init__("stt_providers_exhausted")


class STTChain:
    """Fail-closed, strictly sequential configured-provider policy."""

    def __init__(self, *configured: STTProvider | tuple[STTProvider, ...]) -> None:
        providers = cast(tuple[STTProvider, ...], (
            tuple(configured[0])
            if len(configured) == 1 and isinstance(configured[0], tuple)
            else tuple(configured)
        ))
        if not providers or any(not getattr(provider, "name", "") for provider in providers):
            raise ValueError("at least one named STT provider is required")
        self._providers = providers

    async def transcribe(self, audio_path: Path | str, language: str) -> STTResult:
        path = validate_audio_input(audio_path)
        normalized_language = validate_language(language)
        attempts: list[STTAttempt] = []

        for provider in self._providers:
            started = time.perf_counter()
            try:
                raw_text = await provider.transcribe(path, normalized_language)
                circuit_state = str(getattr(raw_text, "circuit_state", "closed"))
                text = raw_text.strip()
            except asyncio.TimeoutError:
                attempts.append(
                    STTAttempt(provider.name, _elapsed_ms(started), "timeout", "provider_timeout")
                )
                continue
            except STTProviderUnavailable as exc:
                attempts.append(STTAttempt(
                    provider.name, _elapsed_ms(started), "unavailable", exc.code,
                    exc.circuit_state,
                ))
                continue
            except STTTechnicalError as exc:
                attempts.append(
                    STTAttempt(provider.name, _elapsed_ms(started), "technical_failure", exc.code)
                )
                continue

            if not text:
                attempts.append(STTAttempt(
                    provider.name, _elapsed_ms(started), "empty", "empty_transcript",
                    circuit_state,
                ))
                continue
            attempts.append(STTAttempt(
                provider.name, _elapsed_ms(started), "success",
                circuit_state=circuit_state,
            ))
            return STTResult(text=text, provider=provider.name, attempts=tuple(attempts))

        raise STTChainError(tuple(attempts))


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)
