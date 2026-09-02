from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from talktohermes_omnivoice.backend import (
    FIXED_GUIDANCE_SCALE,
    FIXED_SEED,
    FIXED_STEPS,
    MODEL_REVISION,
    OmniVoiceBackend,
    _as_pcm16_wav,
)
from talktohermes_omnivoice.cli import ListenerError, validate_listener_available


class FakeSocket:
    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.bound: tuple[str, int] | None = None
        self.closed = False

    def bind(self, address: tuple[str, int]) -> None:
        if self.error:
            raise self.error
        self.bound = address

    def close(self) -> None:
        self.closed = True


def test_listener_preflight_checks_configured_private_ipv4_on_dedicated_port() -> None:
    sock = FakeSocket()
    validate_listener_available("192.168.100.20", 9090, socket_factory=lambda *_: sock)
    assert sock.bound == ("192.168.100.20", 9090)
    assert sock.closed

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
        ("invalid", 9090),
        ("192.168.100.20", 8181),
        ("192.168.100.20", 9091),
    ):
        with pytest.raises(ListenerError, match="dedicated"):
            validate_listener_available(host, port, socket_factory=lambda *_: FakeSocket())
    with pytest.raises(ListenerError, match="unavailable"):
        validate_listener_available("192.168.100.20", 9090, socket_factory=lambda *_: FakeSocket(OSError("secret OS detail")))


def test_backend_uses_official_api_lazily_with_fixed_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class Config:
        @classmethod
        def from_dict(cls, value: dict[str, object]) -> object:
            events.append(("config", value))
            return "fixed-config"

    class Model:
        @classmethod
        def from_pretrained(cls, value: str, **kwargs: object) -> "Model":
            events.append(("model", value, kwargs))
            return cls()

        def generate(self, **kwargs: object) -> list[list[float]]:
            events.append(("generate", kwargs))
            return [[-1.0, 0.0, 1.0]]

    fake_omni = SimpleNamespace(OmniVoice=Model, OmniVoiceGenerationConfig=Config)
    fake_torch = SimpleNamespace(
        float16="float16",
        manual_seed=lambda seed: events.append(("seed", seed)),
        cuda=SimpleNamespace(is_available=lambda: False),
    )
    original_import = importlib.import_module

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "omnivoice":
            return fake_omni
        if name == "torch":
            return fake_torch
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    backend = OmniVoiceBackend()
    assert events == []
    result = backend.synthesize(
        text="Hallo",
        reference_audio=Path("/private/reference.wav"),
        reference_text="Referenz",
        language="German",
        max_output_bytes=1024,
    )
    assert result.startswith(b"RIFF")
    assert ("seed", FIXED_SEED) in events
    assert ("config", {"num_step": FIXED_STEPS, "guidance_scale": FIXED_GUIDANCE_SCALE}) in events
    assert (
        "model",
        "k2-fsa/OmniVoice",
        {
            "device_map": "cuda:0",
            "dtype": "float16",
            "revision": MODEL_REVISION,
        },
    ) in events
    assert ("generate", {
        "text": "Hallo",
        "ref_audio": "/private/reference.wav",
        "ref_text": "Referenz",
        "language": "German",
        "generation_config": "fixed-config",
    }) in events


def test_model_initialization_is_published_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"model": 0, "config": 0}

    class Config:
        @classmethod
        def from_dict(cls, _value: dict[str, object]) -> object:
            attempts["config"] += 1
            if attempts["config"] < 3:
                raise RuntimeError("injected config failure")
            return "complete-config"

    class Model:
        @classmethod
        def from_pretrained(cls, _value: str, **_kwargs: object) -> "Model":
            attempts["model"] += 1
            return cls()

    fake_modules = {
        "omnivoice": SimpleNamespace(
            OmniVoice=Model,
            OmniVoiceGenerationConfig=Config,
        ),
        "torch": SimpleNamespace(float16="float16"),
    }
    original_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> object:
        if name in fake_modules:
            return fake_modules[name]
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    backend = OmniVoiceBackend()
    for expected_attempt in (1, 2):
        with pytest.raises(RuntimeError, match="injected config failure"):
            backend.ready()
        assert attempts == {"model": expected_attempt, "config": expected_attempt}
        assert backend._model is None
        assert backend._generation_config is None

    backend.ready()
    assert attempts == {"model": 3, "config": 3}
    assert backend._model is not None
    assert backend._generation_config == "complete-config"



def test_wav_serialization_rejects_oversized_model_output_before_iteration() -> None:
    class OversizedWaveform:
        size = 10_000_000

        def reshape(self, *_shape: int) -> "OversizedWaveform":
            return self

        def __iter__(self):
            raise AssertionError("oversized waveform must not be iterated")

    with pytest.raises(RuntimeError, match="invalid model output"):
        _as_pcm16_wav([OversizedWaveform()], max_output_bytes=1024)
