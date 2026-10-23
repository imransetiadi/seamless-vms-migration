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
Section 8 holds the runbooks for what a migration leaves to the operator: Heat stacks, Octavia load balancers,
Designate zones and Manila shares (SDD §23.2).

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
| `up` | deploy or update everything, then wait until it runs | `make seamless-up` (`--demo`: `seamless-demo`) - builds, creates or recreates what changed | `apply -k` the overlay, then PostgreSQL and the control plane at one replica each, both rollouts awaited - PostgreSQL's extra pods, if it was scaled out, gone first |
| `down` | stop everything; the data stays | `make seamless-down` - containers removed, volumes kept | control plane to 0 (its running pod gone; an evicted pod is not waited for), then PostgreSQL to 0 |
| `stop` | stop the control plane alone | `make seamless-stop` | control plane to 0 replicas |
| `start` | start the control plane again | `make seamless-start` - the stopped container as it was, or a new one after `down` | PostgreSQL back to 1 first when it is not at 1 - each extra replica is a database of its own: named, awaited gone (by its pods, also when the count already reads 1), its PVC kept -, then the control plane |
| `restart` | restart the control plane from its current configuration | `make seamless-restart` - recreated from `.env`, the compose files and the sources | `rollout restart` (the old pod ends before the new one starts); a stopped control plane is started instead |
| `scale 0` / `scale 1` | the control plane's replicas | `stop` / `start` | `stop` / `start` |
| `capacity` | scale the one control plane up or down | `--max-migrations`, `--max-cutovers` into `deploy/compose/.env`, then `restart` | `--cpu`/`--memory` checked by a server-side dry run (a limit the API server refuses changes nothing), the same settings into the ConfigMap, then one patch of the pod template - its limits and a restart -, one rollout; a failed patch or rollout - a LimitRange or quota refusing the new pod - is rolled back, the settings put back |
| `status` | what runs, and the API's health | `make seamless-ps` + `GET /api/v1/health` | the Deployment, the StatefulSet, the pods (+ health with `--url`) |
| `logs` | follow the control plane's log | `make seamless-logs` | `logs deployment/seamless -f` |
| `resume` | start again the plans a `--drain` paused | `POST /plans/{id}/start` | the same, through `--url` |

Options: `--namespace NS` (clusters; default `seamless-migrate`), `--demo` (docker/podman `up`, `start`,
`restart`), `--drain FILE` and `--token-file FILE` (section 5), `--url URL`, `--wait-timeout S`, `--timeout S`
(how long a cluster rollout or wait may take; default 600, at least 1), and `--dry-run`, which prints every command
without running any. The exit status is 0 when done, 1 when a step failed, and 2 for a usage error or a refusal,
before anything changed:

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
| a setting in `deploy/compose/.env` for the control plane (ports, concurrency, log level, memory) | `restart` |
| a Jev setting in `deploy/compose/.env` (`TYPESAFE_API_KEY`, `JEV_MCP_AUTH_TOKEN`, `COMPOSE_PROFILES=ai`) | `up` - they are the `jev` sidecar's, which `restart` (the control plane alone) leaves as it was |
| the sources (`git pull` of a new version) or `deploy/compose/compose.yaml` | `up` (rebuilds the image, recreates what changed) |
| `deploy/compose/tokens.yaml` (an entry added or removed by hand; `seamless token create` prints a new one) | `restart` |
| only want the control plane to start afresh | `restart` |

```bash
git pull --ff-only
scripts/seamless-ctl.sh docker up           # new version: the image is rebuilt, the control plane recreated
scripts/seamless-ctl.sh docker up --demo    # ... the same for a demo stack
```

`restart` keeps the mode the control plane ran in: a stack started with `up --demo` comes back a demo, a real
one a real one (`SEAMLESS_DEMO` in your environment or in `.env` wins when you set it). `up` does not: without
`--demo` it recreates the control plane in real mode, over the demo's database - give a demo stack `up --demo`.

### 3.5 Down for maintenance, and back

