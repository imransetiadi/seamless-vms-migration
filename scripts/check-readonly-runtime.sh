#!/usr/bin/env bash
# Runtime check of the cluster security context (QASuite §14.5, Security.md §9.1).
#
# Static checks (kubeconform, trivy config) cannot see what an image writes when it starts: the
# PostgreSQL StatefulSet passed them and still exited 1 under readOnlyRootFilesystem. This script runs
# the images the way the manifests do — read-only root filesystem, all capabilities dropped, no new
# privileges, the manifests' own writable mounts (emptyDir -> tmpfs, PVC -> a scratch volume, Secrets
# left out) — once with OpenShift's arbitrary UID and once with the UID the Kubernetes overlay sets:
#
#   * PostgreSQL (always): the image pinned in deploy/kubernetes/kustomization.yaml with the mounts of
#     deploy/openshift/postgres-statefulset.yaml; ready when /usr/libexec/check-container passes.
#   * the control plane (when CP_IMAGE is set): the ConfigMap's settings in demo mode with the
#     Deployment's literal env and mounts; ready when /api/v1/health answers "ok", then Ansible runs
#     with the executor's ANSIBLE_HOME under the data directory.
#
#   scripts/check-readonly-runtime.sh
#   DOCKER="docker --context colima-seamless" CP_IMAGE=seamless-migrate:0.1.0 scripts/check-readonly-runtime.sh
#   STATEFULSET=old/postgres-statefulset.yaml scripts/check-readonly-runtime.sh   # check another manifest
#
# Exit status: the number of failed checks. Needs python3 with PyYAML (PYTHON=… to choose one).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
read -r -a DOCKER_CMD <<<"${DOCKER:-docker}"
PYTHON=${PYTHON:-python3}
STATEFULSET=${STATEFULSET:-$ROOT/deploy/openshift/postgres-statefulset.yaml}
DEPLOYMENT=${DEPLOYMENT:-$ROOT/deploy/openshift/deployment.yaml}
CONFIGMAP=${CONFIGMAP:-$ROOT/deploy/openshift/configmap.yaml}
OVERLAY=${OVERLAY:-$ROOT/deploy/kubernetes/kustomization.yaml}
WAIT_S=${WAIT_S:-90}
NAME=seamless-rocheck-$$
failures=0

d() { "${DOCKER_CMD[@]}" "$@"; }

