from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = ROOT / "deployment"


def read(relative: str) -> str:
    return (DEPLOYMENT / relative).read_text(encoding="utf-8")


def test_systemd_template_is_per_user_single_process_and_path_locked() -> None:
    unit = read("systemd/talktohermes@.service")
    required = [
        "User=%i",
        "Group=%i",
        "WorkingDirectory=/opt/talktohermes/current/backend",
        "ExecStart=/opt/talktohermes/current/backend/.venv/bin/talktohermes-production /etc/talktohermes/%i.yaml",
        "StateDirectory=talktohermes/%i",
        "StateDirectoryMode=0700",
        "ReadWritePaths=/var/lib/talktohermes/%i",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectKernelLogs=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "Restart=on-failure",
        "StartLimitBurst=3",
        "TimeoutStopSec=20s",
    ]
    for directive in required:
        assert directive in unit
    assert "--workers" not in unit
    assert "EnvironmentFile=" not in unit
    assert "sudo" not in unit
    assert "/bin/sh" not in unit
    assert "0.0.0.0" not in unit


def test_systemd_exposes_only_explicit_read_only_hermes_paths() -> None:
    unit = read("systemd/talktohermes@.service")
    assert "BindReadOnlyPaths=/home/%i/.hermes" in unit
    assert "BindReadOnlyPaths=/home/%i/.local/share/uv/python" in unit
    assert "/home/%i/.local/share/hermes/venv" not in unit
    assert "ReadWritePaths=/home" not in unit


def test_user_service_matches_unprivileged_runtime_constraints() -> None:
    unit = read("systemd/talktohermes-user@.service")
    assert "WantedBy=default.target" in unit
    assert "%h/.config/talktohermes/%i.yaml" in unit
    assert "NoNewPrivileges=true" in unit
    assert "User=" not in unit and "Group=" not in unit
    for unavailable in (
        "ProtectHome=", "ProtectSystem=", "PrivateTmp=",
        "CapabilityBoundingSet=", "IPAddressDeny=",
    ):
        assert unavailable not in unit


def test_three_transactional_deployment_scripts_are_present_and_parse() -> None:
    scripts = (
        "scripts/deploy-hermes-agent-user.sh",
        "scripts/deploy-primary-voice-server-user-services.sh",
        "scripts/deploy-fallback-piper-user.sh",
    )
    for relative in scripts:
        path = DEPLOYMENT / relative
        subprocess.run(["bash", "-n", str(path)], check=True)
        content = path.read_text(encoding="utf-8")
        assert "set -euo pipefail" in content
        assert "rollback" in content
        assert "trap on_exit EXIT" in content
        assert "trap 'exit 130' INT" in content
        assert "trap 'exit 143' TERM" in content
        assert "sudo" not in content

    bridge = read(scripts[0])
    assert "git -c tar.umask=0022 archive" in bridge
    assert "uv sync --frozen --no-dev --no-editable" in bridge
    assert "systemctl --user restart" in bridge
    assert "127.0.0.1" in bridge and "/health" in bridge

    primary_voice_server = read(scripts[1])
    assert "talktohermes-stt-candidate.service" in primary_voice_server
    assert "talktohermes-omnivoice.service" in primary_voice_server
    assert "127.0.0.1:5050/health" in primary_voice_server
    assert "192.168.100.20" in primary_voice_server
    assert "voice_host_ip}:9090/health" in primary_voice_server

    piper = read(scripts[2])
    assert "launchctl bootstrap" in piper
    assert "piper_warm_supervisor.py" in piper
    assert "BIND_IP must be a private non-loopback address" in piper
    assert "warm voice=" in piper


def test_primary_voice_server_user_units_are_hardened_and_secret_free() -> None:
    for relative in (
        "systemd/talktohermes-omnivoice-user.service",
        "systemd/talktohermes-stt-user.service",
    ):
        unit = read(relative)
        assert "WantedBy=default.target" in unit
        assert "NoNewPrivileges=true" in unit
        assert "ProtectSystem=strict" in unit
        assert "Restart=on-failure" in unit
        assert "TOKEN=" not in unit