```bash
scripts/seamless-ctl.sh docker down         # every container removed; the pgdata and data volumes stay
# ... maintain the host: OS updates, a reboot, Docker updates ...
scripts/seamless-ctl.sh docker up           # a demo stack: up --demo
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

The restart cuts off a cutover in flight like any stop. To let it end first, add `--drain` (section 5):
`scripts/seamless-ctl.sh docker capacity --max-migrations 16 --drain ~/.seamless/operator.token`.

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
The rollout restarts the control plane: add `--drain` and `--url` (section 5) to let the cutovers in flight end
first. A namespace's LimitRange or quota judges the new pod, not the Deployment the dry run checks: when the new pod
does not start within `--timeout`, the script puts the settings back and rolls the template back, so the control
plane runs again as it was, and exits 1 - check `kubectl describe replicaset` for the refusal. When the restored
pod is refused too - a LimitRange or a quota tightened since the last rollout -, the script says the template is
back but its pod is not available: roll back no further, and fix the limit or the quota. When the settings could not
be put back, it names them: set them in `seamless-config`, then `restart`, since the pod the rollback started read
the new ones.

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
   names the paused plans, and exits 1 - try again later, or stop without `--drain`. Any other end before the
   stop does the same: the API not answering during the wait, an interrupt (Ctrl-C). A plan whose pause got no
   answer, or an error from the server or a gateway (a 5xx), may be paused all the same: it is named too. One
   whose pause the API refused - any 4xx, answered before anything was applied - was not paused, and is not named.

   Every drain first checks what its action needs, so an action that would be refused pauses no plan: the
   token - one the API takes, an operator's or higher -; on the single host `make`, the Make targets' own checks
   (the Docker context or Podman, `deploy/compose/.env` and `tokens.yaml`) and a `SEAMLESS_HOST_PORT` that is a
   port from 1 to 65535, which Compose loads for every command; on a cluster `kubectl` or `oc`, a read of the
   Deployment - and of PostgreSQL's StatefulSet for `down` - and a server-side dry run of the action's first
   writes, so an expired login, a mistyped `--namespace` or a login that may read but not write shows before any
   plan is paused. `capacity` takes `--drain` too: it ends in a restart; on a cluster it also reads
   `seamless-config` and dry-runs its patches - and a timeout changes nothing.

3. **Start again, then resume the paused plans:**

   ```bash
   scripts/seamless-ctl.sh docker start
   scripts/seamless-ctl.sh docker resume --token-file ~/.seamless/operator.token plan-0001 plan-0002
   ```

   Or press **Resume** on each plan in the dashboard. On a cluster, `resume` needs `--url` too. A plan Start
   refuses - cancelled meanwhile, or one whose migration was blocked (409) - is named, the others start all the
   same, and the script exits 1: see that plan in the dashboard.

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
| `cannot tell the port the stack publishes: give SEAMLESS_HOST_PORT a plain value` | `deploy/compose/.env` sets `SEAMLESS_HOST_PORT` through a variable (a `$`), which only Compose can resolve: write the number itself. |
| `deploy/compose/.env gives KEY a quote it never closes, which Compose refuses with the whole file ...` | A line of `.env` - the named key's, whatever the shell sets and whatever a later line defines - opens a quote it never closes, and Compose refuses the whole file for every command, `make` included: close the quote. Nothing was paused or changed; only `resume`, which runs no Compose, can take `--url` past it. |
| `SEAMLESS_HOST_PORT is '...', no port ...` | The value is no port Compose can publish (an address in front, a stray character, a number outside 1-65535): write the number alone. |
| `the restart's Make target cannot read SEAMLESS_HOST_PORT ...` | The same, found before a `--drain`: nothing was paused or changed. |
| `deploy/compose/.env is missing` / `tokens.yaml is missing` | `make seamless-init` first; nothing was paused or changed. |
| `the token file '...' is empty` | The token file holds no token on its first line: write the operator's token into it (section 5); nothing was paused. |
| `... is needed for this action: ...` | A tool the action uses is not installed - `make`, `curl` and `python3` for a drain, `kubectl` or `oc` on a cluster: install it, and log in to the cluster; nothing was paused. |
| `cannot read the token file ...` | The path names no file this user can read: a typo, a broken link, a directory the user cannot enter. Fix the path or the permissions; nothing was paused. |
| `cannot tell who may read the token file ...` | `find` itself failed while judging the file's mode (not installed, an I/O error): fix that, then run again; nothing was paused. |
| `the token's role is 'viewer' ...` | A drain pauses plans: give an operator's token or higher. Nothing was listed or paused. |
| `the API at ... does not take this token (HTTP 401) ...` | The API refused the token itself (401: unknown or revoked; 403: it may not ask): give a current operator's token. Nothing was listed or paused. |
| `cannot ask the API at ... whose token this is (HTTP ...)` | The API did not answer `GET /api/v1/me` (HTTP 000: no answer; a 5xx: an error): is the control plane up, is `--url` right? Nothing was listed or paused. |
| `cannot read the API's answer to GET /api/v1/me` | The API answered something that is not its `/me` JSON - `--url` names another service. Nothing was listed or paused. |
| `cannot list the running plans at ...` | The API did not list the running plans: is the control plane up? Nothing was paused. |
| `cannot read the plans the API returned` | The listing answered something that is not the API's JSON - `--url` names another service, or a proxy answered with its own page. Nothing was paused. |
| `the API at ... locks this address out after failed attempts (HTTP 429)` | Too many failed sign-ins from this address (Security.md, the lockout): wait a minute, check the token, drain again. Nothing was listed or paused. |
| `make seamless-check-context ... refused ...` | The Make targets' own checks refused before the drain (`down` runs `seamless-check-context` alone, the others `seamless-check-env` too) - the line above says why (the Colima profile or Docker context, `.env` or `tokens.yaml`): nothing was paused or changed. |
| `cannot read deployment/seamless in namespace ...` | `kubectl` or `oc` cannot read the namespace - an expired login, a mistyped `--namespace`, no access: nothing was paused or changed. |
| `cannot read statefulset/seamless-postgres in namespace ...` | The same for PostgreSQL's StatefulSet, which `up`, `start` and a `down --drain` read first: nothing was paused or changed. |
| `kubectl may not ... (a server-side dry run refused it)` / `oc may not ...` | The login may read the namespace but not change it (the `view` role): the drain dry-runs the action's first writes before it pauses a plan. Log in with a role that may scale and patch the Deployment (and PostgreSQL's StatefulSet for `down`); nothing was paused or changed. |
| `a server-side dry run refused the pod template's patch ...` | `capacity`'s patch of the Deployment was refused before anything changed - a limit below the request, an admission rule (a LimitRange, a quota), or a login that may not patch the Deployment; the API server's line above says which. Nothing was paused or changed. |
| `cannot read seamless-config in namespace ...` | `capacity` cannot read the ConfigMap whose settings it changes - a mistyped `--namespace`, no access: nothing was paused or changed. |
| `no StatefulSet seamless-postgres in namespace ...: run up first` | `start` found no PostgreSQL there: run `up` first, or check `--namespace`. |
| `the API refused to pause plan ... (HTTP 4xx): it was not paused` | The API answered before it applied anything - the plan no longer running, a token it no longer takes, a plan another writer keeps changing: it was not paused and is not named for `resume`. Nothing was stopped; look at that plan, then drain again. |
| `the API answered HTTP 5xx to the pause of plan ... - it may be paused all the same` | The API or a gateway (a Route's 502 or 504) answered an error after the request reached it: the plan may be paused, so it is named for `resume`; nothing was stopped. |
| `Colima profile 'seamless' is not running. Run: make seamless-colima-up` | macOS: start the profile first. |
| `Docker Engine does not answer ...` | Linux: `sudo systemctl enable --now docker` and join the `docker` group. |
| `the namespace from the provided object ... does not match` | `--namespace` differs from the kustomization's `namespace:`; change both (section 4.1). |
| `the control plane did not become healthy in 300 s` | `scripts/seamless-ctl.sh docker logs` shows why (a database password that no longer matches the volume, a port in use). |

The tests that pin the script's behaviour are `seamless/tests/test_seamless_ctl.py` (stub tools record every
call; QASuite FR-25).

