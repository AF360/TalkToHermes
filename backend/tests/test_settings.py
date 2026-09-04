from __future__ import annotations

import os
from pathlib import Path

import pytest

from talktohermes.settings import SettingsError, load_settings


def write_instance(
    tmp_path: Path,
    *,
    instance_id: str = "instance-a",
    assistant_name: str = "Klaus",
    port: int = 0,
    host: str = "127.0.0.1",
) -> Path:
    state = tmp_path / instance_id
    state.mkdir(mode=0o700)
    worker = tmp_path / f"{instance_id}-worker"
    worker.mkdir(mode=0o700)
    hermes_root = worker / "hermes-root"
    bin_dir = hermes_root / "venv" / "bin"
    bin_dir.mkdir(parents=True, mode=0o700)
    hermes_root.chmod(0o700)
    python = bin_dir / "talktohermes-python"
    base_dir = tmp_path / "base-python" / "bin"
    base_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    base_python = base_dir / "python3.13"
    base_python.write_text("#!/bin/sh\n", encoding="utf-8")
    base_python.chmod(0o700)
    python.write_bytes(base_python.read_bytes())
    python.chmod(0o700)
    (bin_dir / "python").symlink_to(base_python)
    pyvenv = hermes_root / "venv" / "pyvenv.cfg"
    pyvenv.write_text(f"home = {base_dir}\nversion = 3.13.0\n", encoding="utf-8")
    pyvenv.chmod(0o600)
    (hermes_root / "venv").chmod(0o700)
    script = worker / "hermes_voice_worker.py"
    script.write_text("# worker\n", encoding="utf-8")
    script.chmod(0o700)
    secret = tmp_path / f"{instance_id}.env"
    secret.write_text(
        "APP_TOKEN=" + ("a" * 48)
        + "\nHERMES_API_KEY=" + ("h" * 48)
        + "\nSTT_PRIMARY_TOKEN=" + ("s" * 48)
        + "\nTTS_PRIMARY_TOKEN=" + ("o" * 48)
        + "\n",
        encoding="utf-8",
    )
    secret.chmod(0o600)
    config = tmp_path / f"{instance_id}.yaml"
    config.write_text(
        f"""instance_id: {instance_id}
assistant_name: {assistant_name}
profile: default
development: true
listen_host: {host}
listen_port: {port}
state_dir: {state}
secret_file: {secret}
exposed_tools:
  OpenCodeTool: OpenCodeTool
  functions.browser_exec: BrowserTool
hermes:
  base_url: http://127.0.0.1:8642
stt:
  - type: openai
    url: https://primary-voice-server.home.arpa:9444/v1/audio/transcriptions
    model: large-v3-turbo
  - type: wyoming
    url: tcp://fallback-voice-server.home.arpa:10300
  - type: local
    model: small
tts:
  - type: omnivoice
    url: https://primary-voice-server.home.arpa:9443/v1/audio/speech
    voice: voice-02
  - type: wyoming-piper
    url: tcp://fallback-voice-server.home.arpa:10201
    voice: de_DE-thorsten-medium
  - type: local-piper
    voice: de_DE-ramona-low
voice_worker:
  python: {python}
  script: {script}
  hermes_root: {hermes_root}
""",
        encoding="utf-8",
    )
    return config


def test_migrates_known_legacy_instance_name_without_assistant_name(tmp_path: Path) -> None:
    config = write_instance(tmp_path, instance_id="klaus")
    config.write_text(
        config.read_text(encoding="utf-8").replace("assistant_name: Klaus\n", ""),
        encoding="utf-8",
    )

    assert load_settings(config).assistant_name == "Klaus"


def test_loads_configured_assistant_name(tmp_path: Path) -> None:
    settings = load_settings(write_instance(tmp_path, assistant_name="T’Pol"))

    assert settings.assistant_name == "T’Pol"


def test_rejects_empty_assistant_name(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "assistant_name: Klaus", 'assistant_name: ""'
        ),
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match="invalid instance configuration"):
        load_settings(config)


def test_rejects_overlong_assistant_name(tmp_path: Path) -> None:
    config = write_instance(tmp_path, assistant_name="A" * 65)

    with pytest.raises(SettingsError, match="invalid instance configuration"):
        load_settings(config)


