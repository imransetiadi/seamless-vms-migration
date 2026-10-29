#!/usr/bin/env bash
# ssh-trust.sh - the SSH trust drill against a lab (QASuite S-10, S-11, LAB-SSH01, LAB-SSH04; SDD §26.9,
# plan task ST9): the lab's half of what the test process proves through a real sshd.
#
#   S-10  a rogue sshd at the source conversion host's address, in a cutover before the stop: the run must
#         refuse the host key at once, with the source VM running (conversion_host.key_refused, the
#         migration's error "host key refused: ...", no downtime started).
#   S-11  from the destination host, the run's link key may run neither `id`, a shell, scp nor a forward to
#         another port on the source host.
#   The images' versions (os-release, ssh -V, cloud-init --version) are printed for QASuite §13.
#
# Before S-10 swap the source conversion host's key, as the lab case says (on the host: move
# /etc/ssh/ssh_host_* away and run `ssh-keygen -A`, then restart sshd); the drill checks with ssh-keyscan
# that the host no longer offers its pinned key, and says NOT RUN when it still does.
#
# Environment:
#   SEAMLESS_URL   the control plane (default http://127.0.0.1:8080)
#   TOKEN          an approver's bearer token (handed to curl through a header file, never an argument)
#   MIGRATION      a cold or warm migration awaiting its cutover, whose source VM runs
#   SRC_HOST       the source conversion host's address, as this machine and the destination host reach it
#   DST_HOST       the destination host's address;  SSH_USER (cloud-user);  SSH_IDENTITY the operator's key
#   LINK_KEY       the run's link key's path on the destination host (SDD §26.6)
#   IMAGE_HOSTS    "name=address ..." of the hosts whose image versions to record (optional)
#   BOUND          seconds to wait for the run's verdict (default 600)
# Exit code = number of failed checks. Prints PASS, FAIL, NOT RUN and VERSIONS lines.
set -u
. "$(dirname "$0")/token-headers.sh"   # E2E_TMP, token_header; its EXIT trap removes E2E_TMP (S-30)
: "${TOKEN:?set TOKEN to the token of an approver}"
: "${MIGRATION:?set MIGRATION to the id of the migration}"
: "${SRC_HOST:?set SRC_HOST}" "${DST_HOST:?set DST_HOST}" "${LINK_KEY:?set LINK_KEY}"
: "${SSH_IDENTITY:?set SSH_IDENTITY to the key the operator uses for both hosts}"
BASE="${SEAMLESS_URL:-http://127.0.0.1:8080}/api/v1"
SSH_USER=${SSH_USER:-cloud-user}
BOUND=${BOUND:-600}
H=$(token_header approver "$TOKEN") || exit 2

pass=0; fail=0
ok()     { pass=$((pass+1)); echo "PASS  $1"; }
bad()    { fail=$((fail+1)); echo "FAIL  $1"; }
notrun() { echo "NOT RUN  $1"; }
# q JSON EXPR: EXPR evaluated in Python with the parsed JSON as j; nothing when either fails
q() {
  python3 -c 'import json, sys
try:
    j = json.loads(sys.argv[1])
    print(eval(sys.argv[2]))
except Exception:
    print("")' "$@"
}
get()  { curl -fsS -m 30 -H "$H" "$BASE$1" 2>/dev/null || echo '{}'; }
post() {
  local body=${2:-'{}'}
  curl -fsS -m 60 -X POST -H "$H" -H "Content-Type: application/json" -d "$body" "$BASE$1" 2>/dev/null || echo '{}'
}

# ---------------------------------------------------------------- S-10: the rogue host key (LAB-SSH01)
migration=$(get "/migrations/$MIGRATION")
plan_id=$(q "$migration" 'j["plan_id"]')
src_provider=$(q "$(get "/plans/$plan_id")" 'j["source_provider_id"]')
pinned=$(q "$(get "/providers/$src_provider")" '"\n".join(j["conversion_host_pin"]["keys"])')
offered=$(ssh-keyscan -T 5 "$SRC_HOST" 2>/dev/null | awk 'NF >= 3 {print $2 " " $3}')
echo "S-10  pinned: $(printf '%s' "$pinned" | awk '{print $1}' | paste -sd, -); offered: $(printf '%s' "$offered" | awk '{print $1}' | paste -sd, -)"
if [ -z "$pinned" ] || [ -z "$offered" ]; then
  bad "S-10 the pin of $src_provider or the keys $SRC_HOST offers could not be read"
elif printf '%s\n' "$offered" | grep -qFxf <(printf '%s\n' "$pinned"); then
  notrun "S-10 $SRC_HOST still offers its pinned key: swap the key of the host first (see the header), then run again"
