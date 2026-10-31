#!/usr/bin/env bash
# ha-failover.sh - the HA failover drill against a lab cluster (QASuite LAB-HA01, LAB-HA02, §13.1f; SDD §25.10; plan
# task HA8), on OpenShift or Kubernetes with the HA component's two replicas (SDD §25.7):
#   1. it finds the leading pod through GET /api/v1/health at SEAMLESS_URL - the leader's holder is the pod's name and
#      8 hex digits of its process (SDD §12, §25.6) - and refuses unless that pod is one of two control plane pods or
#      more, so that a follower can take over;
#   2. it waits for a migration of PLAN in cutover whose source VM is stopped - request and approve the cutovers as
#      for any other: the drill only reads -, then reads the leader again;
#   3. it takes the leading pod away by MODE (below);
#   4. it waits for another replica to take the lease with a higher epoch and drive the migrations - its orchestrator
#      running and ticking -, and fails when none does within TAKEOVER_WAIT;
#   5. once the migrations then in cutover, verifying or rolling_back left them, it checks each migration of PLAN as
#      DEMO-06 does (QASuite §6.6): in its last cutover one source stop (migration.downtime_started) and one
#      destination server (destination_server_id), and migration.phase orders the FSM allows, each change starting
#      from the phase the one before reached (SDD §5.1, §5.2, §25.5). It reads what the control plane recorded: a
#      server the old leader created and never recorded shows on the destination cloud alone - count there the
#      servers named as each VM.
# It prints what QASuite §13.1f records - the versions, the measured times, and the downtime of each VM whose step lost
# its leader, beside its estimate - and FAIL with the reason of each failed check.
#
#   read -rs TOKEN && export TOKEN    # a viewer's token or higher
#   SEAMLESS_URL=https://seamless.apps.example.com PLAN=plan-... tests/e2e/ha-failover.sh openshift node
#
# PLATFORM: kubernetes (kubectl) or openshift (oc), as scripts/seamless-ctl.sh picks its CLI. MODE is always given:
# none is a default.
#   delete  `delete pod` of the leading pod: it stops, releases its lease, and the follower takes it at its next try
#           (SDD §25.3's release, as an eviction ends a pod); the Deployment starts another pod
#   node    its node cordoned, then powered off by POWER_OFF_CMD, which only the platform can do (a BMC, a
#           hypervisor, a cloud's API): the lease lapses and the follower takes it (LAB-HA01). The node stays
#           cordoned and off: power it on, then uncordon it
#   db      the leader cut off from PostgreSQL by CUT_DB_CMD, its pod alive: its renew deadline deposes it, the
#           follower takes the lapsed lease (LAB-HA02), and the drill prints what the deposed replica logged of its
#           deposition and of the writes the fence refused. HEAL_DB_CMD, when set, gives the pod its PostgreSQL
#           back once the wait for the takeover ended - the deposed replica may serve on as a follower -, or at an
#           interrupt; else the drill says to undo the cut. The cut must also drop what is connected already: the
#           elector renews on one connection it keeps open (SDD §25.3). No NetworkPolicy added can make it -
#           policies only allow, and seamless-allow-egress-postgres allows every control plane pod -: cut it in the
#           pod's network on its node, or by the network plugin's own deny rules
# The three commands run with sh -c and NODE, POD, POD_IP and NAMESPACE in their environment, as in
#   POWER_OFF_CMD='virsh destroy "$NODE"'
#
# Environment:
#   SEAMLESS_URL   the Route's or the Ingress's URL (default http://127.0.0.1:8080)
#   TOKEN          a viewer's bearer token or higher, handed to curl through a header file, never an argument
#   PLAN           the plan whose cutover the drill takes the leader away in
#   NAMESPACE      the control plane's namespace (default seamless-migrate)
#   CUTOVER_WAIT   seconds to wait for that cutover (default 1800)
#   TAKEOVER_WAIT  seconds from the take-away to another leader driving (default 120: about 35 s after a lost
#                  leader, 60 s the target - SDD §25.5, §25.11 decision 3)
#   SETTLE_WAIT    seconds for the migrations to leave cutover, verifying and rolling_back (default 3600)
#   POLL           seconds between two reads (default 2)
#   PYTHON         a Python that imports seamless_migrate, for the FSM's transitions (default seamless/.venv's)
# The times are whole seconds from the moment the take-away's command returned. Bash 3.2 compatible.
# Exit status: 0 every check passed, 1 a check failed, 2 a usage error or a refusal: nothing was taken away.
set -euo pipefail
R=$(cd "$(dirname "$0")/../.." && pwd)
POD='' NODE='' POD_IP='' HOLDER='' EPOCH='' HEALTH='' CUT=0 FAILED=0

