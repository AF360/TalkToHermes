#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from copy import deepcopy
from pathlib import Path
import stat

from typing import Any, Protocol

MAX_REQUEST_BYTES = 1_048_576
MAX_TEXT_CHARS = 100_000
MAX_TTS_TEXT_CHARS = 2_000
ALLOWED_TTS_PROVIDERS = frozenset({"piper"})
PIPER_VOICE_RE = re.compile(
    r"^[a-z]{2,3}_[A-Z]{2,3}-[a-z0-9_]+-(?:x_)?(?:low|medium|high)$"
)
LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")


class WorkerError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class VoiceBackend(Protocol):
    def normalize(self, text: str) -> str: ...

    def stt_local(
        self, input_path: str, model: str | None = None, language: str | None = None
    ) -> dict[str, Any]: ...

    def tts(
        self,
        text: str,
        provider: str,
        output_path: str,
        voice: str | None,
    ) -> dict[str, Any]: ...


class HermesVoiceBackend:
    """Narrow adapter to the installed Hermes voice implementation."""

    def normalize(self, text: str) -> str:
        from tools.tts_text_normalize import prepare_spoken_text

        return str(prepare_spoken_text(text, max_chars=None) or "")

    def stt_local(
        self, input_path: str, model: str | None = None, language: str | None = None
    ) -> dict[str, Any]:
        from tools import transcription_tools

        primary_language = language.split("-", 1)[0] if language else None
        if getattr(transcription_tools, "_HAS_FASTER_WHISPER", False):
            local_model = model or getattr(
                transcription_tools, "DEFAULT_LOCAL_MODEL", "small"
            )
            return transcription_tools._transcribe_local(
                input_path,
                transcription_tools._normalize_local_model(local_model),
                language=primary_language,
            )
        return transcription_tools.transcribe_audio_local_fallback(input_path, model=model)

    def tts(
        self,
        text: str,
        provider: str,
        output_path: str,
        voice: str | None,
    ) -> dict[str, Any]:
        from tools import tts_tool

        if provider != "piper":
            raise WorkerError("unsupported_provider")
        if not isinstance(voice, str) or PIPER_VOICE_RE.fullmatch(voice) is None:
            raise WorkerError("unsupported_voice")

        loaded = tts_tool._load_tts_config()
        loaded_piper = loaded.get("piper") if isinstance(loaded, dict) else None
        loaded_piper = loaded_piper if isinstance(loaded_piper, dict) else {}
        safe_piper_keys = {
            "voices_dir", "use_cuda", "length_scale", "noise_scale",
            "noise_w_scale", "volume", "normalize_audio", "speaker_id",
        }
        piper = {
            key: deepcopy(value)
            for key, value in loaded_piper.items()
            if key in safe_piper_keys
        }
        voices_dir_raw = piper.get("voices_dir")
        if voices_dir_raw is None:
            get_voices_dir = getattr(tts_tool, "_get_piper_voices_dir", None)
            if not callable(get_voices_dir):
                raise WorkerError("piper_voice_unavailable")
            voices_dir_raw = get_voices_dir()
        model_path = _cached_piper_model(voices_dir_raw, voice)
        piper["voice"] = str(model_path)
        config = {"provider": "piper", "piper": piper}
        generated = tts_tool._generate_piper_tts(text, output_path, config)
        if not isinstance(generated, str) or generated != output_path:
            raise WorkerError("invalid_upstream_response")
        return {
            "success": True,
            "file_path": output_path,
            "provider": "piper",
            "voice": voice,
        }


