from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from talktohermes.app import create_app
from talktohermes.settings import load_settings
from tests.test_settings import write_instance


class Closeable:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def _client(
    tmp_path: Path,
    instance_id: str,
    token_char: str,
    assistant_name: str = "Klaus",
) -> tuple[TestClient, str]:
    config = write_instance(
        tmp_path, instance_id=instance_id, assistant_name=assistant_name
    )
    secret = tmp_path / f"{instance_id}.env"
    token = token_char * 48
    secret.write_text(
        f"APP_TOKEN={token}\nHERMES_API_KEY={'h' * 48}\n"
        f"STT_PRIMARY_TOKEN={'s' * 48}\nTTS_PRIMARY_TOKEN={'o' * 48}\n"
    )
    secret.chmod(0o600)
    return TestClient(create_app(load_settings(config))), token


def test_health_is_public_and_minimal(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, "instance-a", "a")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_v1_requires_bearer_token(tmp_path: Path) -> None:
    client, token = _client(tmp_path, "instance-a", "a")
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "instance_id": "instance-a", "assistant_name": "Klaus"}


def test_status_returns_configured_assistant_name(tmp_path: Path) -> None:
    client, token = _client(tmp_path, "instance-a", "a", assistant_name="T’Pol")

    response = client.get(
        "/v1/status", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["assistant_name"] == "T’Pol"


def test_tokens_are_isolated_between_instances(tmp_path: Path) -> None:
    client_a, token_a = _client(tmp_path, "instance-a", "a")
    client_b, token_b = _client(tmp_path, "instance-b", "b")

    assert client_a.get("/v1/status", headers={"Authorization": f"Bearer {token_b}"}).status_code == 401
    assert client_b.get("/v1/status", headers={"Authorization": f"Bearer {token_a}"}).status_code == 401
    assert client_a.get("/v1/status", headers={"Authorization": f"Bearer {token_a}"}).status_code == 200
    assert client_b.get("/v1/status", headers={"Authorization": f"Bearer {token_b}"}).status_code == 200


def test_profile_cannot_be_selected_by_request(tmp_path: Path) -> None:
    client, token = _client(tmp_path, "instance-a", "a")
    response = client.get(
        "/v1/status?profile=instance-b&instance_id=instance-b",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "instance_id": "instance-a", "assistant_name": "Klaus"}


def test_shutdown_closes_only_explicitly_owned_resources(tmp_path: Path) -> None:
    settings = load_settings(write_instance(tmp_path))
    owned = Closeable()
    injected_hermes = Closeable()
    injected_stt = Closeable()
    injected_tts = Closeable()
    app = create_app(
        settings,
        hermes=injected_hermes,
        stt=injected_stt,
        tts=injected_tts,
        closeables=(owned,),
    )

    with TestClient(app):
        pass

    assert owned.closed == 1
    assert injected_hermes.closed == 0
    assert injected_stt.closed == 0
    assert injected_tts.closed == 0
