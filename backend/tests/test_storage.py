from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from talktohermes.storage import NotFoundError, SCHEMA, Storage, TransitionError


def storage(tmp_path: Path) -> Storage:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return Storage(state / "talktohermes.sqlite3")


def test_creates_private_database_and_conversation_mapping(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hermes-session-1")
    assert conversation.hermes_session_id == "hermes-session-1"
    assert db.get_conversation(conversation.id) == conversation
    assert os.stat(db.path).st_mode & 0o777 == 0o600


def test_conversation_ids_do_not_expose_hermes_session_id(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("secret-hermes-session")
    assert "secret" not in conversation.id
    assert "hermes" not in conversation.id


def test_missing_conversation_raises_not_found(tmp_path: Path) -> None:
    db = storage(tmp_path)
    with pytest.raises(NotFoundError):
        db.get_conversation("missing")


def test_client_turn_id_is_idempotent(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    first, first_created = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    second, second_created = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    assert first.id == second.id
    assert first_created is True
    assert second_created is False


def test_reused_client_turn_id_with_different_input_is_conflict(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    with pytest.raises(ValueError, match="different input"):
        db.create_or_get_text_turn(conversation.id, "client-1", "Andere Nachricht")


def test_concurrent_duplicate_creates_one_turn(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")

    def create() -> tuple[str, bool]:
        turn, created = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
        return turn.id, created

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: create(), range(16)))
    assert len({turn_id for turn_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1


def test_events_are_monotonic_and_survive_reopen(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    turn, _ = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    first = db.append_event(turn.id, "turn.accepted", {"safe": True})
    second = db.append_event(turn.id, "hermes.started", {})
    assert [first.sequence, second.sequence] == [1, 2]

    reopened = Storage(db.path)
    assert [event.sequence for event in reopened.list_events(turn.id)] == [1, 2]


def test_turn_can_be_completed_without_returning_audio_path(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    turn, _ = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    db.set_run(turn.id, "run-1")
    completed = db.complete_turn(turn.id, "Antwort")
    public = completed.public_dict()
    assert public["state"] == "completed"
    assert public["response_text"] == "Antwort"
    assert "audio_path" not in public
    assert "hermes_run_id" not in public


def test_existing_database_is_migrated_for_approval_expiry(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "talktohermes.sqlite3"
    legacy_schema = SCHEMA.replace("    approval_expires_at TEXT,\n", "")
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema)

    Storage(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(turns)")}
    assert "approval_expires_at" in columns


def test_existing_terminal_turn_gets_stable_terminal_timestamp_migration(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "talktohermes.sqlite3"
    legacy_schema = SCHEMA.replace("    terminal_at TEXT,\n", "")
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema)
        connection.execute(
            "INSERT INTO conversations(id, hermes_session_id, created_at, updated_at) "
            "VALUES ('conv-terminal', 'hs-terminal', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            """INSERT INTO turns(
                id, conversation_id, client_turn_id, state, input_text,
                response_text, created_at, updated_at
            ) VALUES (
                'turn-terminal', 'conv-terminal', 'client-terminal', 'completed',
                'input', 'output', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:10+00:00'
            )"""
        )
        connection.execute(
            """INSERT INTO events(turn_id, sequence, event_type, payload_json, created_at)
               VALUES ('turn-terminal', 1, 'turn.completed', '{}',
                       '2026-01-01T00:00:05+00:00')"""
        )

    migrated = Storage(path).get_turn("turn-terminal")

    assert migrated.terminal_at == "2026-01-01T00:00:05+00:00"


def test_voice_turn_persists_response_style_and_migrates_legacy_database(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "talktohermes.sqlite3"
    legacy_schema = SCHEMA.replace("    response_style TEXT NOT NULL DEFAULT 'short',\n", "")
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema)
        connection.execute(
            "INSERT INTO conversations(id, hermes_session_id, created_at, updated_at) "
            "VALUES ('conv-legacy-style', 'hs-legacy-style', 'now', 'now')"
        )
        connection.execute(
            """INSERT INTO turns(
                id, conversation_id, client_turn_id, state, input_text,
                request_fingerprint, upload_path, created_at, updated_at
            ) VALUES (
                'turn-legacy-style', 'conv-legacy-style', 'legacy-client', 'accepted', '',
                'legacy-fingerprint', '/private/legacy-upload', 'now', 'now'
            )"""
        )

    db = Storage(path)
    conversation = db.create_conversation("hs-style")
    turn, _ = db.create_or_get_voice_turn(
        conversation.id, "style", "fingerprint", "/private/upload",
        response_style="detailed",
    )

    assert turn.response_style == "detailed"
    replay, created = db.create_or_get_voice_turn(
        "conv-legacy-style", "legacy-client", "new-fingerprint", "/private/retry",
        response_style="short", legacy_fingerprint="legacy-fingerprint",
    )
    assert replay.id == "turn-legacy-style"
    assert created is False
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(turns)")}
    assert "response_style" in columns


def test_existing_database_is_migrated_with_active_conversation_lifecycle(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "talktohermes.sqlite3"
    legacy_schema = SCHEMA.replace("    lifecycle_state TEXT NOT NULL DEFAULT 'active',\n", "")
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema)
        connection.execute(
            "INSERT INTO conversations(id, hermes_session_id, created_at, updated_at) "
            "VALUES ('conv-legacy', 'hs-legacy', 'now', 'now')"
        )

    migrated = Storage(path)

    assert migrated.get_conversation("conv-legacy").public_dict() == {
        "conversation_id": "conv-legacy", "created_at": "now", "updated_at": "now"
    }
    with sqlite3.connect(path) as connection:
        state_value = connection.execute(
            "SELECT lifecycle_state FROM conversations WHERE id = 'conv-legacy'"
        ).fetchone()[0]
    assert state_value == "active"


def test_begin_delete_serializes_and_rejects_new_text_and_voice_turns(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-delete")
    existing, _ = db.create_or_get_text_turn(conversation.id, "before", "Hallo")

    stable_turns = db.begin_conversation_delete(conversation.id)

    assert [turn.id for turn in stable_turns] == [existing.id]
    with pytest.raises(TransitionError, match="being deleted"):
        db.create_or_get_text_turn(conversation.id, "after-text", "Nein")
    with pytest.raises(TransitionError, match="being deleted"):
        db.create_or_get_voice_turn(conversation.id, "after-voice", "fp", "/tmp/upload")
    assert [turn.id for turn in db.begin_conversation_delete(conversation.id)] == [existing.id]


def test_terminal_turn_cannot_be_overwritten(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    turn, _ = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    db.set_run(turn.id, "run-1")
    db.complete_turn(turn.id, "Antwort")

    with pytest.raises(TransitionError):
        db.fail_turn(turn.id, "late_failure")

    assert db.get_turn(turn.id).state == "completed"


def test_concurrent_terminal_transitions_have_one_winner(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    turn, _ = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    db.set_run(turn.id, "run-1")

    def finish(state: str) -> str:
        try:
            if state == "completed":
                db.complete_turn(turn.id, "Antwort")
            else:
                db.cancel_turn(turn.id)
            return "won"
        except TransitionError:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, ("completed", "cancelled")))

    assert sorted(results) == ["lost", "won"]
    assert db.get_turn(turn.id).state in {"completed", "cancelled"}


def test_approval_can_only_be_claimed_once_while_unexpired(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    turn, _ = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    db.set_run(turn.id, "run-1")
    expires_at = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    db.await_approval(turn.id, expires_at)

    resolved = db.resolve_approval(turn.id, datetime.now(UTC).isoformat())
    assert resolved.state == "thinking"
    assert resolved.approval_expires_at is None
    with pytest.raises(TransitionError):
        db.resolve_approval(turn.id, datetime.now(UTC).isoformat())
    with pytest.raises(TransitionError):
        db.expire_approval(turn.id, datetime.now(UTC).isoformat())


def test_expired_approval_cannot_be_claimed(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    turn, _ = db.create_or_get_text_turn(conversation.id, "client-1", "Hallo")
    db.set_run(turn.id, "run-1")
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    db.await_approval(turn.id, expired)

    with pytest.raises(TransitionError):
        db.resolve_approval(turn.id, datetime.now(UTC).isoformat())
    assert db.get_turn(turn.id).state == "awaiting_approval"

    failed = db.expire_approval(turn.id, datetime.now(UTC).isoformat())
    assert failed.state == "failed"
    assert failed.error_code == "approval_timeout"


def test_piper_completion_marks_degraded_local_audio(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    turn, _ = db.create_or_get_voice_turn(conversation.id, "voice", "fp", "/private/upload")
    db.start_transcription(turn.id)
    db.set_stt_result(turn.id, "wyoming", False, "Frage")
    db.set_run(turn.id, "run-1")
    db.start_synthesis(turn.id, "Antwort")

    completed = db.complete_voice_turn(turn.id, "/private/answer", "piper", "de_DE-ramona-low")

    assert completed.degraded_local_audio == 1


def test_wyoming_piper_completion_is_not_marked_as_local_degraded_audio(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-1")
    turn, _ = db.create_or_get_voice_turn(conversation.id, "voice", "fp", "/private/upload")
    db.start_transcription(turn.id)
    db.set_stt_result(turn.id, "wyoming", False, "Frage")
    db.set_run(turn.id, "run-1")
    db.start_synthesis(turn.id, "Antwort")

    completed = db.complete_voice_turn(
        turn.id, "/private/answer", "wyoming-piper", "de_DE-thorsten-medium"
    )

    assert completed.degraded_local_audio == 0


def test_restart_recovery_terminalizes_each_active_state_with_safe_event(tmp_path: Path) -> None:
    db = storage(tmp_path)
    conversation = db.create_conversation("hs-restart")
    turns = []
    for index, state in enumerate(("accepted", "transcribing", "thinking", "awaiting_approval", "synthesizing")):
        turn, _ = db.create_or_get_text_turn(conversation.id, f"client-{index}", "Hallo")
        if state != "accepted":
            db.start_transcription(turn.id)
            if state in {"thinking", "awaiting_approval", "synthesizing"}:
                db.set_run(turn.id, f"run-{index}")
            if state == "awaiting_approval":
                db.await_approval(turn.id, (datetime.now(UTC) + timedelta(minutes=1)).isoformat())
            elif state == "synthesizing":
                db.start_synthesis(turn.id, "private response")
        turns.append(turn.id)

    assert db.recover_active_turns() == 5

    for turn_id in turns:
        recovered = db.get_turn(turn_id)
        assert recovered.state == "failed"
        assert recovered.error_code == "restart_interrupted"
        event = db.list_events(turn_id)[-1]
        assert event.event_type == "turn.failed"
        assert event.payload == {"error": {"code": "restart_interrupted", "retryable": True}}
