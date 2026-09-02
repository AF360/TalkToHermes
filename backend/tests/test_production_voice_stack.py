from __future__ import annotations

from pathlib import Path

import pytest

from talktohermes import production
from talktohermes.settings import load_settings
from talktohermes.stt import OpenAICompatibleSTT, LocalSTT, STTChain, WyomingSTT
from talktohermes.tts import (
    OmniVoiceTTS,
    BoundedLanguageVerifier,
    DeterministicTextPreparer,
    HermesWorkerTextNormalizer,
    PiperWorkerTTS,
    QualityOrchestrator,
    WyomingPiperTTS,
)
from tests.test_settings import write_instance


@pytest.mark.asyncio
async def test_build_voice_stack_has_exact_hardened_production_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(write_instance(tmp_path))
    monkeypatch.setattr(
        production,
        "_validate_wyoming_stt_paths",
        lambda: (
            Path("/opt/hermes-stt-wyoming/venv/bin/python"),
            Path("/opt/hermes-stt-wyoming/app/wyoming_stt.py"),
        ),
    )

    stack = production.build_voice_stack(settings)
    try:
        assert isinstance(stack.stt, STTChain)
        openai_stt, wyoming, local = stack.stt._providers
        assert isinstance(openai_stt, OpenAICompatibleSTT)
        assert openai_stt.model == "large-v3-turbo"
        assert openai_stt._timeout.connect == 0.5
        assert openai_stt._timeout.read == 120.0
        assert openai_stt._circuit.cooldown_seconds == 45.0
        assert isinstance(wyoming, WyomingSTT)
        assert wyoming.uri == "tcp://fallback-voice-server.home.arpa:10300"
        assert wyoming._python_path == Path("/opt/hermes-stt-wyoming/venv/bin/python")
        assert wyoming._script_path == Path(
            "/opt/hermes-stt-wyoming/app/wyoming_stt.py"
        )
        assert isinstance(local, LocalSTT)

        assert isinstance(stack.tts, QualityOrchestrator)
        assert isinstance(stack.tts._preparer, DeterministicTextPreparer)
        assert isinstance(stack.tts._preparer._normalizer, HermesWorkerTextNormalizer)
        omnivoice_tts, wyoming_tts, local_piper = stack.tts._providers
        assert isinstance(omnivoice_tts, OmniVoiceTTS)
        assert omnivoice_tts.voice == "voice-02"
        assert omnivoice_tts._timeout.connect == 0.5
        assert omnivoice_tts._timeout.read == 120.0
        assert omnivoice_tts._circuit.cooldown_seconds == 45.0
        assert omnivoice_tts._circuit is not openai_stt._circuit
        assert isinstance(wyoming_tts, WyomingPiperTTS)
        assert wyoming_tts.host == "fallback-voice-server.home.arpa"
        assert wyoming_tts.port == 10201
        assert wyoming_tts.voice == "de_DE-thorsten-medium"
        assert isinstance(local_piper, PiperWorkerTTS)
        assert local_piper.voice == "de_DE-ramona-low"
        assert isinstance(stack.tts._verifier, BoundedLanguageVerifier)
        assert stack.tts._verifier._provider is openai_stt
        assert stack.closeables == (openai_stt, omnivoice_tts)
    finally:
        for closeable in stack.closeables:
            await closeable.aclose()


@pytest.mark.asyncio
async def test_build_voice_stack_passes_remote_resilience_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_instance(tmp_path)
    config.write_text(config.read_text()
        .replace("    model: large-v3-turbo\n", "    model: large-v3-turbo\n    connect_timeout_seconds: 0.8\n    response_timeout_seconds: 80\n    circuit_cooldown_seconds: 55\n")
        .replace("    voice: voice-02\n", "    voice: voice-02\n    connect_timeout_seconds: 0.9\n    response_timeout_seconds: 90\n    circuit_cooldown_seconds: 65\n"))
    monkeypatch.setattr(production, "_validate_wyoming_stt_paths", lambda: (
        Path("/opt/hermes-stt-wyoming/venv/bin/python"),
        Path("/opt/hermes-stt-wyoming/app/wyoming_stt.py"),
    ))
    stack = production.build_voice_stack(load_settings(config))
    try:
        stt = stack.stt._providers[0]
        tts = stack.tts._providers[0]
        assert (stt._timeout.connect, stt._timeout.read, stt._circuit.cooldown_seconds) == (0.8, 80, 55)
        assert (tts._timeout.connect, tts._timeout.read, tts._circuit.cooldown_seconds) == (0.9, 90, 65)
    finally:
        for closeable in stack.closeables:
            await closeable.aclose()


@pytest.mark.asyncio
async def test_build_voice_stack_uses_instance_specific_wyoming_piper_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text().replace(
            "voice: de_DE-thorsten-medium", "voice: de_DE-kerstin-low"
        )
    )
    settings = load_settings(config)
    monkeypatch.setattr(
        production,
        "_validate_wyoming_stt_paths",
        lambda: (
            Path("/opt/hermes-stt-wyoming/venv/bin/python"),
            Path("/opt/hermes-stt-wyoming/app/wyoming_stt.py"),
        ),
    )

    stack = production.build_voice_stack(settings)
    try:
        assert stack.tts._providers[1].voice == "de_DE-kerstin-low"
    finally:
        for closeable in stack.closeables:
            await closeable.aclose()


@pytest.mark.asyncio
async def test_build_voice_stack_skips_omitted_optional_fallbacks(
    tmp_path: Path,
) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text()
        .replace(
            "  - type: wyoming\n    url: tcp://fallback-voice-server.home.arpa:10300\n",
            "",
        )
        .replace(
            "  - type: wyoming-piper\n    url: tcp://fallback-voice-server.home.arpa:10201\n    voice: de_DE-thorsten-medium\n",
            "",
        )
    )
    stack = production.build_voice_stack(load_settings(config))
    try:
        assert [provider.name for provider in stack.stt._providers] == ["openai", "local"]
        assert [provider.name for provider in stack.tts._providers] == ["omnivoice", "piper"]
    finally:
        for closeable in stack.closeables:
            await closeable.aclose()


def test_build_voice_stack_rejects_missing_fixed_wyoming_adapter_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(write_instance(tmp_path))
    monkeypatch.setattr(production, "WYOMING_STT_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setattr(production, "WYOMING_STT_SCRIPT", str(tmp_path / "missing-script"))

    with pytest.raises(ValueError, match="fallback voice server"):
        production.build_voice_stack(settings)


def test_wyoming_interpreter_must_resolve_to_reviewed_root_system_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(write_instance(tmp_path))
    monkeypatch.setattr(production, "WYOMING_STT_PYTHON", "/bin/sh")

    with pytest.raises(ValueError, match="fallback voice server interpreter"):
        production.build_voice_stack(settings)