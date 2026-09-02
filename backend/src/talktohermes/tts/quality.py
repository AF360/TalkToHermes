from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import unicodedata
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from talktohermes.stt.base import STTProviderUnavailable, STTTechnicalError


from .base import (
    MAX_TEXT_CHARS,
    SynthesizedAudio,
    TTSProviderUnavailable,
    TTSTechnicalError,
    validate_output_directory,
    validate_text,
    validate_wav_output,
)


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    """Conservative, explicit acceptance and resource bounds."""

    max_word_error_rate: float = 0.20
    max_deletions: int = 0
    max_insertions: int = 0
    # The independent STT verifier can confuse one acoustically similar word,
    # especially for fast cloned voices. Keep omissions/insertions fail-closed,
    # but tolerate one substitution only when the global WER still stays <= 20%.
    max_substitutions: int = 1
    min_duration_seconds: float = 0.08
    max_duration_seconds: float = 300.0
    min_seconds_per_word: float = 0.04
    max_seconds_per_word: float = 3.0
    max_segments: int = 64
    max_segment_chars: int = 500
    max_wav_bytes: int = 32 * 1024 * 1024
    max_total_artifact_bytes: int = 32 * 1024 * 1024
    max_transcript_chars: int = 8_000
    preparer_timeout_seconds: float = 15.0
    provider_timeout_seconds: float = 120.0
    verifier_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if (
            not 0 <= self.max_word_error_rate <= 1
            or min(self.max_deletions, self.max_insertions, self.max_substitutions) < 0
            or self.min_duration_seconds <= 0
            or self.max_duration_seconds < self.min_duration_seconds
            or self.min_seconds_per_word <= 0
            or self.max_seconds_per_word < self.min_seconds_per_word
            or self.max_segments <= 0
            or self.max_segment_chars <= 0
            or self.max_wav_bytes <= 44
            or self.max_total_artifact_bytes <= 44
            or self.max_transcript_chars <= 0
            or min(
                self.preparer_timeout_seconds,
                self.provider_timeout_seconds,
                self.verifier_timeout_seconds,
            ) <= 0
        ):
            raise ValueError("invalid quality thresholds")


@dataclass(frozen=True, slots=True)
class QualityMetrics:
    expected_words: int
    actual_words: int
    substitutions: int
    deletions: int
    insertions: int
    word_error_rate: float
    substitution_ratio: float
    deletion_ratio: float
    insertion_ratio: float

    @property
    def edits(self) -> int:
        return self.substitutions + self.deletions + self.insertions


@dataclass(frozen=True, slots=True)
class QualityReport:
    accepted: bool
    reason: str | None
    metrics: QualityMetrics
    duration_seconds: float


_ONES = (
    "null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun"
)
_TEENS = {
    10: "zehn", 11: "elf", 12: "zwölf", 13: "dreizehn", 14: "vierzehn",
    15: "fünfzehn", 16: "sechzehn", 17: "siebzehn", 18: "achtzehn", 19: "neunzehn",
}
_TENS = {20: "zwanzig", 30: "dreißig", 40: "vierzig", 50: "fünfzig", 60: "sechzig", 70: "siebzig", 80: "achtzig", 90: "neunzig"}


def _under_hundred(number: int, *, compound: bool = False) -> str:
    if number < 10:
        if number == 1 and compound:
            return "ein"
        return _ONES[number]
    if number < 20:
        return _TEENS[number]
    tens, one = divmod(number, 10)
    if one == 0:
        return _TENS[tens * 10]
    return _under_hundred(one, compound=True) + "und" + _TENS[tens * 10]


def _german_number(number: int) -> str:
    if number < 100:
        return _under_hundred(number)
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        prefix = ("ein" if hundreds == 1 else _under_hundred(hundreds)) + "hundert"
        return prefix + (_under_hundred(rest) if rest else "")
    thousands, rest = divmod(number, 1000)
    prefix = ("ein" if thousands == 1 else _under_hundred(thousands)) + "tausend"
    return prefix + (_german_number(rest) if rest else "")


