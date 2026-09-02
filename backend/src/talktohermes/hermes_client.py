from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx

from .response_style import DEFAULT_VOICE_INSTRUCTIONS, build_voice_instructions


class HermesAPIError(RuntimeError):
    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


async def parse_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    buffer = b""
    async for chunk in chunks:
        buffer += chunk
        if len(buffer) > 1_048_576:
            raise HermesAPIError("sse_frame_too_large")
        buffer = buffer.replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            raw_frame, buffer = buffer.split(b"\n\n", 1)
            if not raw_frame or raw_frame.startswith(b":"):
                continue
            event_name: str | None = None
            data_lines: list[str] = []
            for raw_line in raw_frame.split(b"\n"):
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HermesAPIError("invalid_sse_encoding") from exc
                if line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if not separator:
                    continue
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event_name = value
                elif field == "data":
                    data_lines.append(value)
            if not data_lines:
                continue
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError as exc:
                raise HermesAPIError("invalid_sse_json") from exc
            if not isinstance(payload, dict):
                raise HermesAPIError("invalid_sse_payload")
            if event_name:
                payload["event"] = event_name
            yield payload
    if buffer.strip() and not buffer.lstrip().startswith(b":"):
        raise HermesAPIError("incomplete_sse_frame")


class HermesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        voice_instructions: str = DEFAULT_VOICE_INSTRUCTIONS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._voice_instructions = voice_instructions.strip()
        if not self._voice_instructions:
            raise ValueError("voice_instructions are required")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        try:
            response = await self._client.request(
                method, f"{self.base_url}{path}", headers=headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise HermesAPIError("hermes_transport_error") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise HermesAPIError("hermes_http_error", status_code=response.status_code)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HermesAPIError("invalid_hermes_json", status_code=response.status_code) from exc
        if not isinstance(payload, dict):
            raise HermesAPIError("invalid_hermes_payload", status_code=response.status_code)
        return payload

    async def create_session(self, title: str = "TalkToHermes") -> str:
        payload = await self._request(
            "POST",
            "/api/sessions",
            json={"source": "talktohermes", "title": title},
        )
        session = payload.get("session")
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise HermesAPIError("missing_session_id")
        return session_id

    async def get_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        payload = await self._request("GET", f"/api/sessions/{session_id}/messages")
        messages = payload.get("data")
        if not isinstance(messages, list):
            raise HermesAPIError("invalid_session_messages")
        return [message for message in messages if isinstance(message, dict)]

    async def delete_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/api/sessions/{session_id}")

    async def start_run(
        self, session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        messages = await self.get_session_messages(session_id)
        history: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if isinstance(role, str) and role and isinstance(content, str) and content:
                history.append({"role": role, "content": content})
        payload = await self._request(
            "POST",
            "/v1/runs",
            json={
                "input": input_text,
                "session_id": session_id,
                "conversation_history": history,
                "instructions": build_voice_instructions(
                    self._voice_instructions, response_style
                ),
            },
        )
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise HermesAPIError("missing_run_id")
        return run_id

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/runs/{run_id}")

    async def events(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream(
                "GET",
                f"{self.base_url}/v1/runs/{run_id}/events",
                headers={**self._headers, "Accept": "text/event-stream"},
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise HermesAPIError("hermes_http_error", status_code=response.status_code)
                async for event in parse_sse(response.aiter_bytes()):
                    yield event
        except HermesAPIError:
            raise
        except httpx.HTTPError as exc:
            raise HermesAPIError("hermes_transport_error") from exc

    async def approve(self, run_id: str, decision: Literal["once", "deny"]) -> None:
        if decision not in {"once", "deny"}:
            raise ValueError("approval decision must be once or deny")
        await self._request(
            "POST",
            f"/v1/runs/{run_id}/approval",
            json={"choice": decision},
        )

    async def stop(self, run_id: str) -> None:
        await self._request("POST", f"/v1/runs/{run_id}/stop", json={})
