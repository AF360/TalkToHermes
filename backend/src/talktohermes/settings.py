from __future__ import annotations

import ipaddress
import os
import re
import stat
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from .response_style import DEFAULT_VOICE_INSTRUCTIONS
from .worker_security import WorkerPathError, validate_worker_paths

MIN_SECRET_LENGTH = 32
MAX_PROVIDER_TOKEN_LENGTH = 256
PROVIDER_TOKEN_RE = re.compile(
    rf"^[A-Za-z0-9_-]{{{MIN_SECRET_LENGTH},{MAX_PROVIDER_TOKEN_LENGTH}}}$"
)
SECRET_KEYS = frozenset(
    {"APP_TOKEN", "HERMES_API_KEY", "STT_PRIMARY_TOKEN", "TTS_PRIMARY_TOKEN"}
)
INSTANCE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
INSTANCE_MARKER = ".talktohermes-instance"
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PIPER_VOICE_RE = re.compile(
    r"^[a-z]{2,3}_[A-Z]{2,3}-[a-z0-9_]+-(?:x_)?(?:low|medium|high)$"
)


class SettingsError(ValueError):
    pass


class HermesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    api_key: SecretStr
    voice_instructions: str = Field(
        default=DEFAULT_VOICE_INSTRUCTIONS, min_length=1, max_length=4000
    )

    @field_validator("voice_instructions")
    @classmethod
    def validate_voice_instructions(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            character not in "\t\n\r" and ord(character) < 32
            for character in normalized
        ):
            raise ValueError("invalid voice instructions")
        return normalized


class VoiceWorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    python: Path
    script: Path
    hermes_root: Path


class OpenAISTTSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["openai"]
    url: str
    model: str = "large-v3-turbo"
    token: SecretStr = Field(exclude=True)
    connect_timeout_seconds: float = Field(default=0.5, ge=0.1, le=5.0, allow_inf_nan=False)
    response_timeout_seconds: float = Field(default=120.0, ge=0.1, le=300.0, allow_inf_nan=False)
    circuit_cooldown_seconds: float = Field(default=45.0, ge=5.0, le=300.0, allow_inf_nan=False)


class WyomingSTTSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["wyoming"]
    url: str


class LocalSTTSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["local"]
    model: str = "small"


class OmniVoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["omnivoice"]
    url: str
    voice: Literal["voice-01", "voice-02", "voice-03"] = "voice-01"
    token: SecretStr = Field(exclude=True)
    connect_timeout_seconds: float = Field(default=0.5, ge=0.1, le=5.0, allow_inf_nan=False)
    response_timeout_seconds: float = Field(default=120.0, ge=0.1, le=300.0, allow_inf_nan=False)
    circuit_cooldown_seconds: float = Field(default=45.0, ge=5.0, le=300.0, allow_inf_nan=False)


class WyomingPiperSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["wyoming-piper"]
    url: str
    voice: str


class LocalPiperSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["local-piper"]
    voice: str = "de_DE-ramona-low"


STTProviderSettings = Annotated[
    OpenAISTTSettings | WyomingSTTSettings | LocalSTTSettings,
    Field(discriminator="type"),
]
TTSProviderSettings = Annotated[
    OmniVoiceSettings | WyomingPiperSettings | LocalPiperSettings,
    Field(discriminator="type"),
]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    profile: str = "default"
    development: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(ge=0, le=65535)
    state_dir: Path
    secret_file: Path
    app_token: SecretStr
    hermes: HermesSettings
    stt: tuple[STTProviderSettings, ...] = Field(min_length=1, max_length=3)
    tts: tuple[TTSProviderSettings, ...] = Field(min_length=1, max_length=3)
    voice_worker: VoiceWorkerSettings
    retain_failed_audio: bool = True
    failed_audio_retention_hours: float = Field(default=24.0, ge=0, le=24)
    text_retention_hours: float = Field(default=24.0, ge=1, le=24)
    audio_download_grace_seconds: float = Field(default=300.0, ge=1, le=3600)
    cleanup_interval_seconds: float = Field(default=900.0, ge=1, le=900)