def test_instance_example_has_loopback_hermes_and_only_placeholders() -> None:
    config_text = read("config/instance.yaml.example")
    config = yaml.safe_load(config_text)
    assert config["hermes"]["base_url"] == "http://127.0.0.1:8642"
    assert config["listen_host"] == "127.0.0.1"
    assert config["assistant_name"] == "ASSISTANT_NAME"
    assert config["exposed_tools"] == {
        "OpenCodeTool": "OpenCodeTool",
        "functions.browser_exec": "BrowserTool",
    }
    assert config["tool_summaries"] == {
        "OpenCodeTool": "Entwicklungswerkzeug verwendet",
        "BrowserTool": "Webinhalt abgerufen",
    }
    assert config["state_dir"] == "/var/lib/talktohermes/INSTANCE"
    assert config["secret_file"] == "/etc/talktohermes/INSTANCE.secrets"
    assert "api_key" not in config["hermes"]
    assert "app_token" not in config
    assert [provider["type"] for provider in config["stt"]] == [
        "openai", "wyoming", "local"
    ]
    assert config["stt"][0]["url"] == (
        "https://primary-voice-server.home.arpa:9444/v1/audio/transcriptions"
    )
    assert [provider["type"] for provider in config["tts"]] == [
        "omnivoice", "wyoming-piper", "local-piper"
    ]
    assert config["tts"][1]["url"] == (
        "tcp://fallback-voice-server.home.arpa:10201"
    )
    assert config["voice_worker"] == {
        "python": "/home/INSTANCE/.hermes/hermes-agent/venv/bin/talktohermes-python",
        "script": "/opt/talktohermes/current/backend/worker/hermes_voice_worker.py",
        "hermes_root": "/home/INSTANCE/.hermes/hermes-agent",
    }
    assert not re.search(r"(?i)(token|key)\s*:\s*[^#\s]", config_text)

    secrets = read("config/instance.secrets.example")
    assert secrets.splitlines() == [
        "APP_TOKEN=<GENERATE_A_UNIQUE_RANDOM_VALUE_AT_LEAST_32_CHARS>",
        "HERMES_API_KEY=<COPY_THIS_INSTANCE_HERMES_API_KEY>",
        "STT_PRIMARY_TOKEN=<COPY_A_UNIQUE_PRIMARY_STT_BEARER_TOKEN>",
        "TTS_PRIMARY_TOKEN=<COPY_A_UNIQUE_PRIMARY_TTS_BEARER_TOKEN>",
    ]


def test_user_instance_example_matches_user_service_paths() -> None:
    config = yaml.safe_load(read("config/instance.user.yaml.example"))
    assert config["state_dir"] == "/home/INSTANCE/.local/state/talktohermes/INSTANCE"
    assert config["secret_file"] == "/home/INSTANCE/.config/talktohermes/INSTANCE.secrets"
    assert config["assistant_name"] == "ASSISTANT_NAME"
    assert config["exposed_tools"] == {
        "OpenCodeTool": "OpenCodeTool",
        "functions.browser_exec": "BrowserTool",
    }
    assert config["tool_summaries"] == {
        "OpenCodeTool": "Entwicklungswerkzeug verwendet",
        "BrowserTool": "Webinhalt abgerufen",
    }
    assert config["voice_worker"]["script"] == (
        "/home/INSTANCE/.local/opt/talktohermes/current/backend/worker/"
        "hermes_voice_worker.py"
    )
    runbook = read("README.md")
    assert "Preferred bridge user-service installation" in runbook
    assert "Legacy root/system-service install" in runbook
    assert "instance.user.yaml.example" in runbook


def test_systemd_network_allowlist_covers_only_required_voice_hosts() -> None:
    unit = read("systemd/talktohermes@.service")
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert not re.search(r"IPAddressAllow=(?:10|172|192\.168)\.", unit)

    drop_in = read("systemd/talktohermes@.service.d/egress.conf.example")
    assert "IPAddressAllow=DNS_RESOLVER_IP/32" in drop_in
    assert "IPAddressAllow=PRIMARY_VOICE_SERVER_IP/32" in drop_in
    assert "IPAddressAllow=FALLBACK_VOICE_SERVER_IP/32" in drop_in

    runbook = read("README.md")
    assert "fail closed" in runbook.lower()
    assert "systemd-analyze verify" in runbook
    assert "systemctl show" in runbook
    assert "DNS_RESOLVER_IP" in runbook


