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
Section 7 also sets up what 0.5.0 adds: the conversion hosts' host keys (7.2), changed-extent tracking (7.3) and
notifications (7.4). Section 8 holds the runbooks for what a migration leaves to the operator: Heat stacks, Octavia load balancers,
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

The first pre-staging after an upgrade from a version before issue #112's fix adopts the conversion key: see
section 7.1.

**To 0.5.0**, which checks the conversion hosts' SSH host keys on every hop (`SEAMLESS_ANSIBLE_HOST_KEY_CHECKING`,
on by default; SDD §26.8): drain first (section 5). A cutover the upgrade cuts off resumes only once its hosts are
pinned - it checks them before the stop, and one whose source VM was already stopped is rolled back. After the
upgrade, run the check on each provider, or let the first pre-staging do it: a host 0.4.0 made is pinned once from
its console. A host whose console no longer prints the block blocks its plans with `CONV_HOST_KEY_UNKNOWN` until you
declare its keys (section 7.2). 0.4.0's link keys go with the first 0.5.0 run on each host pair; a link you made by
hand stays yours to remove.

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

### 7.1 The conversion key: one per installation (issue #112)

Every OpenStack plan reaches the managed conversion hosts with one conversion key, `/data/conversion/ssh.key` and
`ssh.key.pub` (directory 0700, key 0600). The link key between the two hosts stays per plan.

Before a pre-staging that deploys conversion hosts, the control plane reads the `os_migrate_conv` keypair on each side
whose host it manages (`conversion_host.manage: true`). It compares keys by type and base64, never by the comment.
Then:
- **No installation key yet** (the first pre-staging after the upgrade):
  - When every keypair that exists holds one plan's key, that plan's key pair is copied, never moved, to
    `/data/conversion/`.
  - When neither side holds the keypair, the deploy generates a new key there.
  - Plans in flight keep their own key for their runs until the installation key exists, so a warm migration waiting
    at its cutover gate keeps syncing across the upgrade.
- **A cloud whose keypair holds an earlier plan's key** (one that a plan on other credentials deployed before the
  upgrade) is re-keyed by the pre-staging first:
  1. It logs in to that side's conversion host with the earlier key.
  2. It adds the installation key to the SSH user's `~/.ssh/authorized_keys`.
  3. The deploy then replaces the keypair.

  No host is recreated, and a migration on the host goes on: the earlier key stays authorized.

In the cases below the pre-staging is refused before any playbook runs, and nothing changes on either side. The plan
fails; one with a migration in flight keeps running and pre-stages again every 60 s. The `plan.updated` "pre-staging
failed" event carries the message.

| Message | Meaning and fix |
|---|---|
| `the os_migrate_conv keypairs of <source> (SHA256:...) and <destination> (SHA256:...) hold different keys, and this installation has no conversion key yet to re-key them to` | Each cloud holds another plan's key. Make one of them the installation key (below) and start the plan again: the other side is then re-keyed. |
| `the os_migrate_conv keypair of <provider> (SHA256:...) holds a key that neither this installation's conversion key nor any plan's matches, so its conversion host can be neither reached nor re-keyed` | The keypair was made outside this installation, or its plan's directory is gone. Restore that directory from a backup. If no migration uses that cloud's conversion host, you can instead delete the host and the keypair with that provider's credentials (`openstack server delete <host>`, `openstack keypair delete os_migrate_conv`) and start the plan again. |
| `the installation's conversion key /data/conversion/ssh.key has no readable public key beside it (/data/conversion/ssh.key.pub)` | In the container, run `ssh-keygen -y -f /data/conversion/ssh.key > /data/conversion/ssh.key.pub`, then start the plan again. |

