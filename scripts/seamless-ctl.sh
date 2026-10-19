#!/usr/bin/env bash
# seamless-ctl.sh - bring a Seamless Migrate deployment up or down, stop, start, restart and scale it, the same
# way on every platform (SDD §17.2). The tutorial is docs/Operations.md. Bash 3.2 compatible.
#
# Exit status: 0 done, 1 a step failed, 2 a usage error or a refusal (nothing was run).
set -euo pipefail

PROG=seamless-ctl
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

usage() {
  cat <<'USAGE'
usage: scripts/seamless-ctl.sh PLATFORM ACTION [ARGUMENTS] [OPTIONS]

PLATFORM
  docker | podman          the single-host stack (deploy/compose, SDD §17.1), through its Make targets
  kubernetes | openshift   the cluster manifests (deploy/kubernetes, deploy/openshift), through kubectl or oc

ACTION
  up                 deploy or update everything, then wait until it runs
  down               stop everything; the data stays (volumes, or PVCs and Secrets)
  stop               stop the control plane alone; PostgreSQL keeps running
  start              start the control plane again (PostgreSQL first, back to one replica)
  restart            restart the control plane from its current configuration - after a change
  scale 0|1          the control plane's replicas: 0 stops it, 1 starts it; more is refused - the
                     orchestrator is a singleton (SDD §17, D2)
  capacity           scale the one control plane up or down: --max-migrations N, --max-cutovers N,
                     and on a cluster --cpu C, --memory M (its limits); then a restart
  status             what runs, and the API's health
  logs               follow the control plane's log
  resume PLAN_ID...  start again the plans a --drain paused (needs --token-file)

OPTIONS
  --namespace NS     the cluster namespace (default seamless-migrate)
  --demo             docker/podman up, start, restart: the demo, with simulated clouds
  --drain FILE       before down, stop, restart, scale 0 or capacity: pause every running plan and wait
                     until no migration is in cutover, verifying or rolling_back; FILE holds an
                     operator's token and may be read by its owner alone (chmod 600)
  --token-file FILE  the operator's token for resume (chmod 600)
  --url URL          the API's base URL: on docker/podman http://127.0.0.1:$SEAMLESS_HOST_PORT (8080) by
                     default; on a cluster the Route's or Ingress's URL, needed by --drain and resume
  --wait-timeout S   how long a --drain waits for the cutovers in flight (default 1800)
  --timeout S        how long a cluster rollout or pod removal may take (default 600)
  --dry-run          print the commands and run none
  -h, --help         this help

Examples
  scripts/seamless-ctl.sh docker up --demo
  scripts/seamless-ctl.sh podman restart
  scripts/seamless-ctl.sh kubernetes down --drain ~/.seamless/operator.token --url https://seamless.example.com
  scripts/seamless-ctl.sh openshift capacity --max-migrations 20 --memory 8Gi
USAGE
}

say() { printf '%s\n' "$*"; }
die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }
refuse() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 2; }
# A check that stops the action: a refusal (2) while nothing changed, a failed step (1) once a drain paused a plan
# (SDD §17.2, review RV43A-07).
PAUSED=()
check_failed() { [ ${#PAUSED[@]} -eq 0 ] || die "$*"; refuse "$*"; }
bad_usage() { printf '%s: %s\n\n' "$PROG" "$*" >&2; usage >&2; exit 2; }

DRY=0
# A word as a shell reads it back: as it is when plain, else in single quotes.
quote() {
  local q="'\\''"
  case $1 in
    '') printf "''" ;;
    *[!A-Za-z0-9_./:=@%+,-]*) printf "'%s'" "${1//\'/$q}" ;;
    *) printf '%s' "$1" ;;
  esac
}
# Run a command, or under --dry-run print it - quoted so that it can be pasted - and run nothing.
run() {
  if [ "$DRY" = 1 ]; then
    local out='+' word
    for word in "$@"; do out="$out $(quote "$word")"; done
    say "$out"
  else
    "$@"
  fi
}
need() { [ "$DRY" = 1 ] || command -v "$1" >/dev/null 2>&1 || check_failed "$1 is needed for this action: $2"; }
is_count() { case $1 in '' | *[!0-9]*) return 1 ;; esac; }
# A host port Compose publishes: 1 to 65535 - it refuses more, and 0 picks a random one (SDD §17.2, review RV44A-06).
is_port() {
  local digits
  is_count "$1" || return 1
  digits=${1#"${1%%[!0]*}"} # without leading zeros
  [ -n "$digits" ] && [ ${#digits} -le 5 ] && [ "$digits" -le 65535 ]
}

# --- arguments ----------------------------------------------------------------------------------------------
[ $# -ge 1 ] || bad_usage "a platform is needed"
case $1 in -h | --help | help) usage; exit 0 ;; esac
PLATFORM=$1
shift
case $PLATFORM in
  docker | podman) KIND=host ;;
  kubernetes) KIND=cluster KUBE=kubectl OVERLAY=deploy/kubernetes ;;
  openshift) KIND=cluster KUBE=oc OVERLAY=deploy/openshift ;;
  *) bad_usage "unknown platform '$PLATFORM': docker, podman, kubernetes or openshift" ;;
esac
[ $# -ge 1 ] || bad_usage "an action is needed"
ACTION=$1
shift
case $ACTION in
  up | down | stop | start | restart | scale | capacity | status | logs | resume) ;;
  -h | --help | help) usage; exit 0 ;;
  *) bad_usage "unknown action '$ACTION'" ;;
esac

