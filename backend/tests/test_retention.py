from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from talktohermes.app import _periodic_cleanup
from talktohermes.retention import RetentionManager
from talktohermes.storage import Storage


def setup(tmp_path: Path) -> tuple[Storage, Path, str]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    root = state / "audio"
    root.mkdir(mode=0o700)
    db = Storage(state / "turns.sqlite3")
    conversation = db.create_conversation("session")
    return db, root, conversation.id


def private_file(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"audio")
    path.chmod(0o600)
    return path


def failed_turn(db: Storage, conversation_id: str, upload: Path, client_id: str = "client"):
    turn, _ = db.create_or_get_voice_turn(conversation_id, client_id, "fingerprint", str(upload))
    return db.fail_turn(turn.id, "stt_error")


def completed_turn(db: Storage, conversation_id: str, upload: Path, answer: Path):
    turn, _ = db.create_or_get_voice_turn(conversation_id, "completed", "fingerprint-2", str(upload))
    db.start_transcription(turn.id)
    db.set_stt_result(turn.id, "wyoming", False, "gesprochene Frage")
    db.set_run(turn.id, "run")
    db.start_synthesis(turn.id, "answer")
    return db.complete_voice_turn(turn.id, str(answer), "omnivoice", "voice-02")


def test_terminal_text_is_redacted_at_exactly_24_hours_without_losing_metadata(
    tmp_path: Path,
) -> None:
    db, root, conversation_id = setup(tmp_path)
    turn, _ = db.create_or_get_text_turn(
        conversation_id, "text-retention", "private input"
    )
    db.set_run(turn.id, "run-retention")
    db.append_event(turn.id, "hermes.delta", {"delta": "private output"})
    db.append_event(
        turn.id,
        "hermes.tool_started",
        {"tool": "BrowserTool", "summary": "private search terms"},
    )
    db.append_event(turn.id, "tts.provider_attempt", {"provider": "omnivoice"})
    completed = db.complete_turn(turn.id, "private output")
    assert completed.terminal_at is not None
    terminal_at = datetime.fromisoformat(completed.terminal_at).astimezone(UTC)
    manager = RetentionManager(db, root, text_hours=24)
    assert manager.seconds_until_text_expiry(terminal_at) == 24 * 60 * 60

    manager.cleanup(terminal_at + timedelta(hours=24) - timedelta(microseconds=1))
    retained = db.get_turn(turn.id)
    assert (retained.input_text, retained.response_text) == (
        "private input", "private output"
    )
    assert [event.event_type for event in db.list_events(turn.id)] == [
        "hermes.delta",
        "hermes.tool_started",
        "tts.provider_attempt",
    ]

    manager.cleanup(terminal_at + timedelta(hours=24))
    redacted = db.get_turn(turn.id)
    assert (redacted.input_text, redacted.response_text) == ("", None)
    assert redacted.state == "completed"
    assert redacted.hermes_run_id == "run-retention"
    assert redacted.terminal_at == completed.terminal_at
    assert redacted.updated_at == completed.updated_at
    assert [event.event_type for event in db.list_events(turn.id)] == [
        "hermes.tool_started",
        "tts.provider_attempt"
    ]
    assert db.list_tool_invocations(turn.id) == [
        {
            "id": "tool-2",
            "name": "BrowserTool",
            "status": "invoked",
            "started_at": db.list_events(turn.id)[0].created_at,
            "approval_required": False,
        }
    ]
    assert manager.seconds_until_text_expiry(terminal_at + timedelta(hours=24)) is None

    manager.cleanup(terminal_at + timedelta(days=2))
    repeated = db.get_turn(turn.id)
    assert repeated.updated_at == completed.updated_at
    assert (repeated.input_text, repeated.response_text) == ("", None)


def test_active_turn_text_is_never_redacted(tmp_path: Path) -> None:
    db, root, conversation_id = setup(tmp_path)
    turn, _ = db.create_or_get_text_turn(
        conversation_id, "active-retention", "still processing"
    )
    manager = RetentionManager(db, root, text_hours=24)

    manager.cleanup(datetime.now(UTC) + timedelta(days=30))

    retained = db.get_turn(turn.id)
    assert retained.state == "accepted"
    assert retained.input_text == "still processing"


@pytest.mark.asyncio
async def test_periodic_cleanup_recovers_and_schedules_exact_text_expiry() -> None:
    class FlakyRetention:
        def __init__(self) -> None:
            self.cleanup_calls = 0

        def cleanup(self) -> None:
            self.cleanup_calls += 1
            if self.cleanup_calls == 1:
                raise OSError("transient cleanup failure")

        def seconds_until_text_expiry(self) -> float:
            return 5.0

    retention = FlakyRetention()
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _periodic_cleanup(retention, 900.0, sleep=fake_sleep)  # type: ignore[arg-type]

    assert retention.cleanup_calls == 2
    assert delays == [5.0, 60.0, 5.0]


@pytest.mark.asyncio
async def test_periodic_cleanup_recovers_from_expiry_lookup_failure() -> None:
    class LookupFlakyRetention:
        def __init__(self) -> None:
            self.lookup_calls = 0
            self.cleanup_calls = 0

        def cleanup(self) -> None:
            self.cleanup_calls += 1

        def seconds_until_text_expiry(self) -> float:
            self.lookup_calls += 1
            if self.lookup_calls == 1:
                raise OSError("transient expiry lookup failure")
            return 5.0

    retention = LookupFlakyRetention()
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _periodic_cleanup(retention, 900.0, sleep=fake_sleep)  # type: ignore[arg-type]

    assert retention.cleanup_calls == 1
    assert delays == [60.0, 5.0]


