from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import uuid
from collections import defaultdict
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse

from .audio import MAX_UPLOAD_BYTES, AudioValidationError, private_audio_root, store_upload
from .auth import require_app_token
from .hermes_client import HermesClient
from .models import (
    ApprovalRequest,
    CancelResponse,
    ConversationResponse,
    StatusResponse,
    TextTurnRequest,
    TurnAcceptedResponse,
    TurnResponse,
)
from .settings import Settings
from .storage import NotFoundError, Storage, TOOL_NAME_RE, TransitionError
from .stt.base import STTValidationError, validate_language
from .retention import RetentionManager
from .response_style import RESPONSE_STYLES
from .turns import TERMINAL_STATES, QuiescenceTimeoutError, TurnService


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


async def _periodic_cleanup(
    retention: RetentionManager,
    interval_seconds: float,
    *,
    sleep=asyncio.sleep,
) -> None:
    def next_delay() -> float:
        until_expiry = retention.seconds_until_text_expiry()
        if until_expiry is None:
            return interval_seconds
        return min(interval_seconds, max(until_expiry, 0.001))

    try:
        delay = next_delay()
    except Exception:
        delay = min(interval_seconds, 60.0)
    while True:
        await sleep(delay)
        try:
            retention.cleanup()
            delay = next_delay()
        except Exception:
            # A transient SQLite/filesystem failure must not permanently
            # disable the only periodic retention task.
            delay = min(interval_seconds, 60.0)


