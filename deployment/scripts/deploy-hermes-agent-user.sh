#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 INSTANCE [REVISION] [CANDIDATE_CONFIG]" >&2
  echo "Run as the unprivileged TalkToHermes instance user from a checkout containing the requested revision." >&2
}

[[ $# -ge 1 && $# -le 3 ]] || { usage; exit 2; }
instance=$1
revision=${2:-HEAD}
candidate_config=${3:-}
[[ $instance =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "Invalid instance" >&2; exit 2; }
[[ $(id -u) -ne 0 ]] || { echo "Do not run as root" >&2; exit 2; }

repo=$(git rev-parse --show-toplevel)
cd "$repo"
commit=$(git rev-parse "${revision}^{commit}")
config="$HOME/.config/talktohermes/${instance}.yaml"
[[ -f $config && ! -L $config ]] || {
  echo "Missing or unsafe $config" >&2; exit 2;
}
if [[ -n $candidate_config ]]; then
  [[ $candidate_config = /* && -f $candidate_config && ! -L $candidate_config ]] || {
    echo "CANDIDATE_CONFIG must be an absolute regular file" >&2; exit 2;
  }
  [[ $(stat -c '%u' "$candidate_config") == "$(id -u)" ]] || {
    echo "CANDIDATE_CONFIG must be owned by the current user" >&2; exit 2;
  }
fi
command -v uv >/dev/null
command -v curl >/dev/null

release_root="$HOME/.local/opt/talktohermes/releases"
release="$release_root/$commit"
current="$HOME/.local/opt/talktohermes/current"
unit_dir="$HOME/.config/systemd/user"
unit="$unit_dir/talktohermes@.service"
mkdir -p "$release_root" "$HOME/.cache" "$unit_dir"
backup=$(mktemp -d "$HOME/.cache/talktohermes-deploy.XXXXXX")
chmod 0700 "$backup"
cp -p "$config" "$backup/config"
old_current=$(readlink "$current" 2>/dev/null || true)
had_unit=0
[[ -e $unit ]] && { cp -p "$unit" "$backup/unit"; had_unit=1; }
was_active=0
systemctl --user is-active --quiet "talktohermes@${instance}.service" && was_active=1
was_enabled=0
systemctl --user is-enabled --quiet "talktohermes@${instance}.service" && was_enabled=1

rollback() {
  [[ ${transaction_active:-0} == 1 ]] || return 0
  transaction_active=0
  set +e
  systemctl --user stop "talktohermes@${instance}.service" >/dev/null 2>&1 || true
  cp -p "$backup/config" "$config"
  if [[ -n $old_current ]]; then ln -sfn "$old_current" "$current"; else rm -f "$current"; fi
  if [[ $had_unit == 1 ]]; then cp -p "$backup/unit" "$unit"; else rm -f "$unit"; fi
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  if [[ $was_enabled == 1 ]]; then
    systemctl --user enable "talktohermes@${instance}.service" >/dev/null 2>&1 || true
  else
    systemctl --user disable "talktohermes@${instance}.service" >/dev/null 2>&1 || true
  fi
  if [[ $was_active == 1 ]]; then
    systemctl --user start "talktohermes@${instance}.service" >/dev/null 2>&1 || true
  fi
}
transaction_active=1
on_exit() {
  status=$?
  trap - EXIT INT TERM HUP
  if [[ $transaction_active == 1 ]]; then rollback; fi
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [[ ! -d $release ]]; then
  stage=$(mktemp -d "$release_root/.stage-${commit}.XXXXXX")
  chmod 0755 "$stage"
  git -c tar.umask=0022 archive --format=tar "$commit" | tar -x -C "$stage"
  mv "$stage" "$release"
  if ! (
    set -e
    uv sync --frozen --no-dev --no-editable --project "$release/backend"
    "$release/backend/.venv/bin/python" -m compileall -q \
      "$release/backend/src" "$release/backend/worker"
  ); then
    rm -rf "$release"
    exit 1
  fi
fi

config_to_validate=${candidate_config:-$config}
candidate_port=$("$release/backend/.venv/bin/python" - "$config_to_validate" <<'PY'
import sys
from talktohermes.settings import load_settings

print(load_settings(sys.argv[1]).listen_port)
PY
)
if [[ -n $candidate_config ]]; then
  install -m 0600 "$candidate_config" "$config"
fi

mkdir -p "$(dirname "$current")" "$unit_dir"
ln -sfn "$release" "$current"
install -m 0644 "$release/deployment/systemd/talktohermes-user@.service" "$unit"
systemd-analyze --user verify "$unit"
systemctl --user daemon-reload
systemctl --user enable "talktohermes@${instance}.service"
systemctl --user restart "talktohermes@${instance}.service"

port=$candidate_port
for _ in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null; then
    break
  fi
  sleep 1
done
systemctl --user is-active --quiet "talktohermes@${instance}.service"
curl --fail --silent --show-error --max-time 3 "http://127.0.0.1:${port}/health"
"$release/backend/.venv/bin/python" - "$config" "$port" <<'PY'
import sys

import httpx

from talktohermes.settings import load_settings

settings = load_settings(sys.argv[1])
url = f"http://127.0.0.1:{sys.argv[2]}/v1/status"
headers = {"Authorization": f"Bearer {settings.app_token.get_secret_value()}"}
with httpx.Client(timeout=3.0, trust_env=False) as client:
    if client.get(url).status_code != 401:
        raise RuntimeError("unauthenticated status request did not return 401")
    if client.get(
        url, headers={"Authorization": "Bearer deliberately-wrong"}
    ).status_code != 401:
        raise RuntimeError("wrong-token status request did not return 401")
    response = client.get(url, headers=headers)
    response.raise_for_status()
    expected = {
        "status": "ready",
        "instance_id": settings.instance_id,
        "assistant_name": settings.assistant_name,
    }
    if response.json() != expected:
        raise RuntimeError("authenticated status response violated contract")
PY
transaction_active=0
trap - EXIT INT TERM HUP
rm -rf "$backup"
echo
echo "Deployed TalkToHermes $commit for $instance on loopback port $port"