def _number_words() -> dict[str, str]:
    words: dict[str, str] = {}
    for number in range(10_000):
        spelling = unicodedata.normalize("NFKC", _german_number(number)).casefold()
        words[spelling] = f"#number:{number}"
        if 100 <= number < 200:
            words[spelling.removeprefix("ein")] = f"#number:{number}"
        if 1000 <= number < 2000:
            words[spelling.removeprefix("ein")] = f"#number:{number}"
    words["ein"] = "#number:1"
    return words


_NUMBER_WORDS = _number_words()
_ENGLISH_SMALL = {
    word: number
    for number, word in enumerate(
        (
            "zero", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
            "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
        )
    )
}
_ENGLISH_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _english_number(words: list[str], start: int) -> tuple[int, int] | None:
    total = 0
    current = 0
    index = start
    last: str | None = None
    while index < len(words):
        word = words[index]
        if word in _ENGLISH_SMALL:
            value = _ENGLISH_SMALL[word]
            if last == "tens" and value < 10:
                current += value
            elif last in {None, "hundred", "thousand"}:
                current += value
            else:
                break
            last = "small"
        elif word in _ENGLISH_TENS:
            if last not in {None, "hundred", "thousand"}:
                break
            current += _ENGLISH_TENS[word]
            last = "tens"
        elif word == "hundred":
            if last != "small" or not 1 <= current <= 9:
                break
            current *= 100
            last = "hundred"
        elif word == "thousand":
            if current <= 0 or total:
                break
            total = current * 1000
            current = 0
            last = "thousand"
        else:
            break
        index += 1
    if index == start:
        return None
    return total + current, index - start


def canonical_words(text: str, language: str = "de") -> tuple[str, ...]:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    primary_language = language.split("-", 1)[0].lower()
    canonical: list[str] = []
    for chunk in re.split(r"[,;:.!?]+", normalized_text):
        words = _WORD_RE.findall(chunk)
        index = 0
        while index < len(words):
            word = words[index]
            if word.isdecimal():
                number = int(word)
                canonical.append(f"#number:{number}" if number <= 9999 else str(number))
                index += 1
                continue
            if primary_language == "en":
                parsed = _english_number(words, index)
                if parsed is not None:
                    number, consumed = parsed
                    canonical.append(f"#number:{number}" if number <= 9999 else str(number))
                    index += consumed
                    continue
            number_word = _NUMBER_WORDS.get(word) if primary_language == "de" else None
            canonical.append(word if number_word is None else number_word)
            index += 1
    return tuple(canonical)


def _edit_counts(expected: tuple[str, ...], actual: tuple[str, ...]) -> tuple[int, int, int]:
    # Each cell is (cost, substitutions, deletions, insertions). Candidate order
    # makes equal-cost alignments deterministic and prefers substitution.
    previous = [(index, 0, 0, index) for index in range(len(actual) + 1)]
    for expected_index, expected_word in enumerate(expected, 1):
        current = [(expected_index, 0, expected_index, 0)]
        for actual_index, actual_word in enumerate(actual, 1):
            if expected_word == actual_word:
                current.append(previous[actual_index - 1])
                continue
            diagonal = previous[actual_index - 1]
            delete = previous[actual_index]
            insert = current[actual_index - 1]
            candidates = (
                (diagonal[0] + 1, diagonal[1] + 1, diagonal[2], diagonal[3]),
                (delete[0] + 1, delete[1], delete[2] + 1, delete[3]),
                (insert[0] + 1, insert[1], insert[2], insert[3] + 1),
            )
            current.append(min(candidates, key=lambda item: item[0]))
        previous = current
    _, substitutions, deletions, insertions = previous[-1]
    return substitutions, deletions, insertions


def _metrics(expected: tuple[str, ...], actual: tuple[str, ...]) -> QualityMetrics:
    substitutions, deletions, insertions = _edit_counts(expected, actual)
    denominator = max(1, len(expected))
    return QualityMetrics(
        expected_words=len(expected),
        actual_words=len(actual),
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
        word_error_rate=(substitutions + deletions + insertions) / denominator,
        substitution_ratio=substitutions / denominator,
        deletion_ratio=deletions / denominator,
        insertion_ratio=insertions / denominator,
    )


