from __future__ import annotations

import os
import re
import stat
import wave
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, TypeGuard


import yaml

EXPECTED_PORT = 9090
PRIVATE_IPV4_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
VOICE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
LANGUAGE = re.compile(
    r"^(?:[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}|[A-Za-z][A-Za-z ]{1,63})$"
)


class ConfigError(ValueError):
    """Raised for unsafe or invalid service configuration."""


def is_private_non_loopback_ipv4(value: Any) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    try:
        address = IPv4Address(value)
    except ValueError:
        return False
    return any(
        address in network
        and address not in {network.network_address, network.broadcast_address}
        for network in PRIVATE_IPV4_NETWORKS
    )


@dataclass(frozen=True)
class VoiceProfile:
    reference_audio: Path
    reference_audio_fd: int
    transcript: str
    language: str


@dataclass(frozen=True)
class Settings:
    listen_host: str
    listen_port: int
    token: str
    model_name: str
    max_body_bytes: int
    max_text_chars: int
    max_wav_bytes: int
    inference_timeout_seconds: int
    voices: dict[str, VoiceProfile]
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        for profile in self.voices.values():
            try:
                os.close(profile.reference_audio_fd)
            except OSError:
                pass


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _open_secure_regular_file(
    value: Any, label: str, *, exact_mode: int | None = None
) -> tuple[Path, int]:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{label} must be an absolute path")
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current /= component
        try:
            parent_info = current.lstat()
        except OSError as exc:
            raise ConfigError(f"{label} path is unavailable") from exc
        if stat.S_ISLNK(parent_info.st_mode):
            raise ConfigError(f"{label} path contains a symlink")
        parent_mode = stat.S_IMODE(parent_info.st_mode)
        sticky_root_directory = bool(parent_mode & stat.S_ISVTX) and parent_info.st_uid == 0
        if parent_info.st_uid not in {0, os.geteuid()} or (
            parent_mode & 0o022 and not sticky_root_directory
        ):
            raise ConfigError(f"{label} path has an untrusted parent")

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        path_info = path.lstat()
    except OSError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise ConfigError(f"{label} is unavailable") from exc
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(path_info.st_mode)
        or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
    ):
        os.close(descriptor)
        raise ConfigError(f"{label} must be a stable regular non-symlink file")
    if info.st_uid != os.geteuid():
        os.close(descriptor)
        raise ConfigError(f"{label} must have the service owner")
    if exact_mode is not None and mode != exact_mode:
        os.close(descriptor)
        raise ConfigError(f"{label} must have mode {exact_mode:04o}")
    if exact_mode is None and mode & 0o077:
        os.close(descriptor)
        raise ConfigError(f"{label} must be private")
    return path, descriptor


def _positive_int(raw: dict[str, Any], name: str, default: int, upper: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
        raise ConfigError(f"{name} is invalid")
    return value


def load_config(path: str | Path) -> Settings:
    config_path = Path(path)
    try:
        raw = _mapping(yaml.safe_load(config_path.read_text(encoding="utf-8")), "config")
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError("configuration is unavailable") from exc

    allowed = {
        "listen_host", "listen_port", "token_file", "model_name", "max_body_bytes",
        "max_text_chars", "max_wav_bytes", "inference_timeout_seconds", "voices",
    }
    if set(raw) - allowed:
        raise ConfigError("configuration has unknown fields")
    listen_host = raw.get("listen_host")
    if not is_private_non_loopback_ipv4(listen_host):
        raise ConfigError("listen_host must be a private non-loopback IPv4 address")
    if raw.get("listen_port") != EXPECTED_PORT:
        raise ConfigError("listen_port must be 9090 and must not be 8181")

    token_path, token_fd = _open_secure_regular_file(
        raw.get("token_file"), "token file", exact_mode=0o600
    )
    try:
        with os.fdopen(token_fd, "r", encoding="utf-8") as token_stream:
            token_raw = token_stream.read(257)
    except (OSError, UnicodeError) as exc:
        raise ConfigError("token file is unavailable") from exc
    token = token_raw.rstrip("\n")
    if token_raw not in {token, token + "\n"} or not 32 <= len(token) <= 128 or not token.isascii() or any(ch.isspace() for ch in token):
        raise ConfigError("token file contains an invalid token")

    model_name = raw.get("model_name", "k2-fsa/OmniVoice")
    if model_name != "k2-fsa/OmniVoice":
        raise ConfigError("model_name is invalid")

    voices_raw = _mapping(raw.get("voices"), "voices")
    if not voices_raw or len(voices_raw) > 16:
        raise ConfigError("voices must contain 1 to 16 profiles")
    voices: dict[str, VoiceProfile] = {}
    try:
        for voice_id, profile_value in voices_raw.items():
            if not isinstance(voice_id, str) or VOICE_ID.fullmatch(voice_id) is None:
                raise ConfigError("voice ID is invalid")
            profile = _mapping(profile_value, "voice profile")
            if set(profile) != {"reference_audio", "reference_transcript", "language"}:
                raise ConfigError("voice profile fields are invalid")
            audio, audio_fd = _open_secure_regular_file(
                profile["reference_audio"], "voice file"
            )
            try:
                if audio.suffix.lower() != ".wav":
                    raise ConfigError("voice file must be WAV")
                _transcript_path, transcript_fd = _open_secure_regular_file(
                    profile["reference_transcript"], "voice file"
                )
                with os.fdopen(transcript_fd, "r", encoding="utf-8") as transcript_stream:
                    transcript = transcript_stream.read(16_001).strip()
                if os.fstat(audio_fd).st_size > 20 * 1024 * 1024:
                    raise ConfigError("voice file is too large")
                with os.fdopen(os.dup(audio_fd), "rb") as audio_stream:
                    with wave.open(audio_stream, "rb") as reference_wav:
                        if (
                            reference_wav.getnchannels() < 1
                            or reference_wav.getsampwidth() not in {2, 3, 4}
                            or not 8_000 <= reference_wav.getframerate() <= 192_000
                            or reference_wav.getnframes() < 1
                        ):
                            raise ConfigError("voice WAV is invalid")
                if not transcript or len(transcript) > 16_000:
                    raise ConfigError("voice transcript is invalid")
                language = profile["language"]
                if not isinstance(language, str) or LANGUAGE.fullmatch(language.strip()) is None:
                    raise ConfigError("voice language is invalid")
                language = language.strip()
            except (OSError, UnicodeError, wave.Error, EOFError) as exc:
                os.close(audio_fd)
                raise ConfigError("voice WAV or transcript is unavailable") from exc
            except BaseException:
                os.close(audio_fd)
                raise
            stable_audio = Path(f"/proc/self/fd/{audio_fd}")
            voices[voice_id] = VoiceProfile(stable_audio, audio_fd, transcript, language)
    except BaseException:
        for retained_profile in voices.values():
            os.close(retained_profile.reference_audio_fd)
        raise

    return Settings(
        listen_host=listen_host,
        listen_port=EXPECTED_PORT,
        token=token,
        model_name=model_name,
        max_body_bytes=_positive_int(raw, "max_body_bytes", 16_384, 1_048_576),
        max_text_chars=_positive_int(raw, "max_text_chars", 2_000, 10_000),
        max_wav_bytes=_positive_int(raw, "max_wav_bytes", 32 * 1024 * 1024, 64 * 1024 * 1024),
        inference_timeout_seconds=_positive_int(
            raw, "inference_timeout_seconds", 120, 600
        ),
        voices=voices,
    )
