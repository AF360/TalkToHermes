from __future__ import annotations

import io
import logging
import os
import stat
import sys
import threading
import time
import types
import wave
from pathlib import Path

import pytest

from talktohermes_stt.app import (
    InvalidAudio,
    MAX_CONTENT_LENGTH,
    _load_production_model,
    create_app,
    validate_audio_with_pyav,
)

TOKEN = "A" * 48


def wav_bytes(*, channels: int = 1, width: int = 2, rate: int = 16_000, seconds: float = 0.1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(b"\0" * (int(rate * seconds) * channels * width))
    return output.getvalue()


class FakeModel:
    def __init__(self, text: str = " Hallo Hermes ") -> None:
        self.text = text
        self.calls: list[dict[str, object]] = []

    def transcribe(self, audio: io.BytesIO, **kwargs: object):
        self.calls.append({"audio": audio, **kwargs})
        return iter([type("Segment", (), {"text": self.text})()]), object()


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "stt.token"
    path.write_text(TOKEN + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def client(token_file: Path, model: FakeModel | None = None):
    app = create_app(
        token_file=token_file,
        model_loader=lambda: model or FakeModel(),
        audio_validator=validate_test_wav,
    )
    app.testing = True
    return app.test_client(), app


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def validate_test_wav(audio: io.BytesIO) -> None:
    try:
        with wave.open(audio, "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or not 8_000 <= source.getframerate() <= 48_000
                or source.getnframes() > source.getframerate() * 120
            ):
                raise InvalidAudio
    except (EOFError, wave.Error):
        raise InvalidAudio
    finally:
        audio.seek(0)


def test_authenticated_ready_loads_single_model_lazily(token_file: Path) -> None:
    loads: list[bool] = []
    model = FakeModel()
    app = create_app(
        token_file=token_file,
        model_loader=lambda: loads.append(True) or model,
        audio_validator=validate_test_wav,
    )
    test_client = app.test_client()

    assert test_client.get("/ready").status_code == 401
    assert test_client.get("/ready", headers=auth("B" * 48)).status_code == 401
    assert test_client.get("/ready", headers=auth()).get_json() == {"status": "ready"}
    assert test_client.get("/ready", headers=auth()).status_code == 200
    assert loads == [True]


def test_token_file_must_be_regular_nonsymlink_0600_owned_by_runtime_uid(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.write_text(TOKEN, encoding="ascii")
    good.chmod(0o600)
    symlink = tmp_path / "link"
    symlink.symlink_to(good)
    permissive = tmp_path / "permissive"
    permissive.write_text(TOKEN, encoding="ascii")
    permissive.chmod(0o640)

    for path in (symlink, permissive):
        with pytest.raises(ValueError, match="token file"):
            create_app(token_file=path, model_loader=FakeModel)

    if os.getuid() != 0:
        return
    wrong_owner = tmp_path / "wrong-owner"
    wrong_owner.write_text(TOKEN, encoding="ascii")
    wrong_owner.chmod(0o600)
    os.chown(wrong_owner, 1, -1)
    with pytest.raises(ValueError, match="token file"):
        create_app(token_file=wrong_owner, model_loader=FakeModel)


@pytest.mark.parametrize("token", ["x" * 31, "x" * 257, "!" * 48, "x y" * 16])
def test_token_format_is_url_safe_and_bounded(tmp_path: Path, token: str) -> None:
    path = tmp_path / "token"
    path.write_text(token, encoding="ascii")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="token"):
        create_app(token_file=path, model_loader=FakeModel)


def test_openai_multipart_accepts_requested_language_and_model_options(token_file: Path) -> None:
    model = FakeModel()
    test_client, _ = client(token_file, model)
    response = test_client.post(
        "/v1/audio/transcriptions",
        headers=auth(),
        data={
            "file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav"),
            "model": "large-v3-turbo",
            "language": "en-US",
            "response_format": "json",
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"text": "Hallo Hermes"}
    call = model.calls[0]
    assert isinstance(call.pop("audio"), io.BytesIO)
    assert call == {
        "language": "en",
        "initial_prompt": "Transcribe the speech input for TalkToHermes precisely.",
        "beam_size": 1,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "word_timestamps": False,
    }


@pytest.mark.parametrize(
    "data",
    [
        {"file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav")},
        {"file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav"), "model": "wrong", "language": "de"},
        {"file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav"), "model": "large-v3-turbo", "language": "en; rm -rf /"},
        {"file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav"), "model": "large-v3-turbo", "language": "de", "response_format": "text"},
        {"file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav"), "model": "large-v3-turbo", "language": "de", "unknown": "x"},
        {"file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav"), "model": "large-v3-turbo", "language": "de", "extra": (io.BytesIO(b"x"), "x.wav")},
    ],
)
def test_rejects_missing_invalid_or_unknown_multipart_fields(token_file: Path, data: dict[str, object]) -> None:
    test_client, _ = client(token_file)
    response = test_client.post("/v1/audio/transcriptions", headers=auth(), data=data)
    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_request"}


