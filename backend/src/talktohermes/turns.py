from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from .storage import Storage, TOOL_NAME_RE, TransitionError

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class QuiescenceTimeoutError(TimeoutError):
    pass


class HermesRuns(Protocol):
    async def create_session(self, title: str = "TalkToHermes") -> str: ...

    async def start_run(
        self, session_id: str, input_text: str, response_style: str = "short"
    ) -> str: ...

    def events(self, run_id: str) -> AsyncIterator[dict[str, Any]]: ...

    async def get_run(self, run_id: str) -> dict[str, Any]: ...

    async def approve(self, run_id: str, decision: Literal["once", "deny"]) -> None: ...

    async def stop(self, run_id: str) -> None: ...


class TurnService:
    def __init__(
        self,
        storage: Storage,
        hermes: HermesRuns,
        *,
        stt: Any | None = None,
        tts: Any | None = None,
        audio_root: Any | None = None,
        approval_timeout_seconds: float = 120,
        quiescence_timeout_seconds: float = 1,
    ) -> None:
        if approval_timeout_seconds <= 0 or quiescence_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        self.storage = storage
        self.hermes = hermes
        self.stt = stt
        self.tts = tts
        self.audio_root = audio_root
        self.approval_timeout_seconds = approval_timeout_seconds
        self.quiescence_timeout_seconds = quiescence_timeout_seconds
        self._active_tasks: dict[str, tuple[asyncio.Task[Any], bool]] = {}
        self._starting_runs: set[str] = set()

    def start_text_turn(self, turn_id: str) -> asyncio.Task[Any]:
        return self._start_turn(turn_id, voice=False)

    def start_voice_turn(self, turn_id: str) -> asyncio.Task[Any]:
        return self._start_turn(turn_id, voice=True)

    def _start_turn(self, turn_id: str, *, voice: bool) -> asyncio.Task[Any]:
        existing = self._active_tasks.get(turn_id)
        if existing is not None and not existing[0].done():
            return existing[0]
        coroutine = self.process_voice_turn(turn_id) if voice else self.process_text_turn(turn_id)
        task = asyncio.create_task(coroutine)
        self._active_tasks[turn_id] = (task, voice)
        return task

    async def shutdown(self) -> None:
        tasks = {
            task for task, _ in self._active_tasks.values()
            if task is not asyncio.current_task() and not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=self.quiescence_timeout_seconds)
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass

    async def process_text_turn(self, turn_id: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks[turn_id] = (task, False)
        try:
            turn = self.storage.get_turn(turn_id)
            await self._process_hermes_turn(turn_id, turn.input_text, voice=False)
        finally:
            if task is not None and self._active_tasks.get(turn_id) == (task, False):
                self._active_tasks.pop(turn_id, None)

    async def process_voice_turn(self, turn_id: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks[turn_id] = (task, True)
        try:
            await self._process_voice_turn(turn_id)
        finally:
            if task is not None and self._active_tasks.get(turn_id) == (task, True):
                self._active_tasks.pop(turn_id, None)

    async def _process_voice_turn(self, turn_id: str) -> None:
        turn = self.storage.get_turn(turn_id)
        if turn.state in TERMINAL_STATES:
            return
        if self.stt is None or self.tts is None or self.audio_root is None:
            self.storage.fail_turn(turn_id, "voice_unavailable")
            self.storage.append_event(turn_id, "turn.failed", {"error": {"code": "voice_unavailable", "retryable": False}})
            return
        try:
            self.storage.start_transcription(turn_id)
            self.storage.append_event(turn_id, "stt.started", {})
            if not turn.upload_path:
                raise RuntimeError("missing upload")
            result = await self.stt.transcribe(Path(turn.upload_path), turn.language)
            self._append_provider_attempts(
                turn_id, "stt", getattr(result, "attempts", ()), result.provider
            )
            degraded = result.provider == "local"
            self.storage.set_stt_result(
                turn_id, result.provider, degraded, result.text
            )
            if degraded:
                self.storage.append_event(turn_id, "stt.degraded", {"degraded_local_audio": True})
            self.storage.append_event(turn_id, "stt.completed", {"provider": result.provider})
            await self._process_hermes_turn(turn_id, result.text, voice=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self.storage.get_turn(turn_id)
            if current.state not in TERMINAL_STATES:
                attempts = getattr(exc, "attempts", ())
                attempt_payloads: tuple[dict[str, Any], ...] = ()
                telemetry_error: RuntimeError | None = None
                if attempts:
                    try:
                        attempt_payloads = self._provider_attempt_payloads(attempts, "")
                    except RuntimeError as metadata_error:
                        telemetry_error = metadata_error
                code = getattr(exc, "code", "stt_error")
                self.storage.fail_turn(turn_id, code)
                self._append_provider_attempt_payloads(
                    turn_id, "stt", attempt_payloads
                )
                self.storage.append_event(turn_id, "turn.failed", {"error": {"code": code, "retryable": False}})
                if telemetry_error is not None:
                    raise telemetry_error

    async def _process_hermes_turn(self, turn_id: str, input_text: str, *, voice: bool) -> None:
        turn = self.storage.get_turn(turn_id)
        if turn.state in TERMINAL_STATES:
            return
        conversation = self.storage.get_conversation(turn.conversation_id)
        self.storage.append_event(turn_id, "hermes.started", {})
        output_parts: list[str] = []
        pending_event: asyncio.Future[dict[str, Any]] | None = None
        try:
            self._starting_runs.add(turn_id)
            try:
                run_id = await self.hermes.start_run(
                    conversation.hermes_session_id, input_text, turn.response_style
                )
            finally:
                self._starting_runs.discard(turn_id)
            try:
                self.storage.set_run(turn_id, run_id)
            except TransitionError:
                await self.hermes.stop(run_id)
                if self.storage.get_turn(turn_id).state in TERMINAL_STATES:
                    return
                raise
            events = self.hermes.events(run_id).__aiter__()
            while True:
                current = self.storage.get_turn(turn_id)
                timeout: float | None = None
                if current.state == "awaiting_approval":
                    timeout = self._approval_timeout(current.approval_expires_at)
                    if timeout <= 0:
                        await self._timeout_approval(turn_id, run_id)
                        return
                if pending_event is None:
                    pending_event = asyncio.ensure_future(anext(events))
                try:
                    if timeout is None:
                        event = await pending_event
                    else:
                        event = await asyncio.wait_for(asyncio.shield(pending_event), timeout)
                except StopAsyncIteration:
                    pending_event = None
                    break
                except TimeoutError:
                    if self.storage.get_turn(turn_id).state == "awaiting_approval":
                        await self._timeout_approval(turn_id, run_id)
                        return
                    continue
                pending_event = None
                event_name = str(event.get("event") or "")
                if event_name == "message.delta":
                    delta = str(event.get("delta") or "")
                    if delta:
                        output_parts.append(delta)
                        if turn.include_text:
                            self.storage.append_event(turn_id, "hermes.delta", {"delta": delta})
                elif event_name == "tool.started":
                    tool = event.get("tool")
                    if isinstance(tool, str) and TOOL_NAME_RE.fullmatch(tool) is not None:
                        self.storage.append_event(
                            turn_id, "hermes.tool_started", {"tool": tool}
                        )
                elif event_name == "approval.request":
                    expires_at = self._parse_expiry(event.get("expires_at"))
                    if expires_at is None:
                        expires_at = datetime.now(UTC) + timedelta(
                            seconds=self.approval_timeout_seconds
                        )
                    self.storage.await_approval(turn_id, expires_at.isoformat())
                    safe: dict[str, Any] = {}
                    tool = event.get("tool")
                    if isinstance(tool, str) and TOOL_NAME_RE.fullmatch(tool) is not None:
                        safe["tool"] = tool
                    risk = event.get("risk")
                    if isinstance(risk, str) and risk.lower() in {"low", "medium", "high"}:
                        safe["risk"] = risk.lower()
                    safe["choices"] = ["once", "deny"]
                    self.storage.append_event(turn_id, "hermes.approval_required", safe)
                elif event_name in {"run.failed", "run.error"}:
                    raise RuntimeError("Hermes run failed")
                elif event_name == "run.cancelled":
                    self.storage.cancel_turn(turn_id)
                    self.storage.append_event(turn_id, "turn.cancelled", {})
                    return
                elif event_name == "run.completed":
                    completed_output = event.get("output")
                    if isinstance(completed_output, str) and completed_output:
                        output_parts = [completed_output]

            output = "".join(output_parts).strip()
            if not output:
                status = await self.hermes.get_run(run_id)
                output = str(status.get("output") or "").strip()
            if not output:
                raise RuntimeError("Hermes run returned no output")
            self.storage.append_event(turn_id, "hermes.completed", {})
            if voice:
                self.storage.start_synthesis(turn_id, output)
                self.storage.append_event(turn_id, "tts.started", {})
                speech = await self.tts.synthesize(output, self.audio_root, turn.language)
                attempts = getattr(speech, "attempts", ())
                self._append_provider_attempts(
                    turn_id, "tts", attempts, speech.provider
                )
                if speech.provider != "omnivoice":
                    self.storage.append_event(
                        turn_id, "tts.fallback", {"provider": speech.provider}
                    )
                for attempt in attempts:
                    if (
                        getattr(attempt, "accepted", False)
                        and getattr(attempt, "provider", None) == speech.provider
                        and getattr(attempt, "voice", None) == speech.voice
                    ):
                        self.storage.append_event(
                            turn_id,
                            "tts.segment_completed",
                            {"segment": int(getattr(attempt, "segment_index", 0))},
                        )
                self.storage.complete_voice_turn(
                    turn_id, str(speech.audio_path), speech.provider, speech.voice
                )
                self.storage.append_event(
                    turn_id, "tts.completed", {"provider": speech.provider, "voice": speech.voice}
                )
            else:
                self.storage.complete_turn(turn_id, output)
            self.storage.append_event(turn_id, "turn.completed", {})
        except Exception as exc:
            current = self.storage.get_turn(turn_id)
            if current.state not in TERMINAL_STATES:
                attempts = getattr(exc, "attempts", ())
                was_synthesizing = current.state == "synthesizing"
                attempt_payloads = ()
                telemetry_error = None
                if attempts and was_synthesizing:
                    try:
                        attempt_payloads = self._provider_attempt_payloads(attempts, "")
                    except RuntimeError as metadata_error:
                        telemetry_error = metadata_error
                error_code = (
                    getattr(exc, "code", "internal_error")
                    if was_synthesizing
                    else "hermes_error"
                )
                self.storage.fail_turn(turn_id, error_code)
                self._append_provider_attempt_payloads(
                    turn_id, "tts", attempt_payloads
                )
                self.storage.append_event(
                    turn_id,
                    "turn.failed",
                    {"error": {"code": error_code, "retryable": False}},
                )
                if telemetry_error is not None:
                    raise telemetry_error
        finally:
            if pending_event is not None and not pending_event.done():
                pending_event.cancel()
                try:
                    await pending_event
                except asyncio.CancelledError:
                    pass

    def _provider_attempt_payloads(
        self,
        attempts: tuple[Any, ...],
        selected_provider: str,
    ) -> tuple[dict[str, Any], ...]:
        if (
            selected_provider
            and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", selected_provider) is None
        ):
            raise RuntimeError("invalid provider attempt metadata")
        first_provider = str(getattr(attempts[0], "provider", "")) if attempts else ""
        selected_fallback = (
            selected_provider
            if selected_provider and first_provider and first_provider != selected_provider
            else None
        )
        payloads: list[dict[str, Any]] = []
        for attempt in attempts:
            provider = str(getattr(attempt, "provider", ""))
            outcome = str(getattr(attempt, "outcome", "technical_failure"))
            error = getattr(attempt, "error_code", getattr(attempt, "reason", None))
            circuit_state = str(getattr(attempt, "circuit_state", "closed"))
            try:
                elapsed = float(getattr(attempt, "elapsed_ms", 0.0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("invalid provider attempt metadata") from exc
            if (
                re.fullmatch(r"[a-z][a-z0-9-]{0,31}", provider) is None
                or outcome not in {
                    "success", "unavailable", "timeout", "empty",
                    "technical_failure", "rejected",
                }
                or circuit_state not in {"closed", "open", "half_open"}
                or not math.isfinite(elapsed)
                or elapsed < 0
                or elapsed > 3_600_000
                or (
                    error is not None
                    and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(error)) is None
                )
            ):
                raise RuntimeError("invalid provider attempt metadata")
            payloads.append({
                "provider": provider,
                "outcome": outcome,
                "error_code": None if error is None else str(error),
                "elapsed_ms": elapsed,
                "circuit_state": circuit_state,
                "selected_fallback": selected_fallback,
            })
        return tuple(payloads)

    def _append_provider_attempt_payloads(
        self,
        turn_id: str,
        phase: Literal["stt", "tts"],
        payloads: tuple[dict[str, Any], ...],
    ) -> None:
        for payload in payloads:
            self.storage.append_event(turn_id, f"{phase}.provider_attempt", payload)

    def _append_provider_attempts(
        self,
        turn_id: str,
        phase: Literal["stt", "tts"],
        attempts: tuple[Any, ...],
        selected_provider: str,
    ) -> None:
        self._append_provider_attempt_payloads(
            turn_id,
            phase,
            self._provider_attempt_payloads(attempts, selected_provider),
        )

    async def approve(self, turn_id: str, decision: Literal["once", "deny"]) -> None:
        turn = self.storage.get_turn(turn_id)
        if not turn.hermes_run_id:
            raise ValueError("turn has no Hermes run")
        try:
            self.storage.resolve_approval(turn_id, datetime.now(UTC).isoformat())
        except TransitionError as exc:
            raise ValueError("approval is not active") from exc
        try:
            await self.hermes.approve(turn.hermes_run_id, decision)
        except Exception:
            self.storage.fail_turn(turn_id, "approval_error")
            self.storage.append_event(
                turn_id,
                "turn.failed",
                {"error": {"code": "approval_error", "retryable": False}},
            )
            try:
                await self.hermes.stop(turn.hermes_run_id)
            except Exception:
                pass
            raise
        self.storage.append_event(turn_id, "hermes.approval_resolved", {"decision": decision})

    @staticmethod
    def _parse_expiry(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)

    @staticmethod
    def _approval_timeout(expires_at: str | None) -> float:
        parsed = TurnService._parse_expiry(expires_at)
        if parsed is None:
            return 0
        return (parsed - datetime.now(UTC)).total_seconds()

    async def _timeout_approval(self, turn_id: str, run_id: str) -> None:
        try:
            self.storage.expire_approval(turn_id, datetime.now(UTC).isoformat())
        except TransitionError:
            return
        try:
            await self.hermes.approve(run_id, "deny")
        except Exception:
            pass
        try:
            await self.hermes.stop(run_id)
        except Exception:
            pass
        self.storage.append_event(
            turn_id,
            "turn.failed",
            {"error": {"code": "approval_timeout", "retryable": False}},
        )

    async def cancel(self, turn_id: str, *, wait: bool = False) -> None:
        turn = self.storage.get_turn(turn_id)
        cancelled_here = False
        if turn.state not in TERMINAL_STATES:
            try:
                self.storage.cancel_turn(turn_id)
                cancelled_here = True
            except TransitionError:
                if self.storage.get_turn(turn_id).state not in TERMINAL_STATES:
                    raise
            if cancelled_here:
                self.storage.append_event(turn_id, "turn.cancelled", {})

        active = self._active_tasks.get(turn_id)
        task = active[0] if active is not None else None
        is_voice = active[1] if active is not None else False
        if task is asyncio.current_task():
            task = None
        latest = self.storage.get_turn(turn_id)
        if latest.hermes_run_id:
            try:
                await self.hermes.stop(latest.hermes_run_id)
            except Exception:
                pass
            if (wait or is_voice) and task is not None and not task.done():
                task.cancel()
        elif is_voice and task is not None and turn_id not in self._starting_runs:
            task.cancel()

        if task is not None and (wait or is_voice):
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self.quiescence_timeout_seconds
                )
            except TimeoutError as exc:
                raise QuiescenceTimeoutError("turn did not quiesce") from exc
            except asyncio.CancelledError:
                if task.cancelled():
                    pass
                else:
                    raise
