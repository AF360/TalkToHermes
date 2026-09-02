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
if not address.is_private or address.is_loopback:
    raise SystemExit("VOICE_HOST_IP must be a private non-loopback address")
PY
omni_target="$HOME/TalkToHermes-OmniVoice-Test/source/src/talktohermes_omnivoice"
stt_target="$HOME/TalkToHermes-STT-Candidate/src/talktohermes_stt"
unit_dir="$HOME/.config/systemd/user"
omni_unit="$unit_dir/talktohermes-omnivoice.service"
stt_unit="$unit_dir/talktohermes-stt-candidate.service"

[[ -d $omni_target && -d $stt_target ]]
[[ -x /usr/bin/python3.11 && -x /opt/stt/.venv/bin/python ]]
[[ -f $HOME/TalkToHermes-OmniVoice-Test/state/config.yaml ]]
[[ -f $HOME/.config/talktohermes-stt/token ]]
command -v curl >/dev/null
mkdir -p "$HOME/.cache" "$unit_dir"
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
cp -a "$omni_target" "$backup/omnivoice"
cp -a "$stt_target" "$backup/stt"
omni_had_unit=0
stt_had_unit=0
[[ -e $omni_unit ]] && { cp -p "$omni_unit" "$backup/omni.service"; omni_had_unit=1; }
[[ -e $stt_unit ]] && { cp -p "$stt_unit" "$backup/stt.service"; stt_had_unit=1; }
omni_was_active=0
stt_was_active=0
omni_was_enabled=0
stt_was_enabled=0
systemctl --user is-active --quiet talktohermes-omnivoice.service && omni_was_active=1
systemctl --user is-active --quiet talktohermes-stt-candidate.service && stt_was_active=1
systemctl --user is-enabled --quiet talktohermes-omnivoice.service && omni_was_enabled=1
systemctl --user is-enabled --quiet talktohermes-stt-candidate.service && stt_was_enabled=1

rollback() {
  [[ ${transaction_active:-0} == 1 ]] || return 0
  transaction_active=0
  set +e
  rm -rf "$omni_target" "$stt_target"
  cp -a "$backup/omnivoice" "$omni_target"
  cp -a "$backup/stt" "$stt_target"
  if [[ $omni_had_unit == 1 ]]; then cp -p "$backup/omni.service" "$omni_unit"; else rm -f "$omni_unit"; fi
  if [[ $stt_had_unit == 1 ]]; then cp -p "$backup/stt.service" "$stt_unit"; else rm -f "$stt_unit"; fi
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  if [[ $stt_was_enabled == 1 ]]; then systemctl --user enable talktohermes-stt-candidate.service >/dev/null 2>&1 || true; else systemctl --user disable talktohermes-stt-candidate.service >/dev/null 2>&1 || true; fi
  if [[ $omni_was_enabled == 1 ]]; then systemctl --user enable talktohermes-omnivoice.service >/dev/null 2>&1 || true; else systemctl --user disable talktohermes-omnivoice.service >/dev/null 2>&1 || true; fi
  if [[ $stt_was_active == 1 ]]; then systemctl --user restart talktohermes-stt-candidate.service >/dev/null 2>&1 || true; else systemctl --user stop talktohermes-stt-candidate.service >/dev/null 2>&1 || true; fi
  if [[ $omni_was_active == 1 ]]; then systemctl --user restart talktohermes-omnivoice.service >/dev/null 2>&1 || true; else systemctl --user stop talktohermes-omnivoice.service >/dev/null 2>&1 || true; fi
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
/usr/bin/python3.11 -m compileall -q "$omni_stage"
/opt/stt/.venv/bin/python -m compileall -q "$stt_stage"
rm -rf "$omni_target" "$stt_target"
mv "$omni_stage" "$omni_target"
mv "$stt_stage" "$stt_target"
install -m 0644 "$omni_unit_source" "$omni_unit"
install -m 0644 "$stt_unit_source" "$stt_unit"
systemd-analyze --user verify "$omni_unit" "$stt_unit"
systemctl --user daemon-reload
systemctl --user enable talktohermes-stt-candidate.service talktohermes-omnivoice.service
systemctl --user restart talktohermes-stt-candidate.service
for _ in {1..120}; do
  curl --fail --silent --max-time 2 http://127.0.0.1:5050/health >/dev/null && break
  sleep 1
done
curl --fail --silent --show-error --max-time 3 http://127.0.0.1:5050/health >/dev/null
systemctl --user restart talktohermes-omnivoice.service
for _ in {1..600}; do
  curl --fail --silent --max-time 2 "http://${voice_host_ip}:9090/health" >/dev/null && break
  sleep 1
done
curl --fail --silent --show-error --max-time 3 "http://${voice_host_ip}:9090/health" >/dev/null
systemctl --user is-active --quiet talktohermes-stt-candidate.service
systemctl --user is-active --quiet talktohermes-omnivoice.service
transaction_active=0
trap - EXIT INT TERM HUP
rm -rf "$backup"
echo "primary voice server STT and OmniVoice $commit deployed and healthy"
