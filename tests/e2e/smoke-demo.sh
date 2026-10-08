#!/usr/bin/env bash
# shellcheck disable=SC2034  # variables are read inside the check() eval strings
# smoke-demo.sh - E2 smoke test of the running Seamless Migrate demo stack (QASuite §14.6/14.8).
#
#   make seamless-demo
#   export TOKEN='smg_...'            # the admin token printed once by scripts/compose-init.sh
#   tests/e2e/smoke-demo.sh           # BASE defaults to http://127.0.0.1:8080
#
# Checks health, RBAC, the dashboard, metrics, the seeded demo flow (approvals, cutovers, one
# retry per rolled-back migration), SSE replay, stats, Jev through the sidecar and agentmemory,
# then validates a fresh plan whose SLO nothing meets so Jev is asked for a strategy.
# Never prints the token. Exit code = number of failed checks.
set -u
BASE=${BASE:-http://127.0.0.1:8080}
: "${TOKEN:?export TOKEN=<admin token from compose-init.sh>}"
H="Authorization: Bearer $TOKEN"
pass=0; fail=0
ok()   { pass=$((pass+1)); echo "PASS  $1"; }
bad()  { fail=$((fail+1)); echo "FAIL  $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }

health=$(curl -fsS -m 10 "$BASE/api/v1/health" || echo '{}')
echo "health: $health"
check "health ok + db ok + demo"    '[[ "$health" == *\"status\":\"ok\"* && "$health" == *\"db\":\"ok\"* && "$health" == *\"demo\":true* ]]'
check "unauthenticated /me is 401"  '[ "$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/me")" = 401 ]'
check "readiness is 200 with orchestrator healthy" '[ "$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/ready")" = 200 ] && curl -fsS -m 10 "$BASE/api/v1/ready" | grep -q "\"healthy\":true"'
me=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/me" || echo '{}')
check "/me is admin"                '[[ "$me" == *\"role\":\"admin\"* ]]'
index=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "$BASE/")
check "dashboard served at /"       '[ "$index" = 200 ] && curl -fsS -m 10 "$BASE/" | grep -qi "<div id=\"root\""'
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
deadline=$((SECONDS+300)); final=""; migs='[]'
while [ $SECONDS -lt $deadline ]; do
  migs=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/migrations" || echo '[]')
  summary=$(python3 -c 'import json,sys,collections; ms=json.load(sys.stdin); c=collections.Counter(m["phase"] for m in ms); print(dict(c))' <<<"$migs")
  # retry each rolled-back migration once (the demo injects transient cutover failures)
  for mid in $(python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin) if m["phase"]=="rolled_back" and m["attempts"] < 2))' <<<"$migs"); do
    curl -fsS -m 10 -X POST -H "$H" "$BASE/api/v1/migrations/$mid/retry" >/dev/null 2>&1 && echo "retried $mid"
  done
  # approve + request cutover: warm strategies wait in awaiting_cutover, single-shot ones in ready
  for mid in $(python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin) if m["phase"] in ("awaiting_cutover","ready") and not m.get("cutover_requested")))' <<<"$migs"); do
    curl -fsS -m 10 -X POST -H "$H" -H "Content-Type: application/json" -d '{"comment":"smoke"}' "$BASE/api/v1/migrations/$mid/approve" >/dev/null 2>&1
    curl -fsS -m 10 -X POST -H "$H" -H "Content-Type: application/json" -d '{}' "$BASE/api/v1/migrations/$mid/cutover" >/dev/null 2>&1 && echo "approved+cutover $mid"
  done
  running=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/plans" | python3 -c 'import json,sys; print(sum(1 for p in json.load(sys.stdin) if p["status"]=="running"))')
  done_all=$(python3 -c 'import json,sys; ms=json.load(sys.stdin); print("yes" if ms and int(sys.argv[1])==0 and all(m["phase"] in ("completed","finalized","cancelled","failed","rolled_back","blocked","ready") for m in ms) else "no")' "$running" <<<"$migs")
  if [ "$done_all" = yes ]; then final="$summary"; break; fi
  sleep 5