refuse() { printf 'ha-failover: %s\n' "$*" >&2; exit 2; }
is_count() { case $1 in '' | *[!0-9]*) return 1 ;; esac; }

# --- what is taken away, and how: all of it given before anything runs -------------------------------------------
[ $# -eq 2 ] || refuse "usage: tests/e2e/ha-failover.sh kubernetes|openshift delete|node|db - the mode is always" \
  "given, none is a default (the script's header says what each takes away)"
case $1 in
  kubernetes) KUBE=kubectl ADM='' ;;
  openshift) KUBE=oc ADM=adm ;; # OpenShift keeps the node commands under oc adm
  *) refuse "unknown platform '$1': kubernetes or openshift" ;;
esac
MODE=$2
case $MODE in
  delete) ;;
  node) [ -n "${POWER_OFF_CMD:-}" ] || refuse "mode node powers the leading pod's node off with POWER_OFF_CMD, which" \
    "only the platform can do: set it (the script's header shows how)" ;;
  db) [ -n "${CUT_DB_CMD:-}" ] || refuse "mode db cuts the leading pod off from PostgreSQL with CUT_DB_CMD: set it" \
    "(the script's header says what it must cut)" ;;
  *) refuse "unknown mode '$MODE': delete, node or db" ;;
esac
[ -n "${TOKEN:-}" ] || refuse "read -rs TOKEN && export TOKEN: a viewer's token or higher"
PLAN=${PLAN:-}
case $PLAN in
  '' | .* | *[!A-Za-z0-9._-]*) refuse "set PLAN to the id of the plan whose cutover the drill takes the leader away in" ;;
esac
NS=${NAMESPACE:-seamless-migrate}
case $NS in '' | -* | *[!a-z0-9-]* | *-) refuse "NAMESPACE '$NS' is no Kubernetes namespace name" ;; esac
CUTOVER_WAIT=${CUTOVER_WAIT:-1800} TAKEOVER_WAIT=${TAKEOVER_WAIT:-120} SETTLE_WAIT=${SETTLE_WAIT:-3600} POLL=${POLL:-2}
for setting in "CUTOVER_WAIT=$CUTOVER_WAIT" "TAKEOVER_WAIT=$TAKEOVER_WAIT" "SETTLE_WAIT=$SETTLE_WAIT" "POLL=$POLL"; do
  is_count "${setting#*=}" || refuse "${setting%%=*} takes whole seconds, not '${setting#*=}'"
