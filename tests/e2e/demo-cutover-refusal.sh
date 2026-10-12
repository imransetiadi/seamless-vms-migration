#!/usr/bin/env bash
# shellcheck disable=SC2034  # variables are read inside the check() eval strings
# demo-cutover-refusal.sh - DEMO-07 (QASuite §6.6): the demo VM whose warm passes meet what the
# cutover will refuse (SDD §7.4), on a throwaway demo server of this checkout.
#
#   tests/e2e/demo-cutover-refusal.sh       # needs seamless/.venv (QASuite §14.3); no Docker, no token
#
# Starts `seamless serve --demo` on 127.0.0.1 with its own data directory and a clean environment, so
# a shared demo keeps dc2-portal-02 free: one VM has one migration across plans (SDD §5.4) and no
# plan can be deleted. A loopback demo runs without authentication, so no token is needed. The
# script plans dc2-portal-02 vmware_warm with the cutover on request and checks that:
#   - its first pass records the refusal, and the plan page's summaries carry it;
#   - the cutover requested then fails before the source stops and keeps the refusal;
#   - the retry's pass still records it, and a requested sync pass records none;
#   - the cutover then completes.
# SEAMLESS (the seamless command), PORT and DEMO_SPEED override the defaults.
# Exit code = number of failed checks.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SEAMLESS=${SEAMLESS:-$ROOT/seamless/.venv/bin/seamless}
PORT=${PORT:-$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}
BASE="http://127.0.0.1:$PORT/api/v1"
tmp=${TMPDIR:-/tmp}
DATA=$(mktemp -d "${tmp%/}/seamless-demo07.XXXXXX")
pass=0; fail=0
ok()   { pass=$((pass+1)); echo "PASS  $1"; }
bad()  { fail=$((fail+1)); echo "FAIL  $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
# q JSON EXPR [ARG...]: EXPR evaluated in Python with the parsed JSON as j and the ARGs as a; nothing
# when the JSON or the expression fails, so the check that reads it fails
q() {
  python3 -c 'import json, sys
try:
    j, a = json.loads(sys.argv[1]), sys.argv[3:]
    print(eval(sys.argv[2]))
except Exception:
    print("")' "$@"
}
# stop REASON: nothing left to check once a step the rest builds on failed
stop() { echo "STOP  $1"; echo "== $pass passed, $fail failed =="; exit "$fail"; }
# post PATH [JSON]: the response, {} when the call failed
post() {
  local body=${2:-'{}'}
  curl -fsS -m 60 -X POST -H "Content-Type: application/json" -d "$body" "$BASE$1" || echo '{}'
}

# another server on the port would be planned on instead - a shared demo would lose dc2-portal-02
if curl -s -m 2 -o /dev/null "$BASE/health"; then
  echo "STOP  something already answers on 127.0.0.1:$PORT: pick a free PORT"
  rm -rf "$DATA"
  exit 1
fi
# none of the caller's settings reach the server (database, tokens, Jev, memory, keys): SQLite in
# $DATA, and no injected cutover failures (PYTHONPATH passes, to run another tree's code)
env -i PATH="$PATH" HOME="$HOME" ${PYTHONPATH:+"PYTHONPATH=$PYTHONPATH"} \
  SEAMLESS_DATA_DIR="$DATA" SEAMLESS_DEMO_FAILURE_RATE=0 SEAMLESS_DEMO_SPEED="${DEMO_SPEED:-60}" \
  "$SEAMLESS" serve --demo --host 127.0.0.1 --port "$PORT" >"$DATA/server.log" 2>&1 &
server=$!
trap 'kill "$server" 2>/dev/null; wait "$server" 2>/dev/null; rm -rf "$DATA"' EXIT

health='{}'
for _ in $(seq 1 120); do
  health=$(curl -fsS -m 2 "$BASE/health" 2>/dev/null || echo '{}')
  [[ "$health" == *\"status\":\"ok\"* ]] && break
  kill -0 "$server" 2>/dev/null || break
  sleep 0.5
done
check "throwaway demo server healthy on 127.0.0.1:$PORT" '[[ "$health" == *\"status\":\"ok\"* && "$health" == *\"demo\":true* ]]'
# healthy, and the answer is this server's: it did not exit (on a port taken meanwhile, for one)
if [[ "$health" != *\"status\":\"ok\"* ]] || ! kill -0 "$server" 2>/dev/null; then
  tail -20 "$DATA/server.log"
  stop "the demo server did not start"
fi

mid=""
# the migration once EXPR holds on it (j) or it settles in a phase it waits in (failed, cancelled,
# rolled back, completed), polling for up to $1 s; its last state otherwise
wait_for() {
  local until=$((SECONDS + $1)) m
  while :; do
    m=$(curl -fsS -m 10 "$BASE/migrations/$mid" 2>/dev/null || echo '{}')
    if [ "$(q "$m" "($2) or j.get('phase') in ('failed', 'cancelled', 'rolled_back', 'completed')")" = True ] ||
      [ "$SECONDS" -ge "$until" ]; then echo "$m"; return; fi
    sleep 0.5
  done
}

inventory=$(curl -fsS -m 30 "$BASE/providers/vcenter-dc2/inventory" || echo '[]')
portal=$(q "$inventory" 'json.dumps(next((v for v in (j if isinstance(j, list) else j.get("vms", [])) if v["name"] == "dc2-portal-02"), {}))')
tag=$(q "$portal" 'j.get("tags", {}).get("demo-fault")')
check "dc2-portal-02 is in the VMware inventory, tagged demo-fault: cutover-refusal" '[ "$tag" = cutover-refusal ]'
vm=$(q "$portal" 'j.get("source_id", "")')
held=$(q "$(curl -fsS -m 10 "$BASE/plans" || echo '[]')" 'sum(a[0] in p["vm_ids"] for p in j)' "$vm")
check "no seeded plan holds it (SDD §10)" '[ -n "$vm" ] && [ "$held" = 0 ]'
[ -n "$vm" ] || stop "no dc2-portal-02 to plan"
# as the VMware executor words it (SDD §7.2); the plan maps the NIC's port group DC2-Prod to dc2-prod
refusal=$(q "$portal" '"the NIC {} of dc2-portal-02 on {!r}: port demo-port-1 holds {}".format(j["nics"][0]["mac"], "dc2-prod", j["nics"][0]["fixed_ips"][0]) if j.get("nics") else ""')

body=$(python3 -c 'import json, sys; print(json.dumps({"name": "DEMO-07 portal", "source_provider_id": "vcenter-dc2", "destination_provider_id": "rhoso18", "vm_ids": [sys.argv[1]], "mappings": {"networks": {"DC2-Prod": "dc2-prod"}}, "downtime_slo_s": 900, "default_strategy": "vmware_warm", "require_approval": False, "auto_cutover": False, "keep_warm_interval_s": 86400}))' "$vm")
pid=$(q "$(post /plans "$body")" 'j.get("id", "")')
validated=$(q "$(post "/plans/$pid/validate")" '" ".join("{}/{}".format(m["phase"], m["strategy"]) for m in j.get("migrations", []))')
check "planned and validated: ready, vmware_warm" '[ "$validated" = ready/vmware_warm ]'
status=$(q "$(post "/plans/$pid/start")" 'j.get("status")')
mid=$(q "$(curl -fsS -m 10 "$BASE/migrations?plan_id=$pid" || echo '[]')" 'j[0]["id"] if j else ""')
check "the plan runs" '[ "$status" = running ] && [ -n "$mid" ]'
[ -n "$mid" ] || stop "no migration to follow"

m=$(wait_for 180 'j.get("phase") == "awaiting_cutover" and len(j.get("sync_passes", [])) >= 1')
recorded=$(q "$m" 'j.get("cutover_refusals") == [a[0]]' "$refusal")
check "its first pass records what the cutover will refuse (SDD §7.2)" '[ "$recorded" = True ]'
summarized=$(q "$(curl -fsS -m 10 "$BASE/migrations?plan_id=$pid&view=summary" || echo '[]')" '[s.get("cutover_refusals") for s in j] == [[a[0]]]' "$refusal")
check "the plan page's summaries carry it (SDD §4, §16)" '[ "$summarized" = True ]'

post "/migrations/$mid/cutover" '{"comment":"DEMO-07: before the third pass"}' >/dev/null
m=$(wait_for 180 'j.get("phase") in ("failed", "verifying", "completed", "rolled_back")')
refused=$(q "$m" '(j.get("phase"), j.get("downtime_started_at"), j.get("error")) == ("failed", None, "cutover: " + a[0])' "$refusal")
check "the cutover fails before the source stops: no downtime, no rollback" '[ "$refused" = True ]'
kept=$(q "$m" 'j.get("cutover_refusals") == [a[0]]' "$refusal")
check "the failed migration keeps what the cutover refused" '[ "$kept" = True ]'

post "/migrations/$mid/retry" >/dev/null
m=$(wait_for 180 'j.get("phase") == "awaiting_cutover" and len(j.get("sync_passes", [])) >= 2')
still=$(q "$m" '(len(j.get("sync_passes", [])), j.get("cutover_refusals")) == (2, [a[0]])' "$refusal")
check "the retry's pass (2) still records it" '[ "$still" = True ]'
post "/migrations/$mid/sync" >/dev/null
m=$(wait_for 180 'j.get("phase") == "awaiting_cutover" and len(j.get("sync_passes", [])) >= 3')
cleared=$(q "$m" '(len(j.get("sync_passes", [])), j.get("cutover_refusals")) == (3, [])')
check "a requested sync pass (3) records none" '[ "$cleared" = True ]'

post "/migrations/$mid/cutover" '{"comment":"DEMO-07: after the fix"}' >/dev/null
m=$(wait_for 240 'j.get("phase") in ("completed", "failed", "rolled_back")')
done_=$(q "$m" '(j.get("phase"), j["sync_passes"][-1]["kind"] if j.get("sync_passes") else None, j.get("downtime_started_at") is not None, j.get("cutover_refusals")) == ("completed", "final", True, [])')
check "the cutover then completes after a final pass" '[ "$done_" = True ]'

echo "== $pass passed, $fail failed =="
exit "$fail"
