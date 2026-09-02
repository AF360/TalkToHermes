from __future__ import annotations

import os
import stat
import threading
from functools import wraps
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .storage import Storage, Turn


def _locked(function):
    @wraps(function)
    def synchronized(self, *args, **kwargs):
        with self._lock:
            return function(self, *args, **kwargs)

    return synchronized


class RetentionManager:
    def __init__(
        self,
        storage: Storage,
        audio_root: Path,
        *,
        retain_failed_audio: bool = True,
        failed_hours: float = 24,
        text_hours: float = 24,
        grace_seconds: float = 300,
    ) -> None:
        if not 1 <= text_hours <= 24:
            raise ValueError("text_hours must be between 1 and 24")
        self.storage = storage
        self.root = audio_root
        self.retain_failed_audio = retain_failed_audio
        self.failed_lifetime = timedelta(hours=failed_hours)
        self.text_lifetime = timedelta(hours=text_hours)
        self.orphan_lifetime = timedelta(hours=24)
        self.grace = timedelta(seconds=grace_seconds)
        self._lock = threading.RLock()

    def seconds_until_text_expiry(self, now: datetime | None = None) -> float | None:
        terminal_at = self.storage.next_text_terminal_at()
        if terminal_at is None:
            return None
        current = (now or datetime.now(UTC)).astimezone(UTC)
        return max(0.0, (terminal_at + self.text_lifetime - current).total_seconds())

    @_locked
    def cleanup(self, now: datetime | None = None) -> list[str]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self.storage.redact_expired_text(current - self.text_lifetime)
        failures: list[str] = []
        referenced: set[Path] = set()
        for turn in self.storage.list_turns():
            upload = self._safe(turn.upload_path)
            answer = self._safe(turn.audio_path)
            if upload is not None:
                referenced.add(upload)
                created = datetime.fromisoformat(turn.created_at).astimezone(UTC)
                remove_upload = turn.state in {"completed", "cancelled"} or (
                    turn.state == "failed"
                    and (not self.retain_failed_audio or current - created >= self.failed_lifetime)
                )
                if remove_upload and self._unlink(upload, failures):
                    self.storage.update_audio_references(
                        turn.id, upload_path=None, audio_path=turn.audio_path,
                        audio_lease_until=turn.audio_lease_until,
                    )
            if answer is not None:
                referenced.add(answer)
                expired = False
                if turn.state != "completed":
                    expired = True
                elif turn.audio_lease_until:
                    expired = current >= datetime.fromisoformat(turn.audio_lease_until).astimezone(UTC)
                else:
                    created = datetime.fromisoformat(turn.created_at).astimezone(UTC)
                    expired = current - created >= self.failed_lifetime
                if expired and self._unlink(answer, failures):
                    self.storage.update_audio_references(
                        turn.id, upload_path=self.storage.get_turn(turn.id).upload_path,
                        audio_path=None, audio_lease_until=None,
                    )
        # Remove only safe regular orphan files immediately; never follow links.
        try:
            children = list(self.root.iterdir())
        except OSError:
            return failures + ["audio_root_scan_failed"]
        for child in children:
            if child in referenced:
                continue
            safe = self._safe(str(child))
            if safe is not None:
                try:
                    modified = datetime.fromtimestamp(safe.lstat().st_mtime, UTC)
                except OSError:
                    continue
                if current - modified >= self.orphan_lifetime:
                    self._unlink(safe, failures)
        return failures

    @_locked
    def begin_download_lease(self, turn: Turn, now: datetime | None = None) -> Turn:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        latest = self.storage.get_turn(turn.id)
        if not latest.audio_path or latest.audio_path != turn.audio_path:
            raise ValueError("audio is no longer available")
        if latest.audio_lease_until:
            return latest
        return self.storage.update_audio_references(
            latest.id, upload_path=latest.upload_path, audio_path=latest.audio_path,
            audio_lease_until=(current + self.grace).isoformat(),
        )

    @_locked
    def delete_conversation_artifacts(self, conversation_id: str) -> list[str]:
        """Delete owned regular audio before the database cascade drops references."""
        failures: list[str] = []
        for turn in self.storage.list_turns():
            if turn.conversation_id != conversation_id:
                continue
            for raw in (turn.upload_path, turn.audio_path):
                if not raw:
                    continue
                raw_path = Path(raw)
                try:
                    raw_path.lstat()
                except FileNotFoundError:
                    if raw_path.parent == self.root:
                        continue
                    failures.append("audio_cleanup_unsafe")
                    continue
                except OSError:
                    failures.append("audio_cleanup_failed")
                    continue
                path = self._safe(raw)
                if path is None:
                    failures.append("audio_cleanup_unsafe")
                else:
                    self._unlink(path, failures)
        return failures

    @_locked
    def cleanup_restart_artifacts(self) -> list[str]:
        """Interrupted work is incomplete and must never enter failed-audio retention."""
        failures: list[str] = []
        for turn in self.storage.list_turns():
            if turn.error_code != "restart_interrupted":
                continue
            upload_path = turn.upload_path
            audio_path = turn.audio_path
            upload = self._safe(upload_path)
            answer = self._safe(audio_path)
            if upload is not None and self._unlink(upload, failures):
                upload_path = None
            if answer is not None and self._unlink(answer, failures):
                audio_path = None
            if upload_path != turn.upload_path or audio_path != turn.audio_path:
                self.storage.update_audio_references(
                    turn.id,
                    upload_path=upload_path,
                    audio_path=audio_path,
                    audio_lease_until=None,
                )
        return failures

    def _safe(self, raw: str | None) -> Path | None:
        if not raw:
            return None
        path = Path(raw)
        try:
            info = path.lstat()
        except OSError:
            return None
        if (
            path.parent != self.root
            or path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            return None
        return path

    @staticmethod
    def _unlink(path: Path, failures: list[str]) -> bool:
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            failures.append("audio_cleanup_failed")
            return False
