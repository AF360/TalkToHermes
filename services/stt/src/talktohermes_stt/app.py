from __future__ import annotations

import hmac
import io
import json
import os
import re
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

MODEL_NAME = "large-v3-turbo"
MODEL_PATH = (
    "/opt/stt/.cache/huggingface/hub/"
    "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/"
    "snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
)
PROMPT = "Transcribe the speech input for TalkToHermes precisely."
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MULTIPART_FRAMING_BYTES = 64 * 1024
MAX_CONTENT_LENGTH = MAX_AUDIO_BYTES + MULTIPART_FRAMING_BYTES
MAX_FILENAME_BYTES = 128
MAX_RESPONSE_BYTES = 65_536
MAX_DURATION_SECONDS = 120
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
_INFERENCE_LOCK = threading.Lock()

AUDIO_TYPES = {
    ".wav": frozenset({"audio/wav", "audio/x-wav"}),
    ".flac": frozenset({"audio/flac"}),
    ".m4a": frozenset({"audio/mp4", "audio/x-m4a"}),
    ".mp4": frozenset({"audio/mp4"}),
    ".mp3": frozenset({"audio/mpeg"}),
    ".ogg": frozenset({"audio/ogg"}),
    ".opus": frozenset({"audio/ogg", "audio/opus"}),
    ".webm": frozenset({"audio/webm"}),
}


class Model(Protocol):
    def transcribe(self, audio: io.BytesIO, **kwargs: Any) -> tuple[Any, Any]: ...


class InvalidAudio(ValueError):
    pass