def parse_request(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise WorkerError("request_too_large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkerError("invalid_json") from exc
    if not isinstance(value, dict):
        raise WorkerError("request_must_be_object", "request must be an object")
    return value


def _required_text(request: dict[str, Any]) -> str:
    text = request.get("text")
    if not isinstance(text, str) or not text.strip():
        raise WorkerError("text_required")
    if len(text) > MAX_TEXT_CHARS:
        raise WorkerError("text_too_large")
    return text


def _required_tts_text(request: dict[str, Any]) -> str:
    text = _required_text(request)
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise WorkerError("text_too_large")
    if any(
        character not in "\t\n\r" and unicodedata.category(character).startswith("C")
        for character in text
    ):
        raise WorkerError("invalid_text")
    return text


def _absolute_clean_path(raw: Any, *, must_exist: bool) -> Path:
    if not isinstance(raw, str) or not raw:
        raise WorkerError("invalid_input_path" if must_exist else "invalid_output_path")
    candidate = Path(raw)
    code = "invalid_input_path" if must_exist else "invalid_output_path"
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise WorkerError(code)
    resolved = candidate.resolve(strict=False)
    if must_exist and (not resolved.is_file() or resolved.is_symlink()):
        raise WorkerError(code)
    return resolved


def _private_existing_output(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw:
        raise WorkerError("invalid_output_path")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise WorkerError("invalid_output_path")
    try:
        info = path.lstat()
        parent = path.parent.lstat()
    except OSError as exc:
        raise WorkerError("invalid_output_path") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or path.parent.is_symlink()
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise WorkerError("invalid_output_path")
    return path.resolve(strict=True)


def _cached_piper_model(raw_directory: Any, voice: str) -> Path:
    directory = Path(raw_directory).expanduser()
    if not directory.is_absolute():
        raise WorkerError("piper_voice_unavailable")
    try:
        directory_info = directory.lstat()
    except OSError as exc:
        raise WorkerError("piper_voice_unavailable") from exc
    if directory.is_symlink() or not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid():
        raise WorkerError("piper_voice_unavailable")
    mode = stat.S_IMODE(directory_info.st_mode)
    if mode & 0o022:
        try:
            os.chmod(directory, mode & ~0o022, follow_symlinks=False)
            directory_info = directory.lstat()
        except OSError as exc:
            raise WorkerError("piper_voice_unavailable") from exc
        if stat.S_IMODE(directory_info.st_mode) & 0o022:
            raise WorkerError("piper_voice_unavailable")

    model = directory / f"{voice}.onnx"
    metadata = directory / f"{voice}.onnx.json"
    for asset in (model, metadata):
        try:
            info = asset.lstat()
        except OSError as exc:
            raise WorkerError("piper_voice_unavailable") from exc
        if (
            asset.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise WorkerError("piper_voice_unavailable")
    return model.resolve(strict=True)


def _private_staging_file(output_path: Path) -> Path:
    staging = output_path.with_name(f".tts-worker-{output_path.name}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(staging, flags, 0o600)
    except OSError as exc:
        raise WorkerError("tts_failed") from exc
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return staging


def _validate_staging_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkerError("tts_failed") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size <= 0
    ):
        raise WorkerError("tts_failed")


def dispatch_request(request: dict[str, Any], backend: VoiceBackend) -> dict[str, Any]:
    operation = request.get("operation")

    if operation == "normalize":
        text = backend.normalize(_required_text(request))
        return {"ok": True, "text": text}

    if operation == "stt-local":
        input_path = _absolute_clean_path(request.get("input_path"), must_exist=True)
        model = request.get("model", "small")
        if not isinstance(model, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", model
        ):
            raise WorkerError("unsupported_model")
        language = request.get("language")
        if not isinstance(language, str) or LANGUAGE_TAG_RE.fullmatch(language) is None:
            raise WorkerError("unsupported_language")
        result = backend.stt_local(str(input_path), model, language)
        if not result.get("success"):
            raise WorkerError("stt_failed")
        transcript = str(result.get("transcript") or "").strip()
        if not transcript:
            raise WorkerError("empty_transcript")
        return {
            "ok": True,
            "provider": str(result.get("provider") or "local"),
            "text": transcript,
        }

    if operation == "tts":
        provider = str(request.get("provider") or "").strip().lower()
        if provider not in ALLOWED_TTS_PROVIDERS:
            raise WorkerError("unsupported_provider")
        text = _required_tts_text(request)
        voice = request.get("voice")
        if not isinstance(voice, str) or PIPER_VOICE_RE.fullmatch(voice) is None:
            raise WorkerError("unsupported_voice")
        output_path = _private_existing_output(request.get("output_path"))
        staging_path = _private_staging_file(output_path)
        try:
            result = backend.tts(text, provider, str(staging_path), voice)
            if not result.get("success"):
                raise WorkerError("tts_failed")
            upstream_path = _absolute_clean_path(
                result.get("file_path") or str(staging_path), must_exist=False
            )
            if upstream_path != staging_path:
                raise WorkerError("unexpected_output_path")
            _validate_staging_file(staging_path)
            os.replace(staging_path, output_path)
        finally:
            try:
                staging_path.unlink(missing_ok=True)
            except OSError:
                pass
        return {
            "ok": True,
            "provider": str(result.get("provider") or provider),
            "file_path": str(output_path),
            **({"voice": voice} if voice else {}),
        }

    raise WorkerError("unknown_operation")


def _read_request() -> str:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise WorkerError("request_too_large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerError("invalid_encoding") from exc


def main() -> int:
    try:
        response = dispatch_request(parse_request(_read_request()), HermesVoiceBackend())
        print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        return 0
    except WorkerError as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": exc.code}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2
    except Exception:
        print('{"ok":false,"error":{"code":"internal_error"}}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
