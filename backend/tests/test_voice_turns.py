from __future__ import annotations

import asyncio
import io
import os
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import talktohermes.app as app_module
from talktohermes.app import create_app
from talktohermes.settings import load_settings
from talktohermes.storage import NotFoundError, Storage
from talktohermes.stt.base import STTAttempt, STTResult
from tests.test_settings import write_instance


class Hermes:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.response_styles: list[str] = []
        self.stops: list[str] = []

    async def create_session(self, title: str = "TalkToHermes") -> str:
        return "session-voice"

    async def start_run(
        self, session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        self.prompts.append(input_text)
        self.response_styles.append(response_style)
        return "run-voice"

    async def events(self, run_id: str) -> AsyncIterator[dict]:
        yield {"event": "run.completed", "output": "Die Antwort"}

    async def get_run(self, run_id: str) -> dict:
        return {"output": "Die Antwort"}

    async def approve(self, run_id: str, decision: str) -> None:
        return None

    async def stop(self, run_id: str) -> None:
        self.stops.append(run_id)


class STT:
    def __init__(self) -> None:
        self.languages: list[str] = []

    async def transcribe(self, path: Path, language: str) -> STTResult:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.suffix == ".wav"
        self.languages.append(language)
        return STTResult("Spoken question", "wyoming", (STTAttempt("wyoming", 1, "success"),))


class TTSResult:
    provider = "omnivoice"
    voice = "voice-02"

    def __init__(self, audio_path: Path) -> None:
        self.audio_path = audio_path


class TTS:
    def __init__(self) -> None:
        self.languages: list[str] = []

    async def synthesize(self, text: str, output_dir: Path, language: str = "de") -> TTSResult:
        assert text == "Die Antwort"
        self.languages.append(language)
        path = output_dir / "answer.wav"
        path.write_bytes(wav_bytes())
        path.chmod(0o600)
        return TTSResult(path)


def wav_bytes(*, seconds: float = 0.1, rate: int = 8_000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * int(seconds * rate))
    return stream.getvalue()


def bounded_wav(size: int) -> bytes:
    frames = (size - 44) // 2
    data = wav_bytes(seconds=frames / 48_000, rate=48_000)
    assert len(data) == size
    return data


def client(tmp_path: Path) -> tuple[TestClient, str, Storage, Hermes, STT, TTS]:
    settings = load_settings(write_instance(tmp_path))
    storage = Storage(settings.state_dir / "voice.sqlite3")
    hermes = Hermes()
    stt = STT()
    tts = TTS()
    app = create_app(settings, storage=storage, hermes=hermes, stt=stt, tts=tts)
    return TestClient(app), settings.app_token.get_secret_value(), storage, hermes, stt, tts


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_authenticated_voice_turn_runs_stt_hermes_tts_and_downloads_audio(tmp_path: Path) -> None:
    api, token, storage, hermes, stt, tts = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    response = api.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=auth(token),
        files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
        data={
            "client_turn_id": "a2de28e1-8f40-4ae0-9350-c693e99bcfea",
            "language": "en-US",
            "response_style": "detailed",
        },
    )
    assert response.status_code == 202
    turn_id = response.json()["turn_id"]
    turn = api.get(f"/v1/turns/{turn_id}", headers=auth(token)).json()
    assert turn["state"] == "completed", (turn, [(e.event_type, e.payload) for e in storage.list_events(turn_id)])
    assert turn["response_text"] == "Die Antwort"
    assert turn["degraded_local_audio"] is False
    assert hermes.prompts == ["Spoken question"]
    assert hermes.response_styles == ["detailed"]
    assert stt.languages == ["en-US"]
    assert tts.languages == ["en-US"]
    events = api.get(f"/v1/turns/{turn_id}/events", headers=auth(token)).text
    for event in ("turn.accepted", "stt.started", "stt.completed", "hermes.started", "hermes.completed", "tts.started", "tts.completed", "turn.completed"):
        assert event in events
    assert "Gesprochene Frage" not in events
    assert str(tmp_path) not in events
    audio = api.get(f"/v1/turns/{turn_id}/audio", headers=auth(token))
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.content[:4] == b"RIFF"
    assert storage.get_turn(turn_id).input_text == "Spoken question"
    assert storage.get_turn(turn_id).response_style == "detailed"


