#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import stat
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from talktohermes.app import create_app
from talktohermes.hermes_client import HermesClient
from talktohermes.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]
HERMES_ENV = Path.home() / ".hermes" / ".env"
HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"


def env_secret(name: str) -> str:
    for raw_line in HERMES_ENV.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip("'\"")
    return ""


async def inspect_and_delete(base_url: str, api_key: str, session_id: str) -> list[dict]:
    client = HermesClient(base_url, api_key)
    try:
        messages = await client.get_session_messages(session_id)
        await client.delete_session(session_id)
        return messages
    finally:
        await client.aclose()


def main() -> int:
    marker = "TTH-E2E-" + uuid.uuid4().hex[:12].upper()
    run_dir = ROOT / "runtime" / ("live-e2e-" + uuid.uuid4().hex)
    state_dir = run_dir / "state"
    session_id: str | None = None
    stage = "setup"
    api_key = env_secret("API_SERVER_KEY")
    if len(api_key) < 32:
        print("live_e2e_failed stage=setup code=missing_api_key")
        return 1

    try:
        state_dir.mkdir(parents=True, mode=0o700)
        os.chmod(run_dir, 0o700)
        os.chmod(state_dir, 0o700)
        app_token = secrets.token_urlsafe(48)
        stt_primary_token = secrets.token_urlsafe(48)
        omnivoice_token = secrets.token_urlsafe(48)
        secret_file = run_dir / "instance.env"
        secret_file.write_text(
            f"APP_TOKEN={app_token}\nHERMES_API_KEY={api_key}\n"
            f"STT_PRIMARY_TOKEN={stt_primary_token}\nTTS_PRIMARY_TOKEN={omnivoice_token}\n",
            encoding="utf-8",
        )
        secret_file.chmod(0o600)
        config_file = run_dir / "instance.yaml"
        config_file.write_text(
            f"""instance_id: instance-a-e2e
profile: default
development: true
listen_host: 127.0.0.1
listen_port: 0
state_dir: {state_dir}
secret_file: {secret_file}
hermes:
  base_url: http://127.0.0.1:8642
stt:
  - type: local
    model: small
tts:
  - type: local-piper
    voice: de_DE-ramona-low
voice_worker:
  python: {HERMES_ROOT / 'venv/bin/talktohermes-python'}
  script: {ROOT / 'worker/hermes_voice_worker.py'}
  hermes_root: {HERMES_ROOT}
""",
            encoding="utf-8",
        )
        settings = load_settings(config_file)
        headers = {"Authorization": "Bearer " + app_token}

        stage = "bridge_turns"
        app = create_app(settings)
        with TestClient(app) as bridge:
            stage = "conversation_create"
            conversation_response = bridge.post("/v1/conversations", headers=headers)
            if conversation_response.status_code != 201:
                raise RuntimeError("conversation_create_failed")
            conversation_id = conversation_response.json()["conversation_id"]
            session_id = app.state.storage.get_conversation(conversation_id).hermes_session_id

            stage = "first_turn_submit"
            first_payload = {
                "client_turn_id": "first-" + uuid.uuid4().hex,
                "text": (
                    "Dies ist ein technischer Persistenztest. Antworte ausschließlich mit "
                    f"dem Marker {marker} und merke ihn dir. Verwende keine Werkzeuge."
                ),
            }
            first = bridge.post(
                f"/v1/conversations/{conversation_id}/turns/text",
                headers=headers,
                json=first_payload,
            )
            if first.status_code != 202:
                raise RuntimeError("first_turn_submit_failed")
            first_turn_id = first.json()["turn_id"]
            stage = "first_turn_content"
            first_state = bridge.get(f"/v1/turns/{first_turn_id}", headers=headers).json()
            if first_state.get("state") != "completed" or marker not in first_state.get("response_text", ""):
                raise RuntimeError("first_turn_content_failed")

            stage = "second_turn_submit"
            second_payload = {
                "client_turn_id": "second-" + uuid.uuid4().hex,
                "text": (
                    "Nenne ausschließlich den Marker aus meiner unmittelbar vorherigen "
                    "Nachricht. Verwende keine Werkzeuge."
                ),
            }
            second = bridge.post(
                f"/v1/conversations/{conversation_id}/turns/text",
                headers=headers,
                json=second_payload,
            )
            if second.status_code != 202:
                raise RuntimeError("second_turn_submit_failed")
            second_turn_id = second.json()["turn_id"]
            stage = "session_continuity"
            second_state = bridge.get(f"/v1/turns/{second_turn_id}", headers=headers).json()
            if second_state.get("state") != "completed" or marker not in second_state.get("response_text", ""):
                raise RuntimeError("session_continuity_failed")

            stage = "idempotency"
            repeated = bridge.post(
                f"/v1/conversations/{conversation_id}/turns/text",
                headers=headers,
                json=second_payload,
            )
            if repeated.status_code != 202 or repeated.json()["turn_id"] != second_turn_id:
                raise RuntimeError("idempotency_failed")

        stage = "persistence_check"
        if session_id is None:
            raise RuntimeError("missing_session_id")
        messages = asyncio.run(
            inspect_and_delete(settings.hermes.base_url, api_key, session_id)
        )
        serialized = json.dumps(messages, ensure_ascii=False)
        if len(messages) < 4 or marker not in serialized:
            raise RuntimeError("persisted_messages_failed")

        print("live_e2e_ok")
        print("turns=2 idempotent_replay=1")
        print(f"persisted_messages={len(messages)} marker_recalled=true")
        print("test_session_deleted=true")
        return 0
    except Exception as exc:
        safe_code = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print(f"live_e2e_failed stage={stage} code={safe_code}")
        if session_id:
            try:
                asyncio.run(inspect_and_delete("http://127.0.0.1:8642", api_key, session_id))
                print("test_session_deleted=true")
            except Exception:
                print("test_session_deleted=false")
        return 1
    finally:
        if run_dir.exists():
            shutil.rmtree(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