@pytest.mark.parametrize(
    ("payload", "filename", "content_type"),
    [
        (b"", "audio.wav", "audio/wav"),
        (b"not-wave", "audio.wav", "audio/wav"),
        (wav_bytes(channels=2), "audio.wav", "audio/wav"),
        (wav_bytes(width=1), "audio.wav", "audio/wav"),
        (wav_bytes(rate=7_999), "audio.wav", "audio/wav"),
        (wav_bytes(rate=48_001), "audio.wav", "audio/wav"),
        (wav_bytes(), "x" * 129 + ".wav", "audio/wav"),
        (wav_bytes(), "audio.wav", "application/octet-stream"),
    ],
)
def test_rejects_invalid_audio_before_inference(token_file: Path, payload: bytes, filename: str, content_type: str) -> None:
    model = FakeModel()
    test_client, _ = client(token_file, model)
    response = test_client.post(
        "/v1/audio/transcriptions",
        headers=auth(),
        data={"file": (io.BytesIO(payload), filename, content_type), "model": "large-v3-turbo", "language": "de"},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_audio"}
    assert model.calls == []


def test_request_and_response_are_bounded(token_file: Path) -> None:
    model = FakeModel("x" * 70_000)
    test_client, app = client(token_file, model)
    assert app.config["MAX_CONTENT_LENGTH"] == MAX_CONTENT_LENGTH
    response = test_client.post(
        "/v1/audio/transcriptions",
        headers=auth(),
        data={"file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav"), "model": "large-v3-turbo", "language": "de"},
    )
    assert response.status_code == 500
    assert response.get_json() == {"error": "internal_error"}


def test_global_inference_lock_serializes_requests(token_file: Path) -> None:
    active = 0
    maximum = 0
    guard = threading.Lock()

    class SlowModel(FakeModel):
        def transcribe(self, audio: io.BytesIO, **kwargs: object):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return super().transcribe(audio, **kwargs)

    app = create_app(
        token_file=token_file,
        model_loader=SlowModel,
        audio_validator=validate_test_wav,
    )

    def request() -> int:
        with app.test_client() as test_client:
            return test_client.post(
                "/v1/audio/transcriptions",
                headers=auth(),
                data={"file": (io.BytesIO(wav_bytes()), "audio.wav", "audio/wav"), "model": "large-v3-turbo", "language": "de"},
            ).status_code

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1


def test_errors_and_logs_never_expose_token_transcript_path_or_body(token_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    secret_text = "private transcript body"

    class BrokenModel(FakeModel):
        def transcribe(self, audio: io.BytesIO, **kwargs: object):
            raise RuntimeError(secret_text)

    test_client, _ = client(token_file, BrokenModel())
    caplog.set_level(logging.DEBUG)
    response = test_client.post(
        "/v1/audio/transcriptions",
        headers=auth(),
        data={"file": (io.BytesIO(wav_bytes()), "private-path.wav", "audio/wav"), "model": "large-v3-turbo", "language": "de"},
    )
    rendered = response.get_data(as_text=True) + caplog.text
    assert response.status_code == 500
    for forbidden in (TOKEN, secret_text, "private-path.wav", "RIFF"):
        assert forbidden not in rendered


def test_only_two_routes_are_exposed(token_file: Path) -> None:
    _, app = client(token_file)
    rules = {
        (rule.rule, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
        for rule in app.url_map.iter_rules()
    }
    assert rules == {("/ready", ("GET",)), ("/v1/audio/transcriptions", ("POST",))}


def test_production_model_is_exact_cuda_float16_offline_local_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class WhisperModel:
        def __init__(self, name: str, **kwargs: object) -> None:
            captured.update(name=name, **kwargs)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    _load_production_model()
    assert captured == {
        "name": (
            "/opt/stt/.cache/huggingface/hub/"
            "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/"
            "snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
        ),
        "device": "cuda",
        "compute_type": "float16",
    }


def test_pyav_validator_decodes_and_counts_mono_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    class Container:
        streams = types.SimpleNamespace(audio=[object()], video=[])

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def decode(self, _stream: object):
            frame = types.SimpleNamespace(
                sample_rate=16_000,
                layout=types.SimpleNamespace(channels=[object()]),
                samples=1_600,
            )
            return iter([frame])

    monkeypatch.setitem(sys.modules, "av", types.SimpleNamespace(open=lambda *_a, **_kw: Container()))
    validate_audio_with_pyav(io.BytesIO(b"opaque container"))


def test_approved_non_wav_container_reaches_inference_after_validator(token_file: Path) -> None:
    model = FakeModel()
    app = create_app(
        token_file=token_file,
        model_loader=lambda: model,
        audio_validator=lambda _audio: None,
    )
    response = app.test_client().post(
        "/v1/audio/transcriptions",
        headers=auth(),
        data={
            "file": (io.BytesIO(b"validated m4a"), "audio.m4a", "audio/mp4"),
            "model": "large-v3-turbo",
            "language": "de",
        },
    )
    assert response.status_code == 200
    assert len(model.calls) == 1
