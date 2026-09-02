#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 INSTANCE [REVISION]" >&2
  echo "Run as the unprivileged TalkToHermes instance user from a checkout containing the requested revision." >&2
}

[[ $# -ge 1 && $# -le 2 ]] || { usage; exit 2; }
instance=$1
revision=${2:-HEAD}
[[ $instance =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "Invalid instance" >&2; exit 2; }
[[ $(id -u) -ne 0 ]] || { echo "Do not run as root" >&2; exit 2; }

repo=$(git rev-parse --show-toplevel)
cd "$repo"
commit=$(git rev-parse "${revision}^{commit}")
[[ -f "$HOME/.config/talktohermes/${instance}.yaml" ]] || {
  echo "Missing $HOME/.config/talktohermes/${instance}.yaml" >&2; exit 2;
}
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

mkdir -p "$(dirname "$current")" "$unit_dir"
ln -sfn "$release" "$current"
install -m 0644 "$release/deployment/systemd/talktohermes-user@.service" "$unit"
systemd-analyze --user verify "$unit"
systemctl --user daemon-reload
systemctl --user enable "talktohermes@${instance}.service"
systemctl --user restart "talktohermes@${instance}.service"

config="$HOME/.config/talktohermes/${instance}.yaml"
port=$("$release/backend/.venv/bin/python" - "$config" <<'PY'
import sys
from talktohermes.settings import load_settings
print(load_settings(sys.argv[1]).listen_port)
PY
)
for _ in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null; then
    break
  fi
  sleep 1
done
systemctl --user is-active --quiet "talktohermes@${instance}.service"
curl --fail --silent --show-error --max-time 3 "http://127.0.0.1:${port}/health"
transaction_active=0
trap - EXIT INT TERM HUP
rm -rf "$backup"
echo
echo "Deployed TalkToHermes $commit for $instance on loopback port $port"
