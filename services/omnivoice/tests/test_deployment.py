from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_production_unit_is_dedicated_and_hardened() -> None:
    unit = (ROOT / "deployment" / "talktohermes-omnivoice.service").read_text(encoding="utf-8")
    assert "TALKTOHERMES_OMNIVOICE_LISTENER" not in unit
    assert "8181" not in unit
    for directive in (
        "User=talktohermes-omnivoice",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "Restart=on-failure",
        "MemoryMax=",
        "TasksMax=",
        "UMask=0077",
    ):
        assert directive in unit


def test_sample_config_has_only_placeholders_and_private_dedicated_listener() -> None:
    sample = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "listen_host: 192.168.100.20" in sample
    assert "listen_port: 9090" in sample
    assert "8181" not in sample
    assert "<logical-voice-id>" in sample
    assert "<absolute-private" in sample
