from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from talktohermes.stt.base import (
    STTProviderUnavailable,
    STTTechnicalError,
    STTTranscript,
    STTValidationError,
)
from talktohermes.stt.chain import STTChain, STTChainError


class FakeProvider:
    def __init__(self, name: str, outcomes: list[str | BaseException], calls: list[str]) -> None:
        self.name = name
        self._outcomes = outcomes
        self._calls = calls

    async def transcribe(self, audio_path: Path, language: str) -> str:
        self._calls.append(self.name)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "speech.wav"
    path.write_bytes(b"RIFFspeech")
    return path


def chain_with(outcomes: list[list[str | BaseException]], calls: list[str]) -> STTChain:
    return STTChain(
        FakeProvider("openai", outcomes[0], calls),
        FakeProvider("wyoming", outcomes[1], calls),
        FakeProvider("local", outcomes[2], calls),
    )


@pytest.mark.asyncio
async def test_chain_stops_after_first_nonempty_transcript_and_records_timing(tmp_path: Path) -> None:
    calls: list[str] = []
    result = await chain_with([[" Hallo "], ["unused"], ["unused"]], calls).transcribe(
        audio_file(tmp_path), "de"
    )

    assert calls == ["openai"]
    assert result.text == "Hallo"
    assert result.provider == "openai"
    assert [(attempt.provider, attempt.outcome) for attempt in result.attempts] == [("openai", "success")]
    assert result.attempts[0].elapsed_ms >= 0


@pytest.mark.asyncio
async def test_chain_falls_back_strictly_sequentially_on_technical_timeout_or_empty(tmp_path: Path) -> None:
    calls: list[str] = []
    chain = chain_with(
        [[STTTechnicalError("openai_unavailable")], [asyncio.TimeoutError()], [" Lokal "]], calls
    )

    result = await chain.transcribe(audio_file(tmp_path), "de")

    assert calls == ["openai", "wyoming", "local"]
    assert result.provider == "local"
    assert [attempt.outcome for attempt in result.attempts] == [
        "technical_failure",
        "timeout",
        "success",
    ]
    assert [attempt.error_code for attempt in result.attempts] == [
        "openai_unavailable",
        "provider_timeout",
        None,
    ]


@pytest.mark.asyncio
async def test_provider_unavailable_attempt_records_safe_circuit_metadata(tmp_path: Path) -> None:
    calls: list[str] = []
    chain = chain_with(
        [[STTProviderUnavailable("openai_provider_unavailable", "open")], ["fallback"], ["unused"]],
        calls,
    )
    result = await chain.transcribe(audio_file(tmp_path), "de")
    first = result.attempts[0]
    assert (first.outcome, first.error_code, first.circuit_state) == (
        "unavailable", "openai_provider_unavailable", "open"
    )
    assert first.elapsed_ms >= 0


@pytest.mark.asyncio
async def test_success_attempt_preserves_half_open_circuit_state(tmp_path: Path) -> None:
    calls: list[str] = []
    result = await chain_with(
        [[STTTranscript("primary", "half_open")], ["unused"], ["unused"]], calls
    ).transcribe(audio_file(tmp_path), "de")

    assert result.attempts[0].outcome == "success"
    assert result.attempts[0].circuit_state == "half_open"


@pytest.mark.asyncio
async def test_empty_attempt_preserves_half_open_circuit_state(tmp_path: Path) -> None:
    calls: list[str] = []
    result = await chain_with(
        [[STTTranscript("   ", "half_open")], ["fallback"], ["unused"]], calls
    ).transcribe(audio_file(tmp_path), "de")

    assert result.attempts[0].outcome == "empty"
    assert result.attempts[0].circuit_state == "half_open"


@pytest.mark.asyncio
async def test_empty_transcript_is_a_fallback_condition(tmp_path: Path) -> None:
    calls: list[str] = []
    result = await chain_with([["  "], ["fallback voice server"], ["unused"]], calls).transcribe(
        audio_file(tmp_path), "de"
    )

    assert calls == ["openai", "wyoming"]
    assert [attempt.outcome for attempt in result.attempts] == ["empty", "success"]


@pytest.mark.asyncio
async def test_unexpected_provider_error_fails_closed_without_fallback(tmp_path: Path) -> None:
    calls: list[str] = []
    chain = chain_with([[RuntimeError("programming bug")], ["must not run"], ["must not run"]], calls)

    with pytest.raises(RuntimeError, match="programming bug"):
        await chain.transcribe(audio_file(tmp_path), "de")
    assert calls == ["openai"]


@pytest.mark.asyncio
async def test_cancellation_propagates_without_fallback(tmp_path: Path) -> None:
    calls: list[str] = []
    chain = chain_with([[asyncio.CancelledError()], ["must not run"], ["must not run"]], calls)

    with pytest.raises(asyncio.CancelledError):
        await chain.transcribe(audio_file(tmp_path), "de")
    assert calls == ["openai"]


@pytest.mark.asyncio
async def test_chain_error_preserves_safe_attempt_metadata(tmp_path: Path) -> None:
    calls: list[str] = []
    chain = chain_with(
        [[STTTechnicalError("openai_failed")], [""], [STTTechnicalError("local_failed")]], calls
    )

    with pytest.raises(STTChainError) as caught:
        await chain.transcribe(audio_file(tmp_path), "de")

    assert calls == ["openai", "wyoming", "local"]
    assert [attempt.provider for attempt in caught.value.attempts] == ["openai", "wyoming", "local"]
    assert "speech" not in str(caught.value)


@pytest.mark.asyncio
async def test_chain_accepts_language_tags_and_rejects_invalid_language_or_input(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    chain = chain_with([["English text"], ["unused"], ["unused"]], calls)
    audio = audio_file(tmp_path)

    result = await chain.transcribe(audio, "en-US")
    assert result.text == "English text"
    assert calls == ["openai"]

    with pytest.raises(STTValidationError, match="invalid_language"):
        await chain.transcribe(audio, "de; rm -rf /")

    link = tmp_path / "linked.wav"
    link.symlink_to(audio)
    with pytest.raises(STTValidationError, match="invalid_audio_input"):
        await chain.transcribe(link, "de")

    assert calls == ["openai"]


def test_chain_requires_at_least_one_provider_and_preserves_order() -> None:
    with pytest.raises(ValueError, match="at least one"):
        STTChain()

    calls: list[str] = []
    providers = (
        FakeProvider("openai", [""], calls),
        FakeProvider("local", [""], calls),
    )
    assert STTChain(providers)._providers == providers