def evaluate_quality(
    expected_text: str,
    transcript: str,
    audio_path: Path,
    thresholds: QualityThresholds | None = None,
    language: str = "de",
) -> QualityReport:
    limits = thresholds or QualityThresholds()
    expected = canonical_words(expected_text, language)
    actual = canonical_words(transcript, language)
    metrics = _metrics(expected, actual)
    if not expected:
        return QualityReport(False, "empty_expected", metrics, 0.0)
    if not actual:
        return QualityReport(False, "empty_transcript", metrics, 0.0)
    try:
        validate_wav_output(audio_path, max_bytes=limits.max_wav_bytes)
        with wave.open(str(audio_path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
    except TTSTechnicalError as exc:
        return QualityReport(False, exc.code, metrics, 0.0)
    if duration < limits.min_duration_seconds or duration < len(expected) * limits.min_seconds_per_word:
        return QualityReport(False, "duration_too_short", metrics, duration)
    if duration > limits.max_duration_seconds or duration > len(expected) * limits.max_seconds_per_word:
        return QualityReport(False, "duration_too_long", metrics, duration)
    if metrics.deletions > limits.max_deletions:
        return QualityReport(False, "deletions", metrics, duration)
    if metrics.insertions > limits.max_insertions:
        return QualityReport(False, "insertions", metrics, duration)
    if metrics.substitutions > limits.max_substitutions:
        return QualityReport(False, "substitutions", metrics, duration)
    if metrics.word_error_rate > limits.max_word_error_rate:
        return QualityReport(False, "word_error_rate", metrics, duration)
    return QualityReport(True, None, metrics, duration)


@dataclass(frozen=True, slots=True)
class PreparedText:
    normalized_text: str
    segments: tuple[str, ...]


class AsyncTextPreparer(Protocol):
    """Normalizes a response and applies Hermes sentence/phrase segmentation."""

    async def prepare(self, text: str) -> PreparedText: ...


_HERMES_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])(?:\s|\n)|(?:\n\n)")
_HERMES_MIN_SEGMENT_CHARS = 20


class DeterministicTextPreparer:
    """Normalize once and apply the bounded Hermes SentenceChunker contract."""

    def __init__(
        self,
        normalizer: Callable[[str], Awaitable[str]],
        *,
        max_segment_chars: int = 500,
        max_segments: int = 64,
    ) -> None:
        if not callable(normalizer):
            raise TypeError("normalizer must be callable")
        if max_segment_chars <= 0 or max_segment_chars > MAX_TEXT_CHARS or max_segments <= 0:
            raise ValueError("invalid text preparation bounds")
        self._normalizer = normalizer
        self._max_segment_chars = max_segment_chars
        self._max_segments = max_segments

    async def prepare(self, text: str) -> PreparedText:
        normalized = await self._normalizer(text)
        if not isinstance(normalized, str):
            raise TypeError("normalizer must return a string")
        normalized = normalized.strip()
        validate_text(normalized)
        segments = tuple(self._split(normalized))
        if len(segments) > self._max_segments:
            raise ValueError("normalized text produces too many segments")
        return PreparedText(normalized, segments)

    def _split(self, text: str) -> list[str]:
        natural: list[str] = []
        buffer = text
        search_start = 0
        while match := _HERMES_SENTENCE_BOUNDARY_RE.search(buffer, search_start):
            head = buffer[: match.end()]
            if len(head.strip()) < _HERMES_MIN_SEGMENT_CHARS:
                search_start = match.end()
                continue
            natural.append(head.strip())
            buffer = buffer[match.end():]
            search_start = 0
        if buffer.strip():
            natural.append(buffer.strip())

        bounded: list[str] = []
        for piece in natural:
            while len(piece) > self._max_segment_chars:
                window = piece[: self._max_segment_chars + 1]
                spaces = list(re.finditer(r"\s+", window))
                cut = (
                    spaces[-1].end()
                    if spaces and spaces[-1].end() <= self._max_segment_chars
                    else self._max_segment_chars
                )
                bounded.append(piece[:cut])
                piece = piece[cut:]
            if piece:
                bounded.append(piece)
        return bounded

