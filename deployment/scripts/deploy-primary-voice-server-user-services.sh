#!/usr/bin/env bash
set -euo pipefail

[[ $# -le 3 ]] || { echo "Usage: $0 [REPOSITORY_ROOT] [VOICE_HOST_IP] [REVISION]" >&2; exit 2; }
[[ $(id -u) -ne 0 ]] || { echo "Run as the primary voice server service user, not root" >&2; exit 2; }
repo=${1:-$(git rev-parse --show-toplevel)}
voice_host_ip=${2:-192.168.100.20}
revision=${3:-HEAD}
repo=$(cd "$repo" && pwd)
commit=$(git -C "$repo" rev-parse "${revision}^{commit}")
/usr/bin/python3 - "$voice_host_ip" <<'PY'
import ipaddress, sys
address = ipaddress.ip_address(sys.argv[1])
rfc1918 = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
if address.version != 4 or not any(address in network for network in rfc1918):
    raise SystemExit("VOICE_HOST_IP must be an RFC 1918 IPv4 address")
PY

omni_root="$HOME/.local/opt/talktohermes-omnivoice"
stt_root="$HOME/.local/opt/talktohermes-stt"
omni_target="$omni_root/current/src/talktohermes_omnivoice"
stt_target="$stt_root/current/src/talktohermes_stt"
omni_python="$omni_root/venv/bin/python"
stt_python=/opt/stt/.venv/bin/python
omni_config="$HOME/.config/talktohermes-omnivoice/config.yaml"
omni_token_file="$HOME/.config/talktohermes-omnivoice/token"
stt_token_file="$HOME/.config/talktohermes-stt/token"
unit_dir="$HOME/.config/systemd/user"
omni_service=talktohermes-omnivoice.service
stt_service=talktohermes-stt.service
omni_unit="$unit_dir/$omni_service"
stt_unit="$unit_dir/$stt_service"

[[ -x $omni_python && -x $stt_python ]]
[[ -f $omni_config && -f $omni_token_file && -f $stt_token_file ]]
[[ -d $stt_root/vendor && -d $HOME/.local/share/talktohermes-omnivoice/model ]]
command -v curl >/dev/null
mkdir -p "$HOME/.cache/talktohermes-omnivoice" "$unit_dir" \
  "$(dirname "$omni_target")" "$(dirname "$stt_target")"
backup=$(mktemp -d "$HOME/.cache/talktohermes-primary-voice-server-deploy.XXXXXX")
chmod 0700 "$backup"
artifact_root="$backup/artifact"
mkdir -m 0700 "$artifact_root"
git -C "$repo" -c tar.umask=0022 archive --format=tar "$commit" -- \
  services/omnivoice/src/talktohermes_omnivoice \
  services/stt/src/talktohermes_stt \
  deployment/systemd/talktohermes-omnivoice-user.service \
  deployment/systemd/talktohermes-stt-user.service \
  | tar -x -C "$artifact_root"
omni_src="$artifact_root/services/omnivoice/src/talktohermes_omnivoice"
stt_src="$artifact_root/services/stt/src/talktohermes_stt"
omni_unit_source="$artifact_root/deployment/systemd/talktohermes-omnivoice-user.service"
stt_unit_source="$artifact_root/deployment/systemd/talktohermes-stt-user.service"
[[ -d $omni_src && -d $stt_src && -f $omni_unit_source && -f $stt_unit_source ]]

omni_had_target=0
stt_had_target=0
omni_had_unit=0
stt_had_unit=0
[[ -d $omni_target ]] && { cp -a "$omni_target" "$backup/omnivoice"; omni_had_target=1; }
[[ -d $stt_target ]] && { cp -a "$stt_target" "$backup/stt"; stt_had_target=1; }
[[ -e $omni_unit ]] && { cp -p "$omni_unit" "$backup/omni.service"; omni_had_unit=1; }
[[ -e $stt_unit ]] && { cp -p "$stt_unit" "$backup/stt.service"; stt_had_unit=1; }
omni_was_active=0
stt_was_active=0
omni_was_enabled=0
stt_was_enabled=0
systemctl --user is-active --quiet "$omni_service" && omni_was_active=1
systemctl --user is-active --quiet "$stt_service" && stt_was_active=1
systemctl --user is-enabled --quiet "$omni_service" && omni_was_enabled=1
systemctl --user is-enabled --quiet "$stt_service" && stt_was_enabled=1

omni_stage=''
stt_stage=''
rollback() {
  [[ ${transaction_active:-0} == 1 ]] || return 0
  transaction_active=0
  set +e
  local rollback_failed=0
  restore_or_record_failure() {
    "$@" || rollback_failed=1
    return 0
  }

  restore_or_record_failure rm -rf "$omni_target" "$stt_target"
  [[ $omni_had_target == 1 ]] && restore_or_record_failure cp -a "$backup/omnivoice" "$omni_target"
  [[ $stt_had_target == 1 ]] && restore_or_record_failure cp -a "$backup/stt" "$stt_target"
  if [[ $omni_had_unit == 1 ]]; then
    restore_or_record_failure cp -p "$backup/omni.service" "$omni_unit"
  else
    restore_or_record_failure rm -f "$omni_unit"
  fi
  if [[ $stt_had_unit == 1 ]]; then
    restore_or_record_failure cp -p "$backup/stt.service" "$stt_unit"
  else
    restore_or_record_failure rm -f "$stt_unit"
  fi
  restore_or_record_failure systemctl --user daemon-reload
  if [[ $stt_was_enabled == 1 ]]; then
    restore_or_record_failure systemctl --user enable "$stt_service"
  else
    restore_or_record_failure systemctl --user disable "$stt_service"
  fi
  if [[ $omni_was_enabled == 1 ]]; then
    restore_or_record_failure systemctl --user enable "$omni_service"
  else
    restore_or_record_failure systemctl --user disable "$omni_service"
  fi
  if [[ $stt_was_active == 1 ]]; then
    restore_or_record_failure systemctl --user restart "$stt_service"
  else
    restore_or_record_failure systemctl --user stop "$stt_service"
  fi
  if [[ $omni_was_active == 1 ]]; then
    restore_or_record_failure systemctl --user restart "$omni_service"
  else
    restore_or_record_failure systemctl --user stop "$omni_service"
  fi
  [[ -z $omni_stage ]] || restore_or_record_failure rm -rf "$omni_stage"
  [[ -z $stt_stage ]] || restore_or_record_failure rm -rf "$stt_stage"
  restore_or_record_failure rm -f \
    "$backup/omnivoice.curl.conf" "$backup/stt.curl.conf"

  if [[ $rollback_failed == 0 ]]; then
    restore_or_record_failure rm -rf "$backup"
  fi
  if [[ $rollback_failed != 0 ]]; then
    echo "Rollback incomplete; recovery backup retained at $backup" >&2
  fi
  return "$rollback_failed"
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

omni_stage=$(mktemp -d "$(dirname "$omni_target")/.omnivoice.XXXXXX")
stt_stage=$(mktemp -d "$(dirname "$stt_target")/.stt.XXXXXX")
cp -a "$omni_src"/. "$omni_stage"/
cp -a "$stt_src"/. "$stt_stage"/
"$omni_python" -m compileall -q "$omni_stage"
"$stt_python" -m compileall -q "$stt_stage"
rm -rf "$omni_target" "$stt_target"
mv "$omni_stage" "$omni_target"
mv "$stt_stage" "$stt_target"
install -m 0644 "$omni_unit_source" "$omni_unit"
install -m 0644 "$stt_unit_source" "$stt_unit"
systemd-analyze --user verify "$omni_unit" "$stt_unit"
systemctl --user daemon-reload
systemctl --user enable "$stt_service" "$omni_service"

make_auth_config() {
  local token_file=$1 output=$2 token='' escaped_token
  IFS= read -r token < "$token_file" || [[ -n $token ]]
  [[ -n $token ]]
  escaped_token=${token//\\/\\\\}
  escaped_token=${escaped_token//\"/\\\"}
  printf 'header = "Authorization: Bearer %s"\n' "$escaped_token" > "$output"
  chmod 0600 "$output"
  unset token escaped_token
}
wait_ready() {
  local url=$1 auth_file=$2 attempts=$3 status=''
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if status=$(curl -q --noproxy '*' --config "$auth_file" --silent \
      --output /dev/null --write-out '%{http_code}' --max-time 2 "$url") \
      && [[ $status == 200 ]]; then
      return 0
    fi
    sleep 1
  done
  status=$(curl -q --noproxy '*' --config "$auth_file" --silent --show-error \
    --output /dev/null --write-out '%{http_code}' --max-time 3 "$url")
  [[ $status == 200 ]] || {
    echo "Authenticated readiness did not return 200 for $url" >&2
    return 1
  }
}
require_unauthenticated_401() {
  local url=$1 status
  status=$(curl -q --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' --max-time 3 "$url" || true)
  [[ $status == 401 ]] || { echo "Unauthenticated readiness did not return 401 for $url" >&2; return 1; }
}

omni_auth="$backup/omnivoice.curl.conf"
stt_auth="$backup/stt.curl.conf"
make_auth_config "$omni_token_file" "$omni_auth"
make_auth_config "$stt_token_file" "$stt_auth"
stt_ready_url=http://127.0.0.1:5050/ready
omni_ready_url="http://${voice_host_ip}:9090/ready"

systemctl --user restart "$stt_service"
wait_ready "$stt_ready_url" "$stt_auth" 120
require_unauthenticated_401 "$stt_ready_url"
systemctl --user restart "$omni_service"
wait_ready "$omni_ready_url" "$omni_auth" 600
require_unauthenticated_401 "$omni_ready_url"
systemctl --user is-active --quiet "$stt_service"
systemctl --user is-active --quiet "$omni_service"
rm -f "$omni_auth" "$stt_auth"
transaction_active=0
trap - EXIT INT TERM HUP
if ! rm -rf "$backup"; then
  echo "Warning: could not remove secret-free recovery backup at $backup" >&2
fi
echo "primary voice server STT and OmniVoice $commit deployed and healthy"