def test_rejects_nonprinting_assistant_name(tmp_path: Path) -> None:
    config = write_instance(tmp_path, assistant_name="\u200b")

    with pytest.raises(SettingsError, match="invalid instance configuration"):
        load_settings(config)


def test_rejects_padded_assistant_name(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "assistant_name: Klaus", 'assistant_name: " Klaus "'
        ),
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match="invalid instance configuration"):
        load_settings(config)


def test_loads_valid_isolated_instance(tmp_path: Path) -> None:
    settings = load_settings(write_instance(tmp_path))
    assert settings.instance_id == "instance-a"
    assert settings.assistant_name == "Klaus"
    assert settings.exposed_tools == {
        "OpenCodeTool": "OpenCodeTool",
        "functions.browser_exec": "BrowserTool",
    }
    assert settings.listen_port == 0
    assert settings.app_token.get_secret_value() == "a" * 48
    assert settings.hermes.api_key.get_secret_value() == "h" * 48
    assert settings.stt[0].type == "openai"
    assert settings.stt[0].token.get_secret_value() == "s" * 48
    assert [provider.type for provider in settings.stt] == ["openai", "wyoming", "local"]
    assert settings.tts[0].type == "omnivoice"
    assert settings.tts[0].token.get_secret_value() == "o" * 48
    assert settings.stt[0].connect_timeout_seconds == 0.5
    assert settings.stt[0].response_timeout_seconds == 120.0
    assert settings.stt[0].circuit_cooldown_seconds == 45.0
    assert settings.tts[0].connect_timeout_seconds == 0.5
    assert settings.tts[0].response_timeout_seconds == 120.0
    assert settings.tts[0].circuit_cooldown_seconds == 45.0
    assert [provider.type for provider in settings.tts] == [
        "omnivoice", "wyoming-piper", "local-piper"
    ]
    assert "gesprochenen Unterhaltung" in settings.hermes.voice_instructions
    assert settings.voice_worker.python.name == "talktohermes-python"
    assert settings.voice_worker.python.parent.name == "bin"
    assert settings.text_retention_hours == 24.0
    assert settings.cleanup_interval_seconds == 900.0


def test_text_retention_cannot_exceed_24_hours(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text().replace(
            "voice_worker:\n", "text_retention_hours: 24.1\nvoice_worker:\n"
        )
    )

    with pytest.raises(SettingsError, match="invalid instance configuration"):
        load_settings(config)


def test_cleanup_discovery_interval_cannot_exceed_15_minutes(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text().replace(
            "voice_worker:\n", "cleanup_interval_seconds: 901\nvoice_worker:\n"
        )
    )

    with pytest.raises(SettingsError, match="invalid instance configuration"):
        load_settings(config)


def test_optional_fallback_provider_can_be_omitted(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    text = config.read_text()
    text = text.replace(
        "  - type: wyoming\n    url: tcp://fallback-voice-server.home.arpa:10300\n",
        "",
    ).replace(
        "  - type: wyoming-piper\n    url: tcp://fallback-voice-server.home.arpa:10201\n    voice: de_DE-thorsten-medium\n",
        "",
    )
    config.write_text(text)

    settings = load_settings(config)
    assert [provider.type for provider in settings.stt] == ["openai", "local"]
    assert [provider.type for provider in settings.tts] == ["omnivoice", "local-piper"]


@pytest.mark.parametrize(
    ("entry", "equivalent_entries", "remove_entry", "message"),
    [
        (
            "  - type: openai\n"
            "    url: https://primary-voice-server.home.arpa:9444/v1/audio/transcriptions\n"
            "    model: large-v3-turbo\n",
            "  - type: openai\n"
            "    url: https://primary-voice-server.home.arpa/v1/audio/transcriptions\n"
            "    model: large-v3-turbo\n"
            "  - type: openai\n"
            "    url: https://PRIMARY-VOICE-SERVER.home.arpa:443/v1/audio/transcriptions\n"
            "    model: large-v3-turbo\n",
            "  - type: local\n    model: small\n",
            "duplicate STT provider endpoint",
        ),
        (
            "  - type: omnivoice\n"
            "    url: https://primary-voice-server.home.arpa:9443/v1/audio/speech\n"
            "    voice: voice-02\n",
            "  - type: omnivoice\n"
            "    url: https://primary-voice-server.home.arpa/v1/audio/speech\n"
            "    voice: voice-02\n"
            "  - type: omnivoice\n"
            "    url: https://PRIMARY-VOICE-SERVER.home.arpa:443/v1/audio/speech\n"
            "    voice: voice-02\n",
            "  - type: local-piper\n    voice: de_DE-ramona-low\n",
            "duplicate TTS provider endpoint",
        ),
    ],
)
def test_rejects_duplicate_remote_provider_endpoints(
    tmp_path: Path,
    entry: str,
    equivalent_entries: str,
    remove_entry: str,
    message: str,
) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text().replace(remove_entry, "", 1).replace(
            entry, equivalent_entries, 1
        )
    )

    with pytest.raises(SettingsError, match=message):
        load_settings(config)