def test_caddy_fragment_is_https_custom_port_to_loopback_only() -> None:
    fragment = read("caddy/talktohermes.caddy")
    assert "https://hermes-agent.home.arpa:8443" in fragment
    assert "reverse_proxy 127.0.0.1:18081" in fragment
    assert "http://" not in fragment
    assert "tls internal" in fragment
    assert "cloudflare" not in fragment.lower()
    assert "cloudflared" not in fragment.lower()

    merged = read("caddy/Caddyfile.merged.example")
    assert "hermes-agent.home.arpa" in merged
    assert "reverse_proxy 127.0.0.2:9119" in merged
    assert "header_up Host 127.0.0.2" in merged
    assert "hermes-agent.home.arpa:8443" in merged
    assert "reverse_proxy 127.0.0.1:18081" in merged
    assert merged.count("tls internal") == 2
    assert "cloudflare" not in merged.lower()


def test_custom_caddy_image_builds_pinned_cloudflare_plugin_without_secrets() -> None:
    dockerfile = read("caddy/Dockerfile")
    assert "ARG " not in dockerfile
    assert "sha256:4bdeabce8e79d36b23d1cba7d20598cec2c1117ace960d8ca06071f945e8fc9b" in dockerfile
    assert "sha256:df7f1c2fb114453b951de51a98efc010db1655a92c2e86be6706714e2417a78d" in dockerfile
    assert "FROM caddy:2.11.4-builder@sha256:" in dockerfile
    assert "FROM caddy:2.11.4@sha256:" in dockerfile
    assert "xcaddy build v2.11.4" in dockerfile
    assert "--with github.com/caddy-dns/cloudflare@a8737d095ad5a48ca031cea6ab704057dbc2d250" in dockerfile
    assert "COPY --from=builder /usr/bin/caddy /usr/bin/caddy" in dockerfile
    assert "TOKEN" not in dockerfile
    assert "SECRET" not in dockerfile


def test_caddy_compose_uses_host_network_runtime_mounts_and_no_literal_secrets() -> None:
    compose_text = read("caddy/compose.example.yaml")
    compose = yaml.safe_load(compose_text)
    service = compose["services"]["caddy"]
    assert service["network_mode"] == "host"
    assert service["container_name"] == "caddy-hermesagent"
    assert service["image"] == "local/caddy-hermesagent:2.11.4-cloudflare-a8737d095ad5"
    assert "environment" not in service
    assert "args" not in service["build"]
    assert any("/opt/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" in item for item in service["volumes"])
    assert all("cloudflare_api_token" not in item for item in service["volumes"])
    assert "CF_API_TOKEN=" not in compose_text
    assert "CLOUDFLARE_API_TOKEN" not in compose_text
    assert "APP_TOKEN=" not in compose_text


def test_deployment_runbook_documents_container_and_service_validation() -> None:
    runbook = read("README.md")
    for command in (
        "systemd-analyze verify",
        "caddy list-modules",
        "caddy validate",
        "docker compose config",
        "ss -ltnp",
        "curl",
        "journalctl",
    ):
        assert command in runbook
    for topic in (
        "Install",
        "Upgrade",
        "Rollback",
        "Minimal privileged commands",
        "Port collision",
        "Cross-token isolation",
        "Telegram",
    ):
        assert topic in runbook


def test_runbook_auth_configs_use_the_entered_token() -> None:
    bridge = read("README.md")
    stt = (ROOT / "services" / "stt" / "README.md").read_text(encoding="utf-8")
    for runbook in (bridge, stt):
        assert 'Authorization: Bearer %s' in runbook
        assert 'Authorization: Bearer ***' not in runbook


def test_user_deploy_builds_venv_only_at_final_release_path() -> None:
    script = (ROOT / "deployment" / "scripts" / "deploy-hermes-agent-user.sh").read_text(
        encoding="utf-8"
    )
    move = 'mv "$stage" "$release"'
    sync = 'uv sync --frozen --no-dev --no-editable --project "$release/backend"'
    assert move in script and sync in script
    assert script.index(move) < script.index(sync)
    assert '--project "$stage/backend"' not in script


