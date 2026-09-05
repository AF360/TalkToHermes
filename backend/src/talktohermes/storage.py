from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from .response_style import RESPONSE_STYLES

TOOL_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
MAX_TOOL_INVOCATIONS = 256
MAX_TOOL_LIFECYCLE_EVENTS = 1_024
MAX_EVENT_PAGE = 256

T = TypeVar("T")


class NotFoundError(LookupError):
    pass


class TransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Conversation:
    id: str
    hermes_session_id: str
    created_at: str
    updated_at: str

    def public_dict(self) -> dict[str, Any]:
        return {"conversation_id": self.id, "created_at": self.created_at, "updated_at": self.updated_at}


@dataclass(frozen=True)
class Turn:
    id: str
    conversation_id: str
    client_turn_id: str
    state: str
    input_text: str
    response_text: str | None
    hermes_run_id: str | None
    error_code: str | None
    approval_expires_at: str | None
    terminal_at: str | None
    request_fingerprint: str | None
    upload_path: str | None
    audio_path: str | None
    audio_mime_type: str | None
    stt_provider: str | None
    tts_provider: str | None
    tts_voice: str | None
    degraded_local_audio: int
    audio_lease_until: str | None
    include_text: int
    response_style: str
    language: str
    created_at: str
    updated_at: str

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "turn_id": self.id,
            "conversation_id": self.conversation_id,
            "client_turn_id": self.client_turn_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.response_text is not None and self.include_text:
            result["response_text"] = self.response_text
        if self.input_text and self.include_text:
            result["input_text"] = self.input_text
        if self.error_code is not None:
            result["error_code"] = self.error_code
        result["degraded_local_audio"] = bool(self.degraded_local_audio)
        return result