NS=seamless-migrate DEMO=0 DRAIN='' TOKEN_FILE='' URL='' WAIT_TIMEOUT=1800 TIMEOUT=600
MAX_MIGRATIONS='' MAX_CUTOVERS='' CPU='' MEMORY=''
ARGS=()
while [ $# -gt 0 ]; do
  case $1 in
    --demo) DEMO=1; shift; continue ;;
    --dry-run) DRY=1; shift; continue ;;
    -h | --help) usage; exit 0 ;;
    --namespace | --drain | --token-file | --url | --wait-timeout | --timeout | --max-migrations | \
      --max-cutovers | --cpu | --memory)
      [ $# -ge 2 ] || bad_usage "$1 needs a value"
      value=$2 option=$1
      shift 2 ;;
    --*=*) value=${1#*=} option=${1%%=*}; shift ;;
    --*) bad_usage "unknown option '$1'" ;;
    *) ARGS+=("$1"); shift; continue ;;
  esac
  case $option in
    --namespace) NS=$value ;;
    --drain) DRAIN=$value ;;
    --token-file) TOKEN_FILE=$value ;;
    --url) URL=$value ;;
    --wait-timeout) WAIT_TIMEOUT=$value ;;
    --timeout) TIMEOUT=$value ;;
    --max-migrations) MAX_MIGRATIONS=$value ;;
    --max-cutovers) MAX_CUTOVERS=$value ;;
    --cpu) CPU=$value ;;
    --memory) MEMORY=$value ;;
    *) bad_usage "unknown option '$option'" ;;
  esac
done
NARGS=${#ARGS[@]}

case $NS in '' | -* | *[!a-z0-9-]* | *-) refuse "--namespace '$NS' is no Kubernetes namespace name" ;; esac
is_count "$WAIT_TIMEOUT" || refuse "--wait-timeout takes seconds, not '$WAIT_TIMEOUT'"
is_count "$TIMEOUT" || refuse "--timeout takes seconds, not '$TIMEOUT'"
# in decimal: bash's $(( )) reads a leading zero as octal, where 08 and 09 are errors (review RV38B-06)
WAIT_TIMEOUT=$((10#$WAIT_TIMEOUT)) TIMEOUT=$((10#$TIMEOUT))
# kubectl wait reads 0 as a single check, rollout status as no bound at all (SDD §17.2, review RV43A-02)
[ "$TIMEOUT" -ge 1 ] || refuse "--timeout takes at least 1 second: kubectl wait reads 0 as one check"
if [ -n "$URL" ]; then
  case $URL in http://* | https://*) URL=${URL%/} ;; *) refuse "--url must start with http:// or https://" ;; esac
fi
if [ "$ACTION" = scale ]; then
  [ "$NARGS" = 1 ] || refuse "scale takes one count, 0 or 1"
  case ${ARGS[0]} in
    0 | 1) ;;
    *[!0-9]* | '') refuse "scale takes 0 or 1, not '${ARGS[0]}'" ;;
    *) refuse "scale takes 0 or 1: Seamless Migrate runs a single control plane - its orchestrator is a singleton (SDD §17, D2), and a second replica would run two orchestrators against one database. To scale up, give the one control plane more capacity: $PLATFORM capacity --max-migrations N (docs/Operations.md)" ;;
  esac
elif [ "$ACTION" = resume ]; then
  [ "$NARGS" -ge 1 ] || refuse "resume takes the plans to start again: resume --token-file FILE PLAN_ID..."
  for id in "${ARGS[@]}"; do
    case $id in '' | .* | *[!A-Za-z0-9._-]*) refuse "'$id' is no plan id" ;; esac
  done
  [ -n "$TOKEN_FILE" ] || refuse "resume needs --token-file FILE, an operator's token (chmod 600)"
elif [ "$NARGS" != 0 ]; then
  bad_usage "$ACTION takes no argument '${ARGS[0]}'"
fi
if [ -n "$DRAIN" ]; then
  case $ACTION in down | stop | restart | capacity) ;; scale) [ "${ARGS[0]}" = 0 ] || refuse "--drain goes with scale 0" ;;
    *) refuse "--drain goes with down, stop, restart, scale 0 or capacity" ;; esac
fi
if [ "$DEMO" = 1 ]; then
  [ "$KIND" = host ] || refuse "--demo is the single-host demo stack (docker or podman)"
  case $ACTION in up | start | restart) ;; *) refuse "--demo goes with up, start or restart" ;; esac
fi
if [ "$ACTION" = capacity ]; then
  [ -n "$MAX_MIGRATIONS$MAX_CUTOVERS$CPU$MEMORY" ] || refuse "capacity needs --max-migrations, --max-cutovers, --cpu or --memory"
  for pair in "--max-migrations:$MAX_MIGRATIONS" "--max-cutovers:$MAX_CUTOVERS"; do
    value=${pair#*:}
    if [ -n "$value" ] && { ! is_count "$value" || [ "$value" -lt 1 ]; }; then
      refuse "${pair%%:*} takes a count of at least 1 (SDD §15.1), not '$value'"
    fi
  done
  if [ "$KIND" = host ] && [ -n "$CPU$MEMORY" ]; then
    refuse "on $PLATFORM the control plane's limits live in deploy/compose/compose.yaml (deploy.resources.limits); --cpu and --memory set a cluster's"
  fi
  case $CPU in '' | [0-9]* ) ;; *) refuse "--cpu takes a Kubernetes quantity such as 4 or 1500m" ;; esac
  case $CPU in *[!0-9.m]*) refuse "--cpu takes a Kubernetes quantity such as 4 or 1500m" ;; esac
  case $MEMORY in '' | [0-9]* ) ;; *) refuse "--memory takes a Kubernetes quantity such as 8Gi" ;; esac
  case $MEMORY in *[!0-9.KMGTi]*) refuse "--memory takes a Kubernetes quantity such as 8Gi" ;; esac
