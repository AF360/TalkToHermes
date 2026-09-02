from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

from worker.hermes_voice_worker import (
    HermesVoiceBackend,
    WorkerError,
    dispatch_request,
    parse_request,
)


class FakeVoiceBackend:
    def normalize(self, text: str) -> str:
        return "normalized:" + text

    def stt_local(
        self, input_path: str, model: str | None = None, language: str | None = None
    ) -> dict:
        return {
            "success": True, "transcript": "Hello", "provider": "local",
            "model": model, "language": language,
        }

    def tts(self, text: str, provider: str, output_path: str, voice: str | None) -> dict:
        output = Path(output_path)
        output.write_bytes(b"generated")
        output.chmod(0o600)
        return {
            "success": True,
            "file_path": output_path,
            "provider": provider,
            "voice": voice,
        }


def test_parse_request_rejects_non_object() -> None:
    with pytest.raises(WorkerError, match="object"):
        parse_request("[]")


def test_parse_request_rejects_invalid_json_without_echoing_input() -> None:
    secret = "do-not-echo-this-secret"
    with pytest.raises(WorkerError) as caught:
        parse_request(f'{{"secret":"{secret}"')
    assert secret not in str(caught.value)


def test_dispatch_rejects_unknown_operation() -> None:
    with pytest.raises(WorkerError, match="unknown_operation"):
        dispatch_request({"operation": "shell"}, FakeVoiceBackend())


def test_normalize_uses_existing_backend() -> None:
    response = dispatch_request({"operation": "normalize", "text": "Hallo"}, FakeVoiceBackend())
    assert response == {"ok": True, "text": "normalized:Hallo"}


@pytest.mark.parametrize("provider", ["edge", "openai", "", "Piper;sh"])
def test_tts_allows_only_explicit_piper_provider(provider: str) -> None:
    with pytest.raises(WorkerError, match="unsupported_provider"):
        dispatch_request(
            {
                "operation": "tts",
                "provider": provider,
                "text": "Hallo",
                "output_path": "/tmp/out.wav",
                "voice": "de_DE-thorsten-high",
            },
            FakeVoiceBackend(),
        )


def test_tts_rejects_relative_or_traversal_output_path(tmp_path) -> None:
    for output_path in ("out.wav", str(tmp_path / ".." / "out.wav")):
        with pytest.raises(WorkerError, match="invalid_output_path"):
            dispatch_request(
                {
                    "operation": "tts",
                    "provider": "piper",
                    "text": "Hallo",
                    "output_path": output_path,
                    "voice": "de_DE-thorsten-high",
                },
                FakeVoiceBackend(),
            )


def _private_output(tmp_path, name: str = "speech.wav"):
    tmp_path.chmod(0o700)
    output = tmp_path / name
    output.touch(mode=0o600)
    return output


@pytest.mark.parametrize("voice", ["de_DE-thorsten-high", "de_DE-ramona-low"])
def test_tts_passes_only_allowlisted_piper_voice(tmp_path, voice: str) -> None:
    output = _private_output(tmp_path)
    response = dispatch_request(
        {
            "operation": "tts",
            "provider": "piper",
            "text": "Hallo",
            "output_path": str(output),
            "voice": voice,
        },
        FakeVoiceBackend(),
    )
    assert response["ok"] is True
    assert response["provider"] == "piper"
    assert response["file_path"] == str(output)
    assert response["voice"] == voice


@pytest.mark.parametrize("voice", [None, "", "Thorsten-high", "de_DE-thorsten-high;sh"])
def test_tts_rejects_missing_or_unapproved_piper_voice(tmp_path, voice: str | None) -> None:
    with pytest.raises(WorkerError, match="unsupported_voice"):
        dispatch_request(
            {
                "operation": "tts",
                "provider": "piper",
                "text": "Hallo",
                "output_path": str(tmp_path / "speech.wav"),
                "voice": voice,
            },
            FakeVoiceBackend(),
        )


@pytest.mark.parametrize("text", ["x" * 2001, "Hallo\x00Welt", "Hallo\x01Welt"])
def test_tts_rejects_oversized_or_control_character_text(tmp_path, text: str) -> None:
    with pytest.raises(WorkerError):
        dispatch_request(
            {
                "operation": "tts", "provider": "piper",
                "voice": "de_DE-thorsten-high", "text": text,
                "output_path": str(tmp_path / "speech.wav"),
            },
            FakeVoiceBackend(),
        )


def test_stt_local_requires_absolute_existing_regular_file(tmp_path) -> None:
    missing = tmp_path / "missing.wav"
    with pytest.raises(WorkerError, match="invalid_input_path"):
        dispatch_request({"operation": "stt-local", "input_path": str(missing)}, FakeVoiceBackend())


def test_stt_local_returns_normalized_contract(tmp_path) -> None:
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"RIFF")
    response = dispatch_request(
        {"operation": "stt-local", "input_path": str(audio), "language": "en-US"},
        FakeVoiceBackend(),
    )
    assert response == {"ok": True, "provider": "local", "text": "Hello"}


def test_stt_local_passes_configured_model_to_backend(tmp_path) -> None:
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"RIFF")
    captured: list[tuple[str | None, str | None]] = []

    class Backend(FakeVoiceBackend):
        def stt_local(
            self, input_path: str, model: str | None = None, language: str | None = None
        ) -> dict:
            captured.append((model, language))
            return super().stt_local(input_path, model, language)

    dispatch_request(
        {
            "operation": "stt-local", "input_path": str(audio),
            "model": "large-v3", "language": "en-US",
        },
        Backend(),
    )
    assert captured == [("large-v3", "en-US")]


