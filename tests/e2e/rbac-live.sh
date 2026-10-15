#!/usr/bin/env bash
# rbac-live.sh - live RBAC matrix check against SDD 12 / Security.md 5.2 (bash 3.2 compatible)
# usage: VIEWER=... OPERATOR=... APPROVER=... ADMIN=... bash rbac-live.sh
set -u
BASE=${BASE:-http://127.0.0.1:8080/api/v1}
: "${VIEWER:?}" "${OPERATOR:?}" "${APPROVER:?}" "${ADMIN:?}"
rank()  { case "$1" in viewer) echo 1 ;; operator) echo 2 ;; approver) echo 3 ;; admin) echo 4 ;; esac; }
# shellcheck source=tests/e2e/token-headers.sh
. "$(dirname "$0")/token-headers.sh"   # the tokens reach curl from header files, never argv
VIEWER_H=$(token_header viewer "$VIEWER") || exit 2
OPERATOR_H=$(token_header operator "$OPERATOR") || exit 2
APPROVER_H=$(token_header approver "$APPROVER") || exit 2
ADMIN_H=$(token_header admin "$ADMIN") || exit 2
header() { case "$1" in viewer) echo "$VIEWER_H" ;; operator) echo "$OPERATOR_H" ;; approver) echo "$APPROVER_H" ;; admin) echo "$ADMIN_H" ;; esac; }
fail=0
check() { # METHOD PATH MIN_ROLE
  local code want role
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" -d '{}' -H 'Content-Type: application/json' "$BASE$2")
  [ "$code" = 401 ] || { echo "FAIL no token  $1 $2 -> $code (want 401)"; fail=1; }
  for role in viewer operator approver admin; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" -d '{}' -H 'Content-Type: application/json' \
           -H "$(header "$role")" "$BASE$2")
    if [ "$(rank "$role")" -lt "$(rank "$3")" ]; then want="403"; else want="not 401/403"; fi
    if [ "$want" = 403 ] && [ "$code" != 403 ]; then echo "FAIL $role $1 $2 -> $code (want 403)"; fail=1; fi
    if [ "$want" != 403 ] && { [ "$code" = 401 ] || [ "$code" = 403 ]; }; then echo "FAIL $role $1 $2 -> $code (want allowed)"; fail=1; fi
  done
}
check GET  /me viewer;                         check GET  /providers viewer
check POST /providers admin;                   check DELETE /providers/nope admin
check PATCH /providers/nope admin;             check PUT  /providers/nope/credentials admin
check PUT  /providers/nope/conversion-key admin
check POST /providers/nope/check operator;     check GET  /plans viewer
check POST /plans operator;                    check PATCH /plans/nope operator
check POST /plans/nope/validate operator;      check POST /plans/nope/start operator
check POST /plans/nope/pause operator;         check POST /plans/nope/waves/auto operator
check GET  /migrations viewer;                 check POST /migrations/nope/approve approver
check POST /migrations/nope/cutover approver;  check POST /migrations/nope/sync operator
check POST /migrations/nope/rollback operator; check POST /migrations/nope/retry operator
check POST /migrations/nope/cancel operator;   check POST /migrations/nope/finalize approver
check PUT  /migrations/nope/strategy operator; check GET  /events viewer
check GET  /stats viewer;                      check GET  /advisor/status viewer
check POST /advisor/similar-incidents operator

# plan policy fields (require_approval, require_distinct_approver, auto_cutover, cutover_window) need approver,
# also on POST /plans (SDD 12); the 403 for an operator must win over any 400/404/422 of the same request
policy() { # METHOD OPERATOR-PATH APPROVER-PATH JSON-BODY
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" -d "$4" -H 'Content-Type: application/json' \
         -H "$OPERATOR_H" "$BASE$2")
  [ "$code" = 403 ] || { echo "FAIL operator $1 $2 $4 -> $code (want 403)"; fail=1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" -d "$4" -H 'Content-Type: application/json' \
         -H "$APPROVER_H" "$BASE$3")
  if [ "$code" = 401 ] || [ "$code" = 403 ]; then echo "FAIL approver $1 $3 $4 -> $code (want allowed)"; fail=1; fi
}
policy POST /plans /plans '{"name":"rbac-probe","source_provider_id":"nope","destination_provider_id":"nope","vm_ids":[],"auto_cutover":true}'
# PATCH: an operator may send a policy field at the plan's current value - only a change needs the approver
# (SDD 12) - so the route reads the plan first, and a plan that does not exist is 404 to an operator. The
# operator's probe goes to a real plan, read with the viewer's token, with a value that plan does not hold,
# and names a source provider that does not exist: with the role check in place it is refused 403; with the
# check regressed it meets the provider check (400) before anything is written (review RV16-01). The
# approver's goes to a plan that does not exist. A field the plan lacks is skipped by name (RV16-04).
probes=$(curl -s -H "$VIEWER_H" "$BASE/plans" | python3 -c '
import json, sys
try:
    plan = json.load(sys.stdin)[0]
    lines = [str(plan["id"])]
except Exception:
    sys.exit(0)
window = {"start": "2030-01-01T01:00:00Z", "end": "2030-01-01T05:00:00Z"}
guard = {"source_provider_id": "rbac-probe-none"}
for field in ("require_approval", "require_distinct_approver", "cutover_window"):
    if field not in plan:
        lines.append("SKIP " + field)
    elif field == "cutover_window":
        lines.append(json.dumps(dict({field: None if plan[field] else window}, **guard)))
    else:
        lines.append(json.dumps(dict({field: not plan[field]}, **guard)))
print("\n".join(lines))' 2>/dev/null)
if [ -n "$probes" ]; then
  plan_id=$(sed -n 1p <<<"$probes")
  while IFS= read -r body; do
    case "$body" in
      "SKIP "*) echo "SKIP PATCH policy probe ${body#SKIP }: the plan has no such field" ;;
      *) policy PATCH "/plans/$plan_id" /plans/nope "$body" ;;
    esac
  done < <(sed 1d <<<"$probes")
else
  echo "SKIP PATCH policy probes: no plan on the stack to probe"
fi
if [ "$fail" = 0 ]; then echo "RBAC matrix OK"; else echo "RBAC matrix FAILED"; exit 1; fi