done
# in decimal: $(( )) reads a leading zero as octal
CUTOVER_WAIT=$((10#$CUTOVER_WAIT)) TAKEOVER_WAIT=$((10#$TAKEOVER_WAIT)) SETTLE_WAIT=$((10#$SETTLE_WAIT)) POLL=$((10#$POLL))
for tool in "$KUBE" curl python3; do
  command -v "$tool" >/dev/null 2>&1 || refuse "$tool is needed"
done
PYTHON=${PYTHON:-$R/seamless/.venv/bin/python}
"$PYTHON" -c 'import seamless_migrate.domain.fsm' >/dev/null 2>&1 ||
  refuse "the FSM's check needs a Python that imports seamless_migrate, and $PYTHON does not: create seamless/.venv" \
    "(QASuite §14.3), or set PYTHON"

# shellcheck source=tests/e2e/token-headers.sh disable=SC1091 # followed with shellcheck -x
. "$R/tests/e2e/token-headers.sh" # E2E_TMP, token_header: the token reaches curl from a header file, never argv
H=$(token_header viewer "$TOKEN") || exit 2
S=$E2E_TMP
BASE=${SEAMLESS_URL:-http://127.0.0.1:8080}
BASE=${BASE%/}/api/v1

# CMD run by sh -c with the leading pod's whereabouts in its environment
operator_command() { NODE=$NODE POD=$POD POD_IP=$POD_IP NAMESPACE=$NS sh -c "$1"; }
# The cut leader given back its PostgreSQL, once (db): by HEAL_DB_CMD, else the operator told to undo the cut
give_back() {
  [ "$CUT" = 1 ] || return 0
  CUT=0
  if [ -z "${HEAL_DB_CMD:-}" ]; then
    echo "NOTE      pod $POD stays cut off from PostgreSQL: undo CUT_DB_CMD (HEAL_DB_CMD is not set)"
  elif operator_command "$HEAL_DB_CMD"; then
    echo "HEALED    HEAL_DB_CMD gave pod $POD its PostgreSQL back"
  else
    echo "FAIL  HEAL_DB_CMD failed: pod $POD may still be cut off from PostgreSQL"
    FAILED=1
  fi
}
# an interrupt gives the cut back first; token-headers.sh's EXIT trap, the only one, then removes its directory
trap 'give_back; exit 130' INT
trap 'give_back; exit 143' TERM

get() { curl -fsS -m 30 -H "$H" "$BASE$1"; }
# "HOLDER EPOCH DRIVES" of the health on stdin: the lease's holder and epoch - each "-" while no replica leads -, and 1
# while the leader's orchestrator runs and has ticked (SDD §12)
leader_of() {
  python3 -c 'import json, sys
try:
    health = json.load(sys.stdin)
    leader, orchestrator = health.get("leader") or {}, health.get("orchestrator") or {}
    holder, epoch = leader.get("holder"), leader.get("epoch")
    if not isinstance(holder, str) or len(holder.split()) != 1:
        holder = "-"
    drives = orchestrator.get("running") is True and int(orchestrator.get("ticks") or 0) >= 1
    print(holder, epoch if type(epoch) is int else "-", int(drives))
except Exception:
    print("- - 0")'
}

# The leading pod as the health names it (SDD §12, §25.6) - HOLDER, EPOCH, POD, NODE and POD_IP -, refused unless it
# is one of two control plane pods or more.
find_leader() {
  local pods running
  HEALTH=$(get /health) || refuse "the API at $BASE does not answer GET /api/v1/health"
  read -r HOLDER EPOCH _ <<<"$(printf '%s' "$HEALTH" | leader_of)"
  if [ "$HOLDER" = - ] || [ "$EPOCH" = - ]; then
    refuse "no replica leads: nothing to take away (docs/Operations.md §4.9)"
  fi
  case ${HOLDER##*-} in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) refuse "the lease's holder '$HOLDER' is no pod name with 8 hex digits of its process (SDD §25.3)" ;;
  esac
  POD=${HOLDER%-*}
  pods=$("$KUBE" -n "$NS" get pods -l app.kubernetes.io/name=seamless \
    '--field-selector=status.phase!=Failed,status.phase!=Succeeded' -o name) ||
    refuse "cannot list the control plane's pods in namespace $NS"
  case $'\n'$pods$'\n' in
    *$'\n'"pod/$POD"$'\n'*) ;;
    *) refuse "the leader $POD is no control plane pod of namespace $NS: does SEAMLESS_URL reach this cluster?" ;;
  esac
  running=$(printf '%s\n' "$pods" | grep -c . || true)
  [ "$running" -ge 2 ] || refuse "namespace $NS runs $running control plane pod(s): the drill needs the HA" \
    "component's two replicas (docs/Operations.md §4.7)"
  NODE=$("$KUBE" -n "$NS" get pod "$POD" -o 'jsonpath={.spec.nodeName}') || refuse "cannot read the node of pod $POD"
  POD_IP=$("$KUBE" -n "$NS" get pod "$POD" -o 'jsonpath={.status.podIP}') ||
    refuse "cannot read the address of pod $POD"
  echo "LEADER    $POD on node $NODE ($POD_IP): holder $HOLDER, epoch $EPOCH, of $running control plane pods"
}

# The first migration of the plan in FILE in cutover with its source stopped: "ID STOPPED_AT VM_NAME"
first_cutover() {
  python3 - "$1" <<'EOF'
import json, sys
try:
    migrations = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    migrations = []
for m in migrations:
    if m.get("phase") == "cutover" and m.get("downtime_started_at"):
        print(m["id"], m["downtime_started_at"], m["vm"]["name"])
        break
EOF
}

# The migrations in a step at the take-away that are still in one: "NAME (PHASE), ..."
still_busy() {
  python3 - "$S/before.json" "$S/after.json" <<'EOF'
import json, sys
STEPS = {"cutover", "verifying", "rolling_back"}
try:
    before = json.load(open(sys.argv[1], encoding="utf-8"))
    after = json.load(open(sys.argv[2], encoding="utf-8"))
except Exception:
    print("the plan's migrations could not be read")
    sys.exit()
busy = {m["id"] for m in before if m.get("phase") in STEPS}
print(", ".join(f'{m["vm"]["name"]} ({m["phase"]})' for m in after if m["id"] in busy and m["phase"] in STEPS))
EOF
}

# --- 1, 2: the leader, the versions, the cutover ------------------------------------------------------------------
find_leader
seamless_version=$(printf '%s' "$HEALTH" | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("version") or "?")
except Exception:
    print("?")')
cluster_version=$("$KUBE" version 2>/dev/null |
  awk '/^(Server|Kubernetes) Version:/ { printf "%s%s", sep, $0; sep = ", " }') || true
echo "VERSIONS  seamless $seamless_version; ${cluster_version:-the cluster did not give its version}"

deadline=$((SECONDS + CUTOVER_WAIT))
until get "/migrations?plan_id=$PLAN" >"$S/before.json" 2>/dev/null && cutover=$(first_cutover "$S/before.json") &&
  [ -n "$cutover" ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "FAIL  no migration of plan $PLAN was in cutover with its source stopped within CUTOVER_WAIT" \
      "($CUTOVER_WAIT s): nothing was taken away"
    exit 1
  fi
  sleep "$POLL"
done
read -r mid stopped vm <<<"$cutover"
echo "CUTOVER   $vm ($mid) in cutover, its source stopped at $stopped"
find_leader # again: another replica may have taken over while the drill waited

# --- 3: the take-away ---------------------------------------------------------------------------------------------
case $MODE in
  delete)
    "$KUBE" -n "$NS" delete pod "$POD" --wait=false >/dev/null ||
      { echo "FAIL  cannot delete pod $POD: nothing was taken away"; exit 1; }
    echo "TAKEAWAY  pod $POD deleted"
    ;;
  node)
    "$KUBE" ${ADM:+"$ADM"} cordon "$NODE" >/dev/null ||
      { echo "FAIL  cannot cordon node $NODE: nothing was taken away"; exit 1; }
    if ! operator_command "$POWER_OFF_CMD"; then
      echo "FAIL  POWER_OFF_CMD failed for node $NODE, which stays cordoned: power it off, or" \
        "$KUBE ${ADM:+$ADM }uncordon $NODE"
      exit 1
    fi
    echo "TAKEAWAY  node $NODE cordoned and powered off, with pod $POD"
    echo "NOTE      node $NODE stays cordoned and off: once the drill ended, power it on, then" \
      "$KUBE ${ADM:+$ADM }uncordon $NODE"
    ;;
  db)
    CUT=1 # given back however CUT_DB_CMD ends
    operator_command "$CUT_DB_CMD" || { echo "FAIL  CUT_DB_CMD failed for pod $POD"; give_back; exit 1; }
    echo "TAKEAWAY  pod $POD cut off from PostgreSQL"
    ;;