def test_local_only_lists_do_not_require_unused_provider_tokens(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text()
        .replace(
            "  - type: openai\n    url: https://primary-voice-server.home.arpa:9444/v1/audio/transcriptions\n    model: large-v3-turbo\n",
            "",
        )
        .replace(
            "  - type: wyoming\n    url: tcp://fallback-voice-server.home.arpa:10300\n",
            "",
        )
        .replace(
            "  - type: omnivoice\n    url: https://primary-voice-server.home.arpa:9443/v1/audio/speech\n    voice: voice-02\n",
            "",
        )
        .replace(
            "  - type: wyoming-piper\n    url: tcp://fallback-voice-server.home.arpa:10201\n    voice: de_DE-thorsten-medium\n",
            "",
        )
    )
    secret = tmp_path / "instance-a.env"
    secret.write_text(
        f"APP_TOKEN={'a' * 48}\nHERMES_API_KEY={'h' * 48}\n",
        encoding="utf-8",
    )
    secret.chmod(0o600)

    settings = load_settings(config)
    assert [provider.type for provider in settings.stt] == ["local"]
    assert [provider.type for provider in settings.tts] == ["local-piper"]


def test_voice_instructions_are_configurable_and_bounded(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(config.read_text().replace(
        "  base_url: http://127.0.0.1:8642\n",
        "  base_url: http://127.0.0.1:8642\n  voice_instructions: Antworte sprachlich knapp.\n",
    ))
    assert load_settings(config).hermes.voice_instructions == "Antworte sprachlich knapp."

    config.write_text(config.read_text().replace(
        "Antworte sprachlich knapp.", "x" * 4001,
    ))
    with pytest.raises(SettingsError, match="invalid instance configuration"):
        load_settings(config)


def test_rejects_non_loopback_bridge_bind(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="loopback"):
        load_settings(write_instance(tmp_path, host="0.0.0.0"))


def test_rejects_non_loopback_hermes_url(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(config.read_text().replace("127.0.0.1:8642", "192.168.100.10:8642"))
    with pytest.raises(SettingsError, match="Hermes.*loopback"):
        load_settings(config)


def test_rejects_weak_or_reused_tokens(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    secret = tmp_path / "instance-a.env"
    secret.write_text(
        "APP_TOKEN=short\nHERMES_API_KEY=short\n"
        f"STT_PRIMARY_TOKEN={'s' * 48}\nTTS_PRIMARY_TOKEN={'o' * 48}\n"
    )
    with pytest.raises(SettingsError, match="secret"):
        load_settings(config)

    same = "x" * 48
    secret.write_text(
        f"APP_TOKEN={same}\nHERMES_API_KEY={same}\n"
        f"STT_PRIMARY_TOKEN={'s' * 48}\nTTS_PRIMARY_TOKEN={'o' * 48}\n"
    )
    with pytest.raises(SettingsError, match="different"):
        load_settings(config)


@pytest.mark.parametrize("invalid", ["!" * 48, "x" * 257])
@pytest.mark.parametrize(
    ("key", "valid"),
    [
        ("APP_TOKEN", "a" * 48),
        ("HERMES_API_KEY", "h" * 48),
        ("STT_PRIMARY_TOKEN", "s" * 48),
        ("TTS_PRIMARY_TOKEN", "o" * 48),
    ],
)
def test_rejects_invalid_or_oversized_production_tokens(
    tmp_path: Path, invalid: str, key: str, valid: str
) -> None:
    config = write_instance(tmp_path)
    secret = tmp_path / "instance-a.env"
    original = secret.read_text(encoding="utf-8")
    secret.write_text(original.replace(f"{key}={valid}", f"{key}={invalid}"))

    with pytest.raises(SettingsError, match="secret"):
        load_settings(config)


def test_rejects_unknown_secret_file_keys(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    secret = tmp_path / "instance-a.env"
    secret.write_text(secret.read_text(encoding="utf-8") + "UNREVIEWED_TOKEN=value\n")

    with pytest.raises(SettingsError, match="unknown"):
        load_settings(config)


@pytest.mark.parametrize(
    "duplicate_key",
    ["APP_TOKEN", "HERMES_API_KEY", "STT_PRIMARY_TOKEN", "TTS_PRIMARY_TOKEN"],
)
def test_rejects_any_reused_production_token(tmp_path: Path, duplicate_key: str) -> None:
    config = write_instance(tmp_path)
    secret = tmp_path / "instance-a.env"
    values = {
        "APP_TOKEN": "a" * 48,
        "HERMES_API_KEY": "h" * 48,
        "STT_PRIMARY_TOKEN": "s" * 48,
        "TTS_PRIMARY_TOKEN": "o" * 48,
    }
    values[duplicate_key] = values["APP_TOKEN" if duplicate_key != "APP_TOKEN" else "HERMES_API_KEY"]
    secret.write_text("".join(f"{key}={value}\n" for key, value in values.items()))

    with pytest.raises(SettingsError, match="different"):
        load_settings(config)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "https://primary-voice-server.home.arpa:9444/v1/audio/transcriptions",
            "http://primary-voice-server.home.arpa:9444/v1/audio/transcriptions",
            "HTTPS provider endpoint",
        ),
        (
            "https://primary-voice-server.home.arpa:9443/v1/audio/speech",
            "https://user@primary-voice-server.home.arpa:9443/v1/audio/speech",
            "HTTPS provider endpoint",
        ),
        (
            "tcp://fallback-voice-server.home.arpa:10300",
            "tcp://fallback-voice-server.home.arpa",
            "Wyoming endpoint",
        ),
    ],
)
def test_rejects_malformed_provider_endpoints(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    config = write_instance(tmp_path)
    config.write_text(config.read_text().replace(old, new))

    with pytest.raises(SettingsError, match=message):
        load_settings(config)


def test_rejects_unapproved_omnivoice_voice(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(config.read_text().replace("voice: voice-02", "voice: private-path"))

    with pytest.raises(SettingsError, match="invalid instance configuration"):
        load_settings(config)


def test_wyoming_piper_voice_is_configurable_per_instance(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text().replace(
            "voice: de_DE-thorsten-medium", "voice: de_DE-kerstin-low"
        )
    )

    assert settings_voice(load_settings(config), "wyoming-piper") == "de_DE-kerstin-low"


def test_piper_voices_accept_configured_languages(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text()
        .replace("de_DE-thorsten-medium", "en_US-lessac-medium")
        .replace("de_DE-ramona-low", "en_GB-alba-medium")
    )

    settings = load_settings(config)

    assert settings_voice(settings, "wyoming-piper") == "en_US-lessac-medium"
    assert settings_voice(settings, "local-piper") == "en_GB-alba-medium"


def test_https_providers_accept_standard_port_443(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text()
        .replace("primary-voice-server.home.arpa:9444", "primary-voice-server.home.arpa")
        .replace("primary-voice-server.home.arpa:9443", "primary-voice-server.home.arpa")
    )
    settings = load_settings(config)
    assert getattr(settings.stt[0], "url").startswith("https://primary-voice-server.home.arpa/")
    assert getattr(settings.tts[0], "url").startswith("https://primary-voice-server.home.arpa/")


def test_remote_provider_resilience_bounds_override_and_fail_closed(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(config.read_text().replace(
        "    model: large-v3-turbo\n",
        "    model: large-v3-turbo\n    connect_timeout_seconds: 0.75\n"
        "    response_timeout_seconds: 90\n    circuit_cooldown_seconds: 60\n",
    ))
    settings = load_settings(config)
    assert settings.stt[0].connect_timeout_seconds == 0.75
    assert settings.stt[0].response_timeout_seconds == 90
    assert settings.stt[0].circuit_cooldown_seconds == 60

    for index, (field, invalid) in enumerate((
        ("connect_timeout_seconds", 0),
        ("connect_timeout_seconds", 5.1),
        ("response_timeout_seconds", float("inf")),
        ("circuit_cooldown_seconds", 301),
        ("circuit_cooldown_seconds", float("nan")),
        ("connect_timeout_second", 1),
    )):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        candidate = write_instance(case_dir)
        candidate.write_text(candidate.read_text().replace(
            "    model: large-v3-turbo\n", f"    model: large-v3-turbo\n    {field}: {invalid}\n"
        ))
        with pytest.raises(SettingsError, match="invalid instance configuration"):
            load_settings(candidate)


def settings_voice(settings, provider_type: str) -> str:
    return next(provider.voice for provider in settings.tts if provider.type == provider_type)


def test_rejects_unsafe_piper_voice_id(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(
        config.read_text().replace(
            "voice: de_DE-thorsten-medium", "voice: ../../private/model"
        )
    )

    with pytest.raises(SettingsError, match="Piper voice"):
        load_settings(config)


def test_settings_repr_and_dump_do_not_expose_voice_tokens(tmp_path: Path) -> None:
    settings = load_settings(write_instance(tmp_path))
    rendered = repr(settings) + repr(settings.model_dump()) + settings.model_dump_json()
    assert "s" * 48 not in rendered
    assert "o" * 48 not in rendered


def test_rejects_permissive_secret_file(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    (tmp_path / "instance-a.env").chmod(0o644)
    with pytest.raises(SettingsError, match="0600"):
        load_settings(config)


def test_rejects_permissive_state_directory(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    (tmp_path / "instance-a").chmod(0o755)
    with pytest.raises(SettingsError, match="state_dir"):
        load_settings(config)


def test_rejects_relative_worker_paths(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    python = tmp_path / "instance-a-worker" / "hermes-root" / "venv" / "bin" / "talktohermes-python"
    config.write_text(config.read_text().replace(str(python), "python3"))
    with pytest.raises(SettingsError, match="absolute"):
        load_settings(config)


def test_state_directory_is_privately_bound_to_instance_id(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    load_settings(config)
    marker = tmp_path / "instance-a" / ".talktohermes-instance"
    assert marker.read_text(encoding="utf-8") == "instance_id=instance-a\nprofile=default\n"
    assert os.stat(marker).st_mode & 0o777 == 0o600

    marker.write_text("instance_id=instance-b\nprofile=default\n", encoding="utf-8")
    marker.chmod(0o600)
    with pytest.raises(SettingsError, match="instance_id"):
        load_settings(config)


def test_state_directory_cannot_be_reused_for_another_profile(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    load_settings(config)
    config.write_text(config.read_text().replace("profile: default", "profile: private"))
    with pytest.raises(SettingsError, match="instance_id"):
        load_settings(config)


def test_rejects_missing_or_wrong_type_worker_paths(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    script = tmp_path / "instance-a-worker" / "hermes_voice_worker.py"
    script.unlink()
    with pytest.raises(SettingsError, match="voice_worker.script"):
        load_settings(config)

    script.mkdir()
    with pytest.raises(SettingsError, match="voice_worker.script"):
        load_settings(config)


def test_rejects_worker_paths_that_broaden_runtime_privileges(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    script = tmp_path / "instance-a-worker" / "hermes_voice_worker.py"
    script.chmod(0o722)
    with pytest.raises(SettingsError, match="voice_worker.script"):
        load_settings(config)


def test_rejects_worker_symlinks(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    script = tmp_path / "instance-a-worker" / "hermes_voice_worker.py"
    target = tmp_path / "worker-target.py"
    target.write_text("# target\n", encoding="utf-8")
    script.unlink()
    script.symlink_to(target)
    with pytest.raises(SettingsError, match="voice_worker.script"):
        load_settings(config)


def test_rejects_worker_python_outside_venv_and_writable_pyvenv(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    configured = tmp_path / "instance-a-worker" / "hermes-root" / "venv" / "bin" / "talktohermes-python"
    external = tmp_path / "external-python"
    external.write_text("#!/bin/sh\n", encoding="utf-8")
    external.chmod(0o700)
    config.write_text(config.read_text().replace(str(configured), str(external)))
    with pytest.raises(SettingsError, match="voice_worker.python"):
        load_settings(config)

    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    config = write_instance(other)
    pyvenv = other / "instance-a-worker" / "hermes-root" / "venv" / "pyvenv.cfg"
    pyvenv.chmod(0o620)
    with pytest.raises(SettingsError, match="pyvenv"):
        load_settings(config)
