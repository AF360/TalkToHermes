from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path

import pytest

from talktohermes.tts.base import SynthesizedAudio, TTSProviderUnavailable, TTSTechnicalError
from talktohermes.stt.base import STTProviderUnavailable, STTTechnicalError
from talktohermes.tts.quality import (
    BoundedLanguageVerifier,
    DeterministicTextPreparer,
    PreparedText,
    QualityOrchestrationError,
    QualityOrchestrator,
    QualityThresholds,
    canonical_words,
    combine_wav_segments,
    evaluate_quality,
)


def wav_bytes(*, seconds: float = 1.0, rate: int = 16_000, channels: int = 1, width: int = 2) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return stream.getvalue()


def private_wav(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "sample.wav"
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def test_canonical_words_equate_digits_and_german_number_compounds() -> None:
    assert canonical_words("7, 13; 21! 45? 30 9999", "de") == canonical_words(
        "sieben dreizehn einundzwanzig fünfundvierzig dreißig "
        "neuntausendneunhundertneunundneunzig", "de"
    )
    assert canonical_words("Straße – schön.") == ("strasse", "schön")


def test_canonical_words_equate_english_numbers_without_german_mapping() -> None:
    assert canonical_words("7, 13; 21! 45? 30, 105; 9999", "en-US") == canonical_words(
        "seven, thirteen; twenty one! forty-five? thirty, one hundred five; "
        "nine thousand nine hundred ninety nine",
        "en-US",
    )
    assert canonical_words("100. 5", "en") == canonical_words(
        "one hundred. five", "en"
    )
    assert canonical_words("sieben", "en") == ("sieben",)


def test_quality_keeps_real_omissions_and_repetitions_visible(tmp_path: Path) -> None:
    audio = private_wav(tmp_path, wav_bytes(seconds=1))

    omitted = evaluate_quality("das ist wirklich gut", "das ist gut", audio)
    repeated = evaluate_quality("das ist gut", "das ist ist gut", audio)

    assert not omitted.accepted and omitted.metrics.deletions == 1
    assert omitted.metrics.insertions == omitted.metrics.substitutions == 0
    assert not repeated.accepted and repeated.metrics.insertions == 1
    assert repeated.metrics.deletions == repeated.metrics.substitutions == 0


def test_quality_rejects_empty_audio_transcript_substitutions_and_excessive_wer(tmp_path: Path) -> None:
    empty_audio = private_wav(tmp_path, b"")
    assert evaluate_quality("hallo", "hallo", empty_audio).reason == "empty_wav"

    audio = private_wav(tmp_path, wav_bytes(seconds=1))
    assert evaluate_quality("hallo welt", "", audio).reason == "empty_transcript"
    substituted = evaluate_quality("hallo welt", "hallo mars", audio)
    assert substituted.reason == "word_error_rate"
    assert substituted.metrics.substitutions == 1
    assert substituted.metrics.word_error_rate == 0.5

    verifier_noise = evaluate_quality(
        "eins zwei drei vier fünf sechs sieben acht neun zehn elf",
        "eins zwei drei vier fünf sechs sieben acht neun zehn zwölf",
        audio,
    )
    assert verifier_noise.accepted
    assert verifier_noise.metrics.substitutions == 1
    assert verifier_noise.metrics.word_error_rate < 0.10

    wer_only = evaluate_quality(
        "eins zwei drei vier",
        "falsch zwei falsch vier",
        audio,
        QualityThresholds(max_substitutions=4, max_word_error_rate=0.25),
    )
    assert wer_only.reason == "word_error_rate"
    assert wer_only.metrics.word_error_rate == 0.5


@pytest.mark.parametrize(
    ("seconds", "reason"),
    [(0.05, "duration_too_short"), (8.0, "duration_too_long")],
)
def test_quality_rejects_implausible_duration_with_explicit_bounds(
    tmp_path: Path, seconds: float, reason: str
) -> None:
    thresholds = QualityThresholds(
        min_duration_seconds=0.1,
        max_duration_seconds=10.0,
        min_seconds_per_word=0.1,
        max_seconds_per_word=2.0,
    )
    audio = private_wav(tmp_path, wav_bytes(seconds=seconds))

    report = evaluate_quality("eins zwei drei", "eins zwei drei", audio, thresholds)

    assert not report.accepted
    assert report.reason == reason


@pytest.mark.asyncio
async def test_deterministic_preparer_normalizes_and_splits_without_changing_canonical_order() -> None:
    calls: list[str] = []

    async def normalize(text: str) -> str:
        calls.append(text)
        return "Dr. Müller kommt heute. Danach geht er z. B. nach Hause!"

    prepared = await DeterministicTextPreparer(normalize, max_segment_chars=28).prepare(" raw ")

    assert calls == [" raw "]
    assert prepared.normalized_text == "Dr. Müller kommt heute. Danach geht er z. B. nach Hause!"
    assert all(len(segment) <= 28 for segment in prepared.segments)
    assert canonical_words("".join(prepared.segments)) == canonical_words(prepared.normalized_text)
    assert prepared.segments[0].startswith("Dr. Müller")
    assert any("z. B." in segment for segment in prepared.segments)


@pytest.mark.asyncio
async def test_deterministic_preparer_hard_splits_an_overlong_token_without_omission() -> None:
    text = "Donaudampfschifffahrtsgesellschaft und Ende."

    async def normalize(_: str) -> str:
        return text

    prepared = await DeterministicTextPreparer(normalize, max_segment_chars=10).prepare("raw")

    assert all(0 < len(segment) <= 10 for segment in prepared.segments)
    assert "".join(prepared.segments) == text


@pytest.mark.asyncio
async def test_deterministic_preparer_matches_hermes_sentence_chunker_contract() -> None:
    text = "Ha! Das ist der erste längere Satz.\n\nNächster Absatz"

    async def normalize(_: str) -> str:
        return text

    prepared = await DeterministicTextPreparer(normalize).prepare("raw")

    assert prepared.segments == (
        "Ha! Das ist der erste längere Satz.",
        "Nächster Absatz",
    )


class FakeSTTProvider:
    def __init__(self, result: str) -> None:
        self.result = result
        self.calls: list[tuple[Path, str]] = []

    async def transcribe(self, audio_path: Path, language: str) -> str:
        self.calls.append((audio_path, language))
        return self.result


@pytest.mark.asyncio
async def test_bounded_verifier_uses_requested_language_and_bounds_transcript(
    tmp_path: Path,
) -> None:
    audio = private_wav(tmp_path, wav_bytes())
    provider = FakeSTTProvider("  Hallo Welt  ")
    verifier = BoundedLanguageVerifier(provider, timeout_seconds=1, max_transcript_chars=20)

    assert await verifier.transcribe(audio, "en-US") == "Hallo Welt"
    assert provider.calls == [(audio, "en-US")]

    provider.result = "x" * 21
    with pytest.raises(TTSTechnicalError, match="transcript_too_large"):
        await verifier.transcribe(audio, "en-US")

    async def failed_transcription(_audio_path: Path, _language: str) -> str:
        raise STTTechnicalError("omnivoice_transport_failed")

    provider.transcribe = failed_transcription  # type: ignore[method-assign]
    with pytest.raises(TTSTechnicalError, match="verifier_failed"):
        await verifier.transcribe(audio, "en-US")

    async def unavailable_transcription(_audio_path: Path, _language: str) -> str:
        raise STTProviderUnavailable("openai_provider_unavailable", "open")

    provider.transcribe = unavailable_transcription  # type: ignore[method-assign]
    with pytest.raises(TTSProviderUnavailable) as unavailable:
        await verifier.transcribe(audio, "en-US")
    assert unavailable.value.code == "verifier_provider_unavailable"
    assert unavailable.value.circuit_state == "open"


class FakePreparer:
    def __init__(self, *segments: str) -> None:
        self.segments = segments

    async def prepare(self, text: str) -> PreparedText:
        return PreparedText(" ".join(self.segments) or text.strip(), self.segments)


class FakeProvider:
    def __init__(
        self,
        name: str,
        voice: str,
        calls: list[tuple[str, str]],
        outcomes: list[bytes | BaseException] | None = None,
        circuit_state: str = "closed",
    ) -> None:
        self.name = name
        self.voice = voice
        self.calls = calls
        self.outcomes = list(outcomes or [])
        self.circuit_state = circuit_state

    async def synthesize(
        self, text: str, output_path: Path
    ) -> SynthesizedAudio:
        self.calls.append((self.voice, text))
        outcome = self.outcomes.pop(0) if self.outcomes else wav_bytes(seconds=0.5)
        if isinstance(outcome, BaseException):
            output_path.write_bytes(b"failed")
            raise outcome
        output_path.write_bytes(outcome)
        return SynthesizedAudio(output_path, circuit_state=self.circuit_state)


class FakeVerifier:
    def __init__(self, transcripts: list[str | BaseException]) -> None:
        self.transcripts = list(transcripts)
        self.calls: list[tuple[Path, str]] = []

    async def transcribe(self, audio_path: Path, language: str = "de") -> str:
        self.calls.append((audio_path, language))
        outcome = self.transcripts.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def private_output_dir(tmp_path: Path) -> Path:
    output = tmp_path / "audio"
    output.mkdir(mode=0o700, parents=True)
    output.chmod(0o700)
    return output


def orchestrator(
    segments: tuple[str, ...],
    calls: list[tuple[str, str]],
    transcripts: list[str | BaseException],
    *,
    omni: list[bytes | BaseException] | None = None,
    thorsten: list[bytes | BaseException] | None = None,
    ramona: list[bytes | BaseException] | None = None,
    thresholds: QualityThresholds | None = None,
    omni_circuit_state: str = "closed",
) -> QualityOrchestrator:
    return QualityOrchestrator(
        FakePreparer(*segments),
        FakeProvider(
            "omnivoice", "voice-02", calls, omni,
            circuit_state=omni_circuit_state,
        ),
        FakeProvider("wyoming-piper", "de_DE-thorsten-medium", calls, thorsten),
        FakeProvider("piper", "de_DE-ramona-low", calls, ramona),
        FakeVerifier(transcripts),
        thresholds=thresholds,
    )


@pytest.mark.asyncio
async def test_omni_retries_a_failed_segment_once_then_returns_one_combined_wav(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    output_dir = private_output_dir(tmp_path)
    pipeline = orchestrator(("Hallo Welt",), calls, ["Hallo", "Hallo Welt"])

    result = await pipeline.synthesize("raw response", output_dir)

    assert calls == [("voice-02", "Hallo Welt"), ("voice-02", "Hallo Welt")]
    assert result.provider == "omnivoice" and result.voice == "voice-02"
    assert [report.accepted for report in result.reports] == [True]
    assert result.audio_path.stat().st_mode & 0o777 == 0o600
    assert list(output_dir.iterdir()) == [result.audio_path]


@pytest.mark.asyncio
async def test_omni_connectivity_failure_falls_back_without_retry(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    pipeline = orchestrator(
        ("Hallo",), calls, [],
        omni=[TTSProviderUnavailable("omnivoice_provider_unavailable", "closed")],
    )

    result = await pipeline.synthesize("raw", private_output_dir(tmp_path))

    assert result.provider == "wyoming-piper"
    assert calls == [("voice-02", "Hallo"), ("de_DE-thorsten-medium", "Hallo")]
    assert result.attempts[0].reason == "omnivoice_provider_unavailable"
    assert result.attempts[0].outcome == "unavailable"
    assert result.attempts[0].circuit_state == "closed"
    assert result.attempts[0].elapsed_ms >= 0


@pytest.mark.asyncio
async def test_omni_success_preserves_half_open_circuit_state(tmp_path: Path) -> None:
    pipeline = orchestrator(
        ("Hallo",), [], ["Hallo"], omni_circuit_state="half_open"
    )

    result = await pipeline.synthesize("raw", private_output_dir(tmp_path))

    assert result.provider == "omnivoice"
    assert result.attempts[0].outcome == "success"
    assert result.attempts[0].circuit_state == "half_open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [asyncio.TimeoutError(), TTSTechnicalError("service_unavailable")]
)
async def test_timeout_or_technical_failure_falls_back_without_omni_retry(
    tmp_path: Path, failure: BaseException
) -> None:
    calls: list[tuple[str, str]] = []
    pipeline = orchestrator(
        ("Hallo",), calls, ["Hallo"], omni=[failure, wav_bytes(seconds=0.5)]
    )

    result = await pipeline.synthesize("raw", private_output_dir(tmp_path))

    assert result.voice == "de_DE-thorsten-medium"
    assert calls == [("voice-02", "Hallo"), ("de_DE-thorsten-medium", "Hallo")]
    assert result.attempts[0].reason in {"timeout", "service_unavailable"}


@pytest.mark.asyncio
async def test_omni_failure_regenerates_the_whole_answer_with_one_consistent_thorsten_voice(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    output_dir = private_output_dir(tmp_path)
    pipeline = orchestrator(
        ("Erster Satz", "Zweiter Satz"),
        calls,
        ["Erster Satz", "falsch", "noch falsch", "Erster Satz", "Zweiter Satz"],
    )

    result = await pipeline.synthesize("raw", output_dir)

    assert calls == [
        ("voice-02", "Erster Satz"),
        ("voice-02", "Zweiter Satz"),
        ("voice-02", "Zweiter Satz"),
        ("de_DE-thorsten-medium", "Erster Satz"),
        ("de_DE-thorsten-medium", "Zweiter Satz"),
    ]
    assert result.voice == "de_DE-thorsten-medium"
    assert {attempt.voice for attempt in result.attempts if attempt.accepted} == {
        "voice-02", "de_DE-thorsten-medium"
    }
    assert list(output_dir.iterdir()) == [result.audio_path]


@pytest.mark.asyncio
async def test_thorsten_failure_regenerates_the_whole_answer_with_ramona(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    pipeline = orchestrator(
        ("Eins", "Zwei"),
        calls,
        ["falsch", "falsch"],
        thorsten=[TTSTechnicalError("service_unavailable")],
    )

    result = await pipeline.synthesize("raw", private_output_dir(tmp_path))

    assert calls == [
        ("voice-02", "Eins"), ("voice-02", "Eins"),
        ("de_DE-thorsten-medium", "Eins"),
        ("de_DE-ramona-low", "Eins"), ("de_DE-ramona-low", "Zwei"),
    ]
    assert result.voice == "de_DE-ramona-low"


@pytest.mark.asyncio
async def test_piper_fallback_accepts_valid_bounded_wav_without_stt_gate(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    verifier = FakeVerifier(["falsch", "falsch"])
    pipeline = QualityOrchestrator(
        FakePreparer("Hallo"),
        FakeProvider("omnivoice", "voice-02", calls),
        FakeProvider("wyoming-piper", "de_DE-thorsten-medium", calls),
        FakeProvider("piper", "de_DE-ramona-low", calls),
        verifier,
    )

    result = await pipeline.synthesize("raw", private_output_dir(tmp_path))

    assert result.voice == "de_DE-thorsten-medium"
    assert len(verifier.calls) == 2
    assert result.reports == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segments",
    [("eins", "zwei", "drei"), ("eins", "eins"), ("zwei", "eins")],
)
async def test_orchestrator_rejects_omitted_duplicated_or_reordered_segments(
    tmp_path: Path, segments: tuple[str, ...]
) -> None:
    calls: list[tuple[str, str]] = []
    pipeline = orchestrator(segments, calls, [])
    pipeline._preparer = FakePreparer(*segments)  # type: ignore[attr-defined]

    async def invalid_prepare(_: str) -> PreparedText:
        return PreparedText("eins zwei", segments)

    pipeline._preparer.prepare = invalid_prepare  # type: ignore[method-assign,attr-defined]
    with pytest.raises(ValueError, match="canonical"):
        await pipeline.synthesize("raw", private_output_dir(tmp_path))
    assert calls == []


@pytest.mark.asyncio
async def test_orchestration_applies_preparer_provider_and_verifier_deadlines(tmp_path: Path) -> None:
    class SlowPreparer:
        async def prepare(self, text: str) -> PreparedText:
            await asyncio.sleep(1)
            return PreparedText(text, (text,))

    calls: list[tuple[str, str]] = []
    limits = QualityThresholds(
        preparer_timeout_seconds=0.01,
        provider_timeout_seconds=0.01,
        verifier_timeout_seconds=0.01,
    )
    pipeline = QualityOrchestrator(
        SlowPreparer(),
        FakeProvider("omnivoice", "voice-02", calls),
        FakeProvider("wyoming-piper", "de_DE-thorsten-medium", calls),
        FakeProvider("piper", "de_DE-ramona-low", calls),
        FakeVerifier([]),
        thresholds=limits,
    )
    with pytest.raises(asyncio.TimeoutError):
        await pipeline.synthesize("raw", private_output_dir(tmp_path))


@pytest.mark.asyncio
async def test_aggregate_artifacts_are_bounded_and_combine_failure_is_recorded(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    limits = QualityThresholds(max_wav_bytes=40_000, max_total_artifact_bytes=40_000)
    pipeline = orchestrator(
        ("Eins", "Zwei"), calls, ["Eins", "Zwei"],
        omni=[wav_bytes(seconds=0.1, rate=16_000), wav_bytes(seconds=0.1, rate=22_050)],
        thresholds=limits,
    )

    result = await pipeline.synthesize("raw", private_output_dir(tmp_path))

    assert result.voice == "de_DE-thorsten-medium"
    assert any(attempt.reason == "incompatible_wav" for attempt in result.attempts)

    too_small = QualityThresholds(max_wav_bytes=20_000, max_total_artifact_bytes=25_000)
    exhausted = orchestrator(("Eins", "Zwei"), [], ["Eins", "Zwei"], thresholds=too_small)
    with pytest.raises(QualityOrchestrationError) as caught:
        await exhausted.synthesize("raw", private_output_dir(tmp_path / "bounded"))
    assert any(attempt.reason == "artifact_bytes_exceeded" for attempt in caught.value.attempts)


@pytest.mark.asyncio
async def test_cancellation_propagates_and_cleans_every_artifact(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    output_dir = private_output_dir(tmp_path)
    pipeline = orchestrator(("Hallo",), calls, [], omni=[asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await pipeline.synthesize("raw", output_dir)

    assert calls == [("voice-02", "Hallo")]
    assert list(output_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_programming_errors_propagate_without_fallback_and_cleanup(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    output_dir = private_output_dir(tmp_path)
    pipeline = orchestrator(("Hallo",), calls, [], omni=[ValueError("bug")])

    with pytest.raises(ValueError, match="bug"):
        await pipeline.synthesize("raw", output_dir)

    assert calls == [("voice-02", "Hallo")]
    assert list(output_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_prepared_segments_must_be_nonempty_and_bounded(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    limits = QualityThresholds(max_segments=2, max_segment_chars=5)
    for index, segments in enumerate(((), ("",), ("123456",), ("a", "b", "c"))):
        pipeline = orchestrator(segments, calls, [], thresholds=limits)
        with pytest.raises(ValueError, match="segments"):
            await pipeline.synthesize("raw", private_output_dir(tmp_path / str(index)))
    assert calls == []


def test_wav_combination_requires_compatible_pcm_and_enforces_bound(tmp_path: Path) -> None:
    output_dir = private_output_dir(tmp_path)
    first = private_wav(tmp_path, wav_bytes(seconds=0.1, rate=16_000))
    second = tmp_path / "second.wav"
    second.write_bytes(wav_bytes(seconds=0.1, rate=22_050))
    second.chmod(0o600)

    with pytest.raises(TTSTechnicalError, match="incompatible_wav"):
        combine_wav_segments((first, second), output_dir, max_bytes=100_000)
    assert list(output_dir.iterdir()) == []

    second.write_bytes(wav_bytes(seconds=0.1, rate=16_000))
    combined = combine_wav_segments((first, second), output_dir, max_bytes=100_000)
    with wave.open(str(combined), "rb") as audio:
        assert audio.getframerate() == 16_000
        assert audio.getnframes() == 3_200
    assert combined.stat().st_mode & 0o777 == 0o600
    combined.unlink()

    with pytest.raises(TTSTechnicalError, match="wav_too_large"):
        combine_wav_segments((first, second), output_dir, max_bytes=100)
    assert list(output_dir.iterdir()) == []


def test_package_exports_quality_gate() -> None:
    from talktohermes.tts import (
        BoundedLanguageVerifier as ExportedVerifier,
        DeterministicTextPreparer as ExportedPreparer,
        QualityOrchestrator as ExportedOrchestrator,
        QualityThresholds as ExportedThresholds,
    )

    assert ExportedVerifier is BoundedLanguageVerifier
    assert ExportedPreparer is DeterministicTextPreparer
    assert ExportedOrchestrator is QualityOrchestrator
    assert ExportedThresholds is QualityThresholds
