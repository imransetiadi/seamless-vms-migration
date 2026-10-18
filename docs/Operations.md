# Operating a Seamless Migrate deployment: up, down, restart and scale

This guide shows how to bring a deployment up, take it down for a change, restart it, and scale it up or
down, on every platform Seamless Migrate runs on. One script does it all, the same way everywhere:

```bash
scripts/seamless-ctl.sh PLATFORM ACTION [ARGUMENTS] [OPTIONS]
```

| `PLATFORM` | What it drives | Through |
|---|---|---|
| `docker` | the single-host stack in `deploy/compose/` on Docker: a Linux machine's own Docker Engine, or on macOS the Colima profile `seamless` (SDD §17.1) | the Make targets (`make seamless-*`) |
| `podman` | the same stack on Podman 4.7+ | the Make targets with `SEAMLESS_ENGINE=podman` |
| `kubernetes` | the manifests in `deploy/kubernetes/` (Kubernetes 1.28+) | `kubectl` |
| `openshift` | the manifests in `deploy/openshift/` (OpenShift 4.16+) | `oc` |

The binding description is [SDD §17.2](SDD.md); `scripts/seamless-ctl.sh --help` prints the same options.

---

## 1. What runs, and what "scale" means

A deployment has two parts:

* **The control plane** (`seamless`): the API, the dashboard and the **orchestrator** that drives every
  migration. Its orchestrator is a *singleton*: exactly one may run against a database (SDD §17, open
  decision D2). Two replicas would run two orchestrators that both start, cut over and roll back the same
  migrations. That is why the Kubernetes and OpenShift Deployment has `replicas: 1` and `strategy: Recreate`.
* **PostgreSQL**, the system of record: plans, migrations, events and the dashboard users.

So **scaling** has two meanings here:

* **Scale down / up between 0 and 1.** `scale 0` stops the control plane (PostgreSQL keeps running), `scale 1`
  starts it again. The script refuses any other count.
* **Capacity: scale the one control plane up or down.** `capacity` changes how much work it takes on at once -
  `SEAMLESS_MAX_CONCURRENT_MIGRATIONS` (default 10) and `SEAMLESS_MAX_CONCURRENT_CUTOVERS` (default 3) - and, on a
  cluster, its CPU and memory limits. Each concurrent migration needs about 250 MiB (an `ansible-playbook`
  process, [Performance.md](Performance.md) §8). The data itself is moved by the conversion hosts, so more
  throughput per migration comes from them (Performance.md), not from the control plane.

**Stopping is always safe.** When the control plane stops, its running steps end within the stop grace
(30 s on Compose, 60 s on Kubernetes and OpenShift), and every migration resumes from its last checkpoint when
it runs again (SDD §8). A planned stop can be made quieter with `--drain` (section 5).

**Nothing deletes data.** `down` keeps the database and the data volumes (Compose) or the PVCs, Secrets and the
ConfigMap (clusters). Deleting them stays a deliberate, separate act: `make seamless-reset CONFIRM=yes` on
Compose, deleting the namespace or its PVCs on a cluster.

---

## 2. The actions at a glance

| Action | What it does | docker / podman | kubernetes / openshift |
|---|---|---|---|
| `up` | deploy or update everything, then wait until it runs | `make seamless-up` (`--demo`: `seamless-demo`) - builds, creates or recreates what changed | `apply -k` the overlay, then PostgreSQL and the control plane at one replica each, both rollouts awaited |
| `down` | stop everything; the data stays | `make seamless-down` - containers removed, volumes kept | control plane to 0 (its running pod gone; an evicted pod is not waited for), then PostgreSQL to 0 |
| `stop` | stop the control plane alone | `make seamless-stop` | control plane to 0 replicas |
| `start` | start the control plane again | `make seamless-start` - the stopped container as it was, or a new one after `down` | PostgreSQL to 1 first when it is at 0, then the control plane |
| `restart` | restart the control plane from its current configuration | `make seamless-restart` - recreated from `.env`, the compose files and the sources | `rollout restart` (the old pod ends before the new one starts); a stopped control plane is started instead |
| `scale 0` / `scale 1` | the control plane's replicas | `stop` / `start` | `stop` / `start` |
| `capacity` | scale the one control plane up or down | `--max-migrations`, `--max-cutovers` into `deploy/compose/.env`, then `restart` | `--cpu`/`--memory` checked by a server-side dry run (a refused limit changes nothing), the same settings into the ConfigMap, then one patch of the pod template - its limits and a restart -, one rollout |
| `status` | what runs, and the API's health | `make seamless-ps` + `GET /api/v1/health` | the Deployment, the StatefulSet, the pods (+ health with `--url`) |
| `logs` | follow the control plane's log | `make seamless-logs` | `logs deployment/seamless -f` |
| `resume` | start again the plans a `--drain` paused | `POST /plans/{id}/start` | the same, through `--url` |

