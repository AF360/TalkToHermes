from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STT = ROOT / "services" / "stt"


def read(relative: str) -> str:
    return (STT / relative).read_text(encoding="utf-8")


def test_user_unit_runs_neutral_stt_python_and_single_gunicorn_worker() -> None:
    unit = read("deployment/talktohermes-stt.service")
    for required in (
        "ExecStart=/opt/stt/.venv/bin/python -m gunicorn",
        "--workers 1",
        "--threads 1",
        "--bind 127.0.0.1:5050",
        "--timeout 120",
        "--graceful-timeout 20",
        "--limit-request-line 1024",
        "--limit-request-fields 20",
        "--limit-request-field_size 512",
        "--access-logfile /dev/null",
        "ProtectHome=read-only",
        "ProtectSystem=strict",
        "PrivateDevices=false",
        "MemoryDenyWriteExecute=false",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "KeyringMode=private",
        "RemoveIPC=true",
        "PYTHONNOUSERSITE=1",
        "HF_HUB_OFFLINE=1",
        "TRANSFORMERS_OFFLINE=1",
        "LD_LIBRARY_PATH=/opt/stt/.venv/lib/python3.11/site-packages/nvidia/cudnn/lib",
        "Restart=on-failure",

    ):
        assert required in unit
    assert "User=" not in unit
    assert "ReadWritePaths=" not in unit
    assert "/bin/sh" not in unit
    assert "0.0.0.0" not in unit
    assert "192.168.100.20:5050" not in unit
    assert "IPAddressDeny=" not in unit
    assert "CapabilityBoundingSet=" not in unit
    assert "/opt/coglet-stt" not in unit


def test_caddy_exposes_only_tls_9444_to_loopback_5050() -> None:
    caddy = read("deployment/Caddyfile.stt")
    assert "https://primary-voice-server.home.arpa:9444" in caddy
    assert "reverse_proxy 127.0.0.1:5050" in caddy
    assert "192.168.100.20:5050" not in caddy


def test_runbook_pins_gunicorn_without_invented_hash_and_documents_safe_installation() -> None:
    runbook = read("README.md")
    requirements = read("gunicorn.requirements.in")
    lock = read("gunicorn.requirements.lock")
    assert requirements.strip() == "gunicorn==23.0.0"
    assert "gunicorn==23.0.0" in lock
    assert "ec400d38950de4dfd418cff8328b2c8faed0edb0d517d3394e457c317908ca4d" in lock
    for required in (
        "pip download",
        "sha256sum",
        "--require-hashes",
        "--no-index",
        "--target /opt/talktohermes-stt/vendor",
        "systemd-analyze --user verify",
        "systemctl --user",
        "127.0.0.1:5050",
        "https://primary-voice-server.home.arpa:9444",
        "/opt/stt/.venv/bin/python",
        "PyAV 16.0.1",
        "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    ):
        assert required in runbook
    assert "5005" not in runbook
    assert "TalkWithMe" not in runbook
    assert "/opt/coglet-stt" not in runbook
    assert "sha256:<" not in requirements + lock
