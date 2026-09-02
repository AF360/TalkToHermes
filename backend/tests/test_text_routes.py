from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from talktohermes.app import create_app
from talktohermes.settings import load_settings
from talktohermes.storage import Storage
from tests.test_settings import write_instance


class FakeHermes:
    def __init__(self) -> None:
        self.created_sessions = 0
        self.started_runs: list[tuple[str, str]] = []
        self.approvals: list[tuple[str, str]] = []
        self.stops: list[str] = []
        self.deleted_sessions: list[str] = []

    async def create_session(self, title: str = "TalkToHermes") -> str:
        self.created_sessions += 1
        return f"hermes-session-{self.created_sessions}"

    async def start_run(
        self, session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        self.started_runs.append((session_id, input_text))
        return f"run-{len(self.started_runs)}"

    async def events(self, run_id: str) -> AsyncIterator[dict]:
        yield {"event": "message.delta", "delta": "Hallo "}
        yield {"event": "message.delta", "delta": "Welt"}
        yield {"event": "run.completed", "output": "Hallo Welt"}

    async def get_run(self, run_id: str) -> dict:
        return {"status": "completed", "output": "Hallo Welt"}

    async def approve(self, run_id: str, decision: str) -> None:
        self.approvals.append((run_id, decision))

    async def stop(self, run_id: str) -> None:
        self.stops.append(run_id)

    async def delete_session(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)


def configured_client(tmp_path: Path) -> tuple[TestClient, str, Storage, FakeHermes]:
    config = write_instance(tmp_path)
    settings = load_settings(config)
    storage = Storage(settings.state_dir / "talktohermes.sqlite3")
    hermes = FakeHermes()
    app = create_app(settings, storage=storage, hermes=hermes)
    return TestClient(app), settings.app_token.get_secret_value(), storage, hermes


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_conversation_and_text_turn_complete_in_real_mapping(tmp_path: Path) -> None:
    client, token, storage, hermes = configured_client(tmp_path)
    conversation_response = client.post("/v1/conversations", headers=auth(token))
    assert conversation_response.status_code == 201
    conversation_id = conversation_response.json()["conversation_id"]

    response = client.post(
        f"/v1/conversations/{conversation_id}/turns/text",
        headers=auth(token),
        json={"client_turn_id": "client-1", "text": "Hallo"},
    )
    assert response.status_code == 202
    turn_id = response.json()["turn_id"]

    turn_response = client.get(f"/v1/turns/{turn_id}", headers=auth(token))
    assert turn_response.status_code == 200
    assert turn_response.json()["state"] == "completed"
    assert turn_response.json()["response_text"] == "Hallo Welt"
    assert hermes.started_runs == [("hermes-session-1", "Hallo")]
    assert storage.get_conversation(conversation_id).hermes_session_id == "hermes-session-1"


def test_duplicate_client_turn_id_does_not_start_second_run(tmp_path: Path) -> None:
    client, token, _, hermes = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    payload = {"client_turn_id": "client-1", "text": "Hallo"}
    first = client.post(f"/v1/conversations/{conversation_id}/turns/text", headers=auth(token), json=payload)
    second = client.post(f"/v1/conversations/{conversation_id}/turns/text", headers=auth(token), json=payload)
    assert first.json()["turn_id"] == second.json()["turn_id"]
    assert len(hermes.started_runs) == 1


def test_client_cannot_select_profile_or_instance(tmp_path: Path) -> None:
    client, token, _, _ = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    response = client.post(
        f"/v1/conversations/{conversation_id}/turns/text",
        headers=auth(token),
        json={
            "client_turn_id": "client-1",
            "text": "Hallo",
            "profile": "instance-b",
            "instance_id": "instance-b",
        },
    )
    assert response.status_code == 422


def test_events_are_replayed_as_sse_without_internal_ids(tmp_path: Path) -> None:
    client, token, _, _ = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    turn_id = client.post(
        f"/v1/conversations/{conversation_id}/turns/text",
        headers=auth(token),
        json={"client_turn_id": "client-1", "text": "Hallo"},
    ).json()["turn_id"]
    response = client.get(f"/v1/turns/{turn_id}/events", headers=auth(token))
    assert response.status_code == 200
    assert "turn.accepted" in response.text
    assert "turn.completed" in response.text
    assert "hermes-session" not in response.text
    assert "run-" not in response.text


def test_approval_contract_is_once_or_deny_only(tmp_path: Path) -> None:
    client, token, storage, hermes = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    turn, _ = storage.create_or_get_text_turn(conversation_id, "client-approval", "Test")
    storage.set_run(turn.id, "run-approval")
    storage.await_approval(turn.id, (datetime.now(UTC) + timedelta(minutes=1)).isoformat())

    assert client.post(
        f"/v1/turns/{turn.id}/approval", headers=auth(token), json={"decision": "always"}
    ).status_code == 422
    assert client.post(
        f"/v1/turns/{turn.id}/approval", headers=auth(token), json={"decision": "once"}
    ).status_code == 204
    assert hermes.approvals == [("run-approval", "once")]


def test_cancel_uses_internal_run_id_without_exposing_it(tmp_path: Path) -> None:
    client, token, storage, hermes = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    turn, _ = storage.create_or_get_text_turn(conversation_id, "client-cancel", "Test")
    storage.set_run(turn.id, "run-cancel")
    response = client.post(f"/v1/turns/{turn.id}/cancel", headers=auth(token))
    assert response.status_code == 202
    assert hermes.stops == ["run-cancel"]
    assert "run-cancel" not in response.text


def test_last_event_id_replays_only_newer_events(tmp_path: Path) -> None:
    client, token, _, _ = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    turn_id = client.post(
        f"/v1/conversations/{conversation_id}/turns/text",
        headers=auth(token),
        json={"client_turn_id": "client-replay", "text": "Hallo"},
    ).json()["turn_id"]

    response = client.get(
        f"/v1/turns/{turn_id}/events",
        headers={**auth(token), "Last-Event-ID": "2"},
    )
    ids = [int(line.removeprefix("id: ")) for line in response.text.splitlines() if line.startswith("id: ")]
    assert response.status_code == 200
    assert ids == sorted(ids)
    assert ids and min(ids) > 2


def test_last_event_id_rejects_invalid_negative_and_future_values(tmp_path: Path) -> None:
    client, token, storage, _ = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    turn_id = client.post(
        f"/v1/conversations/{conversation_id}/turns/text",
        headers=auth(token),
        json={"client_turn_id": "client-last-id", "text": "Hallo"},
    ).json()["turn_id"]
    future = len(storage.list_events(turn_id)) + 1

    for value in ("garbage", "-1", str(future)):
        response = client.get(
            f"/v1/turns/{turn_id}/events",
            headers={**auth(token), "Last-Event-ID": value},
        )
        assert response.status_code == 422, value


def test_include_text_false_redacts_response_and_delta_events(tmp_path: Path) -> None:
    client, token, _, _ = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    turn_id = client.post(
        f"/v1/conversations/{conversation_id}/turns/text",
        headers=auth(token),
        json={"client_turn_id": "client-private", "text": "raw transcript", "include_text": False},
    ).json()["turn_id"]

    turn = client.get(f"/v1/turns/{turn_id}", headers=auth(token))
    events = client.get(f"/v1/turns/{turn_id}/events", headers=auth(token))
    assert "response_text" not in turn.json()
    assert "raw transcript" not in events.text
    assert "Hallo Welt" not in events.text
    assert "hermes.delta" not in events.text


@pytest.mark.asyncio
async def test_delete_waits_for_inflight_terminal_race_then_cascades_once(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    settings = load_settings(config)
    storage = Storage(settings.state_dir / "race.sqlite3")
    hermes = FakeHermes()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_start(
        session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        hermes.started_runs.append((session_id, input_text))
        started.set()
        await release.wait()
        return "run-race"

    hermes.start_run = blocked_start  # type: ignore[method-assign]
    app = create_app(settings, storage=storage, hermes=hermes)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        conversation_id = (await api.post("/v1/conversations", headers=auth(
            settings.app_token.get_secret_value()
        ))).json()["conversation_id"]
        create = asyncio.create_task(api.post(
            f"/v1/conversations/{conversation_id}/turns/text",
            headers=auth(settings.app_token.get_secret_value()),
            json={"client_turn_id": "race", "text": "Hallo"},
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        # 202 is returned while processing remains registered and in flight.
        assert (await asyncio.wait_for(create, timeout=1)).status_code == 202
        deleting = asyncio.create_task(api.delete(
            f"/v1/conversations/{conversation_id}",
            headers=auth(settings.app_token.get_secret_value()),
        ))
        await asyncio.sleep(0)
        assert not deleting.done()
        release.set()

        assert (await asyncio.wait_for(deleting, timeout=1)).status_code == 204
        assert hermes.stops == ["run-race"]
        assert hermes.deleted_sessions == []
        assert storage.list_turns() == []


@pytest.mark.asyncio
async def test_delete_timeout_is_bounded_and_preserves_conversation(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    settings = load_settings(config)
    storage = Storage(settings.state_dir / "timeout.sqlite3")
    hermes = FakeHermes()
    started = asyncio.Event()
    release = asyncio.Event()

    async def never_start(
        session_id: str, input_text: str, response_style: str = "short"
    ) -> str:
        started.set()
        await release.wait()
        return "run-late"

    hermes.start_run = never_start  # type: ignore[method-assign]
    app = create_app(settings, storage=storage, hermes=hermes)
    app.state.turn_service.quiescence_timeout_seconds = 0.02
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        headers = auth(settings.app_token.get_secret_value())
        conversation_id = (await api.post("/v1/conversations", headers=headers)).json()[
            "conversation_id"
        ]
        create = asyncio.create_task(api.post(
            f"/v1/conversations/{conversation_id}/turns/text", headers=headers,
            json={"client_turn_id": "timeout", "text": "Hallo"},
        ))
        await asyncio.wait_for(started.wait(), timeout=1)

        response = await asyncio.wait_for(api.delete(
            f"/v1/conversations/{conversation_id}", headers=headers
        ), timeout=0.2)

        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert storage.get_conversation(conversation_id).id == conversation_id
        assert len(storage.list_turns()) == 1
        rejected = await api.post(
            f"/v1/conversations/{conversation_id}/turns/text", headers=headers,
            json={"client_turn_id": "after-timeout", "text": "Nein"},
        )
        assert rejected.status_code == 409
        release.set()
        assert (await asyncio.wait_for(create, timeout=1)).status_code == 202


@pytest.mark.asyncio
async def test_delete_deadline_includes_blocked_approval_lock(tmp_path: Path) -> None:
    settings = load_settings(write_instance(tmp_path))
    storage = Storage(settings.state_dir / "approval-lock.sqlite3")
    hermes = FakeHermes()
    approval_started = asyncio.Event()
    release_approval = asyncio.Event()

    async def blocked_approve(run_id: str, decision: str) -> None:
        approval_started.set()
        await release_approval.wait()
        hermes.approvals.append((run_id, decision))

    hermes.approve = blocked_approve  # type: ignore[method-assign]
    app = create_app(settings, storage=storage, hermes=hermes)
    app.state.turn_service.quiescence_timeout_seconds = 0.02
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        headers = auth(settings.app_token.get_secret_value())
        conversation_id = (await api.post("/v1/conversations", headers=headers)).json()[
            "conversation_id"
        ]
        turn, _ = storage.create_or_get_text_turn(conversation_id, "approval-lock", "Test")
        storage.set_run(turn.id, "run-approval-lock")
        storage.await_approval(
            turn.id, (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        )
        approving = asyncio.create_task(api.post(
            f"/v1/turns/{turn.id}/approval", headers=headers, json={"decision": "once"}
        ))
        await asyncio.wait_for(approval_started.wait(), timeout=1)

        response = await asyncio.wait_for(
            api.delete(f"/v1/conversations/{conversation_id}", headers=headers),
            timeout=0.2,
        )

        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert storage.get_conversation(conversation_id).id == conversation_id
        release_approval.set()
        assert (await asyncio.wait_for(approving, timeout=1)).status_code == 204


def test_approval_is_rejected_while_conversation_is_deleting(tmp_path: Path) -> None:
    client, token, storage, hermes = configured_client(tmp_path)
    conversation_id = client.post("/v1/conversations", headers=auth(token)).json()["conversation_id"]
    turn, _ = storage.create_or_get_text_turn(conversation_id, "deleting-approval", "Test")
    storage.set_run(turn.id, "run-approval")
    storage.await_approval(turn.id, (datetime.now(UTC) + timedelta(minutes=1)).isoformat())
    storage.begin_conversation_delete(conversation_id)

    response = client.post(
        f"/v1/turns/{turn.id}/approval", headers=auth(token), json={"decision": "once"}
    )

    assert response.status_code == 409
    assert hermes.approvals == []