### 7.1 A second plan stops at its pre-staging: "Key name os_migrate_conv present but key hash not the same"

A known issue of 0.4.0-rc.1 (GitHub issue #112), with managed conversion hosts (`conversion_host.manage: true`, the
default):
- every plan generates its own conversion key under `/data/plans/<plan id>/osm/conversion/`;
- the Nova keypair `os_migrate_conv` and the conversion hosts are shared by every plan of the same OpenStack
  credentials.

So the pre-staging of the second plan stops at the keypair task, and the plan fails with:

```
Key name os_migrate_conv present but key hash not the same as offered. Delete key first.
```

**Do not delete the keypair, despite what the message says.** The conversion hosts would still hold the first plan's
key, and the new plan could not log in to them.

Until the fix lands, do one of these:
- **Run one plan per OpenStack credential at a time.**
- **Give the new plan the key of the plan that deployed the conversion hosts,** then start the new plan again (a
  failed plan pre-stages again at its start):
  1. Find the first plan. Its key's fingerprint is the keypair's, and Nova shows the MD5 form.
  2. Copy that plan's key pair into the new plan's directory, which may already hold a key of its own, inside the
     control plane's container. On Compose, `docker compose ... exec seamless sh`; on a cluster, `oc rsh
     deploy/seamless`, or `kubectl exec -it deploy/seamless -- sh`.

```bash
# where the OpenStack CLI reaches the source cloud
openstack keypair show os_migrate_conv -f value -c fingerprint