esac
t0=$SECONDS

# --- 4: another leader, with a higher epoch, that drives ----------------------------------------------------------
deadline=$((t0 + TAKEOVER_WAIT))
took='' drove='' taker='' seen='no answer'
while :; do
  if health=$(get /health 2>/dev/null); then
    read -r holder epoch drives <<<"$(printf '%s' "$health" | leader_of)"
    if [ "$holder" = - ]; then seen='no leader'; else seen="$holder at epoch $epoch"; fi
    if [ "$holder" != - ] && [ "$holder" != "$HOLDER" ] && [ "$epoch" != - ] && [ "$epoch" -gt "$EPOCH" ]; then
      if [ -z "$took" ]; then
        took=$((SECONDS - t0)) taker=${holder%-*}
        echo "TAKEOVER  $taker took the lease at epoch $epoch, $took s after the take-away"
      fi
      if [ "$drives" = 1 ]; then
        drove=$((SECONDS - t0))
        break
      fi
    fi
  fi
  [ "$SECONDS" -lt "$deadline" ] || break
  sleep "$POLL"
done
if [ -n "$drove" ]; then
  echo "RECOVERY  a leader drove the migrations again $drove s after the take-away (SDD §25.11 decision 3: about" \
    "35 s after a lost leader, 60 s the target)"
