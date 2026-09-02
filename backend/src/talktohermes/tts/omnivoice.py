from __future__ import annotations

import asyncio
import os
import re
import ssl
import stat
from pathlib import Path
from urllib.parse import urlparse

import httpx

from talktohermes.provider_resilience import EndpointCircuitBreaker

from .base import (
    MAX_WAV_BYTES,
    SynthesizedAudio,
    TTSProviderUnavailable,
    TTSTechnicalError,
    validate_text,
    validate_wav_output,
)

OMNIVOICE_SPEECH_URL = "https://127.0.0.1:9443/v1/audio/speech"
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 256
MAX_RESPONSE_BYTES = MAX_WAV_BYTES
OMNIVOICE_VOICES = frozenset({"voice-01", "voice-02", "voice-03"})
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


class OmniVoiceTTS:
    name = "omnivoice"

    def __init__(
        self,
        bearer_token: str,
        *,
        voice: str = "voice-01",
        speech_url: str = OMNIVOICE_SPEECH_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
        connect_timeout_seconds: float = 0.5,
        response_timeout_seconds: float = 120.0,
        circuit_breaker: EndpointCircuitBreaker | None = None,
    ) -> None:
        if (
            not isinstance(bearer_token, str)
            or re.fullmatch(
                rf"[A-Za-z0-9_-]{{{MIN_TOKEN_LENGTH},{MAX_TOKEN_LENGTH}}}", bearer_token
            )
            is None
        ):
            raise ValueError("strong primary voice server bearer token required")
        if timeout is not None:
            connect_timeout_seconds = response_timeout_seconds = timeout
        if connect_timeout_seconds <= 0 or response_timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if voice not in OMNIVOICE_VOICES:
            raise ValueError("unsupported OmniVoice voice")
        self.voice = voice
        self._token = bearer_token
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=response_timeout_seconds,
            write=response_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._circuit = circuit_breaker or EndpointCircuitBreaker()
        self._speech_url = _validate_speech_url(speech_url)
        verify = ssl.create_default_context(cafile=SYSTEM_CA_BUNDLE)
        if transport is None:
            self._client = httpx.AsyncClient(
                trust_env=False,
                verify=verify,
            )
        else:
            self._client = httpx.AsyncClient(
                trust_env=False,
                verify=verify,
                transport=transport,
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def synthesize(
        self, text: str, output_path: Path
    ) -> SynthesizedAudio:
        validated_text = validate_text(text)
        path = _validate_target(output_path)
        permit = await self._circuit.acquire()
        if not permit.allowed:
            raise TTSProviderUnavailable("omnivoice_provider_unavailable", permit.state)
        circuit_state = permit.state
        try:
            async with self._client.stream(
                "POST",
                self._speech_url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "audio/wav",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "omnivoice",
                    "voice": self.voice,
                    "input": validated_text,
                    "response_format": "wav",
                },
                timeout=self._timeout,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise TTSTechnicalError("omnivoice_http_failed")
                if response.headers.get("Content-Type", "").strip().lower() != "audio/wav":
                    raise TTSTechnicalError("omnivoice_invalid_content_type")
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        if int(length) > MAX_RESPONSE_BYTES:
                            raise TTSTechnicalError("omnivoice_response_too_large")
                    except ValueError as exc:
                        raise TTSTechnicalError("omnivoice_invalid_response") from exc
                descriptor = _open_target(path)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                raise TTSTechnicalError("omnivoice_response_too_large")
                            output.write(chunk)
                except BaseException:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    raise
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            await permit.unavailable()
            raise TTSProviderUnavailable("omnivoice_provider_unavailable", permit.state) from exc
        except TTSTechnicalError:
            await permit.reachable()
            raise
        except httpx.TimeoutException as exc:
            await permit.reachable()
            raise asyncio.TimeoutError from exc
        except httpx.HTTPError as exc:
            await permit.reachable()
            raise TTSTechnicalError("omnivoice_transport_failed") from exc
        except OSError as exc:
            await permit.reachable()
            raise TTSTechnicalError("omnivoice_output_failed") from exc
        except BaseException:
            await permit.cancelled()
            raise

        await permit.reachable()

        return SynthesizedAudio(
            validate_wav_output(path, max_bytes=MAX_RESPONSE_BYTES),
            circuit_state=circuit_state,
        )


def _validate_target(raw_path: Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise TTSTechnicalError("invalid_output_path") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise TTSTechnicalError("invalid_output_path")
    return path.resolve(strict=True)


def _validate_speech_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid primary voice server speech URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or (port is not None and not 1 <= port <= 65535)
        or parsed.path != "/v1/audio/speech"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid primary voice server speech URL")
    return value


def _open_target(path: Path) -> int:
    flags = os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    os.fchmod(descriptor, 0o600)
    return descriptor