# in the control plane's container: the plan whose key matches
for k in /data/plans/*/osm/conversion/ssh.key.pub; do echo "$(ssh-keygen -E md5 -lf "$k" | cut -d' ' -f2 | sed 's/^MD5://') $k"; done

# still in the container: copy it to the new plan
FIRST=<the plan id whose key matches>
NEW=<the new plan's id>
mkdir -p "/data/plans/$NEW/osm/conversion" && chmod 700 "/data/plans/$NEW/osm/conversion"
cp -p "/data/plans/$FIRST/osm/conversion/ssh.key" "/data/plans/$FIRST/osm/conversion/ssh.key.pub" \
  "/data/plans/$NEW/osm/conversion/"
```

The link key between the conversion hosts needs nothing: each pre-staging writes its plan's link key to both hosts,
and the runs of every plan use the pair the hosts hold.

---

## 8. Runbooks: what a migration leaves to the operator

A migration moves a VM: its server, disks and ports and, as its plan asks, its floating IPs with their addresses, its
DNS record and its Octavia memberships (SDD §23). Four things around the VM stay the operator's (SDD §23.2): the Heat
stack that created it, the Octavia load balancer it serves behind, the Designate zone that holds its names and the
Manila shares it mounts. The pre-flight names the first two - `SRC_VM_HEAT_STACK`, `SRC_VM_AUTOSCALED`,
`SRC_VM_HEAT_UNKNOWN`, `SRC_VM_LB_MEMBER`, `SRC_VM_LB_UNKNOWN` and `MAP_LB_POOL_INCOMPATIBLE` (SDD §9.3) - and their
remediations point here. Like the network cut-over, these runbooks have not yet run on the lab's clouds: the LAB-NET
cases run those for Heat stacks, Octavia load balancers and Designate zones (QASuite §7.2, SDD §23.10), and no lab
case runs the one for Manila shares yet.

Each command runs with a credential of the cloud it names - the source's or the destination's - and `<...>` marks
what you fill in.

### 8.1 Heat stacks

Seamless moves the server, never its stack: Heat's stack abandon and adopt are a preview its convergence engine does
not support, and running the template again would create a second server (SDD §23.2). After the cutover the source
stack still describes the server, and two things can bring that server back: a stack update, which may recreate it,
and an autoscaling group, which may replace it - a second live copy of the VM.

1. **Find the VM's stack** when the pre-flight could not read Heat (`SRC_VM_HEAT_UNKNOWN`), with a credential whose
   role Heat's policy lets list the project's stacks and their resources:

   ```bash
   openstack stack list -f value -c 'Stack Name'
   # in each stack and its nested stacks: the resource whose physical resource is the VM's server
   openstack stack resource list -n 5 --filter physical_resource_id=<server-id> <stack>
   # the stack's autoscaling groups: a group's physical resource is the nested stack of its members
   openstack stack resource list -n 5 --filter type=OS::Heat::AutoScalingGroup <stack>
   ```

   The VM is a group's member (`SRC_VM_AUTOSCALED`) when the stack its server's resource is listed in is the group's
   nested stack, or one nested below it.

2. **Suspend the group's scaling** before the plan validates again: disable each alarm that signals one of the
   group's scaling policies - the stack's `OS::Aodh::*` resources whose alarm actions name a policy's signal URL; the
   alarm's id is the resource's physical resource id - and send the policies no signal yourself:

   ```bash
   openstack stack resource list -n 5 <stack>
   openstack alarm update --enabled False <alarm-id>
   ```

   Then acknowledge `SRC_VM_AUTOSCALED` for the VM in the plan (`acknowledged_findings`, SDD §4.2) and validate: the
   finding stays, as a warning that says the plan acknowledges it.

3. **Until the VM is finalized**, run no stack update at the source: an update may recreate the server, and a
   rollback starts the source server as it was.

4. **After the finalize**, keep the alarms disabled while a member of the group still has to move. Once the
   application has moved whole - every VM of the stack finalized, its other resources migrated or no longer needed -
   delete the source stack:

   ```bash
   openstack stack resource list -n 5 <stack>     # what the delete removes
   openstack stack delete <stack>
   ```

   A stack delete deletes every physical resource the stack still manages, unless the resource's `deletion_policy`
   is `Retain` (Heat's default is `Delete`). When part of the stack stays at the source for good, change its
   template so that it no longer holds the migrated server, update the stack - only once the VM is finalized -, and
   then enable the alarms again (`openstack alarm update --enabled True <alarm-id>`).

5. **At the destination**, a stack that should record the migrated server names it with `external_id`: Heat neither
   builds, manages nor deletes such a resource, and the attribute cannot be updated (Heat's template guide,
   *Resources*), so the stack never recreates what Seamless created. New resources of the application - an
   autoscaling group for its new members, say - go into that stack as usual.

### 8.2 Octavia load balancers

Seamless creates no load balancer and reads no TLS key: the destination's load balancer, its listeners, pools and
health monitors are the operator's, and so are the certificates its listeners use (SDD §23.7). What Seamless moves,
when the plan maps the VM's pool, is the VM's membership (SDD §8.5, the member helper).

1. **Read the source's load balancer**, its listeners, pools, members and health monitors in one tree:

   ```bash
   openstack loadbalancer status show <source-lb>
   openstack loadbalancer listener show <listener>     # protocol, port, TLS container references
   openstack loadbalancer pool show <pool>             # algorithm, session persistence
   ```

   When the pre-flight could not read Octavia (`SRC_VM_LB_UNKNOWN`), check the memberships yourself: the VM is a
   member of a pool when one of the pool's member addresses is one of its fixed IPs
   (`openstack loadbalancer member list <pool>`, `openstack server show -c addresses <server>`).

2. **Create the destination's load balancer** before the plan validates, with a listener, a pool and a health monitor
   for each source pool. Its provider must serve what the pool uses: the OVN provider serves TCP, UDP and SCTP with
   the `SOURCE_IP_PORT` algorithm, TCP, UDP-CONNECT and SCTP health monitors and no session persistence, so HTTP, TLS
   termination or session persistence needs an amphora load balancer (`MAP_LB_POOL_INCOMPATIBLE` names what the
   mapped pool's provider lacks). A TLS-terminating listener needs its certificate in the destination's key manager:
   store it there from your own copy and reference it from the listener.

3. **Map each source pool** to its destination pool in the plan (`network_cutover.lb_pools`: the source pool's id to
   the destination pool's id, SDD §4.2). The helper then drains the VM's source member before the source stops,
   joins the VM to the destination pool after its verification, and on a rollback deletes that member and enables the
   source member again; each step is on the migration's timeline (SDD §8.5).

4. **A pool the plan does not map** moves by hand, in the same order:

   ```bash
   # before the cutover, at the source: no new connection to the VM's member
   openstack loadbalancer member set --disable <source-pool> <source-member>
   # after the verification, at the destination: the VM's address there, on the member's port
   openstack loadbalancer member create --address <address> --protocol-port <port> --subnet-id <subnet> <destination-pool>
   # on a rollback: the reverse
   openstack loadbalancer member delete <destination-pool> <destination-member>
   openstack loadbalancer member set --enable <source-pool> <source-member>
   ```

5. **Send the clients to the destination's load balancer** once its pools have members. A DNS name moves to its VIP's
   address - a plan's DNS target swaps a VM's own record (SDD §23.5); a service's name you swap yourself, or with a
   hook. A floating IP on the source VIP can keep its address as a VM's does (SDD §8.3), by hand and with both clouds
   on one external network: reserve the address at the destination, disassociate it at the source, associate it
   with the destination VIP's port. Creating a floating IP with a given address needs a role Neutron's policy grants
   it (admin by default):

   ```bash
   openstack floating ip create --floating-ip-address <address> <external-network>   # destination
   openstack floating ip unset --port <source-floating-ip>                             # source
   openstack floating ip set --port <vip-port> <destination-floating-ip>               # destination
   ```

6. **After the finalize** the drained source member stays, as the source's load balancer is the operator's (SDD §8.5):
   delete the member, and the source's load balancer once nothing uses it:

   ```bash
   openstack loadbalancer member delete <source-pool> <source-member>
   openstack loadbalancer delete --cascade <source-lb>
   ```

### 8.3 Designate zones

A plan's Designate DNS target is reached with the destination provider's credential (SDD §23.5), so the target's
zone must be in the destination's Designate, in that credential's project, before the swap runs; Neutron's DNS
integration at the destination needs the zone there too (`DST_DNS_INTEGRATION_MISSING`). When the zone lives in the
source's Designate, move it before the window. A zone transfer request moves a zone between the projects of one
Designate, never to another cloud; between clouds the zone file moves:

1. Lower the TTLs of the zone's records and of its delegation well before, so that the change of name servers takes
   effect fast.
2. Export the zone at the source and import it at the destination:

   ```bash
   openstack zone export create <zone>                                # source: the export's id
   openstack zone export showfile <export-id> -f value > zone.txt     # source
   openstack zone import create zone.txt                              # destination
   openstack zone import show <import-id>                             # destination: until it is COMPLETE
   ```

3. Delegate the zone to the destination's name servers at its parent - the parent zone's NS records, or the
   registrar - as the destination lists them:

   ```bash
   openstack recordset list --type NS <zone>                          # destination
   ```

4. Until the parent's NS TTL has run out, both clouds answer for the zone: make each change in both, or in neither.
   Then run the plan's window - its swaps write to the destination's copy - and delete the source's:

   ```bash
   openstack zone delete <zone>                                       # source
   ```

A zone whose authoritative server is not Designate - BIND, or another server that takes RFC 2136 updates - needs no
move: its DNS target is an `rfc2136` one, with the server and a TSIG key (SDD §8.4).

### 8.4 Manila shares

Seamless moves no share: a VM's NFS or CephFS mount stays as the guest has it (SDD §23.2). Manila's own share
migration and replication move a share within one Manila - to another pool or back end, another availability zone -,
not to another cloud. Choose, per share, before the window:

* **Keep the share where it is**, when the migrated VM reaches its export from the destination's network - a storage
  network both clouds route to. An NFS share's access rules name client addresses: when the VM's address changes,
  allow the new one before the window, and remove the old one after the finalize:

  ```bash
  openstack share access create <share> ip <new-address> --access-level rw    # the share's cloud
  openstack share access list <share>
  openstack share access delete <share> <access-id>
  ```

* **Move the data** to a share at the destination: create it, allow the VM's new address, and copy from a host that
  mounts both shares - a first pass while the VM runs, the last once its source has stopped, inside the downtime,
  where a `post_create` hook runs with its timeout counting in the estimate (SDD §23.3, §9.1). Nothing may write to
  either share between the stop and the end of the last pass:

  ```bash
  openstack share create NFS <size-gib> --name <name> --share-type <type>      # destination; --share-network if its type needs one
  openstack share access create <share> ip <address> --access-level rw         # destination
  openstack share export location list <share>                                 # destination: the new export
  rsync -aH --numeric-ids --delete /mnt/source-share/ /mnt/destination-share/  # the copy host, each pass
  ```

  Point the guest's mount - its `/etc/fstab` or automount map - at the new export location, and let the guest mount
  it only once the last pass is done, by hand or with a playbook hook; delete the source share after the finalize
  (`openstack share delete <share>`).