fi
give_back # db: the cut ends with the wait, and the deposed replica may serve on as a follower (LAB-HA02)
if [ -z "$took" ]; then
  echo "FAIL  no new leader within TAKEOVER_WAIT ($TAKEOVER_WAIT s) of the take-away: the health's last answer" \
    "named $seen"
  exit 1
fi
if [ -z "$drove" ]; then
  echo "FAIL  $taker took the lease $took s after the take-away, and its orchestrator did not drive within" \
    "TAKEOVER_WAIT ($TAKEOVER_WAIT s)"
  exit 1
fi
if [ "$MODE" = db ]; then # the deposed replica's own account (SDD §25.8), for QASuite §13.1f
  logged=$({ "$KUBE" -n "$NS" logs "$POD" -c seamless 2>/dev/null || true; } |
    { grep -e 'was deposed from the orchestrator lease' -e 'the fence refused' || true; } | tail -n 20)
  if [ -n "$logged" ]; then
    printf '%s\n' "$logged" | sed 's/^/LOG       /'
  else
    echo "LOG       pod $POD logged no deposition and no refused write"
  fi
fi

# --- 5: the migrations, once out of their steps, checked as DEMO-06 does -------------------------------------------
deadline=$((SECONDS + SETTLE_WAIT))
while :; do
  get "/migrations?plan_id=$PLAN" >"$S/after.json" 2>/dev/null || : >"$S/after.json"
  busy=$(still_busy)
  [ -n "$busy" ] || break
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "FAIL  after SETTLE_WAIT ($SETTLE_WAIT s), still in a step: $busy"
    FAILED=1
    break
  fi
  sleep "$POLL"
done
# every event of the plan, page after page, one page a line
since=0
: >"$S/events"
while :; do
  page=$(get "/events?plan_id=$PLAN&since=$since&limit=1000") ||
    { echo "FAIL  cannot read the events of plan $PLAN"; exit 1; }
  last=$(printf '%s' "$page" | python3 -c 'import json, sys
try:
    events = json.load(sys.stdin)
    print(events[-1]["seq"] if events else 0)
except Exception:
    print("")')
  case $last in
    0) break ;;
    '' | *[!0-9]*) echo "FAIL  cannot read the events of plan $PLAN after seq $since"; exit 1 ;;
  esac
  [ "$last" -gt "$since" ] || break
  printf '%s\n' "$page" >>"$S/events"
  since=$last
done
"$PYTHON" - "$S/before.json" "$S/after.json" "$S/events" <<'EOF' || FAILED=1
import collections, json, sys
from seamless_migrate.domain.enums import Phase
from seamless_migrate.domain.fsm import TRANSITIONS

