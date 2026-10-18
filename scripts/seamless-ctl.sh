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
  start              start the control plane again (PostgreSQL first when it is down)
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
  --drain FILE       before down, stop, restart or scale 0: pause every running plan and wait until no
                     migration is in cutover, verifying or rolling_back; FILE holds an operator's token
                     and may be read by its owner alone (chmod 600)
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
need() { [ "$DRY" = 1 ] || command -v "$1" >/dev/null 2>&1 || die "$1 is needed for this action: $2"; }
is_count() { case $1 in '' | *[!0-9]*) return 1 ;; esac; }

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
  case $ACTION in down | stop | restart) ;; scale) [ "${ARGS[0]}" = 0 ] || refuse "--drain goes with scale 0" ;;
    *) refuse "--drain goes with down, stop, restart or scale 0" ;; esac
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
  # the port Compose publishes: the shell's, else deploy/compose/.env's, else 8080 (SDD §17.2, review RV38B-07)
  port=${SEAMLESS_HOST_PORT-$(sh "$ROOT/scripts/dotenv-value.sh" SEAMLESS_HOST_PORT "$ROOT/deploy/compose/.env")}
  API=${URL:-http://127.0.0.1:${port:-8080}}
else
  API=$URL
  if [ -n "$DRAIN" ] || [ "$ACTION" = resume ]; then
    [ -n "$API" ] || refuse "on $PLATFORM the API is reached through the Route or Ingress: give its URL with --url"
  fi
fi

# --- the operator's token: a header file in a private directory, never a command line (SDD §13.1) -------------
HEADER=
open_token() {
  local file=$1 token=
  case $- in *x*) refuse "xtrace (bash -x) is on and would print the operator's token: run without it" ;; esac
  [ -f "$file" ] && [ -r "$file" ] || refuse "cannot read the token file '$file'"
  # the mode of the file a symlink points to, never the link's own (-L; review RV38B-08)
  if [ -n "$(find -L "$file" -prune \( -perm -040 -o -perm -004 \) -print 2>/dev/null)" ]; then
    refuse "the token file '$file' may be read by others: chmod 600 '$file'"
  fi
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
count_items() { python3 -c 'import json, sys; print(len(json.load(sys.stdin)))'; }

# Pause every running plan, then wait until no migration is in cutover, verifying or rolling_back (SDD §17.2).
PAUSED=()
drain() {
  local plans ids id phase out busy deadline
  if [ "$DRY" = 1 ]; then
    api -H "$HEADER" "$API/api/v1/plans?status=running"
    say "# then, for each running plan:"
    api -X POST -H "$HEADER" -o /dev/null "$API/api/v1/plans/PLAN_ID/pause"
    say "# then wait until no migration is in cutover, verifying or rolling_back (at most ${WAIT_TIMEOUT}s)"
    return
  fi
  plans=$(curl -fsS --max-time 30 -H "$HEADER" "$API/api/v1/plans?status=running") ||
    die "cannot list the running plans at $API (is the control plane up, and the token an operator's?)"
  ids=$(printf '%s' "$plans" | python3 -c '
import json, sys
for plan in json.load(sys.stdin):
    if plan.get("status") == "running":
        print(plan["id"])') || die "cannot read the plans the API returned"
  for id in $ids; do
    case $id in '' | .* | *[!A-Za-z0-9._-]*) die "the API returned a plan id this script will not use: '$id'" ;; esac
    curl -fsS --max-time 30 -X POST -H "$HEADER" -o /dev/null "$API/api/v1/plans/$id/pause" ||
      die "cannot pause plan $id"
    PAUSED+=("$id")
  done
  if [ ${#PAUSED[@]} -gt 0 ]; then say "paused: ${PAUSED[*]}"; else say "no plan was running"; fi
  deadline=$(($(date +%s) + WAIT_TIMEOUT))
  while :; do
    busy=
    for phase in cutover verifying rolling_back; do
      out=$(curl -fsS --max-time 30 -H "$HEADER" "$API/api/v1/migrations?phase=$phase&view=summary&limit=1") ||
        die "cannot list the migrations at $API"
      [ "$(printf '%s' "$out" | count_items)" = 0 ] || busy="$busy $phase"
    done
    [ -n "$busy" ] || break
    if [ "$(date +%s)" -ge "$deadline" ]; then
      printf '%s: a migration is still in%s after %ss: nothing was stopped.%s\n' "$PROG" "$busy" "$WAIT_TIMEOUT" \
        "$(resume_hint)" >&2
      exit 1
    fi
    say "waiting: a migration is in$busy"
    sleep 5
  done
  [ ${#PAUSED[@]} -eq 0 ] || say "the paused plans stay paused.$(resume_hint)"
}
resume_hint() {
  [ ${#PAUSED[@]} -gt 0 ] || return 0
  printf ' Resume them once the control plane runs: scripts/seamless-ctl.sh %s resume --token-file %s%s %s' \
    "$PLATFORM" "$DRAIN" "${URL:+ --url $URL}" "${PAUSED[*]}"
}

# --- the single-host stack: its Make targets (SDD §17.1) --------------------------------------------------------
target() { run make -C "$ROOT" --no-print-directory "$1" "SEAMLESS_ENGINE=$PLATFORM" ${2+"${@:2}"}; }

host_capacity() {
  local env=$ROOT/deploy/compose/.env kv key line tmp settings=()
  [ -n "$MAX_MIGRATIONS" ] && settings+=("SEAMLESS_MAX_CONCURRENT_MIGRATIONS=$MAX_MIGRATIONS")
  [ -n "$MAX_CUTOVERS" ] && settings+=("SEAMLESS_MAX_CONCURRENT_CUTOVERS=$MAX_CUTOVERS")
  [ -f "$env" ] || die "deploy/compose/.env is missing: run make seamless-init first"
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
  local selector="app.kubernetes.io/name=$1" pods
  if [ "$DRY" = 1 ]; then
    kube wait --for=delete pod -l "$selector" "--field-selector=$ACTIVE" "--timeout=${TIMEOUT}s"
    return
  fi
  pods=$("$KUBE" -n "$NS" get pods -l "$selector" "--field-selector=$ACTIVE" -o name)
  [ -z "$pods" ] ||
    "$KUBE" -n "$NS" wait --for=delete pod -l "$selector" "--field-selector=$ACTIVE" "--timeout=${TIMEOUT}s"
}
rollout() { kube rollout status "$1" "--timeout=${TIMEOUT}s"; }
control_plane_to() {
  kube scale deployment/seamless "--replicas=$1"
  if [ "$1" = 0 ]; then wait_gone seamless; else rollout deployment/seamless; fi
}
cluster_start() {
  local replicas=0
  if [ "$DRY" = 1 ]; then
    say "# when PostgreSQL is at 0 replicas:"
  else
    replicas=$("$KUBE" -n "$NS" get statefulset/seamless-postgres -o 'jsonpath={.spec.replicas}') ||
      die "no StatefulSet seamless-postgres in namespace $NS: run up first"
  fi
  if [ "${replicas:-0}" = 0 ]; then
    kube scale statefulset/seamless-postgres --replicas=1
    rollout statefulset/seamless-postgres
  fi
  control_plane_to 1
}
cluster_capacity() {
  local data='' limits='' template patch
  if [ -n "$MAX_MIGRATIONS" ] && [ "$MAX_MIGRATIONS" -gt 10 ] && [ -z "$MEMORY" ]; then
    printf '%s: each concurrent migration needs about 250 MiB (Performance.md §8); the control plane'"'"'s memory limit in deploy/openshift/deployment.yaml (4Gi) is sized for 10 - give --memory for %s\n' \
      "$PROG" "$MAX_MIGRATIONS" >&2
  fi
  [ -n "$MAX_MIGRATIONS" ] && data="\"SEAMLESS_MAX_CONCURRENT_MIGRATIONS\":\"$MAX_MIGRATIONS\""
  [ -n "$MAX_CUTOVERS" ] && data="${data:+$data,}\"SEAMLESS_MAX_CONCURRENT_CUTOVERS\":\"$MAX_CUTOVERS\""
  [ -n "$CPU" ] && limits="\"cpu\":\"$CPU\""
  [ -n "$MEMORY" ] && limits="${limits:+$limits,}\"memory\":\"$MEMORY\""
  # one new pod template - a restart annotation, as rollout restart writes it, and the limits - so the pod restarts once
  # and reads the settings as it starts, also when the limits stay as they are (SDD §17.2, review RV38B-01)
  template="\"metadata\":{\"annotations\":{\"kubectl.kubernetes.io/restartedAt\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}"
  [ -z "$limits" ] ||
    template="$template,\"spec\":{\"containers\":[{\"name\":\"seamless\",\"resources\":{\"limits\":{$limits}}}]}"
  patch="{\"spec\":{\"template\":{$template}}}"
  # limits the API server refuses - below the requests - change nothing: checked before the ConfigMap (review RV38B-02)
  [ -z "$limits" ] || kube patch deployment/seamless -p "$patch" --dry-run=server
  [ -n "$data" ] && kube patch configmap/seamless-config --type merge -p "{\"data\":{$data}}"
  kube patch deployment/seamless -p "$patch"
  rollout deployment/seamless
  say "the next 'up' (apply -k) brings the manifests' values back: put a lasting change into $OVERLAY"
}

cluster() {
  need "$KUBE" "install it and log in to the cluster"
  case $ACTION in
    up)
      kube apply -k "$ROOT/$OVERLAY"
      # an apply restores no replica count an earlier down set (SDD §17.2)
      kube scale statefulset/seamless-postgres --replicas=1
      rollout statefulset/seamless-postgres
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
  for id in "${ARGS[@]}"; do
    api -X POST -H "$HEADER" -o /dev/null "$API/api/v1/plans/$id/start" || die "cannot start plan $id"
    say "started: $id"
  done
  exit 0
fi
[ -z "$DRAIN" ] || drain
if [ "$KIND" = host ]; then host; else cluster; fi