Options: `--namespace NS` (clusters; default `seamless-migrate`), `--demo` (docker/podman `up`, `start`,
`restart`), `--drain FILE` and `--token-file FILE` (section 5), `--url URL`, `--wait-timeout S`, `--timeout S`
(how long a cluster rollout may take; default 600), and `--dry-run`, which prints every command without
running any:

```bash
scripts/seamless-ctl.sh kubernetes down --dry-run
+ kubectl -n seamless-migrate scale deployment/seamless --replicas=0
+ kubectl -n seamless-migrate wait --for=delete pod -l app.kubernetes.io/name=seamless '--field-selector=status.phase!=Failed,status.phase!=Succeeded' --timeout=600s
+ kubectl -n seamless-migrate scale statefulset/seamless-postgres --replicas=0
+ kubectl -n seamless-migrate wait --for=delete pod -l app.kubernetes.io/name=seamless-postgres '--field-selector=status.phase!=Failed,status.phase!=Succeeded' --timeout=600s
```

Exit status: `0` done, `1` a step failed, `2` a usage error or a refusal (nothing was run).

---

## 3. Tutorial: the single-host stack (Docker or Podman)

### 3.1 First start

On a **Linux server** with Docker Engine (Ubuntu, Debian, RHEL, Rocky Linux, AlmaLinux, Fedora), from the
repository root:

```bash
scripts/compose-init.sh                     # once: writes deploy/compose/.env and tokens.yaml, prints the admin token once
scripts/seamless-ctl.sh docker up           # or: docker up --demo (simulated clouds)
scripts/seamless-ctl.sh docker status
```

On **macOS**, start the dedicated Colima profile first (your other Docker contexts stay untouched):

```bash
make seamless-colima-up
scripts/compose-init.sh
scripts/seamless-ctl.sh docker up
```

With **Podman**, use the platform `podman` everywhere instead of `docker`:

```bash
scripts/compose-init.sh
scripts/seamless-ctl.sh podman up
```

The dashboard is at <http://127.0.0.1:8080/> (only loopback is published; from another machine use an SSH
tunnel: `ssh -L 8080:127.0.0.1:8080 you@server`). Save the admin token compose-init printed in your password
manager.

### 3.2 Day to day

```bash
scripts/seamless-ctl.sh docker status       # containers and GET /api/v1/health
scripts/seamless-ctl.sh docker logs         # follow the control plane's log (Ctrl-C to stop following)
```

### 3.3 Stop and start the control plane

```bash
scripts/seamless-ctl.sh docker stop         # or: docker scale 0 - PostgreSQL keeps running
scripts/seamless-ctl.sh docker start        # or: docker scale 1 - the same container, demo or not, as it was
```

### 3.4 After a change

| You changed | Run |
|---|---|
| a setting in `deploy/compose/.env` (ports, concurrency, log level, Jev, memory) | `restart` |
| the sources (`git pull` of a new version) or `deploy/compose/compose.yaml` | `up` (rebuilds the image, recreates what changed) |
| `deploy/compose/tokens.yaml` (tokens added or removed with `seamless token create`) | `restart` |
| only want the control plane to start afresh | `restart` |

```bash
git pull --ff-only
scripts/seamless-ctl.sh docker up           # new version: the image is rebuilt, the control plane recreated
```

`restart` keeps the mode the control plane ran in: a stack started with `up --demo` comes back a demo, a real
one a real one (`SEAMLESS_DEMO` in your environment or in `.env` wins when you set it).

