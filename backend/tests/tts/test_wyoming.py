from __future__ import annotations

import asyncio
import json
import os
import wave
from pathlib import Path
from typing import Any

import pytest


def private_output(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    path = tmp_path / "wyoming.wav"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    return path


async def read_event(reader: asyncio.StreamReader) -> tuple[dict[str, Any], dict[str, Any]]:
    header = json.loads(await reader.readline())
    data_length = int(header.get("data_length", 0))
    data = json.loads(await reader.readexactly(data_length)) if data_length else {}
    return header, data


async def write_event(
    writer: asyncio.StreamWriter,
    event_type: str,
    data: dict[str, Any] | None = None,
    payload: bytes = b"",
) -> None:
    encoded_data = json.dumps(data or {}, separators=(",", ":")).encode()
    header: dict[str, Any] = {"type": event_type, "version": "1.8.0"}
    if encoded_data and data:
        header["data_length"] = len(encoded_data)
    if payload:
        header["payload_length"] = len(payload)
    writer.write(json.dumps(header, separators=(",", ":")).encode() + b"\n")
    if data:
        writer.write(encoded_data)
    if payload:
        writer.write(payload)
    await writer.drain()


def route_wyoming_to_test_server(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    from talktohermes.tts import wyoming

    real_open_connection = asyncio.open_connection

    async def open_connection(host: str, requested_port: int):
        assert (host, requested_port) == ("127.0.0.1", 10200)
        return await real_open_connection("127.0.0.1", port)

    monkeypatch.setattr(wyoming.asyncio, "open_connection", open_connection)


@pytest.mark.asyncio
async def test_wyoming_piper_sends_exact_wyoming_request_and_writes_private_wav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.wyoming import WYOMING_PIPER_VOICE, WyomingPiperTTS

    captured: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        captured.append(await read_event(reader))
        audio_format = {"rate": 22050, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", audio_format)
        await write_event(writer, "audio-chunk", audio_format, b"\x01\x00" * 2205)
        await write_event(writer, "audio-stop", {})
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    output = private_output(tmp_path)
    try:
        adapter = WyomingPiperTTS()
        assert adapter.name == "wyoming-piper"
        assert await adapter.synthesize("Hallo fallback voice server", output) == output
    finally:
        server.close()
        await server.wait_closed()

    assert WYOMING_PIPER_VOICE == "de_DE-thorsten-medium"
    header, data = captured[0]
    assert header == {
        "type": "synthesize",
        "version": "1.8.0",
        "data_length": len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
    }
    assert data == {
        "text": "Hallo fallback voice server",
        "voice": {"name": "de_DE-thorsten-medium"},
    }
    assert output.stat().st_mode & 0o777 == 0o600
    with wave.open(str(output), "rb") as audio:
        assert (audio.getframerate(), audio.getsampwidth(), audio.getnchannels()) == (22050, 2, 1)
        assert audio.getnframes() == 2205


@pytest.mark.asyncio
async def test_wyoming_piper_rejects_unexpected_wyoming_protocol_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.base import TTSTechnicalError
    from talktohermes.tts.wyoming import WyomingPiperTTS

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_event(reader)
        data = b'{"rate":22050,"width":2,"channels":1}'
        header = json.dumps(
            {"type": "audio-start", "version": "9.9.9", "data_length": len(data)},
            separators=(",", ":"),
        ).encode()
        writer.write(header + b"\n" + data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    output = private_output(tmp_path)
    try:
        adapter = WyomingPiperTTS()
        with pytest.raises(TTSTechnicalError, match="wyoming_protocol_version"):
            await adapter.synthesize("Hallo", output)
    finally:
        server.close()
        await server.wait_closed()
    assert output.read_bytes() == b""


@pytest.mark.asyncio
async def test_wyoming_piper_rejects_pcm_not_aligned_to_complete_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.base import TTSTechnicalError
    from talktohermes.tts.wyoming import WyomingPiperTTS

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_event(reader)
        audio_format = {"rate": 22050, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", audio_format)
        await write_event(writer, "audio-chunk", audio_format, b"\x01")
        await write_event(writer, "audio-stop", {})
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    output = private_output(tmp_path)
    try:
        adapter = WyomingPiperTTS()
        with pytest.raises(TTSTechnicalError, match="wyoming_invalid_audio"):
            await adapter.synthesize("Hallo", output)
    finally:
        server.close()
        await server.wait_closed()
    assert output.read_bytes() == b""


@pytest.mark.asyncio
async def test_wyoming_piper_rejects_audio_outside_expected_voice_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.base import TTSTechnicalError
    from talktohermes.tts.wyoming import WyomingPiperTTS

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_event(reader)
        unexpected_format = {"rate": 96000, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", unexpected_format)
        await write_event(writer, "audio-chunk", unexpected_format, b"\x00\x00")
        await write_event(writer, "audio-stop", {})
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    output = private_output(tmp_path)
    try:
        adapter = WyomingPiperTTS()
        with pytest.raises(TTSTechnicalError, match="wyoming_invalid_audio"):
            await adapter.synthesize("Hallo", output)
    finally:
        server.close()
        await server.wait_closed()
    assert output.read_bytes() == b""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": "192.168.100.30"},
        {"port": 10201},
        {"target_validator": lambda _host, _port: None},
    ],
)
def test_wyoming_piper_constructor_exposes_no_target_bypass(
    kwargs: dict[str, Any],
) -> None:
    from talktohermes.tts.wyoming import WyomingPiperTTS

    with pytest.raises(TypeError):
        WyomingPiperTTS(**kwargs)


def test_wyoming_endpoint_properties_are_read_only_and_configurable() -> None:
    from talktohermes.tts.wyoming import WyomingPiperTTS

    adapter = WyomingPiperTTS(url="tcp://primary-voice-server.home.arpa:10202")
    with pytest.raises(AttributeError):
        adapter.host = "127.0.0.1"
    with pytest.raises(AttributeError):
        adapter.port = 1
    assert (adapter.host, adapter.port) == ("primary-voice-server.home.arpa", 10202)


@pytest.mark.parametrize(
    "audio_format",
    [
        {"rate": "22050", "width": 2, "channels": 1},
        {"rate": 22050.9, "width": 2.9, "channels": 1.9},
        {"rate": 22050, "width": 2, "channels": True},
    ],
)
def test_wyoming_audio_metadata_requires_exact_json_integer_types(
    audio_format: dict[str, Any],
) -> None:
    from talktohermes.tts.base import TTSTechnicalError
    from talktohermes.tts.wyoming import _audio_format

    with pytest.raises(TTSTechnicalError, match="wyoming_invalid_audio"):
        _audio_format(audio_format, (22050, 2, 1))


def test_wyoming_piper_accepts_allowlisted_per_instance_voice() -> None:
    from talktohermes.tts.wyoming import WyomingPiperTTS

    adapter = WyomingPiperTTS(voice="de_DE-kerstin-low")
    assert adapter.voice == "de_DE-kerstin-low"


def test_wyoming_piper_accepts_configured_language_voice() -> None:
    from talktohermes.tts.wyoming import WyomingPiperTTS

    adapter = WyomingPiperTTS(voice="en_US-lessac-medium")
    assert adapter.voice == "en_US-lessac-medium"


@pytest.mark.asyncio
async def test_wyoming_piper_uses_expected_format_for_selected_low_quality_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.wyoming import WyomingPiperTTS

    captured: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        captured.append(await read_event(reader))
        audio_format = {"rate": 16000, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", audio_format)
        await write_event(writer, "audio-chunk", audio_format, b"\x00\x00" * 1600)
        await write_event(writer, "audio-stop", {})
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    output = private_output(tmp_path)
    try:
        adapter = WyomingPiperTTS(voice="de_DE-kerstin-low")
        assert await adapter.synthesize("Hallo", output) == output
    finally:
        server.close()
        await server.wait_closed()

    assert captured[0][1]["voice"] == {"name": "de_DE-kerstin-low"}
    with wave.open(str(output), "rb") as audio:
        assert (audio.getframerate(), audio.getsampwidth(), audio.getnchannels()) == (16000, 2, 1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout": 0}, "bounds"),
        ({"max_pcm_bytes": 0}, "bounds"),
    ],
)
def test_wyoming_piper_rejects_invalid_bounds(
    kwargs: dict[str, Any], message: str,
) -> None:
    from talktohermes.tts.wyoming import WyomingPiperTTS

    with pytest.raises(ValueError, match=message):
        WyomingPiperTTS(**kwargs)


@pytest.mark.asyncio
async def test_wyoming_piper_timeout_closes_connection_without_writing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.wyoming import WyomingPiperTTS

    closed = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_event(reader)
        try:
            await reader.read()
        finally:
            closed.set()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    output = private_output(tmp_path)
    try:
        adapter = WyomingPiperTTS(timeout=0.01)
        with pytest.raises(asyncio.TimeoutError):
            await adapter.synthesize("Hallo", output)
        await asyncio.wait_for(closed.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()
    assert output.read_bytes() == b""


@pytest.mark.asyncio
async def test_external_cancellation_closes_connection_and_preserves_empty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.wyoming import WyomingPiperTTS

    request_seen = asyncio.Event()
    connection_closed = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_event(reader)
        request_seen.set()
        try:
            await reader.read()
        finally:
            connection_closed.set()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    output = private_output(tmp_path)
    task = asyncio.create_task(WyomingPiperTTS().synthesize("Hallo", output))
    try:
        await asyncio.wait_for(request_seen.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(connection_closed.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()
    assert output.read_bytes() == b""


@pytest.mark.asyncio
async def test_wyoming_piper_detects_output_path_replacement_without_overwriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.base import TTSTechnicalError
    from talktohermes.tts.wyoming import WyomingPiperTTS

    output = private_output(tmp_path)
    replacement = b"do-not-overwrite"

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_event(reader)
        output.unlink()
        output.write_bytes(replacement)
        output.chmod(0o600)
        audio_format = {"rate": 22050, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", audio_format)
        await write_event(writer, "audio-chunk", audio_format, b"\x00\x00")
        await write_event(writer, "audio-stop", {})
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    try:
        with pytest.raises(TTSTechnicalError, match="output_path_changed"):
            await WyomingPiperTTS().synthesize("Hallo", output)
    finally:
        server.close()
        await server.wait_closed()
    assert output.read_bytes() == replacement


@pytest.mark.asyncio
async def test_wyoming_piper_rejects_output_in_nonprivate_directory(tmp_path: Path) -> None:
    from talktohermes.tts.base import TTSTechnicalError
    from talktohermes.tts.wyoming import WyomingPiperTTS

    output = private_output(tmp_path)
    tmp_path.chmod(0o755)
    with pytest.raises(TTSTechnicalError, match="invalid_output_path"):
        await WyomingPiperTTS().synthesize("Hallo", output)


@pytest.mark.asyncio
async def test_wyoming_piper_rejects_oversized_stream_before_reading_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktohermes.tts.base import TTSTechnicalError
    from talktohermes.tts.wyoming import WyomingPiperTTS

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_event(reader)
        audio_format = {"rate": 22050, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", audio_format)
        encoded_format = json.dumps(audio_format, separators=(",", ":")).encode()
        header = {
            "type": "audio-chunk", "version": "1.8.0",
            "data_length": len(encoded_format), "payload_length": 3,
        }
        writer.write(json.dumps(header, separators=(",", ":")).encode() + b"\n")
        writer.write(encoded_format)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    route_wyoming_to_test_server(monkeypatch, port)
    output = private_output(tmp_path)
    try:
        adapter = WyomingPiperTTS(max_pcm_bytes=2)
        with pytest.raises(TTSTechnicalError, match="wyoming_audio_too_large"):
            await adapter.synthesize("Hallo", output)
    finally:
        server.close()
        await server.wait_closed()
    assert output.read_bytes() == b""