def test_hermes_backend_passes_primary_language_to_local_faster_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    transcription_tools = ModuleType("tools.transcription_tools")
    transcription_tools._HAS_FASTER_WHISPER = True  # type: ignore[attr-defined]
    transcription_tools._normalize_local_model = lambda model: f"normalized:{model}"  # type: ignore[attr-defined]

    def transcribe(path: str, model: str, *, language: str | None = None) -> dict:
        captured.update(path=path, model=model, language=language)
        return {"success": True, "transcript": "Hello", "provider": "local"}

    transcription_tools._transcribe_local = transcribe  # type: ignore[attr-defined]
    tools = ModuleType("tools")
    tools.transcription_tools = transcription_tools  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", transcription_tools)

    result = HermesVoiceBackend().stt_local("/private/input.wav", "small", "en-US")

    assert result["success"] is True
    assert captured == {
        "path": "/private/input.wav",
        "model": "normalized:small",
        "language": "en",
    }


def test_response_is_json_serializable(tmp_path) -> None:
    output = _private_output(tmp_path)
    response = dispatch_request(
        {
            "operation": "tts", "provider": "piper", "voice": "de_DE-ramona-low",
            "text": "Hallo", "output_path": str(output),
        },
        FakeVoiceBackend(),
    )
    json.dumps(response)


def test_tts_requires_precreated_private_owned_output(tmp_path) -> None:
    base = {
        "operation": "tts",
        "provider": "piper",
        "voice": "de_DE-thorsten-high",
        "text": "Hallo",
    }
    candidates = [tmp_path / "missing.wav"]
    insecure = tmp_path / "insecure.wav"
    insecure.touch(mode=0o644)
    candidates.append(insecure)
    target = _private_output(tmp_path, "target.wav")
    link = tmp_path / "link.wav"
    link.symlink_to(target)
    candidates.append(link)

    for candidate in candidates:
        with pytest.raises(WorkerError, match="invalid_output_path"):
            dispatch_request({**base, "output_path": str(candidate)}, FakeVoiceBackend())


def test_hermes_backend_uses_cached_model_and_in_memory_override_without_mutation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir(mode=0o775)
    model = voices_dir / "de_DE-thorsten-high.onnx"
    model.write_bytes(b"model")
    model.chmod(0o600)
    metadata = voices_dir / "de_DE-thorsten-high.onnx.json"
    metadata.write_text("{}", encoding="utf-8")
    metadata.chmod(0o600)
    original = {
        "provider": "edge",
        "piper": {"voice": "existing", "voices_dir": str(voices_dir), "use_cuda": False},
    }
    observed: dict = {}
    tts_tool = ModuleType("tools.tts_tool")
    tts_tool._load_tts_config = lambda: original  # type: ignore[attr-defined]

    def generate_piper(text, output_path, config):
        observed["text"] = text
        observed["output_path"] = output_path
        observed["config"] = config
        return output_path

    tts_tool._generate_piper_tts = generate_piper  # type: ignore[attr-defined]
    tools = ModuleType("tools")
    tools.tts_tool = tts_tool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts_tool)

    output = _private_output(tmp_path)
    result = HermesVoiceBackend().tts(
        "Hallo", "piper", str(output), "de_DE-thorsten-high"
    )

    assert result["success"] is True
    override = observed["config"]
    assert override["provider"] == "piper"
    assert override["piper"]["voice"] == str(model)
    assert stat.S_IMODE(voices_dir.stat().st_mode) & 0o022 == 0
    assert original == {
        "provider": "edge",
        "piper": {"voice": "existing", "voices_dir": str(voices_dir), "use_cuda": False},
    }


def test_hermes_backend_refuses_missing_cached_voice_without_synthesis(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir(mode=0o700)
    called = False
    tts_tool = ModuleType("tools.tts_tool")
    tts_tool._load_tts_config = lambda: {"piper": {"voices_dir": str(voices_dir)}}  # type: ignore[attr-defined]

    def forbidden(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("synthesis must not trigger an implicit download")

    tts_tool._generate_piper_tts = forbidden  # type: ignore[attr-defined]
    tools = ModuleType("tools")
    tools.tts_tool = tts_tool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts_tool)

    with pytest.raises(WorkerError, match="piper_voice_unavailable"):
        HermesVoiceBackend().tts(
            "Hallo", "piper", str(_private_output(tmp_path)), "de_DE-thorsten-high"
        )
    assert called is False


def test_dispatch_stages_output_and_atomically_replaces_symlink_target(tmp_path) -> None:
    final = _private_output(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    victim.chmod(0o600)

    class ReplacingBackend(FakeVoiceBackend):
        def tts(self, text: str, provider: str, output_path: str, voice: str | None) -> dict:
            final.unlink()
            final.symlink_to(victim)
            staging = Path(output_path)
            staging.write_bytes(b"generated")
            staging.chmod(0o600)
            return {
                "success": True,
                "file_path": output_path,
                "provider": provider,
                "voice": voice,
            }

    response = dispatch_request(
        {
            "operation": "tts",
            "provider": "piper",
            "voice": "de_DE-thorsten-high",
            "text": "Hallo",
            "output_path": str(final),
        },
        ReplacingBackend(),
    )
    assert response["ok"] is True
    assert victim.read_bytes() == b"unchanged"
    assert not final.is_symlink()
    assert final.read_bytes() == b"generated"