class OmniTTSProvider(Protocol):
    name: str
    voice: str

    async def synthesize(
        self, text: str, output_path: Path
    ) -> Path | SynthesizedAudio: ...


class PiperTTSProvider(Protocol):
    name: str
    voice: str

    async def synthesize(
        self, text: str, output_path: Path
    ) -> Path | SynthesizedAudio: ...


class VerifierTranscriber(Protocol):
    async def transcribe(self, audio_path: Path, language: str = "de") -> str: ...


class VerifierSTTProvider(Protocol):
    async def transcribe(self, audio_path: Path, language: str) -> str: ...


class BoundedLanguageVerifier:
    """Bound an STT verification call using the turn's validated language tag."""

    def __init__(
        self,
        provider: VerifierSTTProvider,
        *,
        timeout_seconds: float,
        max_transcript_chars: int,
    ) -> None:
        if timeout_seconds <= 0 or max_transcript_chars <= 0:
            raise ValueError("invalid verifier bounds")
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._max_transcript_chars = max_transcript_chars

    async def transcribe(self, audio_path: Path, language: str = "de") -> str:
        try:
            transcript = await asyncio.wait_for(
                self._provider.transcribe(audio_path, language), timeout=self._timeout_seconds
            )
        except STTProviderUnavailable as exc:
            raise TTSProviderUnavailable(
                "verifier_provider_unavailable", exc.circuit_state
            ) from exc
        except STTTechnicalError as exc:
            raise TTSTechnicalError("verifier_failed") from exc
        if not isinstance(transcript, str):
            raise TTSTechnicalError("invalid_transcript")
        if len(transcript) > self._max_transcript_chars:
            raise TTSTechnicalError("transcript_too_large")
        return transcript.strip()


@dataclass(frozen=True, slots=True)
class QualityAttempt:
    provider: str
    voice: str
    segment_index: int
    attempt: int
    accepted: bool
    reason: str | None
    elapsed_ms: float = 0.0
    circuit_state: str = "closed"

    @property
    def outcome(self) -> str:
        if self.accepted:
            return "success"
        if self.reason and self.reason.endswith("provider_unavailable"):
            return "unavailable"
        if self.reason == "timeout":
            return "timeout"
        return "rejected"


@dataclass(frozen=True, slots=True)
class QualityResult:
    audio_path: Path
    provider: str
    voice: str
    prepared: PreparedText
    reports: tuple[QualityReport, ...]
    attempts: tuple[QualityAttempt, ...]


class QualityOrchestrationError(TTSTechnicalError):
    def __init__(self, attempts: tuple[QualityAttempt, ...]) -> None:
        self.attempts = attempts
        super().__init__("quality_tts_providers_exhausted")


def _private_path(directory: Path, *, prefix: str = "quality-") -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".wav", dir=directory)
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


def _cleanup(paths: list[Path] | tuple[Path, ...]) -> None:
    for path in paths:
        _remove(path)