@dataclass(frozen=True)
class Event:
    turn_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    hermes_session_id TEXT NOT NULL UNIQUE,
    lifecycle_state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    client_turn_id TEXT NOT NULL,
    state TEXT NOT NULL,
    input_text TEXT NOT NULL,
    response_text TEXT,
    hermes_run_id TEXT,
    error_code TEXT,
    approval_expires_at TEXT,
    terminal_at TEXT,
    request_fingerprint TEXT,
    upload_path TEXT,
    audio_path TEXT,
    audio_mime_type TEXT,
    stt_provider TEXT,
    tts_provider TEXT,
    tts_voice TEXT,
    degraded_local_audio INTEGER NOT NULL DEFAULT 0,
    audio_lease_until TEXT,
    include_text INTEGER NOT NULL DEFAULT 1,
    response_style TEXT NOT NULL DEFAULT 'short',
    language TEXT NOT NULL DEFAULT 'de',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(conversation_id, client_turn_id)
);
CREATE TABLE IF NOT EXISTS events (
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(turn_id, sequence)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        hermes_session_id=row["hermes_session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _turn(row: sqlite3.Row) -> Turn:
    return Turn(**dict(row))


class Storage:
    """SQLite state store. One application process must own each database file."""
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("database path must be absolute")
        if not self.path.parent.is_dir():
            raise ValueError("database parent must exist")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            conversation_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "lifecycle_state" not in conversation_columns:
                connection.execute(
                    "ALTER TABLE conversations ADD COLUMN lifecycle_state "
                    "TEXT NOT NULL DEFAULT 'active'"
                )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(turns)").fetchall()
            }
            if "approval_expires_at" not in columns:
                connection.execute("ALTER TABLE turns ADD COLUMN approval_expires_at TEXT")
            migrations = {
                "request_fingerprint": "TEXT", "upload_path": "TEXT", "audio_path": "TEXT",
                "audio_mime_type": "TEXT", "stt_provider": "TEXT", "tts_provider": "TEXT",
                "tts_voice": "TEXT", "degraded_local_audio": "INTEGER NOT NULL DEFAULT 0",
                "audio_lease_until": "TEXT",
                "include_text": "INTEGER NOT NULL DEFAULT 1",
                "response_style": "TEXT NOT NULL DEFAULT 'short'",
                "language": "TEXT NOT NULL DEFAULT 'de'",
                "terminal_at": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE turns ADD COLUMN {name} {definition}")
            connection.execute(
                """UPDATE turns
                   SET terminal_at = COALESCE(
                       (SELECT MIN(events.created_at) FROM events
                        WHERE events.turn_id = turns.id
                          AND events.event_type IN (
                              'turn.completed', 'turn.failed', 'turn.cancelled'
                          )),
                       updated_at
                   )
                   WHERE terminal_at IS NULL
                     AND state IN ('completed','failed','cancelled')"""
            )
            connection.execute("PRAGMA journal_mode = WAL")
        os.chmod(self.path, 0o600)

    def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(connection)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def create_conversation(self, hermes_session_id: str) -> Conversation:
        if not hermes_session_id.strip():
            raise ValueError("hermes_session_id is required")
        created = _now()
        conversation_id = "conv_" + uuid.uuid4().hex

        def insert(connection: sqlite3.Connection) -> Conversation:
            connection.execute(
                "INSERT INTO conversations(id, hermes_session_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, hermes_session_id, created, created),
            )
            row = connection.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            assert row is not None
            return _conversation(row)

        return self._write(insert)

    def get_conversation(self, conversation_id: str) -> Conversation:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise NotFoundError("conversation not found")
        return _conversation(row)

    def require_active_conversation(self, conversation_id: str) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("conversation not found")
        if row["lifecycle_state"] != "active":
            raise TransitionError("conversation is being deleted")
        return _conversation(row)

    def list_deleting_conversation_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM conversations WHERE lifecycle_state = 'deleting' ORDER BY id"
            ).fetchall()
        return [row["id"] for row in rows]

    def delete_conversation(self, conversation_id: str) -> bool:
        def delete(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            return cursor.rowcount == 1

        return self._write(delete)

    def begin_conversation_delete(self, conversation_id: str) -> list[Turn]:
        """Durably close creation and return the complete, stable turn set."""
        def begin(connection: sqlite3.Connection) -> list[Turn]:
            cursor = connection.execute(
                "UPDATE conversations SET lifecycle_state = 'deleting', updated_at = ? "
                "WHERE id = ? AND lifecycle_state IN ('active', 'deleting')",
                (_now(), conversation_id),
            )
            if cursor.rowcount != 1:
                if connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone() is None:
                    raise NotFoundError("conversation not found")
                raise TransitionError("conversation lifecycle transition is not allowed")
            rows = connection.execute(
                "SELECT * FROM turns WHERE conversation_id = ? ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
            return [_turn(row) for row in rows]

        return self._write(begin)

    def create_or_get_text_turn(
        self, conversation_id: str, client_turn_id: str, input_text: str, include_text: bool = True
    ) -> tuple[Turn, bool]:
        if not client_turn_id.strip() or not input_text.strip():
            raise ValueError("client_turn_id and input_text are required")

        def create(connection: sqlite3.Connection) -> tuple[Turn, bool]:
            conversation = connection.execute(
                "SELECT lifecycle_state FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation is None:
                raise NotFoundError("conversation not found")
            if conversation["lifecycle_state"] != "active":
                raise TransitionError("conversation is being deleted")
            existing = connection.execute(
                "SELECT * FROM turns WHERE conversation_id = ? AND client_turn_id = ?",
                (conversation_id, client_turn_id),
            ).fetchone()
            if existing is not None:
                turn = _turn(existing)
                if turn.input_text != input_text or bool(turn.include_text) != include_text:
                    raise ValueError("client_turn_id was reused with different input")
                return turn, False

            created = _now()
            turn_id = "turn_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO turns(
                    id, conversation_id, client_turn_id, state, input_text, include_text,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?)""",
                (turn_id, conversation_id, client_turn_id, input_text, int(include_text), created, created),
            )
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            assert row is not None
            return _turn(row), True

        return self._write(create)

    def create_or_get_voice_turn(
        self, conversation_id: str, client_turn_id: str, fingerprint: str, upload_path: str,
        include_text: bool = True, response_style: str = "short",
        legacy_fingerprint: str | None = None,
        language: str = "de",
    ) -> tuple[Turn, bool]:
        if not client_turn_id or not fingerprint or not upload_path:
            raise ValueError("voice turn fields are required")
        if response_style not in RESPONSE_STYLES:
            raise ValueError("unsupported response style")

        def create(connection: sqlite3.Connection) -> tuple[Turn, bool]:
            conversation = connection.execute(
                "SELECT lifecycle_state FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation is None:
                raise NotFoundError("conversation not found")
            if conversation["lifecycle_state"] != "active":
                raise TransitionError("conversation is being deleted")
            existing = connection.execute(
                "SELECT * FROM turns WHERE conversation_id = ? AND client_turn_id = ?",
                (conversation_id, client_turn_id),
            ).fetchone()
            if existing is not None:
                turn = _turn(existing)
                legacy_match = (
                    response_style == "short"
                    and legacy_fingerprint is not None
                    and turn.request_fingerprint == legacy_fingerprint
                )
                if turn.request_fingerprint != fingerprint and not legacy_match:
                    raise ValueError("client_turn_id was reused with different content or options")
                return turn, False
            now = _now()
            turn_id = "turn_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO turns(id, conversation_id, client_turn_id, state, input_text,
                request_fingerprint, upload_path, include_text, response_style, language,
                created_at, updated_at)
                VALUES (?, ?, ?, 'accepted', '', ?, ?, ?, ?, ?, ?, ?)""",
                (turn_id, conversation_id, client_turn_id, fingerprint, upload_path,
                 int(include_text), response_style, language, now, now),
            )
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            assert row is not None
            return _turn(row), True

        return self._write(create)

    def get_turn(self, turn_id: str) -> Turn:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            raise NotFoundError("turn not found")
        return _turn(row)

    def list_turns(self) -> list[Turn]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM turns ORDER BY created_at").fetchall()
        return [_turn(row) for row in rows]

    def next_text_terminal_at(self) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT MIN(terminal_at) AS terminal_at FROM turns
                   WHERE terminal_at IS NOT NULL
                     AND (
                         input_text != '' OR response_text IS NOT NULL
                         OR EXISTS (
                             SELECT 1 FROM events
                             WHERE events.turn_id = turns.id
                               AND events.event_type = 'hermes.delta'
                         )
                     )"""
            ).fetchone()
        value = row["terminal_at"] if row is not None else None
        if value is None:
            return None
        return datetime.fromisoformat(value).astimezone(UTC)

    def redact_expired_text(self, cutoff: datetime) -> int:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        cutoff_value = cutoff.astimezone(UTC).isoformat()

        def redact(connection: sqlite3.Connection) -> int:
            expired_turn_ids = connection.execute(
                """SELECT id FROM turns
                   WHERE terminal_at IS NOT NULL AND terminal_at <= ?""",
                (cutoff_value,),
            ).fetchall()
            if not expired_turn_ids:
                return 0
            connection.executemany(
                "DELETE FROM events WHERE turn_id = ? AND event_type = 'hermes.delta'",
                ((row["id"],) for row in expired_turn_ids),
            )
            connection.execute(
                """UPDATE events
                   SET payload_json = json_remove(payload_json, '$.summary')
                   WHERE event_type = 'hermes.tool_started'
                     AND json_valid(payload_json)
                     AND json_type(payload_json, '$.summary') IS NOT NULL
                     AND turn_id IN (
                         SELECT id FROM turns
                         WHERE terminal_at IS NOT NULL AND terminal_at <= ?
                     )""",
                (cutoff_value,),
            )
            cursor = connection.execute(
                """UPDATE turns SET input_text = '', response_text = NULL
                   WHERE terminal_at IS NOT NULL AND terminal_at <= ?
                     AND (input_text != '' OR response_text IS NOT NULL)""",
                (cutoff_value,),
            )
            return cursor.rowcount

        return self._write(redact)

    def recover_active_turns(self) -> int:
        def recover(connection: sqlite3.Connection) -> int:
            active = connection.execute(
                """SELECT id FROM turns
                WHERE state IN ('accepted','transcribing','thinking','awaiting_approval','synthesizing')"""
            ).fetchall()
            if not active:
                return 0
            now = _now()
            connection.execute(
                """UPDATE turns SET state = 'failed', error_code = 'restart_interrupted',
                approval_expires_at = NULL, terminal_at = ?, updated_at = ?
                WHERE state IN ('accepted','transcribing','thinking','awaiting_approval','synthesizing')""",
                (now, now),
            )
            payload = json.dumps(
                {"error": {"code": "restart_interrupted", "retryable": True}},
                separators=(",", ":"),
            )
            for row in active:
                turn_id = row["id"]
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO events(turn_id, sequence, event_type, payload_json, created_at)
                    VALUES (?, ?, 'turn.failed', ?, ?)""",
                    (turn_id, sequence, payload, now),
                )
            return len(active)
        return self._write(recover)

    def update_audio_references(
        self, turn_id: str, *, upload_path: str | None = None,
        audio_path: str | None = None, audio_lease_until: str | None = None,
    ) -> Turn:
        state = self.get_turn(turn_id).state
        return self._update_turn(
            turn_id, {state}, upload_path=upload_path, audio_path=audio_path,
            audio_lease_until=audio_lease_until,
        )

    def set_run(self, turn_id: str, run_id: str) -> Turn:
        return self._update_turn(
            turn_id, {"accepted", "transcribing"}, state="thinking", hermes_run_id=run_id
        )

    def start_transcription(self, turn_id: str) -> Turn:
        return self._update_turn(turn_id, {"accepted"}, state="transcribing")

    def set_stt_result(
        self, turn_id: str, provider: str, degraded: bool, input_text: str
    ) -> Turn:
        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text is required")
        return self._update_turn(
            turn_id, {"transcribing"}, stt_provider=provider, input_text=input_text,
            degraded_local_audio=int(degraded),
        )

    def start_synthesis(self, turn_id: str, response_text: str) -> Turn:
        return self._update_turn(
            turn_id, {"thinking"}, state="synthesizing", response_text=response_text
        )

    def complete_voice_turn(
        self, turn_id: str, audio_path: str, provider: str, voice: str
    ) -> Turn:
        degraded = bool(self.get_turn(turn_id).degraded_local_audio) or provider == "piper"
        return self._update_turn(
            turn_id, {"synthesizing"}, state="completed", audio_path=audio_path,
            audio_mime_type="audio/wav", tts_provider=provider, tts_voice=voice,
            degraded_local_audio=int(degraded), terminal_at=_now(),
        )

    def complete_turn(self, turn_id: str, response_text: str) -> Turn:
        return self._update_turn(
            turn_id, {"thinking"}, state="completed", response_text=response_text,
            terminal_at=_now(),
        )

    def fail_turn(self, turn_id: str, error_code: str) -> Turn:
        return self._update_turn(
            turn_id,
            {"accepted", "transcribing", "thinking", "awaiting_approval", "synthesizing"},
            state="failed",
            error_code=error_code,
            approval_expires_at=None,
            terminal_at=_now(),
        )

    def cancel_turn(self, turn_id: str) -> Turn:
        return self._update_turn(
            turn_id,
            {"accepted", "transcribing", "thinking", "awaiting_approval", "synthesizing"},
            state="cancelled",
            approval_expires_at=None,
            terminal_at=_now(),
        )

    def await_approval(self, turn_id: str, expires_at: str) -> Turn:
        if not expires_at:
            raise ValueError("approval expiry is required")
        return self._update_turn(
            turn_id,
            {"thinking"},
            state="awaiting_approval",
            approval_expires_at=expires_at,
        )

    def resolve_approval(self, turn_id: str, now: str) -> Turn:
        def resolve(connection: sqlite3.Connection) -> Turn:
            cursor = connection.execute(
                """UPDATE turns
                SET state = 'thinking', approval_expires_at = NULL, updated_at = ?
                WHERE id = ? AND state = 'awaiting_approval'
                  AND approval_expires_at IS NOT NULL AND approval_expires_at > ?""",
                (now, turn_id, now),
            )
            if cursor.rowcount != 1:
                exists = connection.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("turn not found")
                raise TransitionError("approval is not active")
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            assert row is not None
            return _turn(row)

        return self._write(resolve)

    def expire_approval(self, turn_id: str, now: str) -> Turn:
        def expire(connection: sqlite3.Connection) -> Turn:
            cursor = connection.execute(
                """UPDATE turns
                SET state = 'failed', error_code = 'approval_timeout',
                    approval_expires_at = NULL, terminal_at = ?, updated_at = ?
                WHERE id = ? AND state = 'awaiting_approval'
                  AND approval_expires_at IS NOT NULL AND approval_expires_at <= ?""",
                (now, now, turn_id, now),
            )
            if cursor.rowcount != 1:
                exists = connection.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("turn not found")
                raise TransitionError("approval has not expired")
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            assert row is not None
            return _turn(row)

        return self._write(expire)

    def _update_turn(self, turn_id: str, allowed_from: set[str], **fields: Any) -> Turn:
        allowed = {
            "state", "input_text", "response_text", "hermes_run_id", "error_code",
            "approval_expires_at", "terminal_at",
            "upload_path", "audio_path", "audio_mime_type", "stt_provider", "tts_provider",
            "tts_voice", "degraded_local_audio", "audio_lease_until",
        }
        if not fields or not allowed_from or set(fields) - allowed:
            raise ValueError("unsupported turn update")

        def update(connection: sqlite3.Connection) -> Turn:
            now = _now()
            assignments = ", ".join(f"{key} = ?" for key in fields)
            values = [fields[key] for key in fields]
            placeholders = ", ".join("?" for _ in allowed_from)
            cursor = connection.execute(
                f"UPDATE turns SET {assignments}, updated_at = ? "
                f"WHERE id = ? AND state IN ({placeholders})",
                (*values, now, turn_id, *sorted(allowed_from)),
            )
            if cursor.rowcount != 1:
                exists = connection.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("turn not found")
                raise TransitionError("turn state transition is not allowed")
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            assert row is not None
            return _turn(row)

        return self._write(update)

    def append_event(self, turn_id: str, event_type: str, payload: dict[str, Any]) -> Event:
        safe_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        def append(connection: sqlite3.Connection) -> Event:
            if connection.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone() is None:
                raise NotFoundError("turn not found")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM events WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            created = _now()
            connection.execute(
                "INSERT INTO events(turn_id, sequence, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (turn_id, sequence, event_type, safe_payload, created),
            )
            return Event(turn_id, sequence, event_type, dict(payload), created)

        return self._write(append)

    def list_events(self, turn_id: str) -> list[Event]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE turn_id = ? ORDER BY sequence", (turn_id,)
            ).fetchall()
        return [
            Event(
                turn_id=row["turn_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def latest_event_sequence(self, turn_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return int(row["sequence"])

    def has_terminal_event_at_or_before(self, turn_id: str, sequence: int) -> bool:
        if sequence < 0:
            raise ValueError("invalid event sequence")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM events
                       WHERE turn_id = ? AND sequence <= ?
                         AND event_type IN ('turn.completed', 'turn.failed', 'turn.cancelled')
                   ) AS present""",
                (turn_id, sequence),
            ).fetchone()
        return bool(row["present"])

    def list_events_after(
        self, turn_id: str, sequence: int, *, limit: int = MAX_EVENT_PAGE
    ) -> list[Event]:
        if sequence < 0 or not 1 <= limit <= MAX_EVENT_PAGE:
            raise ValueError("invalid event page")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM events
                   WHERE turn_id = ? AND sequence > ?
                   ORDER BY sequence LIMIT ?""",
                (turn_id, sequence, limit),
            ).fetchall()
        return [
            Event(
                turn_id=row["turn_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def list_tool_names(self, turn_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT payload_json FROM events
                   WHERE turn_id = ? AND event_type = 'hermes.tool_started'
                   ORDER BY sequence LIMIT ?""",
                (turn_id, MAX_TOOL_LIFECYCLE_EVENTS),
            ).fetchall()
        tools: list[str] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            tool = payload.get("tool") if isinstance(payload, dict) else None
            if (
                isinstance(tool, str)
                and TOOL_NAME_RE.fullmatch(tool)
                and tool not in tools
            ):
                tools.append(tool)
        return tools

    def list_tool_invocations(self, turn_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM events
                   WHERE turn_id = ? AND event_type IN (
                       'hermes.approval_required',
                       'hermes.approval_resolved',
                       'hermes.tool_started'
                   )
                   ORDER BY sequence
                   LIMIT ?""",
                (turn_id, MAX_TOOL_LIFECYCLE_EVENTS),
            ).fetchall()
        events = [
            Event(
                turn_id=row["turn_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
        pending_approval: dict[str, Any] | None = None
        invocations: list[dict[str, Any]] = []

        for event in events:
            payload = event.payload
            if not isinstance(payload, dict):
                continue
            if event.event_type == "hermes.approval_resolved":
                if payload.get("decision") == "once" and pending_approval is not None:
                    pending_approval["approved"] = True
                else:
                    pending_approval = None
                continue

            if event.event_type == "hermes.approval_required":
                pending_approval = None
                tool = payload.get("tool")
                if isinstance(tool, str) and TOOL_NAME_RE.fullmatch(tool) is not None:
                    pending_approval = {"tool": tool, "approved": False}
                    risk = payload.get("risk")
                    if risk in {"low", "medium", "high"}:
                        pending_approval["risk"] = risk
                continue

            if event.event_type != "hermes.tool_started":
                continue
            tool = payload.get("tool")
            if not isinstance(tool, str) or TOOL_NAME_RE.fullmatch(tool) is None:
                pending_approval = None
                continue

            if len(invocations) >= MAX_TOOL_INVOCATIONS:
                break
            invocation: dict[str, Any] = {
                "id": f"tool-{event.sequence}",
                "name": tool,
                "status": "invoked",
                "started_at": event.created_at,
                "approval_required": False,
            }
            if (
                pending_approval is not None
                and pending_approval.get("approved") is True
                and pending_approval.get("tool") == tool
            ):
                invocation["approval_required"] = True
                if "risk" in pending_approval:
                    invocation["risk"] = pending_approval["risk"]
            pending_approval = None
            invocations.append(invocation)

        return invocations