cleanup() {
  d rm -fv "$NAME" >/dev/null 2>&1 || true
  d volume rm "$NAME-data" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Print the docker run arguments a manifest implies, one per line (python3 + PyYAML).
manifest_args() {
  "$PYTHON" - "$@" <<'PY'
import sys
import yaml

kind, path = sys.argv[1], sys.argv[2]
docs = [d for d in yaml.safe_load_all(open(path)) if d]
obj = next(d for d in docs if d["kind"] == kind)
pod = obj["spec"]["template"]["spec"]
container = pod["containers"][0]
volumes = {v["name"]: v for v in pod.get("volumes", [])}
claims = {t["metadata"]["name"] for t in obj["spec"].get("volumeClaimTemplates", [])}
secret_paths = []
for mount in container["volumeMounts"]:
    source = volumes.get(mount["name"], {})
    if mount["name"] in claims or "persistentVolumeClaim" in source:
        print(f"--volume=@DATA@:{mount['mountPath']}")
    elif "emptyDir" in source:
        print(f"--tmpfs={mount['mountPath']}:mode=1777")
    else:  # Secrets and ConfigMaps hold no writable state
        secret_paths.append(mount["mountPath"])
for env in container.get("env", []):
    value = env.get("value")
    # only literal values that exist outside a cluster: no paths into Secret mounts, no Kubernetes
    # secret store (it needs the API server)
    if value is None or any(value.startswith(p) for p in secret_paths) or value == "kubernetes":
        continue
    print(f"--env={env['name']}={value}")
if kind == "Deployment" and len(sys.argv) > 3:
    configmap = next(d for d in yaml.safe_load_all(open(sys.argv[3])) if d and d["kind"] == "ConfigMap")
    for key, value in configmap["data"].items():
        print(f"--env={key}={value}")
PY
}

pinned_postgres() {
  "$PYTHON" - "$OVERLAY" <<'PY'
import sys
import yaml

overlay = yaml.safe_load(open(sys.argv[1]))
image = next(i for i in overlay["images"] if i["name"] == "registry.redhat.io/rhel9/postgresql-16")
print(f"{image['newName']}@{image['digest']}")
PY
}

# wait_for COMMAND...: retry until it succeeds, the container stops, or WAIT_S passes.
wait_for() {
  local waited=0
  until d exec "$NAME" "$@" >/dev/null 2>&1; do
    if [ "$(d inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
      echo "  the container stopped:" && d logs "$NAME" 2>&1 | tail -5 | sed 's/^/    /'
      return 1
    fi
    if [ "$waited" -ge "$WAIT_S" ]; then
      echo "  not ready after ${WAIT_S}s:" && d logs "$NAME" 2>&1 | tail -5 | sed 's/^/    /'
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
}

# run_hardened USER IMAGE ARGS...: start the container the way the cluster runs it.
run_hardened() {
  local user=$1 image=$2
  shift 2
  cleanup
  d volume create "$NAME-data" >/dev/null
  local args=()
  for arg in "$@"; do args+=("${arg//@DATA@/$NAME-data}"); done
  d run -d --name "$NAME" --read-only --user "$user" --cap-drop ALL \
    --security-opt no-new-privileges "${args[@]}" "$image" >/dev/null
}

check_postgres() {
  local image user line mounts=()
  image=$(pinned_postgres)
  while IFS= read -r line; do mounts+=("$line"); done < <(manifest_args StatefulSet "$STATEFULSET")
  export POSTGRESQL_USER=rocheck POSTGRESQL_DATABASE=rocheck
  POSTGRESQL_PASSWORD=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  export POSTGRESQL_PASSWORD
  for user in 1000680000:0 26:26; do
    echo "PostgreSQL $image as $user ($(basename "$STATEFULSET"))"
    run_hardened "$user" "$image" "${mounts[@]}" \
      --env=POSTGRESQL_USER --env=POSTGRESQL_DATABASE --env=POSTGRESQL_PASSWORD
    if wait_for /usr/libexec/check-container && d exec "$NAME" /usr/libexec/check-container --live >/dev/null; then
      echo "  ready"
    else
      failures=$((failures + 1))
    fi
  done
}

check_control_plane() {
  local user line settings=()
  while IFS= read -r line; do settings+=("$line"); done < <(manifest_args Deployment "$DEPLOYMENT" "$CONFIGMAP")
  for user in 1000680000:0 1001:0; do
    echo "control plane $CP_IMAGE as $user"
    # Kubernetes ignores the image's VOLUME /var/lib/seamless (Docker would give it a writable
    # anonymous volume): keep it read-only like the rest of the root filesystem
    run_hardened "$user" "$CP_IMAGE" "${settings[@]}" --env=SEAMLESS_DEMO=true \
      --tmpfs=/var/lib/seamless:ro
    if wait_for python3 -c "import json, sys, urllib.request; \
r = urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4); \
sys.exit(0 if json.load(r)['status'] == 'ok' else 1)" &&
      d exec "$NAME" sh -c 'H="$SEAMLESS_DATA_DIR/.ansible"; ANSIBLE_HOME="$H" ANSIBLE_LOCAL_TEMP="$H/tmp" \
        ANSIBLE_REMOTE_TEMP="$H/tmp" ansible localhost -m ping' >/dev/null 2>&1; then
      echo "  ready (health ok, Ansible runs with the executor's ANSIBLE_HOME)"
    else
      echo "  failed" && failures=$((failures + 1))
    fi
  done
}

[ "${SKIP_POSTGRES:-}" = 1 ] || check_postgres
[ -z "${CP_IMAGE:-}" ] || check_control_plane
echo "failed checks: $failures"
exit "$failures"