def test_user_deploy_migrates_config_transactionally_and_checks_status() -> None:
    script = (ROOT / "deployment" / "scripts" / "deploy-hermes-agent-user.sh").read_text(
        encoding="utf-8"
    )
    backup = 'cp -p "$config" "$backup/config"'
    validate = 'candidate_port=$("$release/backend/.venv/bin/python" - "$config_to_validate"'
    install_config = 'install -m 0600 "$candidate_config" "$config"'
    switch_release = 'ln -sfn "$release" "$current"'
    restore_config = 'cp -p "$backup/config" "$config"'
    authenticated_status = 'response = client.get(url, headers=headers)'
    explicit_auth_failure = 'raise RuntimeError("unauthenticated status request did not return 401")'
    explicit_contract_failure = 'raise RuntimeError("authenticated status response violated contract")'
    commit_transaction = "transaction_active=0"

    for marker in (
        backup,
        validate,
        install_config,
        switch_release,
        restore_config,
        authenticated_status,
        explicit_auth_failure,
        explicit_contract_failure,
    ):
        assert marker in script
    assert "assert client.get(" not in script
    assert "assert response.json()" not in script
    assert script.index(backup) < script.index(validate)
    assert script.index(validate) < script.index(install_config)
    assert script.index(install_config) < script.index(switch_release)
    assert script.index(authenticated_status) < script.rindex(commit_transaction)


def test_runbook_installs_the_tracked_egress_template_and_provider_ca() -> None:
    runbook = read("README.md")
    assert "systemd/talktohermes@.service.d/egress.conf.example" in runbook
    assert (
        "install -o root -g root -m 0644 "
        "systemd/talktohermes@.service.d/egress.conf.example "
        "/etc/systemd/system/talktohermes@INSTANCE.service.d/egress.conf"
        in runbook
    )
    assert "update-ca-certificates" in runbook
    assert "primary voice server" in runbook.lower()
    assert "httpx" in runbook.lower()


def test_private_home_arpa_pki_and_ios_root_ca_trust_are_documented() -> None:
    runbook = read("README.md")
    assert "tls internal" in runbook
    assert "Certificate Trust Settings" in runbook
    assert "/data/caddy/pki/authorities/local/root.crt" in runbook
    assert "private key" in runbook.lower()
    assert "Cloudflare DNS token" not in runbook

    for relative in (
        "primary-voice-server/Caddyfile.omnivoice",
        "../services/stt/deployment/Caddyfile.stt",
    ):
        assert "tls internal" in read(relative)


def test_product_identity_is_preserved_and_signing_is_portable() -> None:
    project = (ROOT / "ios/TalkToHermes/TalkToHermes.xcodeproj/project.pbxproj").read_text()
    assert "DEVELOPMENT_TEAM" not in project
    assert project.count("PRODUCT_BUNDLE_IDENTIFIER = net.acelab.TalkToHermes;") == 2
    assert project.count("PRODUCT_BUNDLE_IDENTIFIER = net.acelab.TalkToHermesTests;") == 2
    assert project.count("PRODUCT_BUNDLE_IDENTIFIER = net.acelab.TalkToHermesUITests;") == 2
    assert project.count("MARKETING_VERSION = 1.0.2;") == 6
    assert project.count("CURRENT_PROJECT_VERSION = 2;") == 6

    keychain = (ROOT / "ios/TalkToHermes/TalkToHermes/KeychainStore.swift").read_text()
    content = (ROOT / "ios/TalkToHermes/TalkToHermes/ContentView.swift").read_text()
    view_model = (ROOT / "ios/TalkToHermes/TalkToHermes/VoiceViewModel.swift").read_text()
    response_style = (ROOT / "backend/src/talktohermes/response_style.py").read_text()
    assert '"systems.acelab.TalkToHermes"' in keychain
    assert "model.assistantName" in content
    assert "status.instanceID" in view_model
    assert 'String(localized: "%@ denkt nach …")' in view_model
    assert "assistantName" in view_model
    assert "gesprochenen Unterhaltung. Antworte direkt" in response_style