def _read_secret_file(path: Path) -> dict[str, str]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise SettingsError("secret_file must be an absolute regular file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise SettingsError("secret_file must have mode 0600")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SettingsError("invalid secret_file entry")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in SECRET_KEYS:
            raise SettingsError("unknown secret_file entry")
        if key in values:
            raise SettingsError("duplicate secret_file entry")
        values[key] = value.strip().strip("'\"")
    return values


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_worker_paths(worker: VoiceWorkerSettings, runtime_uid: int) -> VoiceWorkerSettings:
    try:
        validated = validate_worker_paths(
            worker.python, worker.script, worker.hermes_root, uid=runtime_uid
        )
    except WorkerPathError as exc:
        raise SettingsError(f"voice_worker.{exc.field} {exc.reason}") from exc
    return worker.model_copy(
        update={
            "python": validated.python,
            "script": validated.script,
            "hermes_root": validated.hermes_root,
        }
    )


def _validate_state_dir(path: Path) -> Path:
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise SettingsError("state_dir must be an absolute directory")
    info = path.stat()
    if info.st_uid != os.getuid():
        raise SettingsError("state_dir must be owned by the runtime user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SettingsError("state_dir must not be accessible by group or others")
    return path.resolve()


def _validate_https_endpoint(value: str, path: str) -> None:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SettingsError("invalid HTTPS provider endpoint") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or (port is not None and not 1 <= port <= 65535)
        or parsed.path != path
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SettingsError("invalid HTTPS provider endpoint")


def _https_endpoint_key(value: str) -> tuple[str, int, str]:
    parsed = urlparse(value)
    assert parsed.hostname is not None
    return parsed.hostname.lower().rstrip("."), parsed.port or 443, parsed.path


def _validate_tcp_endpoint(value: str) -> None:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SettingsError("invalid Wyoming endpoint") from exc
    if (
        parsed.scheme != "tcp"
        or not parsed.hostname
        or port is None
        or not 1 <= port <= 65535
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SettingsError("invalid Wyoming endpoint")


def _bind_state_dir(path: Path, instance_id: str, profile: str) -> None:
    marker = path / INSTANCE_MARKER
    expected = f"instance_id={instance_id}\nprofile={profile}\n"
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = None
    except OSError as exc:
        raise SettingsError("could not bind state_dir to instance_id") from exc
    if descriptor is not None:
        try:
            os.write(descriptor, expected.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    try:
        info = marker.lstat()
        value = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SettingsError("invalid state_dir instance_id marker") from exc
    if (
        marker.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or value != expected
    ):
        raise SettingsError("state_dir binding does not match instance_id/profile")


def load_settings(path: Path | str) -> Settings:
    config_path = Path(path)
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SettingsError("could not read instance configuration") from exc
    if not isinstance(payload, dict):
        raise SettingsError("instance configuration must be an object")

    secret_path_value = payload.get("secret_file")
    if not isinstance(secret_path_value, str):
        raise SettingsError("secret_file is required")
    secret_path = Path(secret_path_value)
    secrets = _read_secret_file(secret_path)
    app_token = secrets.get("APP_TOKEN", "")
    hermes_key = secrets.get("HERMES_API_KEY", "")
    stt_token = secrets.get("STT_PRIMARY_TOKEN", "")
    tts_token = secrets.get("TTS_PRIMARY_TOKEN", "")
    raw_stt = payload.get("stt")
    raw_tts = payload.get("tts")
    if not isinstance(raw_stt, list) or not isinstance(raw_tts, list):
        raise SettingsError("ordered stt and tts provider lists are required")
    tokens = [app_token, hermes_key]
    if any(isinstance(item, dict) and item.get("type") == "openai" for item in raw_stt):
        tokens.append(stt_token)
    if any(isinstance(item, dict) and item.get("type") == "omnivoice" for item in raw_tts):
        tokens.append(tts_token)
    if any(PROVIDER_TOKEN_RE.fullmatch(token) is None for token in tokens):
        raise SettingsError("strong app, Hermes, and voice secrets are required")
    if len(set(tokens)) != len(tokens):
        raise SettingsError("app, Hermes, and voice secrets must be different")

    merged = dict(payload)
    merged["secret_file"] = secret_path
    merged["app_token"] = SecretStr(app_token)
    hermes_payload = merged.get("hermes")
    if not isinstance(hermes_payload, dict):
        raise SettingsError("hermes configuration is required")
    merged["hermes"] = {**hermes_payload, "api_key": SecretStr(hermes_key)}
    stt_payload = raw_stt
    tts_payload = raw_tts
    merged["stt"] = [
        {**provider, "token": SecretStr(stt_token)}
        if isinstance(provider, dict) and provider.get("type") == "openai"
        else provider
        for provider in stt_payload
    ]
    merged["tts"] = [
        {**provider, "token": SecretStr(tts_token)}
        if isinstance(provider, dict) and provider.get("type") == "omnivoice"
        else provider
        for provider in tts_payload
    ]

    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise SettingsError("invalid instance configuration") from exc

    if not INSTANCE_ID_RE.fullmatch(settings.instance_id):
        raise SettingsError("invalid instance_id")
    if not settings.profile.strip():
        raise SettingsError("profile is required")
    if not _is_loopback_host(settings.listen_host):
        raise SettingsError("bridge listen_host must be loopback")

    parsed = urlparse(settings.hermes.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not _is_loopback_host(parsed.hostname):
        raise SettingsError("Hermes base_url must use a loopback host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SettingsError("invalid Hermes base_url")
    for provider in settings.stt:
        if isinstance(provider, OpenAISTTSettings):
            _validate_https_endpoint(provider.url, "/v1/audio/transcriptions")
            if MODEL_ID_RE.fullmatch(provider.model) is None:
                raise SettingsError("invalid STT model")
        elif isinstance(provider, WyomingSTTSettings):
            _validate_tcp_endpoint(provider.url)
        elif MODEL_ID_RE.fullmatch(provider.model) is None:
            raise SettingsError("invalid local STT model")
    for provider in settings.tts:
        if isinstance(provider, OmniVoiceSettings):
            _validate_https_endpoint(provider.url, "/v1/audio/speech")
        elif isinstance(provider, WyomingPiperSettings):
            _validate_tcp_endpoint(provider.url)
            if PIPER_VOICE_RE.fullmatch(provider.voice) is None:
                raise SettingsError("invalid Piper voice")
        elif PIPER_VOICE_RE.fullmatch(provider.voice) is None:
            raise SettingsError("invalid Piper voice")

    stt_endpoints = [
        _https_endpoint_key(provider.url) for provider in settings.stt
        if isinstance(provider, OpenAISTTSettings)
    ]
    if len(stt_endpoints) != len(set(stt_endpoints)):
        raise SettingsError("duplicate STT provider endpoint")
    tts_endpoints = [
        _https_endpoint_key(provider.url) for provider in settings.tts
        if isinstance(provider, OmniVoiceSettings)
    ]
    if len(tts_endpoints) != len(set(tts_endpoints)):
        raise SettingsError("duplicate TTS provider endpoint")

    resolved_state = _validate_state_dir(settings.state_dir)
    resolved_worker = _validate_worker_paths(settings.voice_worker, os.getuid())
    _bind_state_dir(resolved_state, settings.instance_id, settings.profile)
    return settings.model_copy(update={"state_dir": resolved_state, "voice_worker": resolved_worker})