def test_completed_turn_exposes_safe_per_call_tool_metadata(tmp_path: Path) -> None:
    api, token, storage, hermes, _, _ = client(tmp_path)

    async def events(run_id: str) -> AsyncIterator[dict]:
        yield {
            "event": "tool.started",
            "tool": "web_search",
            "preview": "Öffne /private/path mit token=abc123",
        }

        yield {"event": "tool.started", "tool": "invalid tool name", "preview": "secret"}
        yield {
            "event": "tool.started",
            "tool": "mcp__home_assistant__ha_get_state",
            "preview": "entity_id=person.andreas",
        }
        yield {
            "event": "tool.started",
            "tool": "functions.browser_exec",
            "preview": "Suche Wetter in Bochum",
        }
        yield {"event": "tool.started", "tool": "terminal", "preview": "secret"}

        yield {"event": "run.completed", "output": "Die Antwort"}

    hermes.events = events  # type: ignore[method-assign]

    with api:
        conversation_id = api.post(
            "/v1/conversations", headers=auth(token)
        ).json()["conversation_id"]
        submitted = api.post(
            f"/v1/conversations/{conversation_id}/turns",
            headers=auth(token),
            files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
            data={"client_turn_id": "b2de28e1-8f40-4ae0-9350-c693e99bcfea"},
        )

        turn_id = submitted.json()["turn_id"]
        turn: dict = {}
        for _ in range(100):
            turn = api.get(f"/v1/turns/{turn_id}", headers=auth(token)).json()
            if turn["state"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.01)

        assert turn["state"] == "completed", (
            turn,
            [(event.event_type, event.payload) for event in storage.list_events(turn_id)],
        )
        assert turn["input_text"] == "Spoken question"
        assert turn["tools"] == [
            "web_search",
            "mcp__home_assistant__ha_get_state",
            "functions.browser_exec",
            "terminal",
        ]
        assert [
            {key: invocation.get(key) for key in (
                "name", "summary", "status", "approval_required", "risk"
            )}
            for invocation in turn["tool_invocations"]
        ] == [
            {
                "name": "web_search",
                "summary": None,
                "status": "invoked",
                "approval_required": False,
                "risk": None,
            },
            {
                "name": "mcp__home_assistant__ha_get_state",
                "summary": None,
                "status": "invoked",
                "approval_required": False,
                "risk": None,
            },
            {
                "name": "functions.browser_exec",
                "summary": "Browseraktion ausgeführt",
                "status": "invoked",
                "approval_required": False,
                "risk": None,
            },
            {
                "name": "terminal",
                "summary": None,
                "status": "invoked",
                "approval_required": False,
                "risk": None,
            },
        ]
        assert all(
            invocation["id"].startswith("tool-") for invocation in turn["tool_invocations"]
        )
        assert all(invocation["started_at"] for invocation in turn["tool_invocations"])
        assert "/private/path" not in str(turn)
        assert "abc123" not in str(turn)
        assert "invalid tool name" not in str(turn)
        assert "entity_id=person.andreas" not in str(turn)
        event_stream = api.get(
            f"/v1/turns/{turn_id}/events", headers=auth(token)
        ).text
        assert "Browseraktion ausgeführt" in event_stream
        assert "Suche Wetter in Bochum" not in event_stream
        assert "/private/path" not in event_stream
        assert "abc123" not in event_stream
        assert "invalid tool name" not in event_stream
        assert "entity_id=person.andreas" not in event_stream


def test_voice_turn_rejects_unknown_response_style(tmp_path: Path) -> None:
    api, token, _, hermes, _, _ = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    response = api.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=auth(token),
        files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
        data={
            "client_turn_id": "a2de28e1-8f40-4ae0-9350-c693e99bcfea",
            "response_style": "unbounded",
        },
    )
    assert response.status_code == 422
    assert hermes.prompts == []