elif [ -n "$MAX_MIGRATIONS$MAX_CUTOVERS$CPU$MEMORY" ]; then
  refuse "--max-migrations, --max-cutovers, --cpu and --memory go with capacity"
fi

if [ "$KIND" = host ]; then
  # the port Compose publishes: the shell's, else deploy/compose/.env's read by Compose's rules, else 8080; a value
  # Compose would interpolate is refused, never guessed, where the API is asked (SDD §17.2, reviews RV38B-07,
  # RV41A-01, RV41A-02)
  if [ -n "${SEAMLESS_HOST_PORT+set}" ]; then
    port=$SEAMLESS_HOST_PORT
  else
    port=$(sh "$ROOT/scripts/dotenv-value.sh" SEAMLESS_HOST_PORT "$ROOT/deploy/compose/.env") || port='?'
  fi
  port=${port:-8080}
  if [ -z "$URL" ] && { [ -n "$DRAIN" ] || [ "$ACTION" = resume ] || [ "$ACTION" = status ]; }; then
    # restart and capacity end in the restart's Make target, whose Compose publishes this port: --url gets past
    # nothing there (SDD §17.2, reviews RV42A-04, RV43A-05)
    case $port in
      '?')
        case $ACTION in
          restart | capacity) refuse "cannot tell the port the stack publishes: give SEAMLESS_HOST_PORT a plain value" ;;
          *) refuse "cannot tell the port the stack publishes: give SEAMLESS_HOST_PORT a plain value, or --url" ;;
        esac
        ;;
      *)
        if ! is_port "$port"; then
          case $ACTION in
            restart | capacity) refuse "SEAMLESS_HOST_PORT is '$port', no port: give a number from 1 to 65535" ;;
            *) refuse "SEAMLESS_HOST_PORT is '$port', no port: give a number from 1 to 65535, or --url" ;;
          esac
        fi
        ;;
    esac
  fi
  API=${URL:-http://127.0.0.1:$port}
else
  API=$URL
  if [ -n "$DRAIN" ] || [ "$ACTION" = resume ]; then
    [ -n "$API" ] || refuse "on $PLATFORM the API is reached through the Route or Ingress: give its URL with --url"
  fi
fi

# --- the operator's token: a header file in a private directory, never a command line (SDD §13.1) -------------
HEADER=
open_token() {
  local file=$1 token='' judged readable
  case $- in *x*) refuse "xtrace (bash -x) is on and would print the operator's token: run without it" ;; esac
  [ -f "$file" ] && [ -r "$file" ] || refuse "cannot read the token file '$file'"
  # the mode of the file a symlink points to, never the link's own (-L; review RV38B-08); judged by its path - a name
  # like an option as ./-name -, and refused when find cannot tell (review RV41A-11)
  case $file in /*) judged=$file ;; *) judged=./$file ;; esac
  readable=$(find -L "$judged" -prune \( -perm -040 -o -perm -004 \) -print 2>/dev/null) ||
    refuse "cannot tell who may read the token file '$file'"
  [ -z "$readable" ] || refuse "the token file '$file' may be read by others: chmod 600 '$file'"
  if [ "$DRY" = 1 ]; then HEADER='@<private directory>/auth.header'; return; fi
  IFS= read -r token <"$file" || [ -n "$token" ] || refuse "the token file '$file' is empty"
  token=${token%$'\r'}
  [ -n "$token" ] || refuse "the token file '$file' is empty"
  PRIVATE=$(mktemp -d "${TMPDIR:-/tmp}/seamless-ctl.XXXXXX") || die "cannot create a private directory"
  trap 'rm -rf "$PRIVATE"' EXIT
  (umask 077 && printf 'Authorization: Bearer %s\n' "$token" >"$PRIVATE/auth.header")
  token=
  HEADER="@$PRIVATE/auth.header"
}
api() { run curl -fsS --max-time 30 "$@"; }
# A plan's pause, the HTTP status it got on stdout - 000 for none, curl's own failure its exit: curl -f's 22 is any
# status from 400, 5xx too, which may answer a pause applied (SDD §17.2, review RV44A-02).
pause_plan() {
  run curl -sS --max-time 30 -X POST -H "$HEADER" -o /dev/null -w '%{http_code}' "$API/api/v1/plans/$1/pause"
}
count_items() { python3 -c 'import json, sys; print(len(json.load(sys.stdin)))'; }

# Pause every running plan, then wait until no migration is in cutover, verifying or rolling_back (SDD §17.2). The plans
# it paused are named in PAUSED (defined with check_failed).
drain() {
  local me role plans ids id phase out busy deadline rc code
  if [ "$DRY" = 1 ]; then
    run curl -sS --max-time 30 -H "$HEADER" -w '\n%{http_code}' "$API/api/v1/me"
    say "# refused unless the API takes the token and it is an operator's or higher"
    api -H "$HEADER" "$API/api/v1/plans?status=running"
    say "# then, for each running plan:"
    pause_plan PLAN_ID
    say "# then wait until no migration is in cutover, verifying or rolling_back (at most ${WAIT_TIMEOUT}s)"
    return
  fi
  # the token's role before any plan is listed: a viewer's lists them, then every pause is refused; a token the API
  # does not take, or an API that cannot answer, is refused as well - nothing changed yet (SDD §17.2, reviews RV44A-02,
  # RV45A-07)
  rc=0
  me=$(curl -sS --max-time 30 -H "$HEADER" -w '\n%{http_code}' "$API/api/v1/me") || rc=$?
  code=$(printf '%s\n' "$me" | sed -n '$p')
  case $rc:$code in
    0:200) ;;
    0:401 | 0:403) refuse "the API at $API does not take this token (HTTP $code): give an operator's token or higher" ;;
    *) refuse "cannot ask the API at $API whose token this is (HTTP ${code:-000}): is the control plane up?" \
      "Nothing was changed" ;;
  esac
  role=$(printf '%s\n' "$me" | sed '$d' | python3 -c 'import json, sys; print(json.load(sys.stdin).get("role", ""))') ||
    refuse "cannot read the API's answer to GET /api/v1/me: nothing was changed"
  case $role in
    operator | approver | admin) ;;
    *) refuse "the token's role is '$role': a drain pauses plans, which takes an operator's token or higher" ;;
  esac
  plans=$(curl -fsS --max-time 30 -H "$HEADER" "$API/api/v1/plans?status=running") ||
    refuse "cannot list the running plans at $API (is the control plane up?): nothing was changed"
  ids=$(printf '%s' "$plans" | python3 -c '
import json, sys
for plan in json.load(sys.stdin):
    if plan.get("status") == "running":
        print(plan["id"])') || refuse "cannot read the plans the API returned: nothing was changed"
  # every way the drain ends before its stop - a failure, an interrupt - stops nothing, and the plans it paused stay
  # paused: named, with the way to resume them (SDD §17.2, reviews RV38B-05, RV41A-05)
  trap 'drain_failed "interrupted"' INT TERM
  for id in $ids; do
    case $id in
      '' | .* | *[!A-Za-z0-9._-]*) drain_failed "the API returned a plan id this script will not use: '$id'" ;;
    esac
    # named before its answer: a pause the API applied whose answer never came - a timeout, an interrupt - or came as
    # an error after it - a 5xx, a gateway's 502 or 504 - leaves the plan paused, and resume starts a running plan as
    # it is (SDD §17.2, reviews RV42A-06, RV44A-02). One the API refused - any 4xx, answered before anything was
    # applied: a plan no longer running, a token it no longer takes, a plan another writer keeps changing - paused
    # nothing, and resume would start a plan failed meanwhile again: it is named no more (reviews RV43A-04, RV45A-06).
    PAUSED+=("$id")
    rc=0
    code=$(pause_plan "$id") || rc=$?
    case $rc:$code in
      0:2[0-9][0-9]) ;;
      0:4[0-9][0-9])
        unset "PAUSED[$((${#PAUSED[@]} - 1))]"
        drain_failed "the API refused to pause plan $id (HTTP $code): it was not paused" ;;
      0:*) drain_failed "the API answered HTTP $code to the pause of plan $id - it may be paused all the same" ;;
      *) drain_failed "cannot pause plan $id - it may be paused all the same" ;;
    esac
  done
  if [ ${#PAUSED[@]} -gt 0 ]; then say "paused: ${PAUSED[*]}"; else say "no plan was running"; fi
  deadline=$(($(date +%s) + WAIT_TIMEOUT))
  while :; do
    busy=
    for phase in cutover verifying rolling_back; do
      out=$(curl -fsS --max-time 30 -H "$HEADER" "$API/api/v1/migrations?phase=$phase&view=summary&limit=1") ||
        drain_failed "cannot list the migrations at $API"
      [ "$(printf '%s' "$out" | count_items)" = 0 ] || busy="$busy $phase"
    done
    [ -n "$busy" ] || break
    [ "$(date +%s)" -lt "$deadline" ] || drain_failed "a migration is still in$busy after ${WAIT_TIMEOUT}s"
    say "waiting: a migration is in$busy"
    sleep 5
  done
  trap - INT TERM
  [ ${#PAUSED[@]} -eq 0 ] || say "the paused plans stay paused.$(resume_hint)"
}
drain_failed() { die "$1: nothing was stopped.$(resume_hint)"; }
resume_hint() {
  [ ${#PAUSED[@]} -gt 0 ] || return 0
  printf ' Resume them once the control plane runs: scripts/seamless-ctl.sh %s resume --token-file %s%s %s' \
    "$PLATFORM" "$DRAIN" "${URL:+ --url $URL}" "${PAUSED[*]}"
}

# --- the single-host stack: its Make targets (SDD §17.1) --------------------------------------------------------
# A Make target that fails is a failed step - exit 1, naming it -, never make's own 2, a refusal's (SDD §17.2, RV43A-07).
target() { run make -C "$ROOT" --no-print-directory "$1" "SEAMLESS_ENGINE=$PLATFORM" ${2+"${@:2}"} || die "make $1 failed (exit $?)"; }

host_capacity() {
  local env=$ROOT/deploy/compose/.env kv key line tmp settings=()
  [ -n "$MAX_MIGRATIONS" ] && settings+=("SEAMLESS_MAX_CONCURRENT_MIGRATIONS=$MAX_MIGRATIONS")
  [ -n "$MAX_CUTOVERS" ] && settings+=("SEAMLESS_MAX_CONCURRENT_CUTOVERS=$MAX_CUTOVERS")
  capacity_check
  if [ -n "$MAX_MIGRATIONS" ] && [ "$MAX_MIGRATIONS" -gt 10 ]; then
    printf '%s: each concurrent migration needs about 250 MiB (Performance.md §8); the control plane'"'"'s memory limit in deploy/compose/compose.yaml (3g) is sized for 10 - raise deploy.resources.limits.memory there for %s\n' \
      "$PROG" "$MAX_MIGRATIONS" >&2
  fi
  if [ "$DRY" = 1 ]; then
    for kv in "${settings[@]}"; do say "# set $kv in deploy/compose/.env"; done
  else
    tmp=$(mktemp "$env.XXXXXX") || die "cannot write next to $env"
    while IFS= read -r line || [ -n "$line" ]; do
      for kv in "${settings[@]}"; do
        key=${kv%%=*}
        case $line in "$key="*) line=$kv ;; esac
      done
      printf '%s\n' "$line"
    done <"$env" >"$tmp"
    for kv in "${settings[@]}"; do
      key=${kv%%=*}
      grep -q "^$key=" "$tmp" || printf '%s\n' "$kv" >>"$tmp"
    done
    mv -f "$tmp" "$env"
    for kv in "${settings[@]}"; do say "set $kv in deploy/compose/.env"; done
  fi
  target seamless-restart
}

host() {
  local mode=()
  [ "$DEMO" = 1 ] && mode=(SEAMLESS_DEMO=true)
  need make "install GNU make"
  case $ACTION in
    up) if [ "$DEMO" = 1 ]; then target seamless-demo; else target seamless-up; fi ;;
    down) target seamless-down ;;
    stop) target seamless-stop ;;
    start) target seamless-start ${mode[@]+"${mode[@]}"} ;;
    restart) target seamless-restart ${mode[@]+"${mode[@]}"} ;;
    scale) if [ "${ARGS[0]}" = 0 ]; then target seamless-stop; else target seamless-start; fi ;;
    capacity) host_capacity ;;
    status)
      target seamless-ps
      need curl "install curl"
      run curl -fsS --max-time 10 "$API/api/v1/health" || die "the API does not answer at $API"
      say ""
      ;;
    logs) target seamless-logs SEAMLESS_SERVICE=seamless ;;
  esac
}

# --- a cluster: the manifests through kubectl or oc (SDD §17) ---------------------------------------------------
kube() { run "$KUBE" -n "$NS" "$@"; }
# Wait until no running pod of the named app is left - none at all needs no wait. A pod that failed or succeeded (one
# evicted) stays until the pod GC removes it, never by the scale-down: it is not waited for (SDD §17.2, review RV38B-03).
ACTIVE='status.phase!=Failed,status.phase!=Succeeded'
wait_gone() {
  local selector="app.kubernetes.io/name=$1" fields=$ACTIVE${2:+,$2} pods
  if [ "$DRY" = 1 ]; then
    kube wait --for=delete pod -l "$selector" "--field-selector=$fields" "--timeout=${TIMEOUT}s"
    return
  fi
  pods=$("$KUBE" -n "$NS" get pods -l "$selector" "--field-selector=$fields" -o name)
  [ -n "$pods" ] || return 0
  [ -z "${3-}" ] || "$3" "$pods" # what is waited for, said first
  "$KUBE" -n "$NS" wait --for=delete pod -l "$selector" "--field-selector=$fields" "--timeout=${TIMEOUT}s"
}
# PostgreSQL's pods but seamless-postgres-0 gone before a control plane starts: each replica is a database of its own
# (a PVC each) behind the one headless Service the control plane connects to, so the others - scaled out of band - are
# judged by their pods, never by the replica count alone, waited for and named, their PVCs kept (SDD §17.2, reviews
# RV38B-15, RV42A-01, RV42A-02).
postgres_others_gone() { wait_gone seamless-postgres metadata.name!=seamless-postgres-0 postgres_others; }
postgres_others() {
  local pod n=1 kept=''
  for pod in $1; do
    kept="$kept data-${pod#pod/}"
    n=$((n + 1))
  done
  printf '%s: PostgreSQL ran %s replicas, each a database of its own behind one Service: back to one, seamless-postgres-0; what the others hold stays in their PVCs:%s\n' \
    "$PROG" "$n" "$kept" >&2
}
rollout() { kube rollout status "$1" "--timeout=${TIMEOUT}s"; }
control_plane_to() {
  kube scale deployment/seamless "--replicas=$1"
  if [ "$1" = 0 ]; then wait_gone seamless; else rollout deployment/seamless; fi
}
# Before an up's apply, PostgreSQL scaled out of band back to one replica and its other pods awaited gone, judged by the
# pods as start judges them: the apply may roll the control plane's pod at once - a new image in the overlay - onto
# every database. A namespace without the StatefulSet yet (a first up), which its read finds not there, has none; any
# other failure of that read stops up before the apply (SDD §17.2, reviews RV43A-01, RV44A-04).
postgres_one_before_apply() {
  local replicas
  if [ "$DRY" = 1 ]; then
    say "# when PostgreSQL's StatefulSet exists and runs more than one replica:"
    kube scale statefulset/seamless-postgres --replicas=1
    postgres_others_gone
    return
  fi
  replicas=$("$KUBE" -n "$NS" get statefulset/seamless-postgres --ignore-not-found -o 'jsonpath={.spec.replicas}') ||
    check_failed "cannot read statefulset/seamless-postgres in namespace $NS: nothing was changed"
  [ -n "$replicas" ] || return 0 # a first up
  if is_count "$replicas" && [ "$replicas" -gt 1 ]; then kube scale statefulset/seamless-postgres --replicas=1; fi
  postgres_others_gone
}
cluster_start() {
  local replicas=0
  if [ "$DRY" = 1 ]; then
    say "# when PostgreSQL is not at 1 replica:"
  else
    # a namespace without it and a read that failed are refusals, told apart (SDD §17.2, review RV44A-08)
    replicas=$("$KUBE" -n "$NS" get statefulset/seamless-postgres --ignore-not-found -o 'jsonpath={.spec.replicas}') ||
      check_failed "cannot read statefulset/seamless-postgres in namespace $NS: nothing was changed"
    [ -n "$replicas" ] || check_failed "no StatefulSet seamless-postgres in namespace $NS: run up first"
    is_count "$replicas" || die "seamless-postgres reports '$replicas' replicas"
  fi
  if [ "$replicas" != 1 ]; then
    kube scale statefulset/seamless-postgres --replicas=1
    rollout statefulset/seamless-postgres
  fi
  postgres_others_gone # rollout status does not wait for a scale-down
  control_plane_to 1
}
# A cluster's capacity: the ConfigMap's settings, and one new pod template - a restart annotation, as rollout restart
# writes it, and the limits - so the pod restarts once and reads the settings as it starts, also when the limits stay
# as they are (SDD §17.2, review RV38B-01).
CAP_DATA='' CAP_LIMITS='' CAP_PATCH=''
capacity_patch() {
  local template
  CAP_DATA='' CAP_LIMITS=''
  [ -n "$MAX_MIGRATIONS" ] && CAP_DATA="\"SEAMLESS_MAX_CONCURRENT_MIGRATIONS\":\"$MAX_MIGRATIONS\""
  [ -n "$MAX_CUTOVERS" ] && CAP_DATA="${CAP_DATA:+$CAP_DATA,}\"SEAMLESS_MAX_CONCURRENT_CUTOVERS\":\"$MAX_CUTOVERS\""
  [ -n "$CPU" ] && CAP_LIMITS="\"cpu\":\"$CPU\""
  [ -n "$MEMORY" ] && CAP_LIMITS="${CAP_LIMITS:+$CAP_LIMITS,}\"memory\":\"$MEMORY\""
  template="\"metadata\":{\"annotations\":{\"kubectl.kubernetes.io/restartedAt\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}"
  [ -z "$CAP_LIMITS" ] ||
    template="$template,\"spec\":{\"containers\":[{\"name\":\"seamless\",\"resources\":{\"limits\":{$CAP_LIMITS}}}]}"
  CAP_PATCH="{\"spec\":{\"template\":{$template}}}"
}
# What would refuse a capacity, checked before anything changes - and before a drain pauses a plan, so a refused
# capacity leaves every plan running (SDD §17.2, reviews RV38B-22, RV43A-06): on the single host what its restart
# needs; on a cluster the Deployment and the ConfigMap read - a mistyped namespace or an expired login shows here -
# and its limits, which the API server refuses below the requests, by a server-side dry run (review RV38B-02).
capacity_check() {
  if [ "$KIND" = host ]; then
    host_restart_check
  else
    need "$KUBE" "install it and log in to the cluster"
    capacity_patch
    if [ "$DRY" != 1 ]; then
      "$KUBE" -n "$NS" get deployment/seamless -o name >/dev/null ||
        check_failed "cannot read deployment/seamless in namespace $NS: nothing was changed"
      [ -z "$CAP_DATA" ] || capacity_settings_now >/dev/null ||
        check_failed "cannot read seamless-config in namespace $NS: nothing was changed"
    fi
    # its writes, dry-run on the server: the settings' patch, and the template's - its limits, which the API server
    # refuses below the requests, and a login that may read but not write (SDD §17.2, reviews RV38B-02, RV45A-02)
    if [ -n "$CAP_DATA" ]; then
      kube patch configmap/seamless-config --type merge -p "{\"data\":{$CAP_DATA}}" --dry-run=server -o name ||
        check_failed "$KUBE may not patch configmap/seamless-config in namespace $NS (a server-side dry run" \
          "refused it): nothing was changed"
    fi
    if [ -n "$CAP_LIMITS" ]; then
      kube patch deployment/seamless -p "$CAP_PATCH" --dry-run=server -o name ||
        check_failed "the API server refuses those limits (a server-side dry run): nothing was changed"
    else
      kube patch deployment/seamless -p "$CAP_PATCH" --dry-run=server -o name ||
        check_failed "$KUBE may not patch deployment/seamless in namespace $NS (a server-side dry run refused" \
          "it): nothing was changed"
    fi
  fi
}
# What the single host's restart - capacity's last step - needs, which its Make target refuses: make, deploy/compose/.env
# and tokens.yaml (seamless-check-env), and a SEAMLESS_HOST_PORT it reads - one Compose would interpolate it refuses
# (SEAMLESS_DOTENV_STOP) - and Compose publishes: 1 to 65535; the shell's own port wins there, as here. Checked before a
# drain, so no plan is paused for a restart that would stop (SDD §17.2, reviews RV42A-04, RV43A-05, RV43A-06,
# RV44A-06).
host_restart_check() {
  need make "install GNU make"
  [ -f "$ROOT/deploy/compose/.env" ] || check_failed "deploy/compose/.env is missing: run make seamless-init first"
  [ -f "$ROOT/deploy/compose/tokens.yaml" ] ||
    check_failed "deploy/compose/tokens.yaml is missing: run make seamless-init first"
  host_port_check restart
}
# The port every Compose command of the single host loads with the whole project - stop and down too -: the shell's,
# else deploy/compose/.env's read as Compose reads it, else 8080 - down needs no .env -, a port number from 1 to 65535
# (SDD §17.2, reviews RV44A-06, RV45A-01). A value Compose would interpolate only Compose can judge: the restart's Make
# target refuses it (SEAMLESS_DOTENV_STOP), so the restart's check does; a stop's Compose reads it.
host_port_check() {
  local value=''
  if [ -n "${SEAMLESS_HOST_PORT+set}" ]; then
    value=$SEAMLESS_HOST_PORT
  elif [ -f "$ROOT/deploy/compose/.env" ]; then
    if ! value=$(sh "$ROOT/scripts/dotenv-value.sh" SEAMLESS_HOST_PORT "$ROOT/deploy/compose/.env"); then
      [ "${1-}" = restart ] || return 0
      check_failed "the restart's Make target cannot read SEAMLESS_HOST_PORT in deploy/compose/.env: give it a" \
        "plain value; nothing was changed"
    fi
  fi
  is_port "${value:-8080}" ||
    check_failed "SEAMLESS_HOST_PORT is '$value', no port Compose publishes: give a number from 1 to 65535;" \
      "nothing was changed"
}
# What an action's first steps need, checked before its drain pauses a plan, so a refused action leaves every plan
# running (SDD §17.2, reviews RV43A-06, RV44A-01): capacity's own checks; on the single host make, the restart's port
# and the Make targets' own read-only prerequisites - the Docker context or Podman, and .env and tokens.yaml unless
# the action is down -; on a cluster kubectl or oc and a read of the Deployment - and of the StatefulSet down scales -,
# where a mistyped namespace or an expired login shows.
drain_check() {
  local checks=seamless-check-context
  if [ "$KIND" = host ]; then
    need make "install GNU make"
    [ "$ACTION" = down ] || checks="$checks seamless-check-env"
    case $ACTION in restart | capacity) host_restart_check ;; *) host_port_check ;; esac
    # shellcheck disable=SC2086 # the target names, one word each
    run make -C "$ROOT" --no-print-directory $checks "SEAMLESS_ENGINE=$PLATFORM" ||
      check_failed "make $checks refused (exit $?): nothing was changed"
  elif [ "$ACTION" = capacity ]; then
    capacity_check
  else
    need "$KUBE" "install it and log in to the cluster"
    [ "$DRY" != 1 ] || return 0
    "$KUBE" -n "$NS" get deployment/seamless -o name >/dev/null ||
      check_failed "cannot read deployment/seamless in namespace $NS: nothing was changed"
    [ "$ACTION" != down ] || "$KUBE" -n "$NS" get statefulset/seamless-postgres -o name >/dev/null ||
      check_failed "cannot read statefulset/seamless-postgres in namespace $NS: nothing was changed"
    # the writes the action starts with, dry-run on the server: a login that may read but not write - the view role -
    # shows here, before a plan is paused (SDD §17.2, review RV45A-02)
    case $ACTION in
      restart)
        if [ "$("$KUBE" -n "$NS" get deployment/seamless -o 'jsonpath={.spec.replicas}')" = 0 ]; then
          dry_write "scale deployment/seamless" scale deployment/seamless --replicas=1
        else
          # what rollout restart patches - it has no --dry-run of its own
          dry_write "restart deployment/seamless" patch deployment/seamless \
            -p '{"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":"dry-run"}}}}}'
        fi
        ;;
      *)
        dry_write "scale deployment/seamless" scale deployment/seamless --replicas=0
        [ "$ACTION" != down ] ||
          dry_write "scale statefulset/seamless-postgres" scale statefulset/seamless-postgres --replicas=0
        ;;
    esac
  fi
}
# A write dry-run on the API server - its authorization and admission, nothing applied -, refused while nothing changed.
dry_write() {
  local what=$1
  shift
  "$KUBE" -n "$NS" "$@" --dry-run=server -o name >/dev/null ||
    check_failed "$KUBE may not $what in namespace $NS (a server-side dry run refused it): nothing was changed"
}
cluster_capacity() {
  if [ -n "$MAX_MIGRATIONS" ] && [ "$MAX_MIGRATIONS" -gt 10 ] && [ -z "$MEMORY" ]; then
    printf '%s: each concurrent migration needs about 250 MiB (Performance.md §8); the control plane'"'"'s memory limit in deploy/openshift/deployment.yaml (4Gi) is sized for 10 - give --memory for %s\n' \
      "$PROG" "$MAX_MIGRATIONS" >&2
  fi
  capacity_check # again after a drain, which may have taken --wait-timeout
  local before=''
  if [ -n "$CAP_DATA" ]; then
    if [ "$DRY" != 1 ]; then
      before=$(capacity_settings_now) || die "cannot read seamless-config: nothing was changed"
    fi
    kube patch configmap/seamless-config --type merge -p "{\"data\":{$CAP_DATA}}"
  fi
  # a template patch or a rollout that fails puts the settings and the template back, so the control plane runs
  # again as it was - a LimitRange or a quota judges the pod, which no dry run of the Deployment shows (SDD §17.2,
  # reviews RV41A-06, RV41A-07)
  kube patch deployment/seamless -p "$CAP_PATCH" || capacity_failed "the pod template could not be patched"
  rollout deployment/seamless || capacity_failed "the new pod did not start - a LimitRange or a quota may refuse \
its limits ($KUBE -n $NS describe replicaset -l app.kubernetes.io/name=seamless)" undo
  say "the next 'up' (apply -k) brings the manifests' values back: put a lasting change into $OVERLAY"
}
# The settings seamless-config holds for the keys capacity changes, as merge-patch data - a key it lacks as null, which
# removes it again (SDD §17.2, review RV41A-06).
capacity_settings_now() {
  local key value data=''
  for key in SEAMLESS_MAX_CONCURRENT_MIGRATIONS SEAMLESS_MAX_CONCURRENT_CUTOVERS; do
    case $CAP_DATA in *"\"$key\""*) ;; *) continue ;; esac
    value=$("$KUBE" -n "$NS" get configmap/seamless-config -o "go-template={{index .data \"$key\"}}") || return 1
    case $value in
      '<no value>' | '') value=null ;;
      *) value="\"$(printf '%s' "$value" | sed 's/[\\"]/\\&/g')\"" ;;
    esac
    data="${data:+$data,}\"$key\":$value"
  done
  printf '%s' "$data"
}
# Put back what a failed capacity changed, then stop: the ConfigMap's settings (cluster_capacity's $before) first, so
# the pod the undo starts reads them as they were, and after a rollout the template - all the same when the put-back
# fails, which is named with what the settings then need: put back by hand, then after an undo done a restart once
# its pod is available - that pod read the new ones -, after an undo that failed before the rollback by hand, whose
# pod reads them (SDD §17.2, reviews RV41A-06, RV41A-07, RV42A-03, RV43A-03, RV44A-07). The undo is awaited by the
# Deployment becoming Available: its Progressing condition keeps a passed deadline after an undo, which rollout status
# reads as failed at once (review RV42A-07). An undo done whose pod is not Available is said as such: after an undo
# the newest revision is the restored one, so a revision is advised only when the undo itself failed - never a second
# undo (review RV43A-02).
capacity_failed() {
  local left='' kept=''
  if [ -n "$before" ]; then
    kube patch configmap/seamless-config --type merge -p "{\"data\":{$before}}" ||
      kept="seamless-config could not be put back: set {$before} in it yourself"
  fi
  if [ "${2-}" = undo ]; then
    if ! kube rollout undo deployment/seamless; then
      left="the template was not rolled back: find the revision before this capacity with $KUBE -n $NS rollout \
history deployment/seamless and roll back to it (--to-revision)"
      [ -z "$kept" ] || { kept="$kept first"; left="$left - the pod it starts reads the settings put back"; }
    elif ! kube wait --for=condition=Available deployment/seamless "--timeout=${TIMEOUT}s"; then
      left="the template is back as it was, but its pod is not available after ${TIMEOUT}s - see $KUBE -n $NS \
describe replicaset -l app.kubernetes.io/name=seamless; roll back no further"
      [ -z "$kept" ] || kept="$kept, then, once the control plane's pod is available, scripts/seamless-ctl.sh \
$PLATFORM restart: a pod that started before then read the new ones"
    else
      [ -z "$kept" ] || kept="$kept, then scripts/seamless-ctl.sh $PLATFORM restart: the pod the undo started read \
the new ones"
    fi
  fi
  [ -z "$kept$left" ] || die "$1, and $kept${kept:+${left:+; }}$left"
  die "$1: seamless-config and the pod template are put back as they were"
}

cluster() {
  need "$KUBE" "install it and log in to the cluster"
  case $ACTION in
    up)
      postgres_one_before_apply
      kube apply -k "$ROOT/$OVERLAY"
      # an apply restores no replica count an earlier down set (SDD §17.2)
      kube scale statefulset/seamless-postgres --replicas=1
      rollout statefulset/seamless-postgres
      postgres_others_gone # rollout status does not wait for a scale-down
      control_plane_to 1
      say "after a change to the ConfigMap or a Secret alone, run: scripts/seamless-ctl.sh $PLATFORM restart"
      ;;
    down)
      control_plane_to 0
      kube scale statefulset/seamless-postgres --replicas=0
      wait_gone seamless-postgres
      ;;
    stop) control_plane_to 0 ;;
    start) cluster_start ;;
    restart)
      # a control plane stopped at 0 replicas starts, as the single host's restart creates and starts it (SDD §17.2,
      # review RV38B-04)
      if [ "$DRY" != 1 ] && [ "$("$KUBE" -n "$NS" get deployment/seamless -o 'jsonpath={.spec.replicas}')" = 0 ]; then
        control_plane_to 1
      else
        [ "$DRY" != 1 ] || say "# when the control plane is at 0 replicas: scale it to 1 instead"
        kube rollout restart deployment/seamless # Recreate: the old pod ends before the new one starts
        rollout deployment/seamless
      fi
      ;;
    scale) if [ "${ARGS[0]}" = 0 ]; then control_plane_to 0; else cluster_start; fi ;;
    capacity) cluster_capacity ;;
    status)
      kube get deployment/seamless statefulset/seamless-postgres
      kube get pods -l 'app.kubernetes.io/name in (seamless,seamless-postgres)'
      if [ -n "$API" ]; then
        run curl -fsS --max-time 10 "$API/api/v1/health" || die "the API does not answer at $API"
        say ""
      fi
      ;;
    logs) kube logs deployment/seamless -f --tail=200 ;;
  esac
}

# --- main ---------------------------------------------------------------------------------------------------
if [ -n "$DRAIN" ] || [ "$ACTION" = resume ]; then
  need curl "install curl"
  need python3 "--drain and resume read the API's JSON with python3"
  open_token "${DRAIN:-$TOKEN_FILE}"
fi
if [ "$ACTION" = resume ]; then
  # every plan given: one Start refuses (409: cancelled meanwhile, a migration blocked) is named, the others start all
  # the same (SDD §17.2, review RV38B-14)
  refused=0
  for id in "${ARGS[@]}"; do
    if api -X POST -H "$HEADER" -o /dev/null "$API/api/v1/plans/$id/start"; then
      [ "$DRY" = 1 ] || say "started: $id" # a dry run sent nothing (SDD §17.2, review RV44A-09)
    else
      printf '%s: cannot start plan %s: see it in the dashboard\n' "$PROG" "$id" >&2
      refused=1
    fi
  done
  exit "$refused"
fi
if [ -n "$DRAIN" ]; then
  drain_check
  drain
fi
if [ "$KIND" = host ]; then host; else cluster; fi
