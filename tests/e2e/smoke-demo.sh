#!/usr/bin/env bash
# shellcheck disable=SC2034  # variables are read inside the check() eval strings
# smoke-demo.sh - E2 smoke test of the running Seamless Migrate demo stack (QASuite §14.6/14.8).
#
#   make seamless-demo
#   read -rs TOKEN && export TOKEN                    # the admin token printed once by scripts/compose-init.sh
#   read -rs APPROVER_TOKEN && export APPROVER_TOKEN  # an approver of another user: a requester never approves their own
#                                     # cutover (maker-checker, SDD §5.4) — seamless token create --role approver
#   tests/e2e/smoke-demo.sh           # BASE defaults to http://127.0.0.1:8080; SMOKE_WAIT_S (600) bounds the
#                                     # wait for the seeded demo flow to settle
#
# Checks health, RBAC, the dashboard, metrics, the seeded demo flow (approvals, cutovers, one
# retry per rolled-back migration), SSE replay, stats, Jev through the sidecar and agentmemory (each
# skipped, saying why, when it is not configured),
# then validates a plan whose SLO nothing meets so Jev is asked for a strategy: the smoke plan of an
# earlier run, or a new one on a VM no plan holds (SDD §5.4), so a re-run adds no plan.
# Never prints the tokens, nor puts them on a command line. Exit code = number of failed checks.
set -u
BASE=${BASE:-http://127.0.0.1:8080}
: "${TOKEN:?read -rs TOKEN && export TOKEN: the admin token from compose-init.sh}"
: "${APPROVER_TOKEN:?read -rs APPROVER_TOKEN && export APPROVER_TOKEN: an approver token of another user (maker-checker, SDD 5.4)}"
# shellcheck source=tests/e2e/token-headers.sh
. "$(dirname "$0")/token-headers.sh"   # the tokens reach curl from header files, never argv
H=$(token_header admin "$TOKEN") || exit 2
HA=$(token_header approver "$APPROVER_TOKEN") || exit 2
pass=0; fail=0; skipped=0
ok()   { pass=$((pass+1)); echo "PASS  $1"; }
bad()  { fail=$((fail+1)); echo "FAIL  $1"; }
skip() { skipped=$((skipped+1)); echo "SKIP  $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
# a check that needs the smoke plan: skipped, not failed, when the demo has none to give (see below)
needs_plan(){ if [ -n "$no_plan" ]; then skip "$1"; else check "$1" "$2"; fi; }
no_plan=""

# optional integrations (SDD §14.2, §14.3), by the advisor status: a check of one that is not configured is skipped,
# not failed - Jev is off without TYPESAFE_API_KEY at compose-init, memory without SEAMLESS_MEMORY_URL - so a fresh
# Linux host or a CI runner smokes green; a configured one that does not answer still fails its check
jev_on(){ [[ "$advisor" != *'"jev":{"mode":"off"'* ]]; }
memory_on(){ [[ "$advisor" != *'"memory":{"enabled":false'* ]]; }
needs_jev(){ if jev_on; then check "$1" "$2"; else skip "$1 (Jev is off: no TYPESAFE_API_KEY at compose-init)"; fi; }
needs_memory(){ if memory_on; then check "$1" "$2"; else skip "$1 (memory is disabled: no SEAMLESS_MEMORY_URL)"; fi; }

health=$(curl -fsS -m 10 "$BASE/api/v1/health" || echo '{}')
echo "health: $health"
check "health ok + db ok + demo"    '[[ "$health" == *\"status\":\"ok\"* && "$health" == *\"db\":\"ok\"* && "$health" == *\"demo\":true* ]]'
check "unauthenticated /me is 401"  '[ "$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/me")" = 401 ]'
check "readiness is 200 with orchestrator healthy" '[ "$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/ready")" = 200 ] && curl -fsS -m 10 "$BASE/api/v1/ready" | grep -q "\"healthy\":true"'
me=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/me" || echo '{}')
check "/me is admin"                '[[ "$me" == *\"role\":\"admin\"* ]]'
approver_me=$(curl -fsS -m 10 -H "$HA" "$BASE/api/v1/me" || echo '{}')
check "APPROVER_TOKEN is an approver of another user" 'python3 -c "import json,sys; a,b=json.loads(sys.argv[1]),json.loads(sys.argv[2]); sys.exit(0 if a.get(\"role\") in (\"approver\",\"admin\") and a.get(\"name\") != b.get(\"name\") else 1)" "$approver_me" "$me"'
index=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "$BASE/")
check "dashboard served at /"       '[ "$index" = 200 ] && curl -fsS -m 10 "$BASE/" | grep -qi "<div id=\"root\""'
check "security headers on / and the API" 'curl -sSI -m 10 "$BASE/" | grep -qi "^content-security-policy:" && curl -sSI -m 10 "$BASE/api/v1/health" | grep -qi "^x-content-type-options: nosniff"'
check "SPA fallback for /plans"     '[ "$(curl -s -m 10 -o /dev/null -w "%{http_code}" "$BASE/plans")" = 200 ]'
check "metrics endpoint"            'curl -fsS -m 10 -H "$H" "$BASE/api/v1/metrics" | grep -q "^seamless_"'

providers=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/providers" || echo '[]')
check "demo providers seeded"       '[[ "$providers" == *\"source\"* && "$providers" == *\"destination\"* ]]'
plans=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/plans" || echo '[]')
check "demo plans seeded"           '[[ "$plans" == *\"plan-* ]]'

# demo flow: seed_demo already validated and started the plans; drive what is left
# (approve + cutover for the plan that requires approval) and wait for every migration
pid=$(python3 -c 'import json,sys; p=json.load(sys.stdin); print(next((x["id"] for x in p if "Finance" in x["name"] or "finance" in x["name"]), p[0]["id"]))' <<<"$plans")
echo "finance plan: $pid"
python3 -c 'import json,sys; print("plans:", [(x["name"], x["status"]) for x in json.load(sys.stdin)])' <<<"$plans"
# a fresh demo on a small or busy host needs more than 300 s to settle; a settled one ends the wait at once
deadline=$((SECONDS+${SMOKE_WAIT_S:-600})); final=""; migs='[]'
while [ $SECONDS -lt $deadline ]; do
  migs=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/migrations" || echo '[]')
  summary=$(python3 -c 'import json,sys,collections; ms=json.load(sys.stdin); c=collections.Counter(m["phase"] for m in ms); print(dict(c))' <<<"$migs")
  # retry each rolled-back migration once (the demo injects transient cutover failures)
  for mid in $(python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin) if m["phase"]=="rolled_back" and m["attempts"] < 2))' <<<"$migs"); do
    curl -fsS -m 10 -X POST -H "$H" "$BASE/api/v1/migrations/$mid/retry" >/dev/null 2>&1 && echo "retried $mid"
  done
  # request (admin) + approve (another approver): warm strategies wait in awaiting_cutover, single-shot ones
  # in ready; maker-checker (SDD 5.4): the requester's own approval is refused, so another user approves
  for mid in $(python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin) if m["phase"] in ("awaiting_cutover","ready") and not m.get("cutover_requested")))' <<<"$migs"); do
    curl -fsS -m 10 -X POST -H "$H" -H "Content-Type: application/json" -d '{"comment":"Smoke run: window agreed"}' "$BASE/api/v1/migrations/$mid/cutover" >/dev/null 2>&1 || continue
    if [ -z "${self_code:-}" ]; then
      self_code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" -X POST -H "$H" -H "Content-Type: application/json" -d '{"comment":"my own request"}' "$BASE/api/v1/migrations/$mid/approve")
    fi
    curl -fsS -m 10 -X POST -H "$HA" -H "Content-Type: application/json" -d '{"comment":"Smoke run: findings checked by a second approver"}' "$BASE/api/v1/migrations/$mid/approve" >/dev/null 2>&1 && echo "requested+approved $mid"
  done
  running=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/plans" | python3 -c 'import json,sys; print(sum(1 for p in json.load(sys.stdin) if p["status"]=="running"))')
  done_all=$(python3 -c 'import json,sys; ms=json.load(sys.stdin); print("yes" if ms and int(sys.argv[1])==0 and all(m["phase"] in ("completed","finalized","cancelled","failed","rolled_back","blocked","ready") for m in ms) else "no")' "$running" <<<"$migs")
  if [ "$done_all" = yes ]; then final="$summary"; break; fi
  sleep 5
