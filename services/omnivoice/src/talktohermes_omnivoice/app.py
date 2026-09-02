from __future__ import annotations

import asyncio
import hmac
import shutil
import subprocess
import wave
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any, AsyncIterator, Callable, Protocol

from fastapi import Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from typing_extensions import Literal

from .backend import OmniVoiceBackend
from .config import Settings


class SynthesisBackend(Protocol):
    def ready(self) -> None: ...

    def synthesize(self, **kwargs: object) -> bytes: ...


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["omnivoice"]
    voice: str
    input: str
    response_format: Literal["wav", "mp3"]


def _error(status: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": error_type}})


class RequestPolicyMiddleware:
    def __init__(self, app: Any, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http" or scope.get("path") != "/v1/audio/speech" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower() != b"application/json":
            await _send_response(scope, receive, send, _error(415, "Unsupported media type", "invalid_request_error"))
            return
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = self.max_body_bytes + 1
        if declared > self.max_body_bytes:
            await _send_response(scope, receive, send, _error(413, "Request too large", "invalid_request_error"))
            return

        seen = 0
        rejected = False

        async def bounded_receive() -> dict[str, Any]:
            nonlocal seen, rejected
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_body_bytes:
                    rejected = True
                    return {"type": "http.disconnect"}
            return message

        if declared:
            await self.app(scope, bounded_receive, send)
            return
        # Buffer an unframed/chunked request so over-limit data never reaches JSON parsing.
        chunks = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunks.extend(message.get("body", b""))
            if len(chunks) > self.max_body_bytes:
                await _send_response(scope, receive, send, _error(413, "Request too large", "invalid_request_error"))
                return
            if not message.get("more_body", False):
                break
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": bytes(chunks), "more_body": False}

        await self.app(scope, replay, send)
        if rejected:  # pragma: no cover - content-length path is rejected before receive
            return


async def _send_response(scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any], response: Response) -> None:
    await response(scope, receive, send)


def _valid_wav(data: bytes, maximum: int) -> bool:
    if not data or len(data) > maximum:
        return False
    try:
        with wave.open(BytesIO(data), "rb") as wav_file:
            return (
                wav_file.getnchannels() == 1
                and wav_file.getsampwidth() == 2
                and 8_000 <= wav_file.getframerate() <= 192_000
                and wav_file.getnframes() > 0
            )
    except (wave.Error, EOFError):
        return False


def convert_mp3(wav_data: bytes) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or ffmpeg != "/usr/bin/ffmpeg":
        raise RuntimeError("converter unavailable")
    completed = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-map_metadata", "-1", "-f", "mp3", "pipe:1"],
        input=wav_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout or len(completed.stdout) > len(wav_data) * 4 + 1_000_000:
        raise RuntimeError("conversion failed")
    return completed.stdout


def create_app(
    settings: Settings,
    backend: SynthesisBackend | None = None,
    mp3_converter: Callable[[bytes], bytes] = convert_mp3,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            settings.close()

    app = FastAPI(
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestPolicyMiddleware, max_body_bytes=settings.max_body_bytes)
    synthesizer = backend or OmniVoiceBackend(settings.model_name)
    inference_lock = asyncio.Lock()

    def authorize(authorization: str | None = Header(default=None)) -> None:
        supplied = ""
        if authorization is not None and authorization.startswith("Bearer "):
            supplied = authorization[7:]
        if not hmac.compare_digest(supplied.encode("utf-8"), settings.token.encode("utf-8")):
            raise UnauthorizedError

    @app.exception_handler(UnauthorizedError)
    async def unauthorized_handler(_request: Any, _exc: UnauthorizedError) -> JSONResponse:
        return _error(401, "Unauthorized", "authentication_error")

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Any, _exc: RequestValidationError) -> JSONResponse:
        return _error(400, "Invalid request", "invalid_request_error")

    @app.get("/ready", dependencies=[Depends(authorize)])
    async def ready() -> Response:
        if inference_lock.locked():
            return _error(503, "Service busy", "server_error")
        await inference_lock.acquire()
        readiness_task = asyncio.create_task(asyncio.to_thread(synthesizer.ready))
        deferred_release = False
        try:
            await asyncio.wait_for(
                asyncio.shield(readiness_task),
                timeout=settings.inference_timeout_seconds,
            )
        except asyncio.TimeoutError:
            deferred_release = True
            readiness_task.add_done_callback(lambda _task: inference_lock.release())
            return _error(503, "Service unavailable", "server_error")
        except asyncio.CancelledError:
            deferred_release = True
            readiness_task.add_done_callback(lambda _task: inference_lock.release())
            raise
        except Exception:
            return _error(503, "Service unavailable", "server_error")
        finally:
            if not deferred_release:
                inference_lock.release()
        return JSONResponse({"status": "ready"})

    @app.post("/v1/audio/speech", dependencies=[Depends(authorize)])
    async def speech(request: SpeechRequest) -> Response:
        profile = settings.voices.get(request.voice)
        if (
            profile is None
            or not request.input.strip()
            or len(request.input) > settings.max_text_chars
            or any(ord(character) < 32 and character not in "\n\t" for character in request.input)
        ):
            return _error(400, "Invalid request", "invalid_request_error")
        if inference_lock.locked():
            return _error(503, "Service busy", "server_error")
        await inference_lock.acquire()
        task: asyncio.Task[bytes] | None = None
        deferred_release = False
        try:
            task = asyncio.create_task(asyncio.to_thread(
                synthesizer.synthesize,
                text=request.input,
                reference_audio=profile.reference_audio,
                reference_text=profile.transcript,
                language=profile.language,
                max_output_bytes=settings.max_wav_bytes,
            ))
            wav_data = await asyncio.wait_for(
                asyncio.shield(task), timeout=settings.inference_timeout_seconds
            )
            if not _valid_wav(wav_data, settings.max_wav_bytes):
                raise RuntimeError("invalid output")
            if request.response_format == "mp3":
                mp3_data = await asyncio.to_thread(mp3_converter, wav_data)
                return Response(mp3_data, media_type="audio/mpeg")
            return Response(wav_data, media_type="audio/wav")
        except asyncio.TimeoutError:
            if task is not None:
                deferred_release = True
                task.add_done_callback(lambda _task: inference_lock.release())
            return _error(504, "Synthesis timed out", "server_error")
        except asyncio.CancelledError:
            if task is not None:
                deferred_release = True
                task.add_done_callback(lambda _task: inference_lock.release())
            raise
        except Exception:
            return _error(500, "Synthesis failed", "server_error")
        finally:
            if not deferred_release:
                inference_lock.release()

    return app


class UnauthorizedError(Exception):
    pass