def _read_token(path: Path, runtime_uid: int) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("invalid token file") from exc
    try:
        opened = os.fstat(fd)
        try:
            after = path.lstat()
        except OSError as exc:
            raise ValueError("invalid token file") from exc
        identity = (opened.st_dev, opened.st_ino)
        if (
            path.is_symlink()
            or not stat.S_ISREG(opened.st_mode)
            or identity != (before.st_dev, before.st_ino)
            or identity != (after.st_dev, after.st_ino)
            or opened.st_uid != runtime_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > 257
        ):
            raise ValueError("invalid token file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(258)
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid token") from exc
    if TOKEN_RE.fullmatch(token) is None or raw not in {token.encode("ascii"), token.encode("ascii") + b"\n"}:
        raise ValueError("invalid token")
    return token


def _load_production_model() -> Model:
    # Deliberately deferred: importing the service never imports CUDA/faster-whisper.
    from faster_whisper import WhisperModel

    return WhisperModel(
        MODEL_PATH,
        device="cuda",
        compute_type="float16",
    )


def _language_tag(value: str | None) -> str | None:
    raw = value.strip().replace("_", "-") if isinstance(value, str) else ""
    if LANGUAGE_TAG.fullmatch(raw) is None:
        return None
    parts = raw.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return normalized[0]


def validate_audio_with_pyav(audio: io.BytesIO) -> None:
    # PyAV is supplied by the primary voice server's reviewed /opt/stt virtualenv.
    try:
        import av

        audio.seek(0)
        with av.open(audio, mode="r") as container:
            audio_streams = list(container.streams.audio)
            if len(audio_streams) != 1 or len(container.streams.video) != 0:
                raise InvalidAudio
            decoded_samples = 0
            saw_frame = False
            decoded_rate: int | None = None
            for frame in container.decode(audio_streams[0]):
                saw_frame = True
                rate = int(frame.sample_rate or 0)
                channels = len(frame.layout.channels) if frame.layout is not None else 0
                if (
                    channels != 1
                    or not 8_000 <= rate <= 48_000
                    or (decoded_rate is not None and rate != decoded_rate)
                ):
                    raise InvalidAudio
                decoded_rate = rate
                decoded_samples += int(frame.samples)
                if decoded_samples > rate * MAX_DURATION_SECONDS:
                    raise InvalidAudio
            if not saw_frame or decoded_samples <= 0:
                raise InvalidAudio
    except InvalidAudio:
        raise
    except Exception as exc:
        raise InvalidAudio from exc
    finally:
        audio.seek(0)


def create_app(
    *,
    token_file: Path | str | None = None,
    model_loader: Callable[[], Model] = _load_production_model,
    audio_validator: Callable[[io.BytesIO], None] = validate_audio_with_pyav,
    runtime_uid: int | None = None,
) -> Flask:
    configured_token = token_file or os.environ.get("TALKTOHERMES_STT_TOKEN_FILE", "")
    if not configured_token:
        raise ValueError("token file is required")
    token = _read_token(Path(configured_token), os.getuid() if runtime_uid is None else runtime_uid)
    model_lock = threading.Lock()
    model: Model | None = None

    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH, PROPAGATE_EXCEPTIONS=False)

    def authorized() -> bool:
        value = request.headers.get("Authorization", "")
        prefix = "Bearer "
        candidate = value[len(prefix) :] if value.startswith(prefix) else ""
        return hmac.compare_digest(candidate.encode("utf-8"), token.encode("ascii"))

    def get_model() -> Model:
        nonlocal model
        if model is None:
            with model_lock:
                if model is None:
                    model = model_loader()
        return model

    @app.before_request
    def authenticate() -> Any:
        if not authorized():
            return jsonify(error="unauthorized"), 401
        return None

    @app.get("/ready")
    def ready() -> Any:
        get_model()
        return jsonify(status="ready")

    @app.post("/v1/audio/transcriptions")
    def transcriptions() -> Any:
        allowed_form = {"model", "language", "response_format"}
        if set(request.form) - allowed_form or set(request.files) != {"file"}:
            return jsonify(error="invalid_request"), 400
        if len(request.files.getlist("file")) != 1:
            return jsonify(error="invalid_request"), 400
        if (
            len(request.form.getlist("model")) != 1
            or len(request.form.getlist("language")) != 1
            or len(request.form.getlist("response_format")) > 1
        ):
            return jsonify(error="invalid_request"), 400
        language = _language_tag(request.form.get("language"))
        if request.form.get("model") != MODEL_NAME or language is None:
            return jsonify(error="invalid_request"), 400
        if request.form.get("response_format", "json") != "json":
            return jsonify(error="invalid_request"), 400
        upload = request.files["file"]
        filename = upload.filename or ""
        try:
            filename_bytes = filename.encode("utf-8")
        except UnicodeError:
            return jsonify(error="invalid_audio"), 400
        suffix = Path(filename).suffix.lower()
        if (
            not filename
            or Path(filename).name != filename
            or not 1 <= len(filename_bytes) <= MAX_FILENAME_BYTES
            or suffix not in AUDIO_TYPES
            or upload.mimetype not in AUDIO_TYPES[suffix]
        ):
            return jsonify(error="invalid_audio"), 400
        payload = upload.stream.read(MAX_AUDIO_BYTES + 1)
        if not payload or len(payload) > MAX_AUDIO_BYTES:
            return jsonify(error="invalid_audio"), 400
        audio = io.BytesIO(payload)
        try:
            audio_validator(audio)
        except InvalidAudio:
            return jsonify(error="invalid_audio"), 400
        with _INFERENCE_LOCK:
            segments, _ = get_model().transcribe(
                audio,
                language=language,
                initial_prompt=PROMPT,
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
                word_timestamps=False,
            )
            pieces: list[str] = []
            size = 0
            for segment in segments:
                piece = segment.text
                if not isinstance(piece, str):
                    raise RuntimeError("invalid model response")
                size += len(piece.encode("utf-8"))
                if size > MAX_RESPONSE_BYTES:
                    raise RuntimeError("model response too large")
                pieces.append(piece)
        text = "".join(pieces).strip()
        body = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("model response too large")
        return app.response_class(body, status=200, mimetype="application/json")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error: RequestEntityTooLarge) -> Any:
        return jsonify(error="request_too_large"), 413

    @app.errorhandler(Exception)
    def controlled_error(error: Exception) -> Any:
        if isinstance(error, HTTPException):
            return jsonify(error="not_found"), error.code or 404
        app.logger.error("STT request failed")
        return jsonify(error="internal_error"), 500

    return app
