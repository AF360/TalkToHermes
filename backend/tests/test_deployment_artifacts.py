from __future__ import annotations

import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    assert "talktohermes-stt.service" in primary_voice_server
    assert "talktohermes-omnivoice.service" in primary_voice_server
    assert "127.0.0.1:5050/ready" in primary_voice_server
    assert "192.168.100.20" in primary_voice_server
    assert "voice_host_ip}:9090/ready" in primary_voice_server

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
    assert "tool_display_names" not in config
    assert "exposed_tools" not in config
    assert config["tool_summaries"] == {
        "web_search": "Websuche ausgeführt",
        "browser_exec": "Browseraktion ausgeführt",
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
    assert "tool_display_names" not in config
    assert "exposed_tools" not in config
    assert config["tool_summaries"] == {
        "web_search": "Websuche ausgeführt",
        "browser_exec": "Browseraktion ausgeführt",
    }
    assert config["voice_worker"]["script"] == (
        "/home/INSTANCE/.local/opt/talktohermes/current/backend/worker/"
        "hermes_voice_worker.py"
    )
    runbook = read("README.md")
    assert "Preferred bridge user-service installation" in runbook
    assert "Alternative root-owned system-service install" in runbook
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
    assert "hermes-agent.home.arpa:8443" in merged
    assert "reverse_proxy 127.0.0.1:18081" in merged
    assert merged.count("tls internal") == 1
    assert "9119" not in merged
    assert "cloudflare" not in merged.lower()


def test_caddy_example_needs_no_site_specific_custom_image() -> None:
    assert not (DEPLOYMENT / "caddy" / "Dockerfile").exists()


def test_caddy_compose_uses_host_network_runtime_mounts_and_no_literal_secrets() -> None:
    compose_text = read("caddy/compose.example.yaml")
    compose = yaml.safe_load(compose_text)
    service = compose["services"]["caddy"]
    assert service["network_mode"] == "host"
    assert "container_name" not in service
    assert service["image"] == "caddy:2.11.4@sha256:df7f1c2fb114453b951de51a98efc010db1655a92c2e86be6706714e2417a78d"
    assert "environment" not in service
    assert "build" not in service
    assert any("./Caddyfile:/etc/caddy/Caddyfile:ro" in item for item in service["volumes"])
    assert "/opt/" not in compose_text
    assert all("cloudflare_api_token" not in item for item in service["volumes"])
    assert "CF_API_TOKEN=" not in compose_text
    assert "CLOUDFLARE_API_TOKEN" not in compose_text
    assert "APP_TOKEN=" not in compose_text


def test_primary_voice_server_deployer_uses_released_names_and_authenticated_readiness() -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    unit_stt = read("systemd/talktohermes-stt-user.service")
    unit_omni = read("systemd/talktohermes-omnivoice-user.service")

    for historical in (
        "TalkToHermes-OmniVoice-Test",
        "TalkToHermes-STT-Candidate",
        "talktohermes-stt-candidate.service",
        "/health",
    ):
        assert historical not in script
        assert historical not in unit_stt
        assert historical not in unit_omni

    assert "talktohermes-stt.service" in script
    assert "talktohermes-omnivoice.service" in script
    assert "http://127.0.0.1:5050/ready" in script
    assert 'http://${voice_host_ip}:9090/ready' in script
    assert "--config" in script


def test_primary_voice_server_deployer_accepts_only_rfc1918_voice_addresses() -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert network in script
    assert ".is_private" not in script


def test_primary_voice_server_auth_config_accepts_token_file_line_endings(
    tmp_path: Path,
) -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    match = re.search(r"(?ms)^make_auth_config\(\) \{\n.*?^\}\n", script)
    assert match is not None

    token = "A" * 32
    for case, contents in (("newline", token + "\n"), ("no-newline", token)):
        token_file = tmp_path / f"{case}.token"
        auth_file = tmp_path / f"{case}.curl.conf"
        token_file.write_text(contents, encoding="utf-8")

        subprocess.run(
            [
                "bash",
                "-c",
                "set -euo pipefail\n"
                + match.group(0)
                + '\nmake_auth_config "$1" "$2"\n',
                "auth-config-test",
                str(token_file),
                str(auth_file),
            ],
            check=True,
        )

        assert auth_file.read_text(encoding="utf-8") == (
            f'header = "Authorization: Bearer {token}"\n'
        )
        assert auth_file.stat().st_mode & 0o777 == 0o600


def test_primary_voice_server_auth_config_preserves_curl_metacharacters(
    tmp_path: Path,
) -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    match = re.search(r"(?ms)^make_auth_config\(\) \{\n.*?^\}\n", script)
    assert match is not None
    received: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tokens = [
            "A" * 31 + '"',
            "B" * 31 + "\\",
            "C" * 32 + r"\nX-Evil:yes",
            "D" * 32 + r"\rX-Evil:yes",
        ]
        for index, token in enumerate(tokens):
            token_file = tmp_path / f"meta-{index}.token"
            auth_file = tmp_path / f"meta-{index}.curl.conf"
            token_file.write_text(token, encoding="ascii")
            subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -euo pipefail\n"
                    + match.group(0)
                    + '\nmake_auth_config "$1" "$2"\n',
                    "auth-config-test",
                    str(token_file),
                    str(auth_file),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "curl",
                    "--config",
                    str(auth_file),
                    "--fail",
                    "--silent",
                    f"http://127.0.0.1:{server.server_port}/ready",
                ],
                check=True,
            )
            assert received[-1] == f"Bearer {token}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_primary_voice_server_readiness_ignores_curlrc_and_proxies(
    tmp_path: Path,
) -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    match = re.search(r"(?ms)^wait_ready\(\) \{\n.*?^\}\n", script)
    assert match is not None
    target_requests: list[dict[str, str]] = []
    proxy_requests: list[dict[str, str]] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target_requests.append(dict(self.headers.items()))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            proxy_requests.append(dict(self.headers.items()))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    target = HTTPServer(("127.0.0.1", 0), TargetHandler)
    proxy = HTTPServer(("127.0.0.1", 0), ProxyHandler)
    threads = [
        threading.Thread(target=target.serve_forever, daemon=True),
        threading.Thread(target=proxy.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".curlrc").write_text('header = "X-From-Curlrc: yes"\n', encoding="ascii")
        auth_file = tmp_path / "auth.curl.conf"
        auth_file.write_text('header = "Authorization: Bearer test-token"\n', encoding="ascii")
        env = os.environ.copy()
        env.update(
            HOME=str(home),
            http_proxy=f"http://127.0.0.1:{proxy.server_port}",
            HTTP_PROXY=f"http://127.0.0.1:{proxy.server_port}",
            ALL_PROXY=f"http://127.0.0.1:{proxy.server_port}",
            NO_PROXY="",
            no_proxy="",
        )
        subprocess.run(
            [
                "bash",
                "-c",
                "set -euo pipefail\n"
                + match.group(0)
                + '\nwait_ready "$1" "$2" 1\n',
                "readiness-test",
                f"http://127.0.0.1:{target.server_port}/ready",
                str(auth_file),
            ],
            check=True,
            env=env,
        )
    finally:
        target.shutdown()
        proxy.shutdown()
        for thread in threads:
            thread.join(timeout=5)

    assert len(target_requests) == 1
    assert proxy_requests == []
    assert target_requests[0]["Authorization"] == "Bearer test-token"
    assert "X-From-Curlrc" not in target_requests[0]


def test_primary_voice_server_readiness_requires_exact_http_200(tmp_path: Path) -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    match = re.search(r"(?ms)^wait_ready\(\) \{\n.*?^\}\n", script)
    assert match is not None

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        auth_file = tmp_path / "auth.curl.conf"
        auth_file.write_text('header = "Authorization: Bearer test-token"\n', encoding="ascii")
        result = subprocess.run(
            [
                "bash",
                "-c",
                "set -euo pipefail\n"
                + match.group(0)
                + '\nwait_ready "$1" "$2" 1\n',
                "readiness-test",
                f"http://127.0.0.1:{server.server_port}/ready",
                str(auth_file),
            ],
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode != 0


def test_primary_voice_server_commits_only_after_secret_cleanup() -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    success = script[script.rindex('systemctl --user is-active --quiet "$omni_service"') :]
    assert success.index('rm -f "$omni_auth" "$stt_auth"') < success.index(
        "transaction_active=0"
    )
    assert success.index("transaction_active=0") < success.index('rm -rf "$backup"')
    assert 'echo "Warning: could not remove secret-free recovery backup at $backup"' in success


def test_primary_voice_server_deployer_removes_secret_backup_on_every_exit() -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    rollback = script[script.index("rollback() {") : script.index("\n}\ntransaction_active=1")]
    assert 'rm -rf "$backup"' in rollback
    assert script.count('rm -rf "$backup"') == 2


def test_primary_voice_server_incomplete_rollback_preserves_recovery_backup(
    tmp_path: Path,
) -> None:
    script = read("scripts/deploy-primary-voice-server-user-services.sh")
    rollback = script[
        script.index("rollback() {") : script.index("\n}\ntransaction_active=1") + 2
    ]
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "omnivoice.curl.conf").write_text("sensitive", encoding="utf-8")
    (backup / "stt.curl.conf").write_text("sensitive", encoding="utf-8")
    omni_target = tmp_path / "omnivoice-target"
    omni_target.mkdir()
    (omni_target / "bad").write_text("candidate", encoding="utf-8")
    stt_target = tmp_path / "stt-target"
    omni_stage = tmp_path / "omnivoice-stage"
    stt_stage = tmp_path / "stt-stage"
    omni_stage.mkdir()
    stt_stage.mkdir()

    harness = (
        "set -uo pipefail\n"
        "transaction_active=1\n"
        'backup=$1; omni_target=$2; stt_target=$3; omni_stage=$4; stt_stage=$5\n'
        'omni_unit=$6; stt_unit=$7\n'
        "omni_had_target=1; stt_had_target=0; omni_had_unit=0; stt_had_unit=0\n"
        "omni_was_active=0; stt_was_active=0; omni_was_enabled=0; stt_was_enabled=0\n"
        'omni_service=omni.service; stt_service=stt.service\n'
        "systemctl() { return 0; }\n"
        + rollback
        + "\nrollback\n"
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            harness,
            "rollback-test",
            str(backup),
            str(omni_target),
            str(stt_target),
            str(omni_stage),
            str(stt_stage),
            str(tmp_path / "omni.service"),
            str(tmp_path / "stt.service"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert backup.is_dir()
    assert not (backup / "omnivoice.curl.conf").exists()
    assert not (backup / "stt.curl.conf").exists()
    assert not omni_stage.exists()
    assert not stt_stage.exists()


def test_release_version_is_consistent() -> None:
    assert 'version = "1.0.4"' in (ROOT / "backend" / "pyproject.toml").read_text()
    assert 'version="1.0.4"' in (ROOT / "backend" / "src" / "talktohermes" / "app.py").read_text()
    assert "version: 1.0.4" in (ROOT / "api" / "openapi.yaml").read_text()
    lock = (ROOT / "backend" / "uv.lock").read_text()
    assert 'name = "talktohermes"\nversion = "1.0.4"' in lock


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
        "other Hermes clients",
    ):
        assert topic in runbook


def test_runbook_auth_configs_use_the_entered_token() -> None:
    bridge = read("README.md")
    stt = (ROOT / "services" / "stt" / "README.md").read_text(encoding="utf-8")
    for runbook in (bridge, stt):
        assert "%s" in runbook
        assert "***" not in runbook


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
    assert project.count("MARKETING_VERSION = 1.0.4;") == 6
    assert project.count("CURRENT_PROJECT_VERSION = 4;") == 6

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