def combine_wav_segments(
    paths: tuple[Path, ...] | list[Path],
    output_dir: Path | str,
    *,
    max_bytes: int,
) -> Path:
    """Atomically publish compatible PCM WAV segments as one private WAV."""
    directory = validate_output_directory(output_dir)
    if not paths:
        raise TTSTechnicalError("empty_wav_segments")
    if max_bytes <= 44:
        raise ValueError("max_bytes must exceed a WAV header")

    parameters: tuple[int, int, int, str] | None = None
    frame_chunks: list[bytes] = []
    estimated_size = 44
    for path in paths:
        validate_wav_output(path, max_bytes=max_bytes)
        try:
            with wave.open(str(path), "rb") as audio:
                current = (
                    audio.getnchannels(), audio.getsampwidth(),
                    audio.getframerate(), audio.getcomptype(),
                )
                if parameters is None:
                    parameters = current
                elif current != parameters:
                    raise TTSTechnicalError("incompatible_wav")
                frames = audio.readframes(audio.getnframes())
        except TTSTechnicalError:
            raise
        except (OSError, EOFError, wave.Error) as exc:
            raise TTSTechnicalError("invalid_wav") from exc
        estimated_size += len(frames)
        if estimated_size > max_bytes:
            raise TTSTechnicalError("wav_too_large")
        frame_chunks.append(frames)

    assert parameters is not None
    staging: Path | None = None
    final: Path | None = None
    try:
        staging = _private_path(directory, prefix="quality-part-")
        final = _private_path(directory, prefix="quality-result-")
        _remove(final)
        with wave.open(str(staging), "wb") as output:
            output.setnchannels(parameters[0])
            output.setsampwidth(parameters[1])
            output.setframerate(parameters[2])
            output.setcomptype(parameters[3], "not compressed")
            for frames in frame_chunks:
                output.writeframesraw(frames)
        validate_wav_output(staging, max_bytes=max_bytes)
        os.replace(staging, final)
        validate_wav_output(final, max_bytes=max_bytes)
        return final
    except BaseException:
        if staging is not None:
            _remove(staging)
        if final is not None:
            _remove(final)
        raise


