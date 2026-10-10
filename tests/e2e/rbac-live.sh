#!/usr/bin/env bash
# rbac-live.sh - live RBAC matrix check against SDD 12 / Security.md 5.2 (bash 3.2 compatible)
# usage: VIEWER=... OPERATOR=... APPROVER=... ADMIN=... bash rbac-live.sh
set -u
BASE=${BASE:-http://127.0.0.1:8080/api/v1}
: "${VIEWER:?}" "${OPERATOR:?}" "${APPROVER:?}" "${ADMIN:?}"
rank()  { case "$1" in viewer) echo 1 ;; operator) echo 2 ;; approver) echo 3 ;; admin) echo 4 ;; esac; }
token() { case "$1" in viewer) echo "$VIEWER" ;; operator) echo "$OPERATOR" ;; approver) echo "$APPROVER" ;; admin) echo "$ADMIN" ;; esac; }
fail=0
check() { # METHOD PATH MIN_ROLE
  local code want role
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" -d '{}' -H 'Content-Type: application/json' "$BASE$2")
  [ "$code" = 401 ] || { echo "FAIL no token  $1 $2 -> $code (want 401)"; fail=1; }
  for role in viewer operator approver admin; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" -d '{}' -H 'Content-Type: application/json' \
           -H "Authorization: Bearer $(token "$role")" "$BASE$2")
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

# plan policy fields (require_approval, auto_cutover, cutover_window) need approver, also on POST /plans (SDD 12);
# the 403 for an operator must win over any 400/404/422 of the same request
policy() { # METHOD PATH JSON-BODY
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" -d "$3" -H 'Content-Type: application/json' \
         -H "Authorization: Bearer $OPERATOR" "$BASE$2")
  [ "$code" = 403 ] || { echo "FAIL operator $1 $2 $3 -> $code (want 403)"; fail=1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" -d "$3" -H 'Content-Type: application/json' \
         -H "Authorization: Bearer $APPROVER" "$BASE$2")
  if [ "$code" = 401 ] || [ "$code" = 403 ]; then echo "FAIL approver $1 $2 $3 -> $code (want allowed)"; fail=1; fi
}
policy POST  /plans      '{"name":"rbac-probe","source_provider_id":"nope","destination_provider_id":"nope","vm_ids":[],"auto_cutover":true}'
policy PATCH /plans/nope '{"require_approval":false}'
policy PATCH /plans/nope '{"cutover_window":{"start":"2030-01-01T01:00:00Z","end":"2030-01-01T05:00:00Z"}}'
if [ "$fail" = 0 ]; then echo "RBAC matrix OK"; else echo "RBAC matrix FAILED"; exit 1; fi