else
  before=$(q "$(get "/events?migration_id=$MIGRATION&tail=true&limit=1")" 'j[-1]["seq"] if j else 0')
  requested=$(post "/migrations/$MIGRATION/cutover" '{"comment": "SSH trust drill S-10"}')
  if [ "$(q "$requested" '"id" in j')" != True ]; then
    bad "S-10 the cutover request was refused: $(q "$requested" 'j.get("error", j)')"
  else
    verdict=""
    until=$((SECONDS + BOUND))
    while :; do
      m=$(get "/migrations/$MIGRATION")
      phase=$(q "$m" 'j.get("phase")')
      refusals=$(q "$(get "/events?migration_id=$MIGRATION&since=$before&limit=1000")" \
        'sum(1 for e in j if e["kind"] == "conversion_host.key_refused" and e["seq"] > int(sys.argv[3]))' "$before")
      if [ "$(q "$m" 'j.get("downtime_started_at") is not None')" = True ] ||
        [ "$phase" = completed ] || [ "$phase" = finalized ] || [ "$phase" = cutover ]; then
        verdict="FAIL  S-10 the rogue host key was accepted: the migration went on to $phase"
      elif [ "$phase" = failed ]; then
        error=$(q "$m" 'j.get("error") or ""')
        if [ "${refusals:-0}" -gt 0 ] && [[ "$error" == "host key refused:"* ]]; then
          verdict="PASS  S-10 refused before the stop, the source VM running: $error"
        else
          verdict="FAIL  S-10 the migration failed for another reason than the host key: $error"
        fi
      elif [ "$SECONDS" -ge "$until" ]; then
        verdict="FAIL  S-10 no verdict within $BOUND s: phase $phase, $refusals refusal event(s)"
      fi
      [ -n "$verdict" ] && break
      sleep 2
    done
    case $verdict in PASS*) pass=$((pass+1));; *) fail=$((fail+1));; esac
    echo "$verdict"
  fi
fi

# ---------------------------------------------------------------- S-11: the link key's refusals (LAB-SSH04)
# remote CMD: CMD on the destination host, as the operator reaches it
remote() {
  ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$SSH_USER@$DST_HOST" "$1"
}
link="ssh -i '$LINK_KEY' -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
out=$(remote "$link '$SSH_USER@$SRC_HOST' id" 2>&1); status=$?
if [ "$status" -ne 0 ] && [[ "$out" != *uid=* ]]; then ok "S-11 the link key may not run id on $SRC_HOST"
else bad "S-11 the link key ran id on $SRC_HOST (exit $status)"; fi
if remote "$link -T '$SSH_USER@$SRC_HOST' </dev/null" >/dev/null 2>&1; then bad "S-11 the link key opened a shell on $SRC_HOST"
else ok "S-11 the link key may not open a shell on $SRC_HOST"; fi
if remote "scp -i '$LINK_KEY' -o BatchMode=yes -o StrictHostKeyChecking=accept-new /etc/hostname '$SSH_USER@$SRC_HOST:seamless-ssh-trust-drill'" >/dev/null 2>&1; then
  bad "S-11 the link key copied a file with scp to $SRC_HOST"
else ok "S-11 the link key may not scp to $SRC_HOST"; fi
# a forward to another port: the session that stays open 5 s was accepted
out=$(remote "$link -N -o ExitOnForwardFailure=yes -L 18022:127.0.0.1:22 '$SSH_USER@$SRC_HOST' & p=\$!; sleep 5; if kill -0 \$p 2>/dev/null; then kill \$p; echo FORWARD_OPEN; fi" 2>&1)
if [[ "$out" == *FORWARD_OPEN* ]]; then bad "S-11 the link key opened a forward to another port on $SRC_HOST"
else ok "S-11 the link key may not forward to another port on $SRC_HOST"; fi

# ---------------------------------------------------------------- the images' versions (QASuite §13)
# shellcheck disable=SC2086  # IMAGE_HOSTS is a space-separated list
for entry in ${IMAGE_HOSTS:-}; do
  name=${entry%%=*}; address=${entry#*=}
  seen=$(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$SSH_USER@$address" \
    'sh -c ". /etc/os-release && echo \"PRETTY_NAME=$PRETTY_NAME\"; ssh -V 2>&1; cloud-init --version 2>&1 | head -1"' 2>/dev/null)
  if [ -z "$seen" ]; then echo "VERSIONS  $name: not read ($address)"; continue; fi
  pretty=$(printf '%s\n' "$seen" | sed -n 's/^PRETTY_NAME=//p' | head -1 | tr -d '"')
  sshv=$(printf '%s\n' "$seen" | grep -o 'OpenSSH_[^ ,]*' | head -1)
  cloudinit=$(printf '%s\n' "$seen" | grep -o 'cloud-init [0-9][^ ]*' | head -1)
  echo "VERSIONS  $name: ${pretty:-?} | ${sshv:-?} | ${cloudinit:-cloud-init ?}"
done

echo "== $pass passed, $fail failed =="
exit "$fail"