def test_failed_audio_retention_off_removes_upload_immediately(tmp_path: Path) -> None:
    db, root, conversation_id = setup(tmp_path)
    upload = private_file(root, "upload.wav")
    turn = failed_turn(db, conversation_id, upload)

    RetentionManager(db, root, retain_failed_audio=False).cleanup()

    assert not upload.exists()
    assert db.get_turn(turn.id).upload_path is None


def test_failed_audio_is_retained_until_exactly_24_hours(tmp_path: Path) -> None:
    db, root, conversation_id = setup(tmp_path)
    upload = private_file(root, "upload.wav")
    turn = failed_turn(db, conversation_id, upload)
    created = datetime.fromisoformat(turn.created_at).astimezone(UTC)
    manager = RetentionManager(db, root, retain_failed_audio=True, failed_hours=24)

    manager.cleanup(created + timedelta(hours=24) - timedelta(microseconds=1))
    assert upload.exists()
    manager.cleanup(created + timedelta(hours=24))
    assert not upload.exists()


def test_completed_upload_is_removed_but_answer_waits_for_download_lease(tmp_path: Path) -> None:
    db, root, conversation_id = setup(tmp_path)
    upload = private_file(root, "upload.wav")
    answer = private_file(root, "answer.wav")
    turn = completed_turn(db, conversation_id, upload, answer)
    manager = RetentionManager(db, root, grace_seconds=30)

    manager.cleanup()
    assert not upload.exists()
    assert answer.exists()
    assert db.get_turn(turn.id).audio_path == str(answer)


def test_undownloaded_answer_expires_after_24_hours(tmp_path: Path) -> None:
    db, root, conversation_id = setup(tmp_path)
    turn = completed_turn(
        db, conversation_id, private_file(root, "upload.wav"), private_file(root, "answer.wav")
    )
    created = datetime.fromisoformat(turn.created_at).astimezone(UTC)
    manager = RetentionManager(db, root, failed_hours=24)

    manager.cleanup(created + timedelta(hours=24) - timedelta(microseconds=1))
    assert (root / "answer.wav").exists()
    manager.cleanup(created + timedelta(hours=24))
    assert not (root / "answer.wav").exists()


def test_download_reconnect_does_not_extend_existing_lease(tmp_path: Path) -> None:
    db, root, conversation_id = setup(tmp_path)
    turn = completed_turn(
        db, conversation_id, private_file(root, "upload.wav"), private_file(root, "answer.wav")
    )
    manager = RetentionManager(db, root, grace_seconds=30)
    now = datetime.now(UTC)

    first = manager.begin_download_lease(turn, now)
    second = manager.begin_download_lease(first, now + timedelta(seconds=20))

    assert second.audio_lease_until == first.audio_lease_until
    manager.cleanup(now + timedelta(seconds=30))
    assert not (root / "answer.wav").exists()


def test_cleanup_failure_keeps_database_reference_and_returns_bounded_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, root, conversation_id = setup(tmp_path)
    upload = private_file(root, "upload.wav")
    turn = failed_turn(db, conversation_id, upload)
    manager = RetentionManager(db, root, retain_failed_audio=False)

    def fail_unlink(path: Path, failures: list[str]) -> bool:
        failures.append("audio_cleanup_failed")
        return False

    monkeypatch.setattr(manager, "_unlink", fail_unlink)
    assert manager.cleanup() == ["audio_cleanup_failed"]
    assert db.get_turn(turn.id).upload_path == str(upload)


def test_delete_conversation_artifacts_treats_missing_owned_file_as_success(tmp_path: Path) -> None:
    db, root, conversation_id = setup(tmp_path)
    missing = root / "already-removed.wav"
    db.create_or_get_voice_turn(conversation_id, "missing", "fp-missing", str(missing))

    assert RetentionManager(db, root).delete_conversation_artifacts(conversation_id) == []


def test_cleanup_removes_expired_private_regular_orphan_but_never_follows_symlink(tmp_path: Path) -> None:
    db, root, _ = setup(tmp_path)
    orphan = private_file(root, "orphan.wav")
    old = datetime.now(UTC) - timedelta(hours=25)
    orphan.touch()
    import os
    os.utime(orphan, (old.timestamp(), old.timestamp()))
    outside = private_file(tmp_path, "outside.wav")
    link = root / "link.wav"
    link.symlink_to(outside)

    RetentionManager(db, root).cleanup()

    assert not orphan.exists()
    assert link.is_symlink()
    assert outside.exists()


def test_cleanup_keeps_fresh_unreferenced_file_owned_by_inflight_work(tmp_path: Path) -> None:
    db, root, _ = setup(tmp_path)
    inflight = private_file(root, "inflight.wav")

    RetentionManager(db, root).cleanup()

    assert inflight.exists()


def test_cleanup_ignores_wrong_mode_orphan(tmp_path: Path) -> None:
    db, root, _ = setup(tmp_path)
    orphan = private_file(root, "unsafe.wav")
    orphan.chmod(0o644)

    RetentionManager(db, root).cleanup()

    assert orphan.exists()


def test_restart_interrupted_artifacts_are_removed_regardless_of_failed_retention(tmp_path: Path) -> None:
    db, root, conversation_id = setup(tmp_path)
    upload = private_file(root, "interrupted-upload.wav")
    answer = private_file(root, "interrupted-answer.wav")
    turn, _ = db.create_or_get_voice_turn(conversation_id, "restart", "restart-fp", str(upload))
    db.update_audio_references(turn.id, upload_path=str(upload), audio_path=str(answer))
    db.recover_active_turns()

    RetentionManager(db, root, retain_failed_audio=True, failed_hours=24).cleanup_restart_artifacts()

    recovered = db.get_turn(turn.id)
    assert recovered.upload_path is None
    assert recovered.audio_path is None
    assert not upload.exists()
    assert not answer.exists()
