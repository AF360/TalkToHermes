from __future__ import annotations

import json

import httpx
import pytest

from talktohermes.hermes_client import HermesAPIError, HermesClient, parse_sse


@pytest.mark.asyncio
async def test_create_session_and_start_run_use_official_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer secret-key"
        if request.url.path == "/api/sessions":
            body = json.loads(request.content)
            assert body == {"source": "talktohermes", "title": "TalkToHermes"}
            return httpx.Response(201, json={"object": "hermes.session", "session": {"id": "session-1"}})
        if request.url.path == "/api/sessions/session-1/messages":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/runs":
            body = json.loads(request.content)
            assert body["input"] == "Hallo"
            assert body["session_id"] == "session-1"
            assert body["conversation_history"] == []
            assert "ein bis drei Sätzen" in body["instructions"]
            return httpx.Response(202, json={"run_id": "run-1", "status": "started"})
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hermes = HermesClient("http://127.0.0.1:8642", "secret-key", client=client)
        session_id = await hermes.create_session("TalkToHermes")
        run_id = await hermes.start_run(session_id, "Hallo")

    assert session_id == "session-1"
    assert run_id == "run-1"
    assert [request.url.path for request in requests] == [
        "/api/sessions",
        "/api/sessions/session-1/messages",
        "/v1/runs",
    ]


@pytest.mark.asyncio
async def test_start_run_forwards_canonical_session_history() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 1, "role": "user", "content": "Marker ALPHA"},
                        {"id": 2, "role": "assistant", "content": "ALPHA"},
                    ]
                },
            )
        body = json.loads(request.content)
        assert body["conversation_history"] == [
            {"role": "user", "content": "Marker ALPHA"},
            {"role": "assistant", "content": "ALPHA"},
        ]
        return httpx.Response(202, json={"run_id": "run-2"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hermes = HermesClient("http://127.0.0.1:8642", "secret-key", client=client)
        assert await hermes.start_run("session-1", "Welcher Marker?") == "run-2"


@pytest.mark.asyncio
async def test_start_run_combines_configured_voice_overlay_with_response_style() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"data": []})
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "run-detailed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hermes = HermesClient(
            "http://127.0.0.1:8642", "secret-key",
            voice_instructions="VOICE OVERLAY", client=client,
        )
        await hermes.start_run("session-1", "Erkläre das", response_style="detailed")

    assert bodies[0]["instructions"].startswith("VOICE OVERLAY")
    assert "ausführlich" in bodies[0]["instructions"].lower()


@pytest.mark.asyncio
async def test_approval_allows_only_once_and_deny() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"resolved": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hermes = HermesClient("http://127.0.0.1:8642", "secret-key", client=client)
        await hermes.approve("run-1", "once")
        await hermes.approve("run-1", "deny")
        with pytest.raises(ValueError, match="once or deny"):
            await hermes.approve("run-1", "always")  # type: ignore[arg-type]

    assert calls == [{"choice": "once"}, {"choice": "deny"}]


@pytest.mark.asyncio
async def test_stop_uses_official_endpoint() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"status": "stopping"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await HermesClient("http://127.0.0.1:8642", "secret-key", client=client).stop("run-1")
    assert paths == ["/v1/runs/run-1/stop"]


@pytest.mark.asyncio
async def test_sse_parser_handles_chunk_boundaries_comments_and_event_names() -> None:
    async def chunks():
        for chunk in (
            b": keepalive\n\nevent: message.delta\ndata: {\"delta\":\"Hal",
            b"lo\"}\n\nevent: run.completed\ndata: {\"output\":\"Hallo!\"}\n\n",
        ):
            yield chunk

    events = [event async for event in parse_sse(chunks())]
    assert events == [
        {"event": "message.delta", "delta": "Hallo"},
        {"event": "run.completed", "output": "Hallo!"},
    ]


@pytest.mark.asyncio
async def test_malformed_sse_json_is_bounded_error() -> None:
    async def chunks():
        yield b"event: message.delta\ndata: secret-invalid-json\n\n"

    with pytest.raises(HermesAPIError) as caught:
        _ = [event async for event in parse_sse(chunks())]
    assert "secret-invalid-json" not in str(caught.value)


@pytest.mark.asyncio
async def test_http_error_does_not_echo_secret_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="provider leaked secret-value")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hermes = HermesClient("http://127.0.0.1:8642", "secret-key", client=client)
        with pytest.raises(HermesAPIError) as caught:
            await hermes.create_session("TalkToHermes")
    assert "secret-value" not in str(caught.value)
    assert caught.value.status_code == 500
