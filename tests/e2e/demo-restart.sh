#!/usr/bin/env bash
# demo-restart.sh - DEMO-04 and DEMO-06 (QASuite §14.3) against a freshly started demo stack:
# every phase shows up in GET /stats by_phase, and a `compose restart seamless` mid-run leaves
# every migration resuming to a terminal phase with contiguous event seqs and FSM-valid
# migration.phase ordering. Run right after `make seamless-reset CONFIRM=yes && make seamless-demo`.
#
#   export TOKEN='smg_...'        # admin or approver token
#   tests/e2e/demo-restart.sh     # RESTART_AT=<seconds> (default 45), BASE=<url>
# shellcheck disable=SC2034
set -u
R=$(cd "$(dirname "$0")/../.." && pwd)
S=${TMPDIR:-/tmp}
BASE=${BASE:-http://127.0.0.1:8080}
: "${TOKEN:?export TOKEN=<admin or approver token>}"
H="Authorization: Bearer $TOKEN"
C="docker --context colima-seamless compose -p seamless -f $R/deploy/compose/compose.yaml --env-file $R/deploy/compose/.env"
seen=""
restart_at=${RESTART_AT:-45}
restarted=0
deadline=$((SECONDS+600))
while [ $SECONDS -lt $deadline ]; do
  stats=$(curl -fsS -m 5 -H "$H" "$BASE/api/v1/stats" 2>/dev/null || echo '{}')
  phases=$(python3 -c 'import json,sys; s=json.load(sys.stdin); print(" ".join(k for k,v in (s.get("by_phase") or {}).items() if v))' <<<"$stats")
  seen="$seen $phases"
  # approve + cutover what waits for an approver (the VMware plan requires approval)
  migs=$(curl -fsS -m 5 -H "$H" "$BASE/api/v1/migrations" 2>/dev/null || echo '[]')
  for mid in $(python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin) if m["phase"] in ("awaiting_cutover","ready") and not m.get("cutover_requested")))' <<<"$migs"); do
    curl -fsS -m 5 -X POST -H "$H" -H "Content-Type: application/json" -d '{"comment":"demo46"}' "$BASE/api/v1/migrations/$mid/approve" >/dev/null 2>&1
    curl -fsS -m 5 -X POST -H "$H" -H "Content-Type: application/json" -d '{}' "$BASE/api/v1/migrations/$mid/cutover" >/dev/null 2>&1
  done
  for mid in $(python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin) if m["phase"]=="rolled_back" and m["attempts"] < 2))' <<<"$migs"); do
    curl -fsS -m 5 -X POST -H "$H" "$BASE/api/v1/migrations/$mid/retry" >/dev/null 2>&1
  done
  if [ $restarted = 0 ] && [ $SECONDS -ge $restart_at ]; then
    active=$(python3 -c 'import json,sys; ms=json.load(sys.stdin); print(sum(1 for m in ms if m["phase"] in ("precopy","syncing","cutover","verifying","rolling_back")))' <<<"$migs")
    echo "t=${SECONDS}s restarting seamless with $active migration(s) in an active step"
    env -u DOCKER_HOST $C restart seamless >/dev/null 2>&1 && restarted=1
    until curl -fsS -m 2 "$BASE/api/v1/health" >/dev/null 2>&1; do sleep 1; done
    echo "t=${SECONDS}s control plane back"
  fi
  running=$(curl -fsS -m 5 -H "$H" "$BASE/api/v1/plans" 2>/dev/null | python3 -c 'import json,sys; print(sum(1 for p in json.load(sys.stdin) if p["status"]=="running"))' 2>/dev/null || echo 1)
  if [ "$restarted" = 1 ] && [ "$running" = 0 ]; then break; fi
  sleep 2
done
echo "phases seen: $(tr ' ' '\n' <<<"$seen" | sort -u | tr '\n' ' ')"
curl -fsS -m 5 -H "$H" "$BASE/api/v1/migrations" | python3 -c 'import json,sys,collections; ms=json.load(sys.stdin); print("final:", dict(collections.Counter(m["phase"] for m in ms)))'
# DEMO-06: event ordering per migration must follow the FSM, seq contiguous
curl -fsS -m 30 -H "$H" "$BASE/api/v1/events?since=0&limit=1000" > "$S/seamless-events.json"
"$R/seamless/.venv/bin/python" - "$S/seamless-events.json" <<'EOF'
import json, sys, collections
from seamless_migrate.domain import fsm
from seamless_migrate.domain.enums import Phase
events = json.load(open(sys.argv[1]))
seqs = [e["seq"] for e in events]
gaps = [b for a, b in zip(seqs, seqs[1:]) if b != a + 1]
print("events:", len(events), "first seq", seqs[0], "last seq", seqs[-1], "gaps:", gaps[:5])
by = collections.defaultdict(list)
for e in events:
    if e["kind"] == "migration.phase" and e.get("migration_id"):
        by[e["migration_id"]].append(e["data"].get("to") or e["data"].get("to_phase"))
bad = 0
for mid, phases in by.items():
    for a, b in zip(phases, phases[1:]):
        if a and b and Phase(b) not in fsm.TRANSITIONS.get(Phase(a), ()):
            bad += 1; print("invalid order", mid, a, "->", b)
print("migrations with phase events:", len(by), "invalid transitions:", bad)
# DEMO-06 fails on any gap or FSM-invalid ordering (exit code of the script)
sys.exit(1 if gaps or bad else 0)
EOF
status=$?
[ "$status" = 0 ] && echo "DEMO-06 OK" || echo "DEMO-06 FAILED (gaps or invalid transitions above)"
exit $status