class QualityOrchestrator:
    """Verified whole-answer synthesis over an ordered provider list."""

    def __init__(
        self,
        preparer: AsyncTextPreparer,
        *configured: OmniTTSProvider | PiperTTSProvider | VerifierTranscriber,
        thresholds: QualityThresholds | None = None,
    ) -> None:
        if len(configured) < 2:
            raise ValueError("at least one TTS provider and a verifier are required")
        providers = cast(
            tuple[OmniTTSProvider | PiperTTSProvider, ...], tuple(configured[:-1])
        )
        verifier = cast(VerifierTranscriber, configured[-1])
        if any(
            not getattr(provider, "name", "") or not getattr(provider, "voice", "")
            for provider in providers
        ) or not callable(getattr(verifier, "transcribe", None)):
            raise ValueError("invalid TTS provider list or verifier")
        self._preparer = preparer
        self._providers = providers
        self._verifier = verifier
        self._thresholds = thresholds or QualityThresholds()

    async def synthesize(
        self, text: str, output_dir: Path | str, language: str = "de"
    ) -> QualityResult:
        validate_text(text)
        directory = validate_output_directory(output_dir)
        prepared = await asyncio.wait_for(
            self._preparer.prepare(text), timeout=self._thresholds.preparer_timeout_seconds
        )
        self._validate_prepared(prepared)
        attempts: list[QualityAttempt] = []

        for provider in self._providers:
            retries = 2 if provider.name == "omnivoice" else 1
            completed = await self._run_batch(
                provider, prepared.segments, directory, retries, attempts, language
            )
            if completed is None:
                continue
            artifacts, reports = completed
            artifact_limit = min(
                self._thresholds.max_wav_bytes,
                self._thresholds.max_total_artifact_bytes,
            )
            try:
                combined = combine_wav_segments(
                    artifacts, directory,
                    max_bytes=artifact_limit,
                )
            except TTSTechnicalError as exc:
                attempts.append(QualityAttempt(
                    provider.name, provider.voice, -1, 1, False, exc.code,
                ))
                _cleanup(artifacts)
                continue
            except BaseException:
                _cleanup(artifacts)
                raise
            _cleanup(artifacts)
            return QualityResult(
                combined,
                provider.name,
                provider.voice,
                prepared,
                tuple(reports),
                tuple(attempts),
            )
        raise QualityOrchestrationError(tuple(attempts))

    def _validate_prepared(self, prepared: PreparedText) -> None:
        if not isinstance(prepared, PreparedText):
            raise TypeError("preparer must return PreparedText")
        validate_text(prepared.normalized_text)
        segments = prepared.segments
        if (
            not isinstance(segments, tuple)
            or not segments
            or len(segments) > self._thresholds.max_segments
            or any(
                not isinstance(segment, str)
                or not segment.strip()
                or len(segment) > self._thresholds.max_segment_chars
                or len(segment) > MAX_TEXT_CHARS
                for segment in segments
            )
        ):
            raise ValueError("prepared segments must be nonempty and bounded")
        for segment in segments:
            validate_text(segment, max_chars=self._thresholds.max_segment_chars)
        canonical = canonical_words(prepared.normalized_text)
        if (
            canonical_words("".join(segments)) != canonical
            and canonical_words(" ".join(segments)) != canonical
        ):
            raise ValueError("prepared segments must preserve canonical text")

    async def _run_batch(
        self,
        provider: OmniTTSProvider | PiperTTSProvider,
        segments: tuple[str, ...],
        directory: Path,
        retries: int,
        attempts: list[QualityAttempt],
        language: str,
    ) -> tuple[list[Path], list[QualityReport]] | None:
        artifacts: list[Path] = []
        reports: list[QualityReport] = []
        artifact_bytes = 0
        artifact_limit = min(
            self._thresholds.max_wav_bytes,
            self._thresholds.max_total_artifact_bytes,
        )
        try:
            for segment_index, segment in enumerate(segments):
                accepted = False
                for attempt_number in range(1, retries + 1):
                    started = time.perf_counter()
                    retry_quality_rejection = False
                    circuit_state = "closed"
                    path = _private_path(directory)
                    try:
                        returned = await asyncio.wait_for(
                            provider.synthesize(segment, path),
                            timeout=self._thresholds.provider_timeout_seconds,
                        )
                        circuit_state = str(
                            getattr(returned, "circuit_state", "closed")
                        )
                        if Path(returned) != path:
                            raise TTSTechnicalError("unexpected_output_path")
                        validate_wav_output(path, max_bytes=self._thresholds.max_wav_bytes)
                        current_bytes = path.stat().st_size
                        if artifact_bytes + current_bytes > artifact_limit:
                            raise TTSTechnicalError("artifact_bytes_exceeded")
                        if provider.name == "omnivoice":
                            transcript = await asyncio.wait_for(
                                self._verifier.transcribe(path, language),
                                timeout=self._thresholds.verifier_timeout_seconds,
                            )
                            if not isinstance(transcript, str):
                                raise TTSTechnicalError("invalid_transcript")
                            if len(transcript) > self._thresholds.max_transcript_chars:
                                raise TTSTechnicalError("transcript_too_large")
                            report = evaluate_quality(
                                segment, transcript, path, self._thresholds, language
                            )
                        else:
                            report = None
                    except asyncio.TimeoutError:
                        reason = "timeout"
                    except TTSProviderUnavailable as exc:
                        attempts.append(QualityAttempt(
                            provider.name, provider.voice, segment_index,
                            attempt_number, False, exc.code,
                            _elapsed_ms(started), exc.circuit_state,
                        ))
                        _remove(path)
                        _cleanup(artifacts)
                        return None
                    except TTSTechnicalError as exc:
                        reason = exc.code
                    except BaseException:
                        _remove(path)
                        raise
                    else:
                        reason = report.reason if report is not None else None
                        if report is None or report.accepted:
                            attempts.append(QualityAttempt(
                                provider.name, provider.voice, segment_index,
                                attempt_number, True, None,
                                _elapsed_ms(started), circuit_state,
                            ))
                            artifacts.append(path)
                            artifact_bytes += current_bytes
                            if report is not None:
                                reports.append(report)
                            accepted = True
                            break
                        retry_quality_rejection = True
                    attempts.append(QualityAttempt(
                        provider.name, provider.voice, segment_index,
                        attempt_number, False, reason,
                        _elapsed_ms(started), circuit_state,
                    ))
                    _remove(path)
                    if not retry_quality_rejection:
                        _cleanup(artifacts)
                        return None
                if not accepted:
                    _cleanup(artifacts)
                    return None
            return artifacts, reports
        except BaseException:
            _cleanup(artifacts)
            raise


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)