def create_app(
    settings: Settings,
    *,
    storage: Storage | None = None,
    hermes: Any | None = None,
    stt: Any | None = None,
    tts: Any | None = None,
    closeables: Sequence[AsyncCloseable] = (),
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        cleanup_task = asyncio.create_task(
            _periodic_cleanup(
                application.state.retention, settings.cleanup_interval_seconds
            )
        )
        application.state.cleanup_task = cleanup_task
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await application.state.turn_service.shutdown()
            first_close_error: BaseException | None = None
            for closeable in reversed(application.state.owned_closeables):
                try:
                    await closeable.aclose()
                except BaseException as exc:
                    if first_close_error is None:
                        first_close_error = exc
            if first_close_error is not None:
                raise first_close_error

    app = FastAPI(
        title="TalkToHermes Voice Bridge", version="1.0.4", lifespan=lifespan
    )

    class RequestBodyTooLarge(Exception):
        pass

    @app.middleware("http")
    async def bound_voice_multipart(request, call_next):
        if request.method == "POST" and request.url.path.endswith("/turns"):
            # Allow bounded multipart framing in addition to the 10 MiB file.
            total_limit = MAX_UPLOAD_BYTES + 64 * 1024
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > total_limit:
                        return JSONResponse(status_code=413, content={"detail": "audio_too_large"})
                except ValueError:
                    return JSONResponse(status_code=422, content={"detail": "invalid_content_length"})
            original_receive = request._receive
            received = 0

            async def limited_receive():
                nonlocal received
                message = await original_receive()
                if message.get("type") == "http.request":
                    received += len(message.get("body", b""))
                    if received > total_limit:
                        raise RequestBodyTooLarge
                return message

            request._receive = limited_receive
            try:
                return await call_next(request)
            except RequestBodyTooLarge:
                return JSONResponse(status_code=413, content={"detail": "audio_too_large"})
        return await call_next(request)
    app.state.settings = settings
    app.state.storage = storage or Storage(settings.state_dir / "talktohermes.sqlite3")
    app.state.owned_closeables = list(closeables)
    if hermes is None:
        app.state.hermes = HermesClient(
            settings.hermes.base_url,
            settings.hermes.api_key.get_secret_value(),
            voice_instructions=settings.hermes.voice_instructions,
        )
        app.state.owned_closeables.append(app.state.hermes)
    else:
        app.state.hermes = hermes
    app.state.audio_root = private_audio_root(settings.state_dir)
    app.state.retention = RetentionManager(
        app.state.storage,
        app.state.audio_root,
        retain_failed_audio=settings.retain_failed_audio,
        failed_hours=settings.failed_audio_retention_hours,
        text_hours=settings.text_retention_hours,
        grace_seconds=settings.audio_download_grace_seconds,
    )
    app.state.conversation_locks = defaultdict(asyncio.Lock)
    for deleting_id in app.state.storage.list_deleting_conversation_ids():
        if not app.state.retention.delete_conversation_artifacts(deleting_id):
            app.state.storage.delete_conversation(deleting_id)
    app.state.storage.recover_active_turns()
    app.state.retention.cleanup_restart_artifacts()
    app.state.retention.cleanup()
    app.state.turn_service = TurnService(
        app.state.storage,
        app.state.hermes,
        stt=stt,
        tts=tts,
        audio_root=app.state.audio_root,
    )

    def finish_task(task: asyncio.Task[Any], *, cleanup_audio: bool = False) -> None:
        try:
            task.result()
        except BaseException:
            # Processing persists a bounded failure when possible. Retrieving the
            # result here prevents detached-task exception warnings.
            pass
        finally:
            if cleanup_audio:
                app.state.retention.cleanup()


    def require_conversation(conversation_id: str):
        try:
            return app.state.storage.get_conversation(conversation_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc

    def require_turn(turn_id: str):
        try:
            return app.state.storage.get_turn(turn_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/v1/status",
        dependencies=[Depends(require_app_token)],
        response_model=StatusResponse,
    )
    async def bridge_status() -> dict[str, str]:
        return {
            "status": "ready",
            "instance_id": settings.instance_id,
            "assistant_name": settings.assistant_name,
        }

    @app.post(
        "/v1/conversations",
        dependencies=[Depends(require_app_token)],
        response_model=ConversationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_conversation() -> dict[str, Any]:
        title = f"TalkToHermes {settings.instance_id} {uuid.uuid4().hex[:8]}"
        session_id = await app.state.hermes.create_session(title)
        conversation = app.state.storage.create_conversation(session_id)
        return conversation.public_dict()

    @app.get(
        "/v1/conversations/{conversation_id}",
        dependencies=[Depends(require_app_token)],
        response_model=ConversationResponse,
    )
    async def get_conversation(conversation_id: str) -> dict[str, Any]:
        return require_conversation(conversation_id).public_dict()

    @app.delete(
        "/v1/conversations/{conversation_id}",
        dependencies=[Depends(require_app_token)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_conversation(conversation_id: str) -> Response:
        try:
            async with asyncio.timeout(app.state.turn_service.quiescence_timeout_seconds):
                async with app.state.conversation_locks[conversation_id]:
                    try:
                        turns = app.state.storage.begin_conversation_delete(conversation_id)
                    except NotFoundError as exc:
                        raise HTTPException(status_code=404, detail="Not found") from exc
                    for turn in turns:
                        await app.state.turn_service.cancel(turn.id, wait=True)
                    cleanup_failures = app.state.retention.delete_conversation_artifacts(
                        conversation_id
                    )
                    if cleanup_failures:
                        raise HTTPException(status_code=500, detail="audio_cleanup_failed")
                    app.state.storage.delete_conversation(conversation_id)
        except (QuiescenceTimeoutError, TimeoutError) as exc:
            raise HTTPException(
                status_code=503,
                detail="conversation_not_quiescent",
                headers={"Retry-After": "1"},
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if settings.development:

        @app.post(
            "/v1/conversations/{conversation_id}/turns/text",
            dependencies=[Depends(require_app_token)],
            response_model=TurnAcceptedResponse,
            status_code=status.HTTP_202_ACCEPTED,
        )
        async def create_text_turn(
            conversation_id: str,
            request: TextTurnRequest,
        ) -> dict[str, str]:
            require_conversation(conversation_id)
            try:
                turn, created = app.state.storage.create_or_get_text_turn(
                    conversation_id,
                    request.client_turn_id,
                    request.text,
                    request.include_text,
                )
            except NotFoundError as exc:
                raise HTTPException(status_code=404, detail="Not found") from exc
            except (TransitionError, ValueError) as exc:
                raise HTTPException(status_code=409, detail="Idempotency conflict") from exc
            if created:
                app.state.storage.append_event(turn.id, "turn.accepted", {})
                task = app.state.turn_service.start_text_turn(turn.id)
                task.add_done_callback(finish_task)
            return {
                "turn_id": turn.id,
                "state": turn.state,
                "events_url": f"/v1/turns/{turn.id}/events",
            }

    @app.post(
        "/v1/conversations/{conversation_id}/turns",
        dependencies=[Depends(require_app_token)],
        response_model=TurnAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_voice_turn(
        conversation_id: str,
        audio: UploadFile = File(...),
        client_turn_id: str = Form(...),
        language: str = Form("de"),
        voice_id: str = Form("default"),
        include_text: bool = Form(True),
        response_style: str = Form("short"),
    ) -> dict[str, str]:
        require_conversation(conversation_id)
        try:
            parsed_client_turn_id = uuid.UUID(client_turn_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid client_turn_id") from exc
        if str(parsed_client_turn_id) != client_turn_id:
            raise HTTPException(status_code=422, detail="Invalid client_turn_id")
        try:
            language = validate_language(language)
        except STTValidationError as exc:
            raise HTTPException(status_code=422, detail="Unsupported voice options") from exc
        if voice_id != "default" or response_style not in RESPONSE_STYLES:
            raise HTTPException(status_code=422, detail="Unsupported voice options")
        try:
            upload_path, digest, _ = await store_upload(audio, app.state.audio_root)
        except AudioValidationError as exc:
            code = status.HTTP_413_CONTENT_TOO_LARGE if exc.code == "audio_too_large" else 422
            raise HTTPException(status_code=code, detail=exc.code) from exc
        legacy_fingerprint_input = f"{digest}\0{language}\0{voice_id}\0{int(include_text)}"
        legacy_fingerprint = hashlib.sha256(legacy_fingerprint_input.encode()).hexdigest()
        fingerprint = hashlib.sha256(
            f"{legacy_fingerprint_input}\0{response_style}".encode()
        ).hexdigest()
        try:
            turn, created = app.state.storage.create_or_get_voice_turn(
                conversation_id, client_turn_id, fingerprint, str(upload_path),
                include_text, response_style, legacy_fingerprint, language
            )
        except NotFoundError as exc:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="Not found") from exc
        except (TransitionError, ValueError) as exc:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail="Idempotency conflict") from exc
        except Exception:
            upload_path.unlink(missing_ok=True)
            raise
        if created:
            app.state.storage.append_event(turn.id, "turn.accepted", {})
            task = app.state.turn_service.start_voice_turn(turn.id)
            task.add_done_callback(
                lambda completed: finish_task(completed, cleanup_audio=True)
            )
        else:
            upload_path.unlink(missing_ok=True)
        return {
            "turn_id": turn.id,
            "state": turn.state,
            "events_url": f"/v1/turns/{turn.id}/events",
        }

    @app.get(
        "/v1/turns/{turn_id}",
        dependencies=[Depends(require_app_token)],
        response_model=TurnResponse,
        response_model_exclude_none=True,
    )
    async def get_turn(turn_id: str) -> dict[str, Any]:
        turn = require_turn(turn_id)
        result = turn.public_dict()
        result["tools"] = app.state.storage.list_tool_names(turn_id)
        result["tool_invocations"] = app.state.storage.list_tool_invocations(turn_id)
        for invocation in result["tool_invocations"]:
            summary = settings.tool_summaries.get(invocation["name"])
            if summary is not None:
                invocation["summary"] = summary
        return result

    @app.get(
        "/v1/turns/{turn_id}/events",
        dependencies=[Depends(require_app_token)],
        response_class=StreamingResponse,
    )
    async def turn_events(
        turn_id: str, last_event_id: str | None = Header(None, alias="Last-Event-ID")
    ) -> StreamingResponse:
        require_turn(turn_id)
        try:
            replay_after = int(last_event_id or "0")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid Last-Event-ID") from exc
        latest_sequence = app.state.storage.latest_event_sequence(turn_id)
        if replay_after < 0 or replay_after > latest_sequence:
            raise HTTPException(status_code=422, detail="Invalid Last-Event-ID")
        async def stream():
            sequence = replay_after
            while True:
                events = app.state.storage.list_events_after(turn_id, sequence)
                for event in events:
                    event_payload = event.payload
                    if event.event_type == "hermes.tool_started":
                        tool = event.payload.get("tool")
                        if not isinstance(tool, str) or TOOL_NAME_RE.fullmatch(tool) is None:
                            sequence = event.sequence
                            continue
                        event_payload = {"tool": tool}
                        summary = settings.tool_summaries.get(tool)
                        if summary is not None:
                            event_payload["summary"] = summary
                    payload = {
                        "turn_id": turn_id,
                        "sequence": event.sequence,
                        "timestamp": event.created_at,
                        **event_payload,
                    }
                    yield (
                        f"id: {event.sequence}\n"
                        f"event: {event.event_type}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    )
                    sequence = event.sequence
                if (
                    app.state.storage.get_turn(turn_id).state in TERMINAL_STATES
                    and app.state.storage.has_terminal_event_at_or_before(
                        turn_id, sequence
                    )
                    and sequence >= app.state.storage.latest_event_sequence(turn_id)
                ):
                    break
                await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get(
        "/v1/turns/{turn_id}/audio",
        dependencies=[Depends(require_app_token)],
        response_class=StreamingResponse,
    )
    async def turn_audio(turn_id: str) -> StreamingResponse:
        turn = require_turn(turn_id)
        if turn.state != "completed" or not turn.audio_path:
            raise HTTPException(status_code=404, detail="Audio not available")
        path = Path(turn.audio_path)
        descriptor: int | None = None
        try:
            if path.parent.resolve(strict=True) != app.state.audio_root:
                raise OSError("outside audio root")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise OSError("unsafe audio file")
            turn = app.state.retention.begin_download_lease(turn)
        except (OSError, ValueError):
            if descriptor is not None:
                os.close(descriptor)
            raise HTTPException(status_code=404, detail="Audio not available")

        async def stream_audio():
            assert descriptor is not None
            try:
                while chunk := await asyncio.to_thread(os.read, descriptor, 64 * 1024):
                    yield chunk
            finally:
                os.close(descriptor)

        return StreamingResponse(
            stream_audio(),
            media_type=turn.audio_mime_type or "audio/wav",
            headers={
                "Content-Disposition": 'attachment; filename="answer.wav"',
                "Content-Length": str(info.st_size),
            },
        )

    @app.post(
        "/v1/turns/{turn_id}/approval",
        dependencies=[Depends(require_app_token)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def approve_turn(turn_id: str, request: ApprovalRequest) -> Response:
        turn = require_turn(turn_id)
        async with app.state.conversation_locks[turn.conversation_id]:
            try:
                app.state.storage.require_active_conversation(turn.conversation_id)
                await app.state.turn_service.approve(turn_id, request.decision)
            except (TransitionError, ValueError) as exc:
                raise HTTPException(status_code=409, detail="Approval not active") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/v1/turns/{turn_id}/cancel",
        dependencies=[Depends(require_app_token)],
        response_model=CancelResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel_turn(turn_id: str) -> dict[str, str]:
        require_turn(turn_id)
        await app.state.turn_service.cancel(turn_id)
        app.state.retention.cleanup()
        return {"status": "cancelled"}

    return app
