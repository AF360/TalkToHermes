from __future__ import annotations

import json
import re
import ssl
from pathlib import Path
from urllib.parse import urlparse

import httpx

from talktohermes.provider_resilience import EndpointCircuitBreaker

from .base import (
    STTProviderUnavailable,
    STTTechnicalError,
    STTTranscript,
    STTValidationError,
    validate_audio_input,
    validate_language,
)

MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 256
MAX_RESPONSE_BYTES = 65_536
MODEL = "large-v3-turbo"
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

_CONTENT_TYPES = {
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}


class OpenAICompatibleSTT:
    name = "openai"

    def __init__(
        self,
        url: str,
        bearer_token: str,
        *,
        model: str = "large-v3-turbo",
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        connect_timeout_seconds: float = 0.5,
        response_timeout_seconds: float = 120.0,
        circuit_breaker: EndpointCircuitBreaker | None = None,
    ) -> None:
        parsed = urlparse(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid primary voice server target") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or (port is not None and not 1 <= port <= 65535)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/v1/audio/transcriptions"}
        ):
            raise ValueError("invalid primary voice server target")
        if (
            not isinstance(bearer_token, str)
            or re.fullmatch(
                rf"[A-Za-z0-9_-]{{{MIN_TOKEN_LENGTH},{MAX_TOKEN_LENGTH}}}",
                bearer_token,
            )
            is None
        ):
            raise ValueError("strong primary voice server bearer token required")
        if timeout is not None:
            connect_timeout_seconds = response_timeout_seconds = timeout
        if connect_timeout_seconds <= 0 or response_timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", model) is None:
            raise ValueError("invalid STT model")
        self._url = (
            f"{url.rstrip('/')}/v1/audio/transcriptions"
            if parsed.path == ""
            else url
        )
        self.model = model
        self._token = bearer_token
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=response_timeout_seconds,
            write=response_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._circuit = circuit_breaker or EndpointCircuitBreaker()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            verify=ssl.create_default_context(cafile=SYSTEM_CA_BUNDLE),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def transcribe(self, audio_path: Path, language: str) -> str:
        path = validate_audio_input(audio_path)
        normalized_language = validate_language(language)
        content_type = _CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            raise STTValidationError("unsupported_audio_type")
        permit = await self._circuit.acquire()
        if not permit.allowed:
            raise STTProviderUnavailable("openai_provider_unavailable", permit.state)
        circuit_state = permit.state
        try:
            audio = path.read_bytes()
        except OSError as exc:
            await permit.cancelled()
            raise STTTechnicalError("openai_audio_read_failed") from exc

        try:
            async with self._client.stream(
                "POST",
                self._url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                files={"file": (f"audio{path.suffix.lower()}", audio, content_type)},
                data={
                    "model": self.model,
                    "language": normalized_language,
                    "response_format": "json",
                },
                timeout=self._timeout,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise STTTechnicalError("openai_http_failed")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_RESPONSE_BYTES:
                            raise STTTechnicalError("openai_response_too_large")
                    except ValueError as exc:
                        raise STTTechnicalError("openai_invalid_response") from exc
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise STTTechnicalError("openai_response_too_large")
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            await permit.unavailable()
            raise STTProviderUnavailable("openai_provider_unavailable", permit.state) from exc
        except STTTechnicalError:
            await permit.reachable()
            raise
        except httpx.TimeoutException as exc:
            await permit.reachable()
            raise TimeoutError from exc
        except httpx.HTTPError as exc:
            await permit.reachable()
            raise STTTechnicalError("openai_transport_failed") from exc
        except BaseException:
            await permit.cancelled()
            raise
        await permit.reachable()

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise STTTechnicalError("openai_invalid_response") from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise STTTechnicalError("openai_invalid_response")
        return STTTranscript(text.strip(), circuit_state)
