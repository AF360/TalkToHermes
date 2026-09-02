from __future__ import annotations

import asyncio
import io
import stat
import wave
from pathlib import Path

import pytest

from talktohermes.tts.base import TTSTechnicalError, TTSValidationError
from talktohermes.tts.chain import TTSChain, TTSChainError


def wav_bytes(frames: int = 16) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * frames)
    return stream.getvalue()


def private_output_dir(tmp_path: Path) -> Path:
    output = tmp_path / "audio"
    output.mkdir(mode=0o700)
    output.chmod(0o700)
    return output


class FakeProvider:
    def __init__(self, name: str, voice: str, outcome: bytes | BaseException, calls: list[str]) -> None:
        self.name = name
        self.voice = voice
        self._outcome = outcome
        self._calls = calls

    async def synthesize(self, text: str, output_path: Path) -> Path:
        self._calls.append(self.name)
        assert output_path.exists()
        assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
        if isinstance(self._outcome, BaseException):
            output_path.write_bytes(b"failed-artifact")
            raise self._outcome
        output_path.write_bytes(self._outcome)
        return output_path


def chain_with(outcomes: list[bytes | BaseException], calls: list[str], *, max_wav_bytes: int = 1024) -> TTSChain:
    return TTSChain(
        FakeProvider("omnivoice", "voice-02", outcomes[0], calls),
        FakeProvider("wyoming-piper", "de_DE-thorsten-medium", outcomes[1], calls),
        FakeProvider("piper", "de_DE-ramona-low", outcomes[2], calls),
        max_wav_bytes=max_wav_bytes,
    )


@pytest.mark.asyncio
async def test_chain_stops_after_first_valid_wav_and_records_timing(tmp_path: Path) -> None:
    calls: list[str] = []
    result = await chain_with([wav_bytes(), wav_bytes(), wav_bytes()], calls).synthesize(
        "Hallo Welt", private_output_dir(tmp_path)
    )

    assert calls == ["omnivoice"]
    assert result.provider == "omnivoice"
    assert result.voice == "voice-02"
    assert result.audio_path.read_bytes() == wav_bytes()
    assert [(a.provider, a.voice, a.outcome, a.error_code) for a in result.attempts] == [
        ("omnivoice", "voice-02", "success", None)
    ]
    assert result.attempts[0].elapsed_ms >= 0


@pytest.mark.asyncio
async def test_chain_falls_back_strictly_sequentially_and_cleans_failed_files(tmp_path: Path) -> None:
    calls: list[str] = []
    output_dir = private_output_dir(tmp_path)
    chain = chain_with(
        [TTSTechnicalError("omnivoice_unavailable"), asyncio.TimeoutError(), wav_bytes()], calls
    )

    result = await chain.synthesize("Hallo", output_dir)

    assert calls == ["omnivoice", "wyoming-piper", "piper"]
    assert result.voice == "de_DE-ramona-low"
    assert [a.outcome for a in result.attempts] == ["technical_failure", "timeout", "success"]
    assert [a.error_code for a in result.attempts] == [
        "omnivoice_unavailable", "provider_timeout", None
    ]
    assert list(output_dir.iterdir()) == [result.audio_path]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_audio", "outcome", "code"),
    [
        (b"", "empty", "empty_wav"),
        (b"not-wave", "invalid", "invalid_wav"),
        (wav_bytes()[:-4], "invalid", "invalid_wav"),
        (wav_bytes() + b"trailing-payload", "invalid", "invalid_wav"),
        (wav_bytes(600), "oversized", "wav_too_large"),
    ],
)
async def test_empty_invalid_or_oversized_wav_falls_back(
    tmp_path: Path, bad_audio: bytes, outcome: str, code: str
) -> None:
    calls: list[str] = []
    result = await chain_with([bad_audio, wav_bytes(), wav_bytes()], calls, max_wav_bytes=1024).synthesize(
        "Hallo", private_output_dir(tmp_path)
    )
    assert calls == ["omnivoice", "wyoming-piper"]
    assert result.attempts[0].outcome == outcome
    assert result.attempts[0].error_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), RuntimeError("bug"), ValueError("bad")])
async def test_cancellation_and_programming_errors_propagate_without_fallback_or_artifacts(
    tmp_path: Path, error: BaseException
) -> None:
    calls: list[str] = []
    output_dir = private_output_dir(tmp_path)
    with pytest.raises(type(error)):
        await chain_with([error, wav_bytes(), wav_bytes()], calls).synthesize("Hallo", output_dir)
    assert calls == ["omnivoice"]
    assert list(output_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_exhaustion_preserves_attempt_metadata_without_text_or_paths(tmp_path: Path) -> None:
    calls: list[str] = []
    output_dir = private_output_dir(tmp_path)
    secret_text = "Geheimer Inhalt"
    with pytest.raises(TTSChainError) as caught:
        await chain_with([b"", b"bad", TTSTechnicalError("worker_failed")], calls).synthesize(
            secret_text, output_dir
        )
    assert len(caught.value.attempts) == 3
    assert secret_text not in str(caught.value)
    assert str(output_dir) not in str(caught.value)
    assert list(output_dir.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "  \n\t", "a\x00b", "a\x01b", "x" * 2001])
async def test_text_validation_happens_before_provider_calls(tmp_path: Path, text: str) -> None:
    calls: list[str] = []
    with pytest.raises(TTSValidationError):
        await chain_with([wav_bytes(), wav_bytes(), wav_bytes()], calls).synthesize(
            text, private_output_dir(tmp_path)
        )
    assert calls == []


@pytest.mark.asyncio
async def test_output_directory_must_be_absolute_owned_private_existing_directory(tmp_path: Path) -> None:
    calls: list[str] = []
    chain = chain_with([wav_bytes(), wav_bytes(), wav_bytes()], calls)
    private = private_output_dir(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)

    for invalid in (Path("relative"), tmp_path / "missing", link, public):
        with pytest.raises(TTSValidationError, match="output"):
            await chain.synthesize("Hallo", invalid)
    assert calls == []


def test_chain_requires_at_least_one_provider_and_preserves_order() -> None:
    with pytest.raises(ValueError, match="at least one"):
        TTSChain()

    calls: list[str] = []
    providers = (
        FakeProvider("omnivoice", "voice-02", wav_bytes(), calls),
        FakeProvider("piper", "de_DE-ramona-low", wav_bytes(), calls),
    )
    assert TTSChain(providers)._providers == providers
