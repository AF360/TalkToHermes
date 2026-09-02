from __future__ import annotations

import io
import os
import wave
from pathlib import Path

import pytest

from talktohermes.audio import AudioValidationError, private_audio_root, validate_actual_audio


def wav_bytes(*, seconds: float, rate: int = 8_000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * int(seconds * rate))
    return stream.getvalue()


def write_private(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    ("mime", "data"),
    [
        ("audio/wav", b"not-wave"),
        ("audio/x-wav", b"not-wave"),
        ("audio/m4a", b"\0\0\0\x18notf"),
        ("audio/mp4", b"\0\0\0\x18notf"),
        ("audio/x-caf", b"not-caf"),
        ("audio/ogg", b"not-ogg"),
    ],
)
def test_declared_mime_must_match_actual_container(tmp_path: Path, mime: str, data: bytes) -> None:
    path = write_private(tmp_path / "audio.bin", data)
    with pytest.raises(AudioValidationError, match="audio_format_mismatch"):
        validate_actual_audio(path, mime)


def test_wav_duration_accepts_exactly_120_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "talktohermes.audio._probe_duration",
        lambda _: pytest.fail("WAV duration must not depend on ffprobe"),
    )
    validate_actual_audio(write_private(tmp_path / "audio.wav", wav_bytes(seconds=120)), "audio/wav")


def test_wav_duration_rejects_over_120_seconds(tmp_path: Path) -> None:
    path = write_private(tmp_path / "audio.wav", wav_bytes(seconds=120.001))
    with pytest.raises(AudioValidationError, match="audio_too_long"):
        validate_actual_audio(path, "audio/wav")


@pytest.mark.parametrize(
    ("mime", "data"),
    [
        ("audio/m4a", b"\0\0\0\x18ftypM4A \0\0\0\0M4A is not enough"),
        ("audio/mp4", b"\0\0\0\x18ftypisom\0\0\0\0MP4 is not enough"),
        ("audio/x-caf", b"caff\0\1\0\0CAF is not enough"),
        ("audio/ogg", b"OggS\0\2Ogg is not enough"),
    ],
)
def test_non_wav_container_fails_closed_when_duration_cannot_be_established(
    tmp_path: Path, mime: str, data: bytes
) -> None:
    path = write_private(tmp_path / "audio.bin", data)
    with pytest.raises(AudioValidationError, match="audio_duration_unavailable"):
        validate_actual_audio(path, mime)


def test_non_wav_container_accepts_bounded_probed_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_private(tmp_path / "audio.ogg", b"OggS\0\2valid-container")
    monkeypatch.setattr("talktohermes.audio._probe_duration", lambda _: 120.0)
    validate_actual_audio(path, "audio/ogg")


def test_non_wav_container_rejects_probed_duration_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_private(tmp_path / "audio.caf", b"caff\0\1\0\0valid-container")
    monkeypatch.setattr("talktohermes.audio._probe_duration", lambda _: 120.001)
    with pytest.raises(AudioValidationError, match="audio_too_long"):
        validate_actual_audio(path, "audio/x-caf")


@pytest.mark.parametrize("raw", [b"nan\n", b"inf\n", b"-inf\n"])
def test_ffprobe_non_finite_duration_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    import subprocess
    from types import SimpleNamespace

    path = write_private(tmp_path / "audio.ogg", b"OggS\0\2valid-container")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=raw),
    )
    with pytest.raises(AudioValidationError, match="audio_duration_unavailable"):
        validate_actual_audio(path, "audio/ogg")


def test_private_audio_root_rejects_symlink(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (state / "audio").symlink_to(target)
    with pytest.raises(AudioValidationError, match="invalid_audio_root"):
        private_audio_root(state)


def test_private_audio_root_rejects_wrong_mode(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "audio").mkdir(mode=0o755)
    with pytest.raises(AudioValidationError, match="invalid_audio_root"):
        private_audio_root(state)


def test_private_audio_root_rejects_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "audio").mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(state / "audio").st_uid + 1)
    with pytest.raises(AudioValidationError, match="invalid_audio_root"):
        private_audio_root(state)