before = {m["id"]: m for m in json.load(open(sys.argv[1], encoding="utf-8"))}
after = json.load(open(sys.argv[2], encoding="utf-8"))
events = collections.defaultdict(list)
with open(sys.argv[3], encoding="utf-8") as pages:
    for page in pages:
        for event in json.loads(page):
            if event.get("migration_id"):
                events[event["migration_id"]].append(event)


def allowed(frm, to):
    try:
        return Phase(to) in TRANSITIONS.get(Phase(frm), ())
    except ValueError:  # a phase the FSM does not know
        return False


def phase(change, end):  # its "from" or "to", as DEMO-06 reads it
    return change["data"].get(end) or change["data"].get(end + "_phase")


def counted(n, what):
    return f"{n} {what}" + ("" if n == 1 else "s")


def seconds(value):
    return "not recorded" if value is None else f"{value:.0f} s"


failed = False
for m in after:
    name, mid = m["vm"]["name"], m["id"]
    mine = sorted(events[mid], key=lambda e: e["seq"])
    changes = [e for e in mine if e["kind"] == "migration.phase"]
    problems = []
    # DEMO-06's check - each change to a phase the FSM allows after the one before - and each change starting where
    # the one before ended, which a second orchestrator writing the migration breaks while each move may be allowed
    for prev, change in zip([None, *changes], changes):
        frm, to, ended = phase(change, "from"), phase(change, "to"), prev and phase(prev, "to")
        if frm and ended and frm != ended:
            problems.append(f"{frm} -> {to} at seq {change['seq']} follows the change to {ended} at seq "
                            f"{prev['seq']}: it starts from another phase (SDD §5.1)")
        elif (ended or frm) and not allowed(ended or frm, to):
            problems.append(f"{ended or frm} -> {to} at seq {change['seq']}, a change the FSM does not allow "
                            "(SDD §5.1)")
    # its last cutover, from the last change into it: the source stopped once, one destination server (SDD §25.5)
    entered = [i for i, e in enumerate(mine) if e["kind"] == "migration.phase" and phase(e, "to") == "cutover"]
    cutover = mine[entered[-1]:] if entered else []
    stops = [e["seq"] for e in cutover if e["kind"] == "migration.downtime_started"]
    servers = {e["data"].get("destination_server_id") for e in cutover if e["kind"] == "migration.phase"}
    if cutover:  # the server it records, now and at the take-away
        servers |= {m.get("destination_server_id"), before.get(mid, {}).get("destination_server_id")}
    servers.discard(None)
    if len(stops) > 1:
        problems.append(f"{len(stops)} source stops in its last cutover (migration.downtime_started at seq "
                        f"{', '.join(map(str, stops))}): a cutover stops its source once and keeps its first "
                        "stop's time (SDD §5.2, §25.5)")
    if len(servers) > 1:
        problems.append(f"{len(servers)} destination servers in its last cutover ({', '.join(sorted(servers))}): a "
                        "resumed cutover finds the server by its record or its name (SDD §7.2, §25.5)")
    for problem in problems:
        print(f"FAIL  {name} ({mid}): {problem}")
    if not problems:
        print(f"PASS  {name} ({mid}): {counted(len(stops), 'source stop')}, "
              f"{counted(len(servers), 'destination server')}, {counted(len(changes), 'phase change')} "
              "in orders the FSM allows")
    failed = failed or bool(problems)
    if before.get(mid, {}).get("phase") in ("cutover", "verifying", "rolling_back"):  # a step lost its leader
        print(f"DOWNTIME  {name} ({mid}): {seconds(m.get('actual_downtime_s'))}, estimated "
              f"{seconds((m.get('estimate') or {}).get('downtime_s'))} - the failover's time and the step's work "
              "done again count in it (SDD §25.5)")
sys.exit(1 if failed else 0)
EOF
if [ "$FAILED" = 0 ]; then
  echo "== the drill passed: mode $MODE, a new leader $took s and a leader driving $drove s after the take-away =="
else
  echo "== the drill failed: see each FAIL above =="
fi
exit "$FAILED"