def test_upload_enforces_exact_ten_mib_boundary_before_provider_call(tmp_path: Path) -> None:
    api, token, _, hermes, _, _ = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    endpoint = f"/v1/conversations/{conversation_id}/turns"
    first = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("boundary.wav", bounded_wav(10 * 1024 * 1024), "audio/wav")},
        data={"client_turn_id": "67ebc888-5fa6-46c3-ad4f-36315e4f50b5"},
    )
    assert first.status_code == 202
    over = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("over.wav", bounded_wav(10 * 1024 * 1024) + b"x", "audio/wav")},
        data={"client_turn_id": "ec26e095-b46e-47e2-829f-8f870e754c73"},
    )
    assert over.status_code == 413
    parser_bound = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("huge.wav", b"x" * (11 * 1024 * 1024), "audio/wav")},
        data={"client_turn_id": "fc26e095-b46e-47e2-829f-8f870e754c73"},
    )
    assert parser_bound.status_code == 413
    assert len(hermes.prompts) == 1


def test_voice_turn_requires_canonical_uuid(tmp_path: Path) -> None:
    api, token, _, _, _, _ = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    endpoint = f"/v1/conversations/{conversation_id}/turns"
    for value in ("", "not-a-uuid", "{a2de28e1-8f40-4ae0-9350-c693e99bcfea}", "A2DE28E1-8F40-4AE0-9350-C693E99BCFEA"):
        response = api.post(
            endpoint,
            headers=auth(token),
            files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
            data={"client_turn_id": value},
        )
        assert response.status_code == 422, value


def test_voice_idempotency_requires_same_content_and_options(tmp_path: Path) -> None:
    api, token, _, hermes, _, _ = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    endpoint = f"/v1/conversations/{conversation_id}/turns"
    turn_uuid = "a2de28e1-8f40-4ae0-9350-c693e99bcfea"
    request = {
        "headers": auth(token),
        "files": {"audio": ("question.wav", wav_bytes(), "audio/wav")},
        "data": {"client_turn_id": turn_uuid, "include_text": "false"},
    }
    first = api.post(endpoint, **request)
    duplicate = api.post(endpoint, **request)
    conflict_content = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("question.wav", wav_bytes(seconds=0.2), "audio/wav")},
        data={"client_turn_id": turn_uuid, "include_text": "false"},
    )
    conflict_options = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
        data={"client_turn_id": turn_uuid, "include_text": "true"},
    )
    conflict_style = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
        data={
            "client_turn_id": turn_uuid,
            "include_text": "false",
            "response_style": "detailed",
        },
    )

    assert first.json()["turn_id"] == duplicate.json()["turn_id"]
    assert conflict_content.status_code == 409
    assert conflict_options.status_code == 409
    assert conflict_style.status_code == 409
    assert hermes.prompts == ["Spoken question"]
    turn = api.get(f"/v1/turns/{first.json()['turn_id']}", headers=auth(token)).json()
    assert "response_text" not in turn


def test_empty_wrong_mime_and_filename_traversal_are_handled_safely(tmp_path: Path) -> None:
    api, token, _, hermes, _, _ = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    endpoint = f"/v1/conversations/{conversation_id}/turns"
    empty = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("empty.wav", b"", "audio/wav")},
        data={"client_turn_id": "a2de28e1-8f40-4ae0-9350-c693e99bcfea"},
    )
    wrong = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("question.wav", wav_bytes(), "text/plain")},
        data={"client_turn_id": "b2de28e1-8f40-4ae0-9350-c693e99bcfea"},
    )
    traversal = api.post(
        endpoint,
        headers=auth(token),
        files={"audio": ("../../secret.wav", wav_bytes(), "audio/wav")},
        data={"client_turn_id": "c2de28e1-8f40-4ae0-9350-c693e99bcfea"},
    )

    assert empty.status_code == 422
    assert wrong.status_code == 422
    assert traversal.status_code == 202
    assert not (tmp_path / "secret.wav").exists()
    assert len(hermes.prompts) == 1


def test_upload_rejects_wav_over_120_seconds(tmp_path: Path) -> None:
    api, token, _, hermes, _, _ = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    response = api.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=auth(token),
        files={"audio": ("long.wav", wav_bytes(seconds=120.001), "audio/wav")},
        data={"client_turn_id": "a2de28e1-8f40-4ae0-9350-c693e99bcfea"},
    )
    assert response.status_code == 422
    assert hermes.prompts == []


