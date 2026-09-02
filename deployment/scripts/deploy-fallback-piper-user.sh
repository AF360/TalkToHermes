#!/bin/bash
set -euo pipefail

usage() {
  echo "Usage: $0 INSTANCE VOICE PORT BIND_IP [SERVER_ROOT]" >&2
  echo "Example: $0 instance-a de_DE-thorsten-medium 10201 192.168.100.30" >&2
}
[[ $# -ge 4 && $# -le 5 ]] || { usage; exit 2; }
[[ $(id -u) -ne 0 ]] || { echo "Run as the logged-in voice-service user, not root" >&2; exit 2; }
instance=$1
voice=$2
port=$3
bind_ip=$4
server_root=${5:-$HOME/server}
[[ $instance =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "Invalid instance" >&2; exit 2; }
[[ $voice =~ ^[a-z]{2,3}_[A-Z]{2,3}-[a-z0-9_]+-(x_)?(low|medium|high)$ ]] || { echo "Invalid Piper voice" >&2; exit 2; }
[[ $port =~ ^[0-9]+$ ]] && (( port >= 1024 && port <= 65535 )) || { echo "Invalid port" >&2; exit 2; }
/usr/bin/python3 - "$bind_ip" <<'PY'
import ipaddress, sys
address = ipaddress.ip_address(sys.argv[1])
if not address.is_private or address.is_loopback:
    raise SystemExit("BIND_IP must be a private non-loopback address")
PY

script_dir=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$script_dir/../.." && pwd)
supervisor_source="$repo/deployment/launchd/piper_warm_supervisor.py"
python="$server_root/venv/bin/python"
piper="$server_root/venv/bin/wyoming-piper"
data_dir="$server_root/wyoming-data/piper"
install_dir="$server_root/talktohermes-piper"
label="systems.talktohermes.piper.${instance}"
plist="$HOME/Library/LaunchAgents/${label}.plist"
domain="gui/$(id -u)"
uri="tcp://${bind_ip}:${port}"
log="$HOME/Library/Logs/talktohermes-piper-${instance}.log"
error_log="$HOME/Library/Logs/talktohermes-piper-${instance}.error.log"

[[ -x $python && -x $piper && -f $supervisor_source ]]
[[ -f "$data_dir/${voice}.onnx" && -f "$data_dir/${voice}.onnx.json" ]]
mkdir -p "$HOME/.cache" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
backup=$(mktemp -d "$HOME/.cache/talktohermes-piper-deploy.XXXXXX")
chmod 0700 "$backup"
had_plist=0
had_supervisor=0
[[ -e $plist ]] && { cp -p "$plist" "$backup/plist"; had_plist=1; }
[[ -e $install_dir/piper_warm_supervisor.py ]] && {
  cp -p "$install_dir/piper_warm_supervisor.py" "$backup/supervisor"; had_supervisor=1;
}
was_loaded=0
launchctl print "$domain/$label" >/dev/null 2>&1 && was_loaded=1

rollback() {
  [[ ${transaction_active:-0} == 1 ]] || return 0
  transaction_active=0
  set +e
  launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
  if [[ $had_plist == 1 ]]; then cp -p "$backup/plist" "$plist"; else rm -f "$plist"; fi
  if [[ $had_supervisor == 1 ]]; then
    cp -p "$backup/supervisor" "$install_dir/piper_warm_supervisor.py"
  else
    rm -f "$install_dir/piper_warm_supervisor.py"
  fi
  if [[ $was_loaded == 1 && -e $plist ]]; then
    launchctl bootstrap "$domain" "$plist" >/dev/null 2>&1 || true
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

launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
if lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
  echo "Port $port is owned by another process" >&2
  false
fi
mkdir -p "$install_dir"
chmod 0755 "$install_dir"
install -m 0644 "$supervisor_source" "$install_dir/piper_warm_supervisor.py"
"$python" -m py_compile "$install_dir/piper_warm_supervisor.py"

/usr/bin/python3 - "$plist" "$label" "$python" "$install_dir/piper_warm_supervisor.py" \
  "$piper" "$voice" "$uri" "$data_dir" "$server_root" "$log" "$error_log" <<'PY'
import plistlib, sys
(path, label, python, supervisor, piper, voice, uri, data_dir, cwd, log, error_log) = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [python, supervisor, "--piper", piper, "--voice", voice,
                         "--uri", uri, "--data-dir", data_dir],
    "WorkingDirectory": cwd,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 5,
    "ProcessType": "Interactive",
    "StandardOutPath": log,
    "StandardErrorPath": error_log,
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
}
with open(path, "wb") as output:
    plistlib.dump(payload, output)
PY
chmod 0644 "$plist"
plutil -lint "$plist"
: > "$log"
: > "$error_log"
launchctl bootstrap "$domain" "$plist"
launchctl kickstart -k "$domain/$label"

for _ in {1..60}; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN \
     && grep -q "warm voice=${voice} uri=${uri}" "$log" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ ${ready:-0} == 1 ]]
launchctl print "$domain/$label" >/dev/null
transaction_active=0
trap - EXIT INT TERM HUP
rm -rf "$backup"
echo "Piper $instance is supervised, listening on $uri, and warm with $voice"