done
echo "final phases: ${final:-$summary (timeout)}"
check "migrations reached completed"  '[[ -n "$final" && "$final" == *completed* ]]'
check "no failed migration"           '[[ -n "$final" && "$final" != *failed* ]]'
python3 -c 'import json,sys; ms=json.load(sys.stdin); print("downtime_s:", sorted(round(m["actual_downtime_s"],1) for m in ms if m.get("actual_downtime_s") is not None)); print("blocked:", [(m["vm"]["name"], [f["code"] for f in m["findings"] if f["severity"]=="blocker"]) for m in ms if m["phase"]=="blocked"])' <<<"$migs"
# plan status after the run
plans=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/plans" || echo '[]')
python3 -c 'import json,sys; print("plans:", [(x["name"], x["status"]) for x in json.load(sys.stdin)])' <<<"$plans"
# validation is refused while a plan runs (409), allowed again afterwards
code=$(curl -s -m 60 -o /dev/null -w "%{http_code}" -X POST -H "$H" "$BASE/api/v1/plans/$pid/validate")
check "re-validate answers 200/409"   '[ "$code" = 200 ] || [ "$code" = 409 ]'

stats=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/stats?plan_id=$pid" || echo '{}')
check "stats report completions"    '[[ "$stats" == *\"completed\"* ]]'
events=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/events?limit=5" || echo '[]')
check "events readable"             '[[ "$events" == *\"kind\"* ]]'
check "SSE stream replays since=0"  'curl -sS -m 4 -N -H "$H" "$BASE/api/v1/events/stream?since=0" 2>/dev/null | head -c 400 | grep -q "^id:\|^data:"'

advisor=$(curl -fsS -m 20 -H "$H" "$BASE/api/v1/advisor/status" || echo '{}')
echo "advisor: $advisor"
check "jev available via sidecar"   '[[ "$advisor" == *\"jev\":{\"mode\":\"http\",\"available\":true* ]]'
check "memory available via host"   '[[ "$advisor" == *\"memory\":{\"enabled\":true,\"available\":true* ]]'
sim=$(curl -fsS -m 30 -X POST -H "$H" -H "Content-Type: application/json" -d '{"query":"warm cutover converged database"}' "$BASE/api/v1/advisor/similar-incidents" || echo '{}')
check "similar-incidents answers"   '[[ "$sim" == *\"hits\"* ]]'

# Jev strategy decision through the HTTP sidecar: a new plan with an SLO nothing meets makes
# the advisor consult Jev (cold and warm are both eligible on the demo OpenStack source)
vm_ids=$(curl -fsS -m 30 -H "$H" "$BASE/api/v1/providers/rhosp17-finance/inventory" | python3 -c 'import json,sys; inv=json.load(sys.stdin); vms=inv if isinstance(inv, list) else inv.get("vms", []); print(json.dumps([v["source_id"] for v in vms if not v.get("flavor_extra_specs")][:2]))')
newplan=$(curl -fsS -m 30 -X POST -H "$H" -H "Content-Type: application/json" -d "{\"name\":\"smoke jev\",\"source_provider_id\":\"rhosp17-finance\",\"destination_provider_id\":\"rhoso18\",\"vm_ids\":$vm_ids,\"downtime_slo_s\":60,\"mappings\":{\"networks\":{\"finance-app\":\"finance-app\",\"finance-db\":\"finance-db\"},\"volume_types\":{\"ceph-ssd\":\"ceph-ssd\",\"ceph-hdd\":\"ceph-hdd\"}}}" "$BASE/api/v1/plans" || echo '{}')
npid=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' <<<"$newplan")
check "smoke plan created"            '[ -n "$npid" ]'
vreport=$(curl -fsS -m 120 -X POST -H "$H" "$BASE/api/v1/plans/$npid/validate" || echo '{}')
check "smoke plan validated"          '[[ "$vreport" == *\"migrations\"* ]]'
notes=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/migrations?plan_id=$npid" | python3 -c 'import json,sys; ms=json.load(sys.stdin); print(json.dumps([(m["vm"]["name"], n["source"], n["summary"][:80], n["data"].get("applied")) for m in ms for n in m["advisor_notes"]]))')
echo "advisor notes: $notes"
check "jev strategy note recorded"    '[[ "$notes" == *\"jev\"* ]]'
advisor=$(curl -fsS -m 20 -H "$H" "$BASE/api/v1/advisor/status" || echo '{}')
check "jev has no error after decide" '[[ "$advisor" == *\"last_error\":null* ]] || [[ "$advisor" != *candidates* ]]'
echo "advisor after: $advisor"

echo "== $pass passed, $fail failed =="
exit $fail
