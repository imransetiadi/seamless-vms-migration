#!/usr/bin/env bash
# demo-journey.sh - AC-1 shape in demo mode (QASuite §14.7, FR-17): wait for a warm migration that waits
# for its cutover, cut it over, assert the outcome and print its audit trail.
#
#   export TOKEN='smg_...'            # an admin or approver token (never printed)
#   tests/e2e/demo-journey.sh         # BASE defaults to http://127.0.0.1:8080/api/v1
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8080/api/v1}
: "${TOKEN:?export TOKEN=<admin or approver token>}"
api() { curl -fsS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }

echo "== health";   curl -fsS "$BASE/health" | jq -e '.status == "ok" and .db == "ok" and .demo == true'
echo "== identity"; api "$BASE/me" | jq -e '.role == "admin" or .role == "approver"'
echo "== plans";    api "$BASE/plans" | jq -e 'length >= 2'

MID=""
for _ in $(seq 1 180); do                       # the demo runs at SEAMLESS_DEMO_SPEED, so poll
  # warm or vmware_warm: the finance plan cuts over by itself, the VMware plan's migrations wait for an approver
  MID=$(api "$BASE/migrations?phase=awaiting_cutover" | jq -r '[.[] | select(.strategy == "warm" or .strategy == "vmware_warm")][0].id // empty')
  [ -n "$MID" ] && break
  sleep 5
done
[ -n "$MID" ] || { echo "no warm migration reached awaiting_cutover"; exit 1; }

echo "== cutover $MID"
api -X POST "$BASE/migrations/$MID/cutover" -d '{"force_window": true, "comment": "qa demo journey"}' \
  | jq -e '.cutover_requested == true and (.approvals | length) >= 1'

PHASE=""
for _ in $(seq 1 120); do
  PHASE=$(api "$BASE/migrations/$MID" | jq -r .phase)
  case "$PHASE" in completed|rolled_back|failed) break ;; esac
  sleep 5
done
api "$BASE/migrations/$MID" | jq '{phase, actual_downtime_s, passes: (.sync_passes | length), approvals: (.approvals | length)}'

# the demo injects failures (SEAMLESS_DEMO_FAILURE_RATE, default 0.1): completed and rolled_back are both valid
[ "$PHASE" = completed ] || [ "$PHASE" = rolled_back ] || { echo "unexpected terminal phase: $PHASE"; exit 1; }
if [ "$PHASE" = completed ]; then
  api "$BASE/migrations/$MID" | jq -e '.actual_downtime_s != null and .actual_downtime_s > 0 and (.sync_passes | map(.kind) | index("final")) != null'
fi
echo "== audit trail"
api "$BASE/events?migration_id=$MID&limit=1000" | jq -r '.[].kind' | sort | uniq -c