done
echo "final phases: ${final:-$summary (timeout)}"
check "migrations reached completed"  '[[ -n "$final" && "$final" == *completed* ]]'
check "maker-checker: the requester's own approval is refused (409)" '[ "${self_code:-none}" = 409 ] || [ "${self_code:-none}" = none ]'
check "no failed migration"           '[[ -n "$final" && "$final" != *failed* ]]'
python3 -c 'import json,sys; ms=json.load(sys.stdin); print("downtime_s:", sorted(round(m["actual_downtime_s"],1) for m in ms if m.get("actual_downtime_s") is not None)); print("blocked:", [(m["vm"]["name"], [f["code"] for f in m["findings"] if f["severity"]=="blocker"]) for m in ms if m["phase"]=="blocked"])' <<<"$migs"
# plan status after the run
plans=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/plans" || echo '[]')
python3 -c 'import json,sys; print("plans:", [(x["name"], x["status"]) for x in json.load(sys.stdin)])' <<<"$plans"
# validation is refused while a plan runs (409), allowed again afterwards; a plan that still
# lists a VM whose migration was cancelled - one the seed's pre-flight blocked, none since the demo
# maps its GPU VMs (plan task G9) - is refused with 400 naming it (SDD §5.4)
revalidate=$(curl -s -m 60 -w "\n%{http_code}" -X POST -H "$H" "$BASE/api/v1/plans/$pid/validate")
code=${revalidate##*$'\n'}
check "re-validate answers 200/409, or 400 naming a cancelled VM" \
  '[ "$code" = 200 ] || [ "$code" = 409 ] || { [ "$code" = 400 ] && [[ "$revalidate" == *"cancelled migration"* ]]; }'

stats=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/stats?plan_id=$pid" || echo '{}')
check "stats report completions"    '[[ "$stats" == *\"completed\"* ]]'
events=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/events?limit=5" || echo '[]')
check "events readable"             '[[ "$events" == *\"kind\"* ]]'
check "SSE stream replays since=0"  'curl -sS -m 4 -N -H "$H" "$BASE/api/v1/events/stream?since=0" 2>/dev/null | head -c 400 | grep -q "^id:\|^data:"'

advisor=$(curl -fsS -m 20 -H "$H" "$BASE/api/v1/advisor/status" || echo '{}')
echo "advisor: $advisor"
needs_jev "jev available via sidecar"   '[[ "$advisor" == *\"jev\":{\"mode\":\"http\",\"available\":true* ]]'
needs_memory "memory available via host"   '[[ "$advisor" == *\"memory\":{\"enabled\":true,\"available\":true* ]]'
sim=$(curl -fsS -m 30 -X POST -H "$H" -H "Content-Type: application/json" -d '{"query":"warm cutover converged database"}' "$BASE/api/v1/advisor/similar-incidents" || echo '{}')
check "similar-incidents answers"   '[[ "$sim" == *\"hits\"* ]]'

# Jev strategy decision through the HTTP sidecar: a plan whose SLO nothing meets makes the advisor
# consult Jev at validation (cold and warm are both eligible on the demo OpenStack source). One VM has
# one migration across plans (SDD §5.4) and the API has no plan delete, so the smoke plan of an earlier
# run is validated again when it can be; otherwise a new one takes one VM no plan holds (the demo
# leaves report-01 and batch-01 unplanned, SDD §10) and leaves the other for a plan of your own.
smoke_validate() {  # the plan's validation report, then its HTTP status on the last line
  curl -s -m 120 -w "\n%{http_code}" -X POST -H "$H" "$BASE/api/v1/plans/$1/validate"
}
npid=""
vreport='{}'
# A smoke plan someone gave a strategy override (from the dashboard) is theirs now: its choice is no longer
# automatic, so the advisor is not asked (SDD §14.4). It is left as it is, like one without a VM, and
# another one is used.
for earlier in $(curl -fsS -m 30 -H "$H" "$BASE/api/v1/plans" |
  python3 -c 'import json,sys; print(" ".join(p["id"] for p in reversed(json.load(sys.stdin)) if p["name"] == "smoke jev" and p.get("vm_ids") and not p.get("strategy_overrides")))'); do
  answer=$(smoke_validate "$earlier")
  if [ "${answer##*$'\n'}" = 200 ]; then npid=$earlier; vreport=${answer%$'\n'*}; break; fi
done
if [ -z "$npid" ]; then
  vm_ids=$({ curl -fsS -m 30 -H "$H" "$BASE/api/v1/providers/rhosp17-finance/inventory"; echo; curl -fsS -m 30 -H "$H" "$BASE/api/v1/migrations"; } |
    python3 -c '
import json, sys
inventory, migrations = (json.loads(part) for part in sys.stdin.read().split("\n", 1))
vms = inventory if isinstance(inventory, list) else inventory.get("vms", [])
held = {m["vm"]["source_id"] for m in migrations if m["phase"] not in ("cancelled", "finalized", "rolled_back")}
print(json.dumps([v["source_id"] for v in vms if not v.get("flavor_extra_specs") and v["source_id"] not in held][:1]))')
  if [ "$vm_ids" = "[]" ]; then
    # not a product failure: this demo volume has nothing left to plan. A completed migration holds its VM until
    # it is finalized (SDD §5.4), and smoke plans made before the one-plan-per-VM rule hold web-01 and web-02.
    no_plan="no finance VM is free and no earlier smoke plan can be validated again"
    skip "a VM no plan holds is free for the smoke plan (SDD §5.4, §10)"
    echo "      $no_plan: the advisor checks below are skipped. A fresh demo volume, or finalizing or rolling"
    echo "      back a finance VM from the dashboard, gives the smoke a VM again."
  else
    ok "a VM no plan holds is free for the smoke plan (SDD §5.4, §10)"
  fi
  if [ "$vm_ids" != "[]" ]; then  # no free VM: no plan is created (the API cannot delete one)
    newplan=$(curl -fsS -m 30 -X POST -H "$H" -H "Content-Type: application/json" -d "{\"name\":\"smoke jev\",\"source_provider_id\":\"rhosp17-finance\",\"destination_provider_id\":\"rhoso18\",\"vm_ids\":$vm_ids,\"downtime_slo_s\":60,\"mappings\":{\"networks\":{\"finance-app\":\"finance-app\",\"finance-db\":\"finance-db\"},\"volume_types\":{\"ceph-ssd\":\"ceph-ssd\",\"ceph-hdd\":\"ceph-hdd\"}}}" "$BASE/api/v1/plans" || echo '{}')
    npid=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' <<<"$newplan")
    answer=$(smoke_validate "$npid")
    [ "${answer##*$'\n'}" = 200 ] && vreport=${answer%$'\n'*}
  fi
fi
needs_plan "smoke plan validated (an earlier one, or a new one)" '[[ "$vreport" == *\"migrations\"* ]]'
# validation records the recommendation and its decision apart; the migration runs the decision (SDD §14.4)
advice=$({ curl -fsS -m 10 -H "$H" "$BASE/api/v1/advisor/recommendations?plan_id=$npid&limit=200" || echo '{}'; echo; curl -fsS -m 10 -H "$H" "$BASE/api/v1/migrations?plan_id=$npid" || echo '[]'; } |
  python3 -c '
import json, sys
page, migrations = (json.loads(part) for part in sys.stdin.read().split("\n", 1))
stored = [r for r in page.get("items", []) if not r["legacy"]]  # newest first
newest = {}
for r in stored:
    newest.setdefault(r["migration_id"], r)
decided = [m for m in migrations if m["id"] in newest and newest[m["id"]]["decision"]["final_option"]]
runs = bool(decided) and all(m["strategy"] == newest[m["id"]]["decision"]["final_option"] for m in decided)
seen = [(r["subject"]["name"], r["model"]["name"], r["recommendation"]["option"], r["recommendation"]["confidence"], r["decision"]["status"], r["decision"]["final_option"]) for r in stored[:3]]
print(json.dumps({"recommendations": seen, "runs_decision": runs}, separators=(",", ":")))')
echo "advisor recommendations: $advice"
if jev_on; then
  needs_plan "jev strategy recommendation recorded (SDD §14.4)" '[[ "$advice" == *\"jev\"* ]]'
else
  skip "jev strategy recommendation recorded (SDD §14.4) (Jev is off: the rules decide)"
fi
needs_plan "the migration runs its newest decision (ADV-01)" '[[ "$advice" == *\"runs_decision\":true* ]]'
# the newest recommendation is audited (created, then one decision) and its stored input re-hashes (SDD §14.5)
audit=$({ curl -fsS -m 10 -H "$H" "$BASE/api/v1/advisor/recommendations?plan_id=$npid&limit=200" || echo '{}'; echo; curl -fsS -m 10 -H "$H" "$BASE/api/v1/events?audit=true&tail=true&limit=1000&plan_id=$npid" || echo '[]'; } |
  python3 -c '
import json, sys
page, events = (json.loads(part) for part in sys.stdin.read().split("\n", 1))
rec = next((r for r in page.get("items", []) if not r["legacy"]), None)
kinds = [e["kind"] for e in events if rec and e["data"].get("target_id") == rec["id"]]
print(json.dumps({"kinds": kinds, "sha": rec and rec.get("input_snapshot_sha256")}, separators=(",", ":")))')
echo "advisor audit: $audit"
needs_plan "the recommendation is audited: created, then one decision (ADV-05)" '[[ "$audit" =~ \"kinds\":\[\"recommendation\.created\",\"decision\.[a-z_]+\"\] ]]'
sha=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("sha") or "")' <<<"$audit")
rehash=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/advisor/snapshots/$sha" 2>/dev/null | python3 -c '
import hashlib, json, sys
s = json.load(sys.stdin)
canonical = json.dumps(s["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
print(hashlib.sha256(canonical.encode("utf-8")).hexdigest() == s["sha256"])' 2>/dev/null || echo error)
needs_plan "its input snapshot re-hashes to its SHA-256 (ADV-05)" '[ -n "$sha" ] && [ "$rehash" = True ]'
advisor=$(curl -fsS -m 20 -H "$H" "$BASE/api/v1/advisor/status" || echo '{}')
check "jev has no error after decide" '[[ "$advisor" == *\"last_error\":null* ]]'
echo "advisor after: $advisor"

echo "== $pass passed, $fail failed, $skipped skipped =="
exit $fail