def test_audio_download_rejects_symlink_and_insecure_mode(tmp_path: Path) -> None:
    api, token, storage, _, _, _ = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    response = api.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=auth(token),
        files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
        data={"client_turn_id": "a2de28e1-8f40-4ae0-9350-c693e99bcfea"},
    )
    turn = storage.get_turn(response.json()["turn_id"])
    answer = Path(turn.audio_path or "")
    answer.chmod(0o644)
    assert api.get(f"/v1/turns/{turn.id}/audio", headers=auth(token)).status_code == 404

    answer.chmod(0o600)
    target = answer.with_name("target.wav")
    answer.rename(target)
    answer.symlink_to(target)
    assert api.get(f"/v1/turns/{turn.id}/audio", headers=auth(token)).status_code == 404


def test_deleting_conversation_removes_its_audio_artifacts(tmp_path: Path) -> None:
    api, token, storage, _, _, _ = client(tmp_path)
    conversation_id = api.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    response = api.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=auth(token),
        files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
        data={"client_turn_id": "a2de28e1-8f40-4ae0-9350-c693e99bcfea"},
    )
    answer = Path(storage.get_turn(response.json()["turn_id"]).audio_path or "")
    assert answer.exists()

    assert api.delete(f"/v1/conversations/{conversation_id}", headers=auth(token)).status_code == 204
    assert not answer.exists()


@pytest.mark.asyncio
async def test_voice_upload_rejected_after_concurrent_delete_is_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(write_instance(tmp_path))
    storage = Storage(settings.state_dir / "upload-race.sqlite3")
    hermes = Hermes()
    app = create_app(settings, storage=storage, hermes=hermes, stt=STT(), tts=TTS())
    stored = asyncio.Event()
    release = asyncio.Event()
    original_store = app_module.store_upload

    async def paused_store(*args, **kwargs):
        result = await original_store(*args, **kwargs)
        stored.set()
        await release.wait()
        return result

    monkeypatch.setattr(app_module, "store_upload", paused_store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        headers = auth(settings.app_token.get_secret_value())
        conversation_id = (await api.post("/v1/conversations", headers=headers)).json()[
            "conversation_id"
        ]
        creating = asyncio.create_task(api.post(
            f"/v1/conversations/{conversation_id}/turns",
            headers=headers,
            files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
            data={"client_turn_id": "a2de28e1-8f40-4ae0-9350-c693e99bcfea"},
        ))
        await asyncio.wait_for(stored.wait(), timeout=1)

        deleted = await api.delete(f"/v1/conversations/{conversation_id}", headers=headers)
        release.set()
        rejected = await asyncio.wait_for(creating, timeout=1)

    assert deleted.status_code == 204
    assert rejected.status_code == 404
    assert list(app.state.audio_root.iterdir()) == []
    assert storage.list_turns() == []
    assert hermes.prompts == []


def test_unexpected_voice_mapping_failure_unlinks_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(write_instance(tmp_path))
    storage = Storage(settings.state_dir / "mapping-failure.sqlite3")
    app = create_app(settings, storage=storage, hermes=Hermes(), stt=STT(), tts=TTS())
    api = TestClient(app, raise_server_exceptions=False)
    headers = auth(settings.app_token.get_secret_value())
    conversation_id = api.post("/v1/conversations", headers=headers).json()["conversation_id"]

    def fail_mapping(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(storage, "create_or_get_voice_turn", fail_mapping)
    response = api.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=headers,
        files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
        data={"client_turn_id": "4c32840a-0eba-4c0a-b1a0-6e5f39987f24"},
    )

    assert response.status_code == 500
    assert list(app.state.audio_root.iterdir()) == []


def test_startup_resumes_durable_conversation_deletion(tmp_path: Path) -> None:
    settings = load_settings(write_instance(tmp_path))
    storage = Storage(settings.state_dir / "resume-delete.sqlite3")
    conversation = storage.create_conversation("session-resume")
    upload = settings.state_dir / "audio" / "resume.wav"
    upload.parent.mkdir(mode=0o700)
    upload.write_bytes(wav_bytes())
    upload.chmod(0o600)
    storage.create_or_get_voice_turn(
        conversation.id, "resume", "resume-fingerprint", str(upload)
    )
    storage.begin_conversation_delete(conversation.id)

    create_app(settings, storage=storage, hermes=Hermes(), stt=STT(), tts=TTS())

    assert not upload.exists()
    with pytest.raises(NotFoundError):
        storage.get_conversation(conversation.id)