Run these in the control plane's container (Compose: `docker compose ... exec seamless sh`; a cluster: `oc rsh
deploy/seamless` or `kubectl exec -it deploy/seamless -- sh`):

```bash
# which key a fingerprint of the message is
for k in /data/conversion/ssh.key.pub /data/plans/*/osm/conversion/ssh.key.pub; do
  [ -f "$k" ] && echo "$(ssh-keygen -lf "$k" | cut -d' ' -f2) $k"
done
# make a plan's key the installation key: the .pub first (the private key's presence marks the key)
PLAN=<the plan id>
mkdir -p /data/conversion && chmod 700 /data/conversion
cp -p "/data/plans/$PLAN/osm/conversion/ssh.key.pub" /data/conversion/
cp -p "/data/plans/$PLAN/osm/conversion/ssh.key" /data/conversion/ && chmod 600 /data/conversion/ssh.key
```

Other cases:
- **Hosts you manage yourself** (`manage: false`) are never read or changed. Add `/data/conversion/ssh.key.pub` to
  their SSH user's `authorized_keys` once.
- **Plans in flight on another cloud pair after the upgrade:**
  1. If a run fails with `Permission denied (publickey)`, authorize the installation key on both of that plan's
     conversion hosts, logging in with the plan's earlier key:
     `ssh -i "/data/plans/$PLAN/osm/conversion/ssh.key" <ssh_user>@<host> 'cat >> ~/.ssh/authorized_keys' <
     /data/conversion/ssh.key.pub`.
  2. Then retry the migration.

  The next pre-staging on that pair also replaces its keypair.
- **Re-keyed hosts** keep the earlier key authorized. Once no plan of that key is in flight, remove its line by hand
  (Security.md §6.4).

### 7.2 The conversion hosts' SSH host keys (0.5.0)

Every hop of the data path checks a conversion host against a key known before the first connection (SDD §26): the
key Seamless injected when it created the host, the key its console printed at first boot, or the keys you declare.
The provider page shows each host's **Host keys** - the fingerprints, where they came from (`Declared`, the pin's
`injected` or `console`), the server and the time - and warns while `SEAMLESS_ANSIBLE_HOST_KEY_CHECKING` is `false`
(then no hop checks a key, and validation adds the warning `CONV_HOST_KEYS_UNCHECKED` to every plan).

**Declaring a host's keys.** For a host you bring (`conversion_host.manage: false`, a VMware destination's
conversion host), or one whose console does not print the `SSH HOST KEY KEYS` block, read the keys on the host
itself - on its console, or over a connection you trust for another reason:

```bash
cat /etc/ssh/ssh_host_ed25519_key.pub   # and ecdsa or rsa, if sshd offers those
```

Paste them, one per line, into **Host keys** of the provider form (`PATCH /api/v1/providers/{id}` →
`conversion_host.ssh_host_keys`). A line that is no public key is refused by name, never quoted back. Declared keys
win over the pin; clear them to go back to it. They are the keys of the provider's own host - the one its own
credential reaches, which every plan without a project on that side uses - and of no project's host (SDD §26.10
decision 12).

**A project's conversion host** (SDD §26.4, §26.10 decisions 11 to 14). A plan with a project on a side deploys its
own conversion host in that project, with the project's credential, under the provider's `conversion_host.name`
(else `os_migrate_conv_src` or `os_migrate_conv_dst`). Its pin is the project credential's, set from the key its
pre-staging injects, or from its console by the plan's first run when the host exists without a pin; deleting the
credential deletes the pin with it. The provider's declared keys do not reach the host, and it is looked up by its
name in its project alone, never at the provider's address. It takes a floating IP of its own project - one the
project holds detached on the host's external network, else a new one - and counts against that project's quotas,
the floating IP's included; the provider's `conversion_host.address` stays its own host's.

**A plan blocked by `CONV_HOST_KEY_UNKNOWN`.** The plan needs a host that has neither declared keys nor a pin and
that Seamless will not create, or whose pin belongs to another server than the one the host resolves to. The message
names the host by its side, provider and name - "in project ..." for a project's host:

- **The provider's own host:** declare its keys as above; or, while it has no pin, let the provider check pin it
  from its console (its image's cloud-init prints the `SSH HOST KEY KEYS` block); or, for a host Seamless manages,
  delete it and start the plan again: the pre-staging creates it with an injected key and pins it.
- **A project's host** (above), which declared keys do not reach: delete it in that project and start the plan
  again - the plan's pre-staging creates it there with an injected key and pins it as the project credential's pin.
  One without a pin needs nothing: the plan's first run pins it from its console with that project's credential.

**A refused key.** A run that meets another key fails at once and for good: the migration's reason starts with
`host key refused:` and names the side, the server, the address, the pinned and the offered fingerprints, and the
event `conversion_host.key_refused` (Events, under Providers and Security) carries the same - alert on it, since a
refused key may be an attack. A cutover checks both hosts and its link before the source VM stops, so the source keeps
running; one whose source was already stopped rolls back. Find out why the key changed before anything else:

- **The host was rebuilt or replaced** (the reason `conversion host replaced:`, or the provider's check reporting
  it - in its status for the provider's own host, in the project's entry for a project's): a conversion host deleted
  and created again under its name is a new server, and the old pin does not apply. For the provider's own host,
  declare the new host's keys, or delete it and let a pre-staging create and pin it again; for a project's host,
  delete it and let the plan's pre-staging create and pin it again - declared keys do not reach it.
- **The image regenerated its keys** (a host Seamless created that "does not present" its injected key): check the
  image's cloud-init (`ssh_deletekeys`, `ssh_keys` honoured; SDD §26.3), then delete the host and pre-stage again.
- **Neither**: treat the host's network as compromised (Security.md §13), and do not declare the offered key.

Seamless never accepts a refused key on its own: no prompt, no `accept-new`, no new pin for the same server. The way
out is always a declared key - the provider's own host's alone - or a host pre-staged again.

**The link between the hosts.** Each run makes its own link key on the destination host and writes one restricted
line into the source host's `~/.ssh/authorized_keys` (comment `seamless-link workload=<server id> run=<run>`); the run
removes both when it ends. A run also removes its migration's earlier lines and every expired one, and the first
0.5.0 run on a host pair removes 0.4.0's `CONVERSION LINK KEY` block and the destination's `~/.ssh/id_rsa` when it
holds the plan's old link key. A link you made by hand - another comment, another key - is yours to remove.
### 7.3 Changed-extent tracking: the Ceph user, its key and the route (0.5.0, Technology Preview)

Changed-extent tracking (SDD §28, FR-26) lets a warm migration's final pass read only the chunks Ceph's `rbd diff`
lists between Cinder's pass snapshots, instead of scanning every disk on both sides: the downtime loses its scan term
(Performance.md §4.6). It is a **Technology Preview** until LAB-CE01–CE07 and PERF-E2E-SM1b pass (QASuite §7, §9),
and off by default twice: a source provider must hold Ceph access, and a plan must turn `extent_tracking` on. A disk
that cannot use extents scans in full, as today, with its reason recorded; tracking never fails a pass.

**What qualifies.** A Cinder volume, not encrypted, whose pool is of the `rbd` family on the provider's cluster: the
source check reports each `rbd` entry of `storage_backends` with its `ceph_fsid` and `ceph_pool`, read from Cinder's
`location_info`, and a disk qualifies when that fsid is the provider's. Image-booted disks, encrypted volumes, volumes
of another backend and volumes whose `host` or `name_id` the credential cannot read (admin reads under the default
policies) stay untracked: the validation says so (`EXTENT_TRACKING_PARTIAL`, each disk and why).

**1. The Ceph user** (owner decision 3, SDD §28.11). A metadata-only user: it computes fast diffs from the images'
object maps and cannot read a volume's data. Run on a monitor, once per tracked pool (`volumes` below), with the
address of the *source* conversion host:

```bash
POOL=volumes
HOST=<the source conversion host's address on the Ceph public network>
ceph auth get-or-create client.seamless \
  mon "profile rbd network $HOST/32" \
  osd "allow r class-read pool=$POOL object_prefix rbd_header. network $HOST/32, \
       allow r class-read pool=$POOL object_prefix rbd_object_map. network $HOST/32, \
       allow r class-read pool=$POOL object_prefix rbd_id. network $HOST/32, \
       allow r class-read pool=$POOL object_prefix rbd_directory network $HOST/32, \
       allow r class-read pool=$POOL object_prefix rbd_info network $HOST/32"
```

The object set is what SDD §28.4 proposes; the lab confirms it (LAB-CE04, `TODO(LAB)`). If the lab shows the
metadata-only user cannot diff, the fallback is `osd "profile rbd-read-only pool=$POOL network $HOST/32"`, which
reads every tenant's volumes of the pool - keep the `network` limit. Nothing writes: Cinder makes and deletes every
snapshot. A `network` cap refuses the key from any other address (LAB-CE04), so a re-created managed conversion host
with a new address needs `ceph auth caps` again. `client.seamless` is the default `client` of the provider's `ceph`;
another name goes into `ceph.client`.

**2. The route.** The source conversion host reaches the monitors (TCP 3300, and 6789 for messenger v1) and the OSDs
(TCP 6800–7300) of the Ceph public network; open them to that host's address alone (Security.md §6.4, §8). A managed
conversion host sits on its own tenant network behind a router, and a director-deployed RHOSP 17.1 keeps Ceph on its
isolated Storage network: either route the conversion network there, or use an existing conversion host
(`conversion_host.manage: false`) that already reaches Ceph. The control plane never talks to Ceph. The pre-staging
of a plan that tracks extents installs `ceph-common` on the source host (`os_migrate_conversion_host_ceph_client`,
`conversion_host_content`); the image's repository must carry it.

**3. Ceph access on the provider, and the key.** An admin sets the source provider's `ceph` - the cluster's `fsid`
(`ceph fsid`), its monitors as the conversion host reaches them, the `client` - with `PATCH /api/v1/providers/{id}`
or the dashboard's provider page, then writes the key, write-only. The key never goes on a command line: pipe it from
`ceph auth get-key` into a request body, with the token in a header file as section 5 does.

```bash
PROVIDER=<the source provider id>
ceph auth get-key client.seamless \
  | python3 -c 'import json, sys; print(json.dumps({"key": sys.stdin.read().strip()}))' \
  | curl -sS -X PUT "$URL/api/v1/providers/$PROVIDER/ceph-key" -H @"$HEADER_FILE" \
         -H 'Content-Type: application/json' --data-binary @-
```

`GET /api/v1/providers/{id}` then shows `ceph.key_updated_at` and nothing of the key: it lives in the secret store
as `provider-{id}.ceph`, reaches each run as 0600 files beside `secrets.yml`, deleted with it, and the conversion host
as a 0700 directory for one query, removed after it (SDD §13.3, §28.8; Security.md C4-08). The event
`provider.credentials_updated` names the secret, never a value. `ceph: null` on the provider, and the provider's
deletion, delete the secret: delete the Ceph user at the cluster then.

**4. The snapshot quota.** A tracked volume keeps the Cinder snapshot of its last completed pass as the diff base
until the next pass completes: one more snapshot per volume than today, two during a pass. Raise the source project's
quota before the first tracked plan: `openstack quota set --snapshots <n> --project <source project>` (and
`--gigabytes` where snapshots count against it). The cutover deletes the bases with the final snapshot's cleanup; a
rollback, a cancel and a change of strategy delete them too.

**5. The plan.** `extent_tracking: true` on the plan (`PATCH /api/v1/plans/{id}`, `seamless plan apply`, or the plan's
page); refused with 422 on a VMware source. It may change at any time: off, the next pass scans; on, the next pass
checks. The validation warns with `CEPH_ACCESS_MISSING` when the source has no Ceph access, no key or no `rbd` pool of
its fsid: every pass scans in full then.

**6. Reading each pass's extent use.** `GET /api/v1/migrations/{id}` carries `sync_passes[].extents`, one entry per
device, and the migration page shows them; `migration.sync_pass` events carry the same (SDD §28.5):

| `mode` | Meaning | What to do |
|---|---|---|
| `checked` | a full scan that compared the extents with what changed; `missed: 0` proves the tracker on this disk for that interval; `query_s` is the query's time | nothing: the pre-copy and keep-warm passes check (owner decision 5) |
| `checked`, `missed > 0` | the extents missed a write: the disk scans in full from then on, and `EXTENT_TRACKING_MISSED` is raised | read the compute nodes' librbd and the cluster's release (item 7); the migration completes, with the scan in its downtime |
| `read` | the final pass read only the listed chunks: the downtime lost the scan | nothing |
| `scanned`, `first_pass` / `unchecked` / `no_base` | the first pass, or no check of the disk passed yet, or no base is recorded (the first pass after a rollback that created a server, LAB-CE06) | nothing: the next pass checks |
| `scanned`, `not_tracked` / `encrypted` | the disk does not qualify (item "What qualifies") | expected; `EXTENT_TRACKING_PARTIAL` named it at validation |
| `scanned`, `base_gone` / `resized` | Cinder no longer has the base snapshot `available`, or the volume's size or `name_id` changed (a retype, an extend) | expected after such an operation; the next pass checks again |
| `scanned`, `assumed_zero` | the base pass ran with `os_migrate_warm_assume_zero` and did not read the destination in full | the next `checked` pass proves the destination; keep `assume_zero` off for tracked plans |
| `scanned`, `query_failed` | `rbd diff` failed, took longer than its timeout (300 s) or listed an extent beyond the volume | the monitors unreachable, the key or the caps wrong, fast-diff off or the object map invalid (`rbd info` shows `object map invalid`/`fast diff invalid` flags; `rbd object-map rebuild <pool>/volume-<id>` as admin): fix, the next pass checks again |
| `{}` | the plan does not track extents | - |

A final pass that scanned keeps today's downtime for that disk; the estimate says so beforehand
(`Migration.tracked_disks`, Performance.md §4.6), and the calibration of the query time
(`observed_extent_query_s`) comes from the checked passes.

**7. The Ceph releases.** Ceph #61616 ("volume data corruption when using rbd-mirror w/failover"): an object map
forked for a snapshot taken under I/O could miss writes, so an `rbd diff` of those snapshots "may be incomplete". The
librbd fix is in Pacific 16.2.14, Quincy 17.2.7 and Reef 18.1.2 and every later release (Squid 19). The map is kept by
the image's writer - QEMU's librbd on the **source compute nodes** -, whose version no OpenStack API shows, and RHCS
6.1, which RHOSP 17.1 supports, is built from Quincy 17.2.6 (`TODO(LAB): confirm` which builds carry the fix). Read
both before the first tracked plan and record them (LAB-CE02):

```bash
ceph versions                                   # the cluster: mon, osd, mds by release
# each source compute node's librbd (RHOSP 17.1 runs libvirt in a container):
sudo podman exec nova_libvirt rpm -q librbd1    # director-deployed
rpm -q librbd1  ||  dpkg -l librbd1             # a plain host, a community cloud
```

The checks guard at run time: every pre-copy pass of a tracked disk is a `checked` pass, which counts the chunks that
changed outside the extents; a miss turns tracking off for that disk and raises `EXTENT_TRACKING_MISSED`. An image
whose fast-diff feature is off or whose map is invalid still diffs, one request per object - slower, correct: `rbd
feature enable <pool>/volume-<id> object-map fast-diff` and `rbd object-map rebuild` make it fast again.

### 7.4 Notifications: the relay, Slack, a webhook receiver and the egress (0.5.0)

Seamless tells people what happens to their VMs (SDD §24, FR-48–FR-51). An admin keeps a catalog of channels on the
**Notifications** page (Activity group): email through the installation's SMTP relay, Slack incoming webhooks, and
HTTPS webhooks signed as Standard Webhooks specify. An operator subscribes a channel to a plan on the plan's page
(**Notifications**, **Add subscription**); an admin subscribes one to every plan on the Notifications page. A
subscription's topics are `approval`, `downtime` and `problem` unless it asks for `outcome` and `progress` too. A
channel sends its messages one at a time, at least once and at most one a second, and drops a message whose event is
an hour old; no message ever delays a migration (SDD §24.6). A message names the VM, the plan, the phase, times and
the downtime, never a credential, an address or an error's text (Security.md §8.1).

**1. The SMTP relay** (email channels). The installation has one relay, which the control plane's environment
configures (SDD §15.1, §24.3); the client is Python's `smtplib`, over TLS alone:

| Variable | Default | Meaning |
|---|---|---|
| `SEAMLESS_SMTP_HOST`, `SEAMLESS_SMTP_FROM` | unset | the relay and the sender's address; while either is unset, an email channel cannot be added (422) and an existing one's messages fail with `no_relay` |
| `SEAMLESS_SMTP_PORT` | `587` | the relay's port |
| `SEAMLESS_SMTP_SECURITY` | `starttls` | or `tls`, implicit TLS on port 465, which RFC 8314 prefers. There is no plain-text mode: a relay that offers no STARTTLS fails the message (`no_starttls`) |
| `SEAMLESS_SMTP_CA_FILE` | unset | a CA file the relay's certificate is verified against instead of the system's CAs - a private CA, mounted into the container |
| `SEAMLESS_SMTP_SECRET` | unset | the name of a secret with the keys `username` and `password`, sent after TLS alone and read at each message; unset, the client does not authenticate and the relay admits the control plane by its address |
| `SEAMLESS_PUBLIC_URL` | unset | for every channel kind: the address people reach the dashboard at. A message ends with a link to its migration's or plan's page, ids alone and never a token; unset, it carries no link |

A relay that no longer takes a password is reached through one that admits the control plane by its address: 0.5.0
has no OAuth for SMTP, and Exchange Online turns basic SMTP authentication off by default at the end of 2026
(SDD §24.10, `TODO(LAB): confirm`).

On the **single host** the stack's `compose.yaml` hands the control plane only the variables it names, so these go
into the git-ignored override `deploy/compose/compose.local.yaml` (deploy/compose/README.md, "Using real clouds"),
which every later `up` and `restart` must load too:

```yaml
# deploy/compose/compose.local.yaml   (git-ignored)
services:
  seamless:
    environment:
      SEAMLESS_SMTP_HOST: smtp.example.com
      SEAMLESS_SMTP_FROM: seamless@example.com
      SEAMLESS_SMTP_PORT: "465"
      SEAMLESS_SMTP_SECURITY: tls
      SEAMLESS_SMTP_SECRET: smtp-relay              # leave it out when the relay admits this host by its address
      SEAMLESS_PUBLIC_URL: https://seamless.example.com
```

```bash
export SEAMLESS_EXTRA_COMPOSE_FILE=deploy/compose/compose.local.yaml   # keep it set: up and restart load it
scripts/seamless-ctl.sh docker up
```

The secret `smtp-relay` is two 0600 files in the `seamless-secrets` volume. Write them through the container, the
user and the password typed at prompts - the password unseen - and handed over on standard input, never on a command
line (`$C` as [deploy/compose/README.md](../deploy/compose/README.md) defines it; on macOS add
`--context colima-seamless` after `docker`):

```bash
read -r RELAY_USER && read -rs RELAY_PASSWORD
printf '%s\n%s\n' "$RELAY_USER" "$RELAY_PASSWORD" | $C exec -T seamless sh -c 'umask 077 &&
  d=/var/run/secrets/seamless/smtp-relay && mkdir -p "$d" && IFS= read -r u && IFS= read -r p &&
  printf %s "$u" > "$d/username" && printf %s "$p" > "$d/password"'
unset RELAY_USER RELAY_PASSWORD
```

On a **cluster** the variables go into the ConfigMap `seamless-config` (`deploy/openshift/configmap.yaml` for a
lasting change), which the control plane reads when it starts: `restart` after a change (section 4.4). The secret is
the Secret `seamless-smtp-relay`, labelled so that the store reads it - it reads no other Secret it did not write
(SDD §13.3). The pipeline creates it, and replaces it on a rotation (`kubectl` alike on Kubernetes):

```bash
d=$(mktemp -d) && read -r RELAY_USER && read -rs RELAY_PASSWORD &&
  (umask 077 && printf '%s' "$RELAY_USER" > "$d/username" && printf '%s' "$RELAY_PASSWORD" > "$d/password") &&
  unset RELAY_USER RELAY_PASSWORD
oc -n seamless-migrate create secret generic seamless-smtp-relay --from-file="$d/username" \
     --from-file="$d/password" --dry-run=client -o yaml \
  | oc label --local -f - seamless.io/provider-secret=true -o yaml | oc -n seamless-migrate apply -f -
rm -rf "$d"
scripts/seamless-ctl.sh openshift restart
```

**2. The egress.** On Kubernetes and OpenShift the NetworkPolicy `seamless-allow-egress-external`
(`deploy/openshift/networkpolicy.yaml`) opens TCP 587 and 465 towards addresses outside the cluster for the relay, and
443, which Slack and the webhook receivers use (SDD §17). A relay or a receiver on another port needs that port added
to the rule; one inside the cluster needs a rule of its own, since the external rule excepts the cluster's own
networks (as `configmap.yaml` says of an in-cluster agentmemory). Tighten `0.0.0.0/0` to the relay's and the
receivers' addresses where they are fixed (Security.md §8); Slack's are not. On the single host Compose's `frontend`
network restricts nothing: the host's firewall must let the control plane reach the relay's port and 443.

**3. A Slack channel.**

1. In Slack, add an incoming webhook for the Slack channel that should get the messages: a Slack app of the workspace
   with incoming webhooks turned on, and a webhook added for that channel. Slack fixes the channel, the name and the
   icon a webhook posts with, so one webhook is one channel of the catalog. Its URL is the credential: whoever holds it
   posts into that Slack channel, and Slack revokes a URL it finds published (Security.md C2-16).
2. On the Notifications page, **Add channel** with the kind Slack, then **Set secret** on its row and paste the URL.
   Nothing shows it again: no route returns it, and `channel.updated` records `"fields": ["secret"]` alone.
3. **Send test**: the outcome is `sent`, and the test message is in the Slack channel.
4. Subscribe it: on a plan's page, **Notifications**, **Add subscription** (an operator); for every plan, **Add
   every-plan subscription** on the Notifications page (an admin).

The same through the API, for automation: an admin's token in a header file in a private directory, and the URL typed
at a prompt that does not echo it, so neither reaches a command line or the shell's history (Security.md §7.2):

```bash
URL=https://seamless.apps.example.com           # the single host: http://127.0.0.1:8080
HEADER_FILE=$(mktemp -d)/auth                   # mktemp -d makes a 0700 directory
read -rs TOKEN && (umask 077 && printf 'Authorization: Bearer %s\n' "$TOKEN" > "$HEADER_FILE") && unset TOKEN
curl -sS -X POST "$URL/api/v1/notifications/channels" -H @"$HEADER_FILE" -H 'Content-Type: application/json' \
     -d '{"name": "ops-slack", "kind": "slack", "description": "Cutovers of the finance plans"}'
python3 -c 'import getpass, json; print(json.dumps({"url": getpass.getpass("Slack webhook URL: ").strip()}))' \
  | curl -sS -X PUT "$URL/api/v1/notifications/channels/ops-slack/secret" -H @"$HEADER_FILE" \
         -H 'Content-Type: application/json' --data-binary @-
curl -sS -X POST "$URL/api/v1/notifications/channels/ops-slack/test" -H @"$HEADER_FILE"    # {"outcome":"sent",...}
rm -rf "$(dirname "$HEADER_FILE")"              # done: the header file goes with its directory
```

**4. A webhook channel and its receiver.**

1. **Add channel** with the kind Webhook and the receiver's URL: `https://`, a host and no user information. Keep
   tokens out of the URL - the signature proves who sent the request -; the API shows the URL in full to an admin
   alone.
2. The signing key: **Set secret**, then **Generate key** makes one in the browser; **Copy key**, give it to the
   receiver, then **Save secret** - the dialog shows the key until it closes, and nothing shows it again. Or make one
   yourself, `whsec_` and the base64 of 24 to 64 random bytes, in a file only you can read; give the receiver the same
   key and write it, with `URL` and a `HEADER_FILE` made as in item 3:

   ```bash
   KEY_FILE=$(dirname "$HEADER_FILE")/signing.key
   (umask 077 && python3 -c 'import base64, secrets; print("whsec_" + base64.b64encode(secrets.token_bytes(32)).decode())' > "$KEY_FILE")
   python3 -c 'import json, sys; print(json.dumps({"signing_key": open(sys.argv[1]).read().strip()}))' "$KEY_FILE" \
     | curl -sS -X PUT "$URL/api/v1/notifications/channels/app-hook/secret" -H @"$HEADER_FILE" \
            -H 'Content-Type: application/json' --data-binary @-
   rm -rf "$(dirname "$HEADER_FILE")"         # the token's header file and the key file go with their directory
   ```

3. **What the receiver checks.** Each request is a POST with `Content-Type: application/json` and three headers:
   `webhook-id`, `webhook-timestamp` (the try's time in Unix seconds) and `webhook-signature`. A Standard Webhooks
   library makes the first two checks; the third is the receiver's own:
   * **The signature.** Take the body as it arrived, byte for byte, before any JSON parsing. Compute the HMAC-SHA256
     of `webhook-id`, a `.`, `webhook-timestamp`, a `.` and the body, keyed with the bytes the base64 after `whsec_`
     encodes; base64 it, and compare it in constant time with the value after `v1,` in `webhook-signature` (the header
     may carry several signatures, separated by spaces: one match is enough). Refuse the request otherwise.
   * **The timestamp.** Refuse a `webhook-timestamp` far from the receiver's own clock: a replayed request is old.
     Each try is signed anew with its own time, so a retry passes.
   * **The id.** Answer 2xx to a `webhook-id` already accepted, and do nothing more with it: the id, `<channel>_<seq>`
     (`_2` after it when one event made two messages), is the same on every try and after a restart of the control
     plane (SDD §24.6). Keep the ids longer than a copy can arrive - a message is not sent once its event is an hour
     old, and its tries take minutes more -: two hours is enough.

   Answer with a 2xx within 15 s and do the work afterwards: at 15 s the control plane gives the try up and tries
   again. 408, 429 and a 5xx are tried again, a `Retry-After` of up to 300 s honoured; any other 4xx, 410 included,
   and a 3xx fail the message for good - no redirect is followed. The body is the Standard Webhooks payload,
   `{"type": "<the trigger>", "timestamp": "<the event's time>", "data": {...}}`, `type` a trigger such as
   `downtime.started` and `data` holding `title`, `lines`, `link`, `topic`, `event_seq`, `event_kind`, `plan_id`,
   `plan_name`, `wave_id`, `wave_name`, `migration_id`, `vm_name`, `strategy`, `phase`, `downtime_s`, `estimate_s`
   and `slo_s` (SDD §24.5). A receiver is also the way to a service Seamless does not speak - Microsoft Teams, a
   paging service: it turns the verified document into that service's call.
4. **Send test**, then subscribe the channel as in item 3.

A channel's secret made outside the control plane works under the channel's own name: the file `url` or
`signing_key` in the directory `notify-<name>` of the `seamless-secrets` volume, or on a cluster the Secret
`seamless-notify-<name>` with that key, labelled `seamless.io/provider-secret: "true"` as above, or the variables
`SEAMLESS_SECRET_NOTIFY_<NAME>_URL` or `SEAMLESS_SECRET_NOTIFY_<NAME>_SIGNING_KEY` in the control plane's environment (the
name upper-cased, every other character `_`), read after the files and the store (SDD §13.3, §24.3; since PR #151).

**5. Rotating a channel's secret.** The control plane reads a channel's secret at every try, so a new one takes
effect at the next try, with no restart (SDD §24.3).

* **A Slack webhook:** add a new webhook for the same Slack channel, set it (**Set secret**, or the `PUT` of item 3),
  **Send test**, then remove the old webhook in the Slack app's incoming webhooks, which revokes its URL. A URL that
  leaked goes first (Security.md §13.2, playbook I): until the new one is set, the channel's messages fail at once
  (`no_service`) and are recorded so.
* **A signing key:** let the receiver accept a signature made with either the old or the new key, set the new one,
  **Send test**, and drop the old key at the receiver a minute later: only a request already in flight carries it.
* **The relay's credential:** make the new password at the relay - or, better, a new relay user -, write the secret
  `SEAMLESS_SMTP_SECRET` names again (item 1's commands), **Send test** on an email channel, then retire the old one.
  A message the relay refuses meanwhile fails for good (`535`).
* **Deleting a channel** deletes the secret the store wrote for it; when the store could not, `channel.deleted` says
  so (`secret_left`): remove it by hand - the directory `notify-<name>` in the `seamless-secrets` volume, or the
  Secret `seamless-notify-<name>`.

**6. Watching delivery.** The Notifications page shows each channel's state: its last outcome with the time and the
reason, and its failures in a row (`GET /api/v1/notifications/channels`, each channel's `cursor`). The Events page
lists one `notification.sent` or `notification.failed` per message, with its channel, trigger, key, tries and reason -
never the URL, a recipient or the body. `/metrics` counts them as `seamless_notifications_total{kind,outcome}`: alert
on `increase(seamless_notifications_total{outcome="failed"}[15m]) > 0`. **Disable** stops a channel while its
receiver is down for maintenance: the channel skips the events meanwhile, and **Enable** sends again from then on.

| Reason | Meaning and fix |
|---|---|
| `no_relay` | `SEAMLESS_SMTP_HOST` or `SEAMLESS_SMTP_FROM` is unset (item 1). |
| `no_starttls` | The relay offered no STARTTLS on that port, and nothing was sent: turn STARTTLS on at the relay, or use `tls` on 465. |
| `tls_error` | The TLS session with the relay failed, most often on a certificate the system's CAs do not verify: give its CA in `SEAMLESS_SMTP_CA_FILE`, and check that `SEAMLESS_SMTP_HOST` is a name the certificate holds. Tried again. |
| an SMTP reply code: `451`, `530`, `535`, `550`, ... | The relay's reply: a 4yz is tried again, a 5yz is final. `530` and `535`: authentication required or refused - check the secret `SEAMLESS_SMTP_SECRET` names; `550`, `553`, `554`: the relay refuses the sender or every recipient. A message the relay accepted for some recipients is sent, and counts the others as `refused`. |
| `no_secret` | A Slack channel whose secret is missing or whose `url` does not begin with `https://hooks.slack.com/services/`, or a webhook channel without a `signing_key` of `whsec_` and base64: set it (item 3 or 4). Nothing was sent. |
| `no_service`, `channel_not_found`, `channel_is_archived`, `invalid_payload`, another 4xx (Slack) | The webhook was removed or revoked, its Slack channel archived or deleted, or the app removed: add a new webhook and set it (item 5). Final. |
| `redirect` | The receiver answered a 3xx: give the channel the URL the receiver answers at itself. Final. |
| `410`, another 4xx (a webhook) | The receiver refused the message - a signature it could not verify (its key is not the channel's), a timestamp its clock calls old. Final. |
| `429`, a 5xx, or an error's class: `ConnectError`, `ConnectTimeout`, `ReadTimeout`, `TimeoutError`, `ConnectionRefusedError`, ... | Tried 5 times over about 5 minutes, then failed: is the egress open (item 2), and the relay or the receiver up? |
| `expired` | Messages whose event was more than an hour old when their turn came - after an outage of the channel or of the control plane: one `notification.failed` counts them (`expired`) with their first and last `seq`. The channel goes on with the next. |

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
