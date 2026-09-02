from __future__ import annotations

import asyncio
import os
import stat
import threading
import wave
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from talktohermes_omnivoice.app import create_app
from talktohermes_omnivoice.config import ConfigError, load_config


class FakeBackend:
    def __init__(self, result: bytes | Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.ready_calls = 0
        self.result = result if result is not None else wav_bytes()

    def ready(self) -> None:
        self.ready_calls += 1

    def synthesize(self, **kwargs: object) -> bytes:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def wav_bytes() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(b"\0\0" * 100)
    return output.getvalue()


def make_config(
    tmp_path: Path,
    *,
    host: str = "192.168.100.20",
    port: int = 9090,
    language: str = "de",
) -> tuple[Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    token = "t" * 48
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    audio = tmp_path / "reference.wav"
    audio.write_bytes(wav_bytes())
    audio.chmod(0o600)
    transcript = tmp_path / "reference.txt"
    transcript.write_text("A server-side reference transcript.\n", encoding="utf-8")
    transcript.chmod(0o600)
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""listen_host: {host}
listen_port: {port}
token_file: {token_file}
model_name: k2-fsa/OmniVoice
max_body_bytes: 4096
max_text_chars: 200
voices:
  voice-01:
    reference_audio: {audio}
    reference_transcript: {transcript}
    language: {language}
""",
        encoding="utf-8",
    )
    return config, token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_load_config_accepts_configured_private_ipv4_listener_and_private_server_files(tmp_path: Path) -> None:
    path, token = make_config(tmp_path)
    settings = load_config(path)
    assert settings.listen_host == "192.168.100.20"
    assert settings.listen_port == 9090
    assert settings.token == token
    assert settings.voices["voice-01"].transcript == "A server-side reference transcript."
    assert settings.voices["voice-01"].language == "de"

    english, _ = make_config(tmp_path / "english", language="en")
    assert load_config(english).voices["voice-01"].language == "en"

    malformed_language, _ = make_config(tmp_path / "malformed-language", language="en;rm")
    with pytest.raises(ConfigError, match="language"):
        load_config(malformed_language)

    alternate, _ = make_config(tmp_path / "alternate", host="10.23.45.67")
    assert load_config(alternate).listen_host == "10.23.45.67"

    for host, port in (
        ("10.0.0.0", 9090),
        ("10.255.255.255", 9090),
        ("172.16.0.0", 9090),
        ("172.31.255.255", 9090),
        ("192.168.0.0", 9090),
        ("192.168.255.255", 9090),
        ("0.0.0.0", 9090),
        ("127.0.0.1", 9090),
        ("8.8.8.8", 9090),
        ("192.0.2.20", 9090),
        ("primary-voice-server.home.arpa", 9090),
        ("not-an-ip", 9090),
        ("192.168.100.20", 8181),
        ("192.168.100.20", 9091),
    ):
        bad, _ = make_config(tmp_path / f"bad-{host.replace('.', '-')}-{port}", host=host, port=port)
        with pytest.raises(ConfigError):
            load_config(bad)


def test_load_config_rejects_unsafe_secret_voice_files_and_ids(tmp_path: Path) -> None:
    path, _ = make_config(tmp_path)
    token_file = tmp_path / "token"
    token_file.chmod(0o640)
    with pytest.raises(ConfigError, match="token file"):
        load_config(path)

    token_file.chmod(0o600)
    audio = tmp_path / "reference.wav"
    audio.unlink()
    audio.symlink_to(tmp_path / "real.wav")
    (tmp_path / "real.wav").write_bytes(wav_bytes())
    (tmp_path / "real.wav").chmod(0o600)
    with pytest.raises(ConfigError, match="voice file"):
        load_config(path)

    path, _ = make_config(tmp_path / "malformed")
    malformed = tmp_path / "malformed" / "reference.wav"
    malformed.write_bytes(b"not a wav")
    malformed.chmod(0o600)
    with pytest.raises(ConfigError, match="WAV"):
        load_config(path)

    real_dir = tmp_path / "real-parent"
    path, _ = make_config(real_dir)
    alias_dir = tmp_path / "linked-parent"
    alias_dir.symlink_to(real_dir, target_is_directory=True)
    path.write_text(path.read_text().replace(str(real_dir), str(alias_dir)))
    with pytest.raises(ConfigError, match="symlink"):
        load_config(path)

    path, _ = make_config(tmp_path / "id")
    text = path.read_text().replace("voice-01:", "../../private:")
    path.write_text(text)
    with pytest.raises(ConfigError, match="voice ID"):
        load_config(path)


def test_reference_audio_descriptor_remains_stable_after_path_replacement(
    tmp_path: Path,
) -> None:
    path, _ = make_config(tmp_path)
    original = (tmp_path / "reference.wav").read_bytes()
    settings = load_config(path)
    stable_path = settings.voices["voice-01"].reference_audio
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(wav_bytes() + b"replacement")
    replacement.chmod(0o600)
    (tmp_path / "reference.wav").unlink()
    (tmp_path / "reference.wav").symlink_to(replacement)
    try:
        assert stable_path.read_bytes() == original
    finally:
        settings.close()


def test_openapi_docs_are_absent_and_readiness_is_authenticated(tmp_path: Path) -> None:
    path, token = make_config(tmp_path)
    backend = FakeBackend()
    with TestClient(create_app(load_config(path), backend)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/ready").status_code == 401
        assert client.get("/ready", headers=auth("wrong")).status_code == 401
        ready = client.get("/ready", headers=auth(token))
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}
    assert backend.ready_calls == 1


def test_wav_request_is_openai_compatible_and_uses_only_server_profile(tmp_path: Path) -> None:
    path, token = make_config(tmp_path)
    backend = FakeBackend()
    settings = load_config(path)
    stable_reference = settings.voices["voice-01"].reference_audio
    with TestClient(create_app(settings, backend)) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth(token),
            json={"model": "omnivoice", "voice": "voice-01", "input": "Hallo Welt", "response_format": "wav"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF")
    assert backend.calls == [{
        "text": "Hallo Welt",
        "reference_audio": stable_reference,
        "reference_text": "A server-side reference transcript.",
        "language": "de",
        "max_output_bytes": settings.max_wav_bytes,
    }]


@pytest.mark.parametrize("payload", [
    {"model": "wrong", "voice": "voice-01", "input": "hello", "response_format": "wav"},
    {"model": "omnivoice", "voice": "missing", "input": "hello", "response_format": "wav"},
    {"model": "omnivoice", "voice": "/tmp/private.wav", "input": "hello", "response_format": "wav"},
    {"model": "omnivoice", "voice": "voice-01", "input": "", "response_format": "wav"},
    {"model": "omnivoice", "voice": "voice-01", "input": "hello", "response_format": "flac"},
    {"model": "omnivoice", "voice": "voice-01", "input": "hello", "response_format": "wav", "audio": "/tmp/x.wav"},
])
def test_invalid_requests_are_controlled_and_do_not_reach_backend(tmp_path: Path, payload: dict[str, str]) -> None:
    path, token = make_config(tmp_path)
    backend = FakeBackend()
    with TestClient(create_app(load_config(path), backend)) as client:
        response = client.post("/v1/audio/speech", headers=auth(token), json=payload)
    assert response.status_code == 400
    assert response.json() == {"error": {"message": "Invalid request", "type": "invalid_request_error"}}
    assert backend.calls == []


def test_text_body_and_content_type_are_bounded(tmp_path: Path) -> None:
    path, token = make_config(tmp_path)
    backend = FakeBackend()
    with TestClient(create_app(load_config(path), backend)) as client:
        too_long = client.post("/v1/audio/speech", headers=auth(token), json={
            "model": "omnivoice", "voice": "voice-01", "input": "x" * 201, "response_format": "wav"
        })
        too_large = client.post(
            "/v1/audio/speech", headers={**auth(token), "Content-Type": "application/json"}, content=b"{" + b" " * 5000
        )
        wrong_type = client.post("/v1/audio/speech", headers={**auth(token), "Content-Type": "text/plain"}, content=b"hello")
    assert too_long.status_code == 400
    assert too_large.status_code == 413
    assert wrong_type.status_code == 415
    assert backend.calls == []


def test_backend_errors_are_generic_and_never_echo_sensitive_data(tmp_path: Path) -> None:
    path, token = make_config(tmp_path)
    secret = "VERY-SENSITIVE-TEXT"
    backend = FakeBackend(RuntimeError(f"failed at /private/path for {secret}"))
    with TestClient(create_app(load_config(path), backend)) as client:
        response = client.post("/v1/audio/speech", headers=auth(token), json={
            "model": "omnivoice", "voice": "voice-01", "input": secret, "response_format": "wav"
        })
    assert response.status_code == 500
    assert response.json() == {"error": {"message": "Synthesis failed", "type": "server_error"}}
    assert secret not in response.text
    assert "/private/path" not in response.text


def test_mp3_uses_injected_bounded_converter(tmp_path: Path) -> None:
    path, token = make_config(tmp_path)
    seen: list[bytes] = []

    def converter(data: bytes) -> bytes:
        seen.append(data)
        return b"ID3-safe-output"

    with TestClient(create_app(load_config(path), FakeBackend(), converter)) as client:
        response = client.post("/v1/audio/speech", headers=auth(token), json={
            "model": "omnivoice", "voice": "voice-01", "input": "Hallo", "response_format": "mp3"
        })
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"ID3-safe-output"
    assert len(seen) == 1 and seen[0].startswith(b"RIFF")


@pytest.mark.asyncio
async def test_concurrent_inference_fails_fast(tmp_path: Path) -> None:
    path, token = make_config(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingBackend:
        def synthesize(self, **_kwargs: object) -> bytes:
            started.set()
            if not release.wait(2):
                raise RuntimeError("test timeout")
            return wav_bytes()

    transport = httpx.ASGITransport(app=create_app(load_config(path), BlockingBackend()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post("/v1/audio/speech", headers=auth(token), json={
            "model": "omnivoice", "voice": "voice-01", "input": "first", "response_format": "wav"
        }))
        assert await asyncio.to_thread(started.wait, 1)
        second = await client.post("/v1/audio/speech", headers=auth(token), json={
            "model": "omnivoice", "voice": "voice-01", "input": "second", "response_format": "wav"
        })
        release.set()
        first_response = await first
    assert second.status_code == 503
    assert second.json() == {"error": {"message": "Service busy", "type": "server_error"}}
    assert first_response.status_code == 200


@pytest.mark.asyncio
async def test_inference_timeout_returns_bounded_error_and_keeps_gpu_serialized(
    tmp_path: Path,
) -> None:
    path, token = make_config(tmp_path)
    settings = replace(load_config(path), inference_timeout_seconds=0.01)
    release = threading.Event()

    class SlowBackend:
        def ready(self) -> None:
            return None

        def synthesize(self, **_kwargs: object) -> bytes:
            release.wait(2)
            return wav_bytes()

    transport = httpx.ASGITransport(app=create_app(settings, SlowBackend()))
    payload = {
        "model": "omnivoice",
        "voice": "voice-01",
        "input": "Hallo",
        "response_format": "wav",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        timed_out = await client.post("/v1/audio/speech", headers=auth(token), json=payload)
        busy = await client.post("/v1/audio/speech", headers=auth(token), json=payload)
        release.set()
        for _ in range(100):
            recovered = await client.get("/ready", headers=auth(token))
            if recovered.status_code == 200:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("inference lock was not released after worker completion")

    assert timed_out.status_code == 504
    assert timed_out.json() == {
        "error": {"message": "Synthesis timed out", "type": "server_error"}
    }
    assert busy.status_code == 503