### 3.5 Down for maintenance, and back

```bash
scripts/seamless-ctl.sh docker down         # every container removed; the pgdata and data volumes stay
# ... maintain the host: OS updates, a reboot, Docker updates ...
scripts/seamless-ctl.sh docker up
```

Take a backup before a change you cannot undo: the `pg_dump` recipe of
[deploy/compose/README.md](../deploy/compose/README.md#data-backup-reset).

### 3.6 Scale: capacity of the one control plane

```bash
scripts/seamless-ctl.sh docker capacity --max-migrations 16 --max-cutovers 4    # scale up
scripts/seamless-ctl.sh docker capacity --max-migrations 5 --max-cutovers 1     # scale down
```

The settings are written to `deploy/compose/.env` (the file keeps its other lines and its 0600 mode) and the
control plane restarts with them. The control plane's memory limit in `deploy/compose/compose.yaml` is `3g`,
sized for 10 concurrent migrations at about 250 MiB each: above 10, raise `deploy.resources.limits.memory` of the
`seamless` service there first - the script warns. On macOS the Colima profile itself has 6 GiB by default
(`SEAMLESS_COLIMA_MEMORY`): to give the VM more, stop the profile (`colima stop seamless`) and start it again with
the new size (`make seamless-colima-up SEAMLESS_COLIMA_MEMORY=10`); Colima takes a new CPU or memory count when it
starts a stopped profile, never a smaller disk.

---

## 4. Tutorial: Kubernetes and OpenShift

### 4.1 First deployment

Create the Secrets first (`deploy/openshift/secret-example.yaml` lists them; on Kubernetes also the TLS
secret `seamless-tls`), set your image in the kustomization (`newName`/`newTag`, or a digest), then:

```bash
scripts/seamless-ctl.sh kubernetes up       # kubectl apply -k deploy/kubernetes, both scaled to 1, rollouts awaited
scripts/seamless-ctl.sh openshift up        # oc apply -k deploy/openshift
```

Both use the namespace `seamless-migrate`, which the kustomizations set. To deploy into another one, change
`namespace:` in the kustomization first and pass the same name with `--namespace` - `apply` refuses objects whose
namespace differs from the one given.

### 4.2 Day to day

```bash
scripts/seamless-ctl.sh openshift status --url https://seamless.apps.example.com    # pods, rollouts, health
scripts/seamless-ctl.sh openshift logs
```

### 4.3 Stop, start and scale

```bash
scripts/seamless-ctl.sh kubernetes stop      # = scale 0: the control plane's pod ends; PostgreSQL keeps running
scripts/seamless-ctl.sh kubernetes start     # = scale 1: PostgreSQL first when a down left it at 0
scripts/seamless-ctl.sh kubernetes scale 2   # refused: one orchestrator only (SDD §17, D2)
```

### 4.4 After a change

| You changed | Run |
|---|---|
| a manifest in the overlay (resources, NetworkPolicy, Route/Ingress, a new image tag) | `up` - the apply rolls the pod when its template changed |
| only the ConfigMap `seamless-config` (applied with `up`, or edited) | `restart` - settings are read when the pod starts |
| a Secret (clouds.yaml, tokens, the database URL, the AI keys) | `restart` |

### 4.5 Down for maintenance, and back

```bash
scripts/seamless-ctl.sh openshift down      # control plane to 0, then PostgreSQL to 0; PVCs, Secrets, ConfigMap stay
# ... cluster or storage maintenance ...
scripts/seamless-ctl.sh openshift up
```

With the backup overlay (`deploy/openshift/backup/`) installed, its nightly job fails while PostgreSQL is at 0
replicas. For a longer down, suspend it and resume it after `up`:

```bash
oc -n seamless-migrate patch cronjob/seamless-pg-backup -p '{"spec":{"suspend":true}}'
oc -n seamless-migrate patch cronjob/seamless-pg-backup -p '{"spec":{"suspend":false}}'
```

### 4.6 Scale: capacity of the one control plane

```bash
scripts/seamless-ctl.sh kubernetes capacity --max-migrations 20 --max-cutovers 4 --cpu 6 --memory 8Gi
```

The concurrency settings are patched into the ConfigMap and `--cpu`/`--memory` become the container's limits, in
one rollout. These are live changes: the next `up` (`apply -k`) brings the manifests' values back, so put a
lasting change into the overlay - `deploy/openshift/configmap.yaml` and the `resources:` of
`deploy/openshift/deployment.yaml` (or a kustomize patch of your own). The requests and limits there are sized for
10 concurrent migrations at about 250 MiB each: above 10 without `--memory`, the script warns, as on the single host.

---

## 5. A planned stop: drain first, resume after

A stop is safe at any time, but a cutover cut off mid-way resumes after the restart with its source VM still
stopped, so its downtime grows by the length of the stop. Before planned maintenance, let the work in flight
end first:

1. **Store an operator token in a file only you can read.** The script reads the token from that file and
   hands it to curl through a header file in a private directory - never on a command line, where `ps` and
   the shell history would keep it ([Security.md](Security.md) §7.2):

   ```bash
   mkdir -p ~/.seamless && chmod 700 ~/.seamless
   read -rs TOKEN && (umask 077 && printf '%s\n' "$TOKEN" > ~/.seamless/operator.token) && unset TOKEN
   ```

   A file group or others may read is refused (`chmod 600` it). The token must be an operator's or higher:
   a drain pauses plans.

2. **Stop with `--drain`.** It pauses every running plan, so no new migration starts, and waits until no
   migration is in `cutover`, `verifying` or `rolling_back` (at most `--wait-timeout` seconds, default 1800);
   then it stops. Pre-copy and sync passes are not waited for: they resume after the restart.

   ```bash
   scripts/seamless-ctl.sh docker stop --drain ~/.seamless/operator.token
   scripts/seamless-ctl.sh openshift down --drain ~/.seamless/operator.token --url https://seamless.apps.example.com
   ```

   It prints the plans it paused. If a cutover is still in flight when the wait ends, it **stops nothing**,
   names the paused plans, and exits 1 - try again later, or stop without `--drain`.

3. **Start again, then resume the paused plans:**

   ```bash
   scripts/seamless-ctl.sh docker start
   scripts/seamless-ctl.sh docker resume --token-file ~/.seamless/operator.token plan-0001 plan-0002
   ```

   Or press **Start** on each plan in the dashboard. On a cluster, `resume` needs `--url` too.

`--drain` and `resume` need `curl` and `python3` on the machine that runs the script. On a cluster whose Route
or Ingress uses a private CA, point curl at it: `export CURL_CA_BUNDLE=/path/to/ca.pem`.

---

## 6. Upgrading to a new version

Follow [RELEASING.md](../RELEASING.md) §9: back up, drain (section 5), roll the new image - `up` after `git pull`
on Compose, the new tag in the overlay and `up` on a cluster -, verify `GET /api/v1/health` names the new
version, then resume the plans.

---

## 7. Troubleshooting

| Message | Meaning and fix |
|---|---|
| `scale takes 0 or 1 ...` | One control plane only; to scale up use `capacity`. |
| `the token file ... may be read by others` | `chmod 600` the file. |
| `xtrace (bash -x) is on ...` | Run without `bash -x`: it would print the token. |
| `a migration is still in cutover after 1800s: nothing was stopped` | A cutover outlasted `--wait-timeout`. Nothing was stopped and the plans stay paused: wait and retry, or raise `--wait-timeout`. |
| `on kubernetes the API is reached through the Route or Ingress: give its URL with --url` | `--drain`, `resume` and the health check need the external URL on a cluster. |
| `Colima profile 'seamless' is not running. Run: make seamless-colima-up` | macOS: start the profile first. |
| `Docker Engine does not answer ...` | Linux: `sudo systemctl enable --now docker` and join the `docker` group. |
| `the namespace from the provided object ... does not match` | `--namespace` differs from the kustomization's `namespace:`; change both (section 4.1). |
| `the control plane did not become healthy in 300 s` | `scripts/seamless-ctl.sh docker logs` shows why (a database password that no longer matches the volume, a port in use). |

The tests that pin the script's behaviour are `seamless/tests/test_seamless_ctl.py` (stub tools record every
call; QASuite FR-25).
