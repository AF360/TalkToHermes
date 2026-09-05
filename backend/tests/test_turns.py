from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from talktohermes.storage import Storage, TransitionError
from talktohermes.stt.base import STTAttempt, STTResult
from talktohermes.stt.chain import STTChainError
from talktohermes.turns import QuiescenceTimeoutError, TurnService


class StartRunRaceHermes:
    def __init__(self) -> None:
        self.start_called = asyncio.Event()
        self.release_start = asyncio.Event()
        self.started_runs = 0
        self.stops: list[str] = []

    async def create_session(self, title: str = "TalkToHermes") -> str:
        return "session-1"

    async def start_run(
        self, session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        self.started_runs += 1
        self.start_called.set()
        await self.release_start.wait()
        return "run-race"

    async def events(self, run_id: str) -> AsyncIterator[dict]:
        if False:
            yield {}

    async def get_run(self, run_id: str) -> dict:
        return {}

    async def approve(self, run_id: str, decision: str) -> None:
        return None

    async def stop(self, run_id: str) -> None:
        self.stops.append(run_id)


class CancellationRaceHermes:
    def __init__(self, storage: Storage, turn_id: str) -> None:
        self.storage = storage
        self.turn_id = turn_id
        self.stop_called = asyncio.Event()

    async def create_session(self, title: str = "TalkToHermes") -> str:
        return "session-1"

    async def start_run(
        self, session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        return "run-1"

    async def events(self, run_id: str) -> AsyncIterator[dict]:
        await self.stop_called.wait()
        yield {"event": "run.cancelled"}

    async def get_run(self, run_id: str) -> dict:
        return {}

    async def approve(self, run_id: str, decision: str) -> None:
        return None

    async def stop(self, run_id: str) -> None:
        self.stop_called.set()
        for _ in range(100):
            if self.storage.get_turn(self.turn_id).state == "cancelled":
                return
            await asyncio.sleep(0.001)
        raise AssertionError("event consumer did not observe cancellation")


class ApprovalHermes:
    def __init__(self, expires_at: str | None = None) -> None:
        self.expires_at = expires_at
        self.approvals: list[tuple[str, str]] = []
        self.stops: list[str] = []

    async def create_session(self, title: str = "TalkToHermes") -> str:
        return "session-1"

    async def start_run(
        self, session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        return "run-1"

    async def events(self, run_id: str) -> AsyncIterator[dict]:
        event = {"event": "approval.request", "tool": "shell"}
        if self.expires_at is not None:
            event["expires_at"] = self.expires_at
        yield event
        await asyncio.Event().wait()

    async def get_run(self, run_id: str) -> dict:
        return {}

    async def approve(self, run_id: str, decision: str) -> None:
        self.approvals.append((run_id, decision))

    async def stop(self, run_id: str) -> None:
        self.stops.append(run_id)


def make_turn(tmp_path: Path) -> tuple[Storage, str]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    storage = Storage(state / "talktohermes.sqlite3")
    conversation = storage.create_conversation("session-1")
    turn, _ = storage.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    return storage, turn.id


@pytest.mark.asyncio
async def test_cancel_before_processing_prevents_upstream_run(tmp_path: Path) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = StartRunRaceHermes()
    service = TurnService(storage, hermes)

    await service.cancel(turn_id)
    hermes.release_start.set()
    await service.process_text_turn(turn_id)

    assert storage.get_turn(turn_id).state == "cancelled"
    assert hermes.started_runs == 0
    assert hermes.stops == []


@pytest.mark.asyncio
async def test_cancel_while_start_run_is_in_flight_stops_new_run(tmp_path: Path) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = StartRunRaceHermes()
    service = TurnService(storage, hermes)
    processing = asyncio.create_task(service.process_text_turn(turn_id))
    await asyncio.wait_for(hermes.start_called.wait(), timeout=1)

    await service.cancel(turn_id)
    hermes.release_start.set()
    await asyncio.wait_for(processing, timeout=1)

    assert storage.get_turn(turn_id).state == "cancelled"
    assert hermes.started_runs == 1
    assert hermes.stops == ["run-race"]


@pytest.mark.asyncio
async def test_waiting_terminal_cancel_quiesces_text_start_run_before_deletion(tmp_path: Path) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = StartRunRaceHermes()
    service = TurnService(storage, hermes)
    processing = asyncio.create_task(service.process_text_turn(turn_id))
    await asyncio.wait_for(hermes.start_called.wait(), timeout=1)
    await service.cancel(turn_id)

    quiescing = asyncio.create_task(service.cancel(turn_id, wait=True))
    await asyncio.sleep(0)
    assert not quiescing.done()
    hermes.release_start.set()
    await asyncio.wait_for(quiescing, timeout=1)
    await asyncio.wait_for(processing, timeout=1)

    assert storage.get_turn(turn_id).state == "cancelled"
    assert hermes.stops == ["run-race"]


@pytest.mark.asyncio
async def test_transition_error_terminal_race_still_uses_quiescence_path(tmp_path: Path) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = StartRunRaceHermes()
    service = TurnService(storage, hermes, quiescence_timeout_seconds=0.5)
    processing = service.start_text_turn(turn_id)
    await asyncio.wait_for(hermes.start_called.wait(), timeout=1)
    original_cancel = storage.cancel_turn

    def lose_terminal_race(raced_turn_id: str):
        original_cancel(raced_turn_id)
        raise TransitionError("lost terminal race")

    storage.cancel_turn = lose_terminal_race  # type: ignore[method-assign]
    quiescing = asyncio.create_task(service.cancel(turn_id, wait=True))
    await asyncio.sleep(0)

    assert not quiescing.done()
    hermes.release_start.set()
    await asyncio.wait_for(quiescing, timeout=1)
    await asyncio.wait_for(processing, timeout=1)
    assert hermes.stops == ["run-race"]


@pytest.mark.asyncio
async def test_never_returning_start_run_bounds_quiescence_without_cancelling_consumer(
    tmp_path: Path,
) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = StartRunRaceHermes()
    service = TurnService(storage, hermes, quiescence_timeout_seconds=0.02)
    processing = service.start_text_turn(turn_id)
    await asyncio.wait_for(hermes.start_called.wait(), timeout=1)

    with pytest.raises(QuiescenceTimeoutError):
        await asyncio.wait_for(service.cancel(turn_id, wait=True), timeout=0.2)

    assert storage.get_turn(turn_id).state == "cancelled"
    assert not processing.done()
    hermes.release_start.set()
    await asyncio.wait_for(processing, timeout=1)
    assert hermes.stops == ["run-race"]


@pytest.mark.asyncio
async def test_cancel_is_idempotent_when_run_cancel_event_wins_race(tmp_path: Path) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = CancellationRaceHermes(storage, turn_id)
    service = TurnService(storage, hermes)
    processing = asyncio.create_task(service.process_text_turn(turn_id))
    for _ in range(100):
        if storage.get_turn(turn_id).state == "thinking":
            break
        await asyncio.sleep(0.001)
    else:
        pytest.fail("turn never entered thinking")

    await service.cancel(turn_id)
    await asyncio.wait_for(processing, timeout=1)

    assert storage.get_turn(turn_id).state == "cancelled"
    events = [event.event_type for event in storage.list_events(turn_id)]
    assert events.count("turn.cancelled") == 1


@pytest.mark.asyncio
async def test_approval_is_rejected_unless_explicitly_awaiting(tmp_path: Path) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = ApprovalHermes()
    service = TurnService(storage, hermes)
    storage.set_run(turn_id, "run-1")

    with pytest.raises(ValueError, match="not active"):
        await service.approve(turn_id, "once")

    assert hermes.approvals == []


@pytest.mark.asyncio
async def test_approval_transport_failure_emits_redacted_terminal_event_and_stops(tmp_path: Path) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = ApprovalHermes()
    service = TurnService(storage, hermes)
    storage.set_run(turn_id, "run-1")
    storage.await_approval(turn_id, (datetime.now(UTC) + timedelta(minutes=1)).isoformat())

    async def fail_approval(run_id: str, decision: str) -> None:
        raise RuntimeError("secret upstream details")

    hermes.approve = fail_approval  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="secret upstream details"):
        await service.approve(turn_id, "once")

    assert storage.get_turn(turn_id).state == "failed"
    terminal = [event for event in storage.list_events(turn_id) if event.event_type == "turn.failed"]
    assert len(terminal) == 1
    assert terminal[0].payload == {
        "error": {"code": "approval_error", "retryable": False}
    }
    assert hermes.stops == ["run-1"]


@pytest.mark.asyncio
async def test_approval_timeout_denies_stops_and_terminalizes_without_upstream_expiry(
    tmp_path: Path,
) -> None:
    storage, turn_id = make_turn(tmp_path)
    hermes = ApprovalHermes()
    service = TurnService(storage, hermes, approval_timeout_seconds=0.02)

    await asyncio.wait_for(service.process_text_turn(turn_id), timeout=1)

    assert hermes.approvals == [("run-1", "deny")]
    assert hermes.stops == ["run-1"]
    turn = storage.get_turn(turn_id)
    assert turn.state == "failed"
    assert turn.error_code == "approval_timeout"
    approval_events = [
        event
        for event in storage.list_events(turn_id)
        if event.event_type == "hermes.approval_required"
    ]
    assert len(approval_events) == 1
    assert approval_events[0].payload["tool"] == "shell"


@pytest.mark.asyncio
async def test_approved_request_cannot_be_lost_to_timeout_race(tmp_path: Path) -> None:
    storage, turn_id = make_turn(tmp_path)
    expires_at = (datetime.now(UTC) + timedelta(milliseconds=50)).isoformat()
    hermes = ApprovalHermes(expires_at)
    service = TurnService(storage, hermes)
    processing = asyncio.create_task(service.process_text_turn(turn_id))
    try:
        for _ in range(100):
            if storage.get_turn(turn_id).state == "awaiting_approval":
                break
            await asyncio.sleep(0.001)
        else:
            pytest.fail("turn never entered awaiting_approval")

        await service.approve(turn_id, "once")
        await asyncio.sleep(0.075)

        assert not processing.done()
        assert storage.get_turn(turn_id).state == "thinking"
        assert hermes.approvals == [("run-1", "once")]
        assert hermes.stops == []
    finally:
        processing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await processing


class CompletedHermes(StartRunRaceHermes):
    async def start_run(
        self, session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        return "run-voice"

    async def events(self, run_id: str) -> AsyncIterator[dict]:
        yield {"event": "run.completed", "output": "Antwort"}


class BlockingSTT:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def transcribe(self, path: Path, language: str) -> STTResult:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ImmediateSTT:
    async def transcribe(self, path: Path, language: str) -> STTResult:
        return STTResult("Frage", "wyoming", (STTAttempt("wyoming", 0, "success"),))


class FallbackSTT:
    async def transcribe(self, path: Path, language: str) -> STTResult:
        return STTResult(
            "Geheime Frage",
            "wyoming",
            (
                STTAttempt(
                    "openai", 12.5, "unavailable",
                    "openai_provider_unavailable", "open",
                ),
                STTAttempt("wyoming", 8.25, "success"),
            ),
        )


class ExhaustedSTT:
    async def transcribe(self, path: Path, language: str) -> STTResult:
        raise STTChainError((
            STTAttempt(
                "openai", 14.0, "unavailable",
                "openai_provider_unavailable", "open",
            ),
        ))


class InvalidTelemetrySTT:
    async def transcribe(self, path: Path, language: str) -> STTResult:
        raise STTChainError((
            STTAttempt(
                "openai", None, "unavailable",  # type: ignore[arg-type]
                "openai_provider_unavailable", "open",
            ),
        ))


class FallbackTTS:
    async def synthesize(self, text: str, output_dir: Path, language: str = "de"):
        output = output_dir / "private-result.wav"
        output.write_bytes(b"RIFFprivate")
        output.chmod(0o600)
        unavailable = type("Attempt", (), {
            "provider": "omnivoice",
            "voice": "voice-02",
            "segment_index": 0,
            "attempt": 1,
            "accepted": False,
            "reason": "omnivoice_provider_unavailable",
            "outcome": "unavailable",
            "elapsed_ms": 10.5,
            "circuit_state": "open",
        })()
        success = type("Attempt", (), {
            "provider": "wyoming-piper",
            "voice": "de_DE-thorsten-medium",
            "segment_index": 0,
            "attempt": 1,
            "accepted": True,
            "reason": None,
            "outcome": "success",
            "elapsed_ms": 7.5,
            "circuit_state": "closed",
        })()
        return type("Speech", (), {
            "audio_path": output,
            "provider": "wyoming-piper",
            "voice": "de_DE-thorsten-medium",
            "attempts": (unavailable, success),
        })()


class BlockingTTS:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def synthesize(self, text: str, output_dir: Path, language: str = "de"):
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def make_voice_turn(tmp_path: Path) -> tuple[Storage, str, Path]:
    state = tmp_path / "voice-state"
    state.mkdir(mode=0o700)
    audio_root = state / "audio"
    audio_root.mkdir(mode=0o700)
    upload = audio_root / "upload.wav"
    upload.write_bytes(b"private")
    upload.chmod(0o600)
    storage = Storage(state / "voice.sqlite3")
    conversation = storage.create_conversation("session-voice")
    turn, _ = storage.create_or_get_voice_turn(conversation.id, "client-voice", "fp", str(upload))
    return storage, turn.id, audio_root


@pytest.mark.asyncio
async def test_cancel_during_transcribing_cancels_and_awaits_active_stt(tmp_path: Path) -> None:
    storage, turn_id, audio_root = make_voice_turn(tmp_path)
    stt = BlockingSTT()
    service = TurnService(storage, CompletedHermes(), stt=stt, tts=BlockingTTS(), audio_root=audio_root)
    processing = asyncio.create_task(service.process_voice_turn(turn_id))
    await asyncio.wait_for(stt.entered.wait(), timeout=1)

    await service.cancel(turn_id)

    assert processing.done()
    assert storage.get_turn(turn_id).state == "cancelled"


@pytest.mark.asyncio
async def test_cancel_during_synthesizing_cancels_and_awaits_active_tts(tmp_path: Path) -> None:
    storage, turn_id, audio_root = make_voice_turn(tmp_path)
    tts = BlockingTTS()
    service = TurnService(storage, CompletedHermes(), stt=ImmediateSTT(), tts=tts, audio_root=audio_root)
    processing = asyncio.create_task(service.process_voice_turn(turn_id))
    await asyncio.wait_for(tts.entered.wait(), timeout=1)
    await service.cancel(turn_id)

    assert processing.done()
    assert storage.get_turn(turn_id).state == "cancelled"


@pytest.mark.asyncio
async def test_voice_fallback_emits_only_privacy_safe_attempt_telemetry(tmp_path: Path) -> None:
    storage, turn_id, audio_root = make_voice_turn(tmp_path)
    service = TurnService(
        storage, CompletedHermes(), stt=FallbackSTT(), tts=FallbackTTS(),
        audio_root=audio_root,
    )

    await service.process_voice_turn(turn_id)

    events = storage.list_events(turn_id)
    stt_attempts = [event.payload for event in events if event.event_type == "stt.provider_attempt"]
    tts_attempts = [event.payload for event in events if event.event_type == "tts.provider_attempt"]
    assert stt_attempts == [
        {
            "provider": "openai", "outcome": "unavailable",
            "error_code": "openai_provider_unavailable", "elapsed_ms": 12.5,
            "circuit_state": "open", "selected_fallback": "wyoming",
        },
        {
            "provider": "wyoming", "outcome": "success",
            "error_code": None, "elapsed_ms": 8.25,
            "circuit_state": "closed", "selected_fallback": "wyoming",
        },
    ]
    assert tts_attempts[0] == {
        "provider": "omnivoice", "outcome": "unavailable",
        "error_code": "omnivoice_provider_unavailable", "elapsed_ms": 10.5,
        "circuit_state": "open", "selected_fallback": "wyoming-piper",
    }
    serialized = repr(stt_attempts + tts_attempts)
    for forbidden in ("Geheime Frage", "Antwort", str(audio_root), "Bearer", "token"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_failed_stt_still_emits_safe_attempt_telemetry(tmp_path: Path) -> None:
    storage, turn_id, audio_root = make_voice_turn(tmp_path)
    service = TurnService(
        storage, CompletedHermes(), stt=ExhaustedSTT(), tts=BlockingTTS(),
        audio_root=audio_root,
    )

    await service.process_voice_turn(turn_id)

    events = storage.list_events(turn_id)
    attempts = [
        event.payload for event in events
        if event.event_type == "stt.provider_attempt"
    ]
    event_types = [event.event_type for event in events]
    assert event_types.index("stt.provider_attempt") < event_types.index("turn.failed")
    assert attempts == [{
        "provider": "openai", "outcome": "unavailable",
        "error_code": "openai_provider_unavailable", "elapsed_ms": 14.0,
        "circuit_state": "open", "selected_fallback": None,
    }]


@pytest.mark.asyncio
async def test_invalid_failure_telemetry_cannot_strand_active_turn(tmp_path: Path) -> None:
    storage, turn_id, audio_root = make_voice_turn(tmp_path)
    service = TurnService(
        storage, CompletedHermes(), stt=InvalidTelemetrySTT(), tts=BlockingTTS(),
        audio_root=audio_root,
    )

    with pytest.raises(RuntimeError, match="invalid provider attempt metadata"):
        await service.process_voice_turn(turn_id)

    assert storage.get_turn(turn_id).state == "failed"
    assert storage.list_events(turn_id)[-1].event_type == "turn.failed"


@pytest.mark.parametrize(
    ("selected_provider", "elapsed_ms"),
    [("https://private.example/secret", 1.0), ("wyoming", float("nan"))],
)
def test_attempt_telemetry_rejects_unsafe_fallback_or_nonfinite_timing(
    tmp_path: Path, selected_provider: str, elapsed_ms: float
) -> None:
    storage, turn_id = make_turn(tmp_path)
    service = TurnService(storage, CompletedHermes())
    attempt = type("Attempt", (), {
        "provider": "openai", "outcome": "unavailable",
        "error_code": "openai_provider_unavailable",
        "elapsed_ms": elapsed_ms, "circuit_state": "open",
    })()

    with pytest.raises(RuntimeError, match="invalid provider attempt metadata"):
        service._append_provider_attempts(
            turn_id, "stt", (attempt,), selected_provider
        )

    assert not [
        event for event in storage.list_events(turn_id)
        if event.event_type == "stt.provider_attempt"
    ]
