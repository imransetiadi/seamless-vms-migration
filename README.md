# Seamless Migrate

[![ci](https://github.com/imransetiadi/seamless-vms-migration/actions/workflows/ci.yml/badge.svg)](https://github.com/imransetiadi/seamless-vms-migration/actions/workflows/ci.yml)

**Move virtual machines from Red Hat OpenStack Platform 17.1, community OpenStack or VMware vSphere into
Red Hat OpenStack Services on OpenShift (RHOSO) 18.0 or community OpenStack 2023.1 Antelope – 2026.2 Hibiscus
— with minutes of downtime, predictable before it starts, approved by the right people, and reversible until
you finalize.**

> **Independent project.** Seamless Migrate is not affiliated with, sponsored by, or endorsed by
> Red Hat, Inc., the Open Infrastructure Foundation (OpenStack), or Broadcom Inc. (VMware).
> Product names are used only to describe compatibility. See
> [Trademarks and affiliation](#trademarks-and-affiliation).

Seamless Migrate 0.1.0 extends the [os-migrate](#collection-reference-os-migrate) Ansible collection (this
repository's root, which stays the *data mover*) with a warm migration path, a control plane with a REST/SSE
API and CLI, a dashboard, and optional AI assistance (Jev for bounded judgments, agentmemory for lessons
learned). It runs on OpenShift or Kubernetes, or on a single host with Docker Compose: a Linux server's own
Docker Engine (Ubuntu, Debian, RHEL, Rocky Linux, AlmaLinux, Fedora; no Colima needed), a Mac with a dedicated
[Colima](https://github.com/abiosoft/colima) profile, or Podman.

## What you get

| Strategy (`Strategy` enum) | Source | How the data moves | Downtime is driven by |
|---|---|---|---|
| `cold` | OpenStack | os-migrate stop → copy over NBD/SSH → create | the whole copy |
| `warm` | OpenStack | snapshot pre-copy while the VM runs, then a hash-based delta pass (BLAKE2b chunks) after shutdown | the final delta **plus a device scan** (the largest disk; a VM's disks scan in parallel) |
| `storage_handover` | OpenStack on shared Ceph or NetApp ONTAP (NFS, iSCSI, FC) | Cinder unmanage → manage, no data copy | metadata operations (minutes) |
| `vmware_cold` / `vmware_warm` | VMware | `os_migrate.vmware_migration_kit` (full copy, or CBT passes + cutover) | full copy / final CBT delta |

* **One workflow for every source:** providers, plans, waves (pilot first), pre-flight findings, downtime
  estimates and an automatic strategy choice per VM ([SDD §3, §9](docs/SDD.md)).
* **Safe by construction:** approval and change-window gates, per-VM rollback until `finalize`, finalize
  needs an approver *and* the typed VM name, resumable after a control-plane restart, audit trail for every
  action ([SDD §5, §8, §13](docs/SDD.md)).
* **Assisted, never autonomous:** Jev may break ties between eligible strategies, classify workloads into
  risk tiers and review post-cutover evidence — always inside deterministic bounds, and everything works
  with Jev off ([SDD §14](docs/SDD.md)). agentmemory recalls similar past incidents on failures.
* **One dashboard for every hypervisor:** add and edit OpenStack Community, Kolla-Ansible, Red Hat OpenStack 17.1,
  VMware vCenter and RHOSO 18.0 providers from the UI with platform presets, write-only credentials kept in the
  platform secret store (never in the database) or a named secret managed outside Seamless, the conversion-host
  SSH key, and a one-click connection test.
* **Every common guest OS:** RHEL 5–10, CentOS, Rocky, AlmaLinux, Oracle Linux, Ubuntu (every LTS and
  interim release), Debian 7–14, SLES/openSUSE, Windows Server 2003–2025 and Windows 7–11 are identified,
  shown with their support state and migrated — legacy releases get warnings with remediation, never a
  silent block. Windows guests are verified on RDP/WinRM ports instead of the serial console, and a storage
  handover keeps UEFI, machine-type and bus settings ([SDD §9.5](docs/SDD.md)). See *Guest OS support* below.
* **Operable:** live dashboard (WCAG 2.2 AA), Prometheus metrics (incl. orchestrator tick timing),
  structured JSON logs (uvicorn's access lines included), `/health` and a `/ready` probe that answers
  503 while the database or the orchestrator loop is down, audit log export and retention
  (`seamless events export|prune`; the dashboard's Events page reads the trail back to its start and exports it), per-address lockout after repeated failed authentications, a
  per-step wall-clock ceiling (`SEAMLESS_STEP_TIMEOUT_S`) so a hung playbook never holds a stopped
  source, cleanup of the data path when a pass is cancelled, PostgreSQL 16 as the system of record
  ([SDD §11, §12, §16, §18](docs/SDD.md)).

```
 Operators ─HTTPS─►  seamless control plane (FastAPI REST+SSE · orchestrator FSM · planner · advisor)
 Dashboard / CLI        │ PostgreSQL                │ ansible-playbook              │ Jev MCP · agentmemory
                        ▼                           ▼
                   plans · events        os-migrate collection (this repo)
                                          │ snapshot ► tmp volume ► src conversion host
                                          │             blocksync ──SSH──► dst conversion host ► new volumes
                                          ▼                                                     ▼
                         Source: RHOSP 17.1 / OpenStack / vCenter                  RHOSO 18.0 or community OpenStack
                                                                                   (boots the VM)
```

## Destination platforms

RHOSO 18.0 is the validated destination. Community OpenStack destinations run on the same code path — the
OpenStack Community and Kolla-Ansible presets are offered for the destination role — and 0.2.0 validates them
by the support tiers of [SDD §21](docs/SDD.md), which follow the upstream lifecycle on
[releases.openstack.org](https://releases.openstack.org/):

| Tier | Series | Upstream phase (2026-10-16) | What a Seamless release does |
|---|---|---|---|
| 1 | 2026.2 Hibiscus, 2026.1 Gazpacho, 2025.2 Flamingo, 2025.1 Epoxy | Maintained | the full destination lab matrix |
| 2 | 2024.1 Caracal, 2023.1 Antelope | Unmaintained (SLURP) | a compatibility smoke run |
| 3 | 2024.2 Dalmatian, 2023.2 Bobcat | End of Life | capability checks only, with a warning |
| Outside | Zed and older; series newer than 2026.2 | — | capability checks only, with a warning |

From 0.2.0 a missing capability blocks a plan whatever the tier ([SDD §21.4](docs/SDD.md)). Mind the policy
defaults now: under the secure-RBAC defaults (upstream's default from 2023.2) a VM that keeps a fixed IP on another
project's network needs the `service` or `admin` role on the destination — `advsvc` no longer does — while
Seamless's validation judges the older rule until 0.2.0 ([SDD §21.5](docs/SDD.md)). Use a CentOS Stream 9 or
RHEL 9 conversion host image until 0.2.0 adds others ([SDD §21.6](docs/SDD.md)).

## Guest OS support

OpenStack sources (RHOSP 17.1, community, Kolla) move KVM guests to KVM: every guest that boots on the source
boots on the destination, because the volumes keep their boot properties. VMware sources are converted by
virt-v2v on the RHEL 9 conversion host, which is where vendor support differs ([SDD §9.5](docs/SDD.md), Red
Hat's virt-v2v support matrix):

| Guest | From OpenStack | From VMware (virt-v2v) | Lifecycle (2026-10) |
|---|---|---|---|
| RHEL 7, 8, 9, 10 | migrates | supported | 8–10 current; 7 legacy |
| RHEL / CentOS 6 | migrates | works, not supported by Red Hat — test a copy (`GUEST_CONVERSION_UNVERIFIED`) | legacy |
| RHEL / CentOS ≤ 5 | migrates | needs virtio drivers prepared in the guest (`GUEST_CONVERSION_UNSUPPORTED`) | legacy |
| Rocky, AlmaLinux, Oracle Linux, CentOS 7/Stream | migrates | works, not supported by Red Hat — test a copy | Rocky/Alma/Oracle 8+ current |
| Ubuntu (all releases), Debian (all releases) | migrates | Technology Preview — test a copy | Ubuntu 22.04/24.04/26.04 and Debian 12/13 current |
| SLES, openSUSE | migrates | works, not supported; btrfs roots are not convertible | SLES 15 current |
| Windows Server 2016, 2019, 2022, 2025; Windows 10/11 | migrates; verified on RDP/WinRM | supported | 2016+ current |
| Windows Server 2003, 2008, 2008 R2, 2012, 2012 R2; Windows 7/8 | migrates; verified on RDP/WinRM | needs virtio-win drivers from an older release installed first | legacy |

Legacy releases are flagged (`GUEST_OS_LEGACY`), never blocked. Set `os_distro`/`os_version` image properties
or run VMware Tools so every guest is identified (`GUEST_OS_UNKNOWN` otherwise).

## Quick start — Linux server with Docker (Ubuntu, Debian, RHEL, Rocky Linux, AlmaLinux)

On Linux the stack runs on the machine's own Docker Engine: no Colima and no Node.js (the image builds the
dashboard itself). You need Docker Engine 20.10+ with the Compose plugin v2.20+, `git`, `make`, `curl` and
`openssl`, and about 4 CPUs, 6 GiB of RAM and 20 GiB of disk for the images and the database.

**1. Install Docker Engine** from Docker's own repository (the distributions' own packages often lack the
Compose v2 plugin):

```bash
# RHEL 8/9/10, Rocky Linux, AlmaLinux - Docker's RHEL repository
sudo dnf -y install dnf-plugins-core git make curl openssl
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Ubuntu 22.04/24.04/26.04 or Debian: set up Docker's apt repository as Docker's guide shows
# (https://docs.docker.com/engine/install/ubuntu/ or /debian/), then:
sudo apt-get install -y git make curl openssl docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Other distributions (Fedora, SLES, …): [Docker's install guides](https://docs.docker.com/engine/install/). Remove
Podman's `podman-docker` shim first if it is installed, since it takes the `docker` command.

**2. Start Docker now and at every boot, and use it without sudo:**

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"     # then log out and back in; check with: docker info
docker compose version              # v2.20 or newer
```

The `docker` group grants root-level rights on the machine: add only the account that runs Seamless. Run every
`make` target as that account, not with sudo, because the files `compose-init.sh` writes belong to it.

**3. Start Seamless:**

```bash
git clone https://github.com/imransetiadi/seamless-vms-migration.git
cd seamless-vms-migration
scripts/compose-init.sh          # writes deploy/compose/.env and tokens.yaml; prints the admin API token ONCE - store it now
make seamless-up                 # builds the image and starts PostgreSQL and the control plane (bring your own clouds)
# make seamless-demo             # the same stack with simulated clouds and seeded plans, to try it first
curl -fsS http://127.0.0.1:8080/api/v1/health
```

The first `make seamless-up` builds the image (several minutes: OS errata, Python packages, the Ansible
collections and the dashboard). Every container restarts with Docker after a reboot (`restart: unless-stopped`).
`make seamless-logs` follows the logs, `make seamless-down` stops the stack and keeps its data.

**4. Open the dashboard.** The control plane listens on the server's `127.0.0.1:8080` only, never on its network
interfaces. From your workstation, tunnel to it and sign in with the admin token:

```bash
ssh -L 8080:127.0.0.1:8080 you@your-server   # then open http://127.0.0.1:8080/ on the workstation
```

* SELinux (RHEL, Rocky Linux, AlmaLinux, Fedora) can stay enforcing: the token file mount is relabelled
  (`selinux: z`). A `clouds.yaml` you mount for real clouds needs the same: see
  [Using real clouds](deploy/compose/README.md#using-real-clouds).
* No firewall port has to be opened, since nothing listens on the network.
* A rootless Docker Engine: export `DOCKER_HOST` as its setup tool prints; the targets honour it (not tested in CI).
* Podman instead of Docker: see [Other container hosts](#other-container-hosts-podman-docker-desktop).

## Quick start — macOS (Colima profile `seamless`)

Prerequisites: [Colima](https://github.com/abiosoft/colima), the Docker CLI with the Compose plugin, GNU
`make`, `openssl`. The stack only ever talks to the Docker context `colima-seamless`, and Colima is started with
`--activate=false`: your active Docker context, your other Colima profiles and your other contexts are not changed.

```bash
make seamless-colima-up          # start profile "seamless" (4 CPU / 6 GiB / 40 GiB), idempotent, keeps your Docker context
make dashboard-build             # optional: the image builds the UI itself; a build here is reused (faster)

# Generate .env + tokens.yaml once. TYPESAFE_API_KEY (Jev) and AGENTMEMORY_SECRET are optional and are
# read from the environment of your shell (never typed on a command line); they are written only to the
# git-ignored, mode-600 .env and never printed.
scripts/compose-init.sh          # prints the admin API token ONCE - store it now

make seamless-demo               # build + start postgres, jev (if enabled) and seamless with simulated clouds
# make seamless-up               # same stack without the demo seed (bring your own clouds.yaml)

open http://127.0.0.1:8080/      # sign in at /login with the admin token (127.0.0.1: localhost may be another program)
curl -fsS http://127.0.0.1:8080/api/v1/health
make seamless-logs               # follow logs        make seamless-down   # stop, keep data
make seamless-check              # CI's checks that need only the venv and node_modules: tests with the 85 % coverage
                                 # gate, ruff, DEMO-07, collection tests, ansible-test sanity, playbook syntax, ansible-lint,
                                 # installed scanners, dashboard unit tests and build. The rest of CI, each on its own:
(cd dashboard && npx playwright install chromium && npm run test:e2e)   # browser smoke of the built dashboard (mock mode)
npm --prefix dashboard run build && node tests/e2e/demo-cutover-refusal-browser.mjs && node tests/e2e/demo-approval-browser.mjs && node tests/e2e/demo-signin-browser.mjs && node tests/e2e/demo-keyboard-browser.mjs && node tests/e2e/demo-stream-browser.mjs && node tests/e2e/demo-overview-browser.mjs
                                 # the browser journeys: DEMO-07, maker-checker, signing in, the keyboard run, the
                                 # event stream across a dropped connection and the Overview against /stats
SEAMLESS_TEST_PG_URL='postgresql+psycopg://<user>:<password>@127.0.0.1:55432/<db>' make seamless-test   # PostgreSQL store tests
seamless/.venv/bin/pip install pip-audit && seamless/.venv/bin/pip-audit && npm --prefix dashboard audit --omit=dev --audit-level=high
                                 # the dependency audits, as CI runs them (pip-audit; npm audit)
trivy config --severity HIGH,CRITICAL --exit-code 1 deploy   # trivy's configuration scan of the manifests (needs trivy)
make deploy-runtime-check        # PostgreSQL and the built image under the manifests' security context; kubeconform,
                                 # trivy's image scan and the SBOM need those tools (see .github/workflows/ci.yml)
pre-commit install               # gitleaks, ruff, shellcheck, actionlint on each commit (.pre-commit-config.yaml)
```

The control plane is published on `127.0.0.1:8080` only; PostgreSQL is never published; Jev (profile `ai`)
is reachable only from the control plane. The stack reaches your host's agentmemory at
`http://host.docker.internal:3111`. Details, troubleshooting and the full variable list:
[deploy/compose/README.md](deploy/compose/README.md).

### Other container hosts: Podman, Docker Desktop

The same Compose stack runs on every engine. The Make targets pick one by the host: on Linux the machine's own
Docker Engine, on macOS the Colima profile `seamless`. Variables choose another.

```bash
# Docker Desktop, or any other Docker context
make seamless-demo SEAMLESS_DOCKER_CONTEXT=desktop-linux

# Podman 4.7+ (Linux, or macOS/Windows with `podman machine start`): `podman compose` drives the stack,
# with docker-compose as its provider when installed (recommended) or podman-compose
scripts/compose-init.sh
make seamless-demo SEAMLESS_ENGINE=podman
make seamless-logs SEAMLESS_ENGINE=podman      make seamless-down SEAMLESS_ENGINE=podman
```

On SELinux hosts (RHEL, Fedora) the token file mount is relabelled automatically (`selinux: z`). With Podman the
control plane reaches the host's agentmemory at `http://host.containers.internal:3111`; set
`SEAMLESS_MEMORY_URL` in `deploy/compose/.env` accordingly.

### Without containers (control plane development)

```bash
cd seamless && python3 -m venv .venv && .venv/bin/pip install -e '.[dev,jev,collection]'
.venv/bin/pytest -q                        # SQLite always; PostgreSQL too when SEAMLESS_TEST_PG_URL is set
SEAMLESS_SECRETS_DIR=data/secrets .venv/bin/seamless serve --demo   # http://127.0.0.1:8080 (demo mode on loopback needs no token)
```

Sign-in details entered in the dashboard (Providers → Add or Edit) go to `SEAMLESS_SECRETS_DIR`: here 0600 files under
`seamless/data/secrets`, which git ignores. Its default, `/var/run/secrets/seamless`, is the container's path, and a
user cannot write it on a host.

## Deploy on OpenShift

Kustomize manifests live in [`deploy/openshift/`](deploy/openshift/) (Namespace, ServiceAccount, ConfigMap,
PVC, PostgreSQL 16 StatefulSet + Service, single-replica Deployment with a hardened security context, Service,
edge-TLS Route, default-deny NetworkPolicies). Secrets are **not** part of the kustomization — create them
from the placeholders in `secret-example.yaml` (or an external secret store), build and push the image
(`podman build -f seamless/Containerfile -t <registry>/seamless-migrate:0.1.0 .` from the repository root),
set the image in `kustomization.yaml`, then:

```bash
oc apply -k deploy/openshift
oc -n seamless-migrate rollout status deploy/seamless
oc -n seamless-migrate get route seamless
```

## Deploy on Kubernetes

[`deploy/kubernetes/`](deploy/kubernetes/) is a Kustomize overlay of the OpenShift manifests for vanilla
Kubernetes 1.28+ (EKS, AKS, GKE, RKE2, k3s, kubeadm): an Ingress replaces the Route (ingress-nginx annotations for
the live event stream), the pods get explicit UIDs, the NetworkPolicies target CoreDNS and the ingress controller,
and PostgreSQL uses the public `quay.io/sclorg/postgresql-16-c9s` image (same interface as the Red Hat one).
Build and push the image with Docker or Podman, set it and the Ingress host in the overlay, create the Secrets of
`deploy/openshift/secret-example.yaml` plus the TLS secret `seamless-tls`, then:

```bash
kubectl apply -k deploy/kubernetes
kubectl -n seamless-migrate rollout status deploy/seamless
kubectl -n seamless-migrate get ingress seamless
```

Credentials entered in the dashboard are stored as Kubernetes Secrets on both platforms
(`SEAMLESS_SECRET_STORE=kubernetes`, the Role in `secret-store-rbac.yaml`). CI renders both overlays and validates
them against the Kubernetes 1.30 schemas.

| Platform | Where | Command |
|---|---|---|
| Docker Engine on Linux (Ubuntu, Debian, RHEL, Rocky Linux, AlmaLinux, Fedora) | `deploy/compose/` | `make seamless-up` (this machine's engine; no Colima) |
| Docker on macOS (Colima), Docker Desktop | `deploy/compose/` | `make seamless-colima-up && make seamless-up` (`SEAMLESS_DOCKER_CONTEXT=…` for Docker Desktop) |
| Podman 4.7+ | `deploy/compose/` | `make seamless-up SEAMLESS_ENGINE=podman` |
| Kubernetes 1.28+ | `deploy/kubernetes/` | `kubectl apply -k deploy/kubernetes` |
| OpenShift 4.16+ | `deploy/openshift/` | `oc apply -k deploy/openshift` |

## Documentation

| Document | Contents |
|---|---|
| [docs/PRD.md](docs/PRD.md) | Problem, goals, personas, journeys, requirements `FR-01…FR-28`, `NFR-01…NFR-11` |
| [docs/SDD.md](docs/SDD.md) | **Binding design**: architecture, strategies, state machine, blocksync protocol, REST API, security model, AI integration, deployment |
| [docs/MEMORY.md](docs/MEMORY.md) | Memory architecture (agentmemory, PostgreSQL audit memory, contributor memory), redaction, governance, decision log |
| [docs/QASuite.md](docs/QASuite.md) | Test strategy, traceability `FR → test`, acceptance scenarios, lab matrix, security/resilience tests, exact commands |
| [docs/Security.md](docs/Security.md) | Threat model (STRIDE), controls, AI safety, baseline os-migrate findings, checklists, incident response |
| [docs/Performance.md](docs/Performance.md) | Downtime model with worked examples, targets, blocksync design, benchmarks, tuning, capacity planning |
| [docs/superpowers/plans/](docs/superpowers/plans/2026-10-08-seamless-rhoso-migration.md) | 0.1.0 implementation plan (tasks and test names) |
| [CLAUDE.md](CLAUDE.md) | How agents and contributors work in this repository |
| [RELEASING.md](RELEASING.md) | How a release is gated, tagged and published as a GitHub Release: versions, tags, artifacts, notes template |

## Repository layout

```text
plugins/ roles/ playbooks/   os-migrate collection + warm path (blocksync, warm modules, warm role)
seamless/                    control plane: src/seamless_migrate/, tests/, Containerfile
dashboard/                   React 18 + Vite + TypeScript dashboard (design tokens: SDD §16)
deploy/compose/              Docker Compose stack: a Linux host's Docker Engine, Colima on macOS, or Podman
deploy/openshift/            kustomize manifests (OpenShift 4.16+)
deploy/kubernetes/           kustomize overlay of those manifests for vanilla Kubernetes 1.28+
scripts/compose-init.sh      generates .env and tokens.yaml for the Compose stack
scripts/check-readonly-runtime.sh  runs the images as the manifests do (make deploy-runtime-check)
tests/                       collection tests (unit/, sanity/, func/, perf/); demo-stack scripts (e2e/)
docs/                        PRD, SDD, MEMORY, QASuite, Security, Performance, plans
.mcp.json  .claude/  CLAUDE.md   agent tooling (Jev + agentmemory MCP servers, plugins, working agreement)
```

## Contributing with agents

`.mcp.json` registers two MCP servers pinned to exact versions — `jev` (`@jkudish/jev-mcp@0.14.1`, needs
`TYPESAFE_API_KEY` in your environment) and `agentmemory` (`@agentmemory/mcp@0.9.30`, talks to
`AGENTMEMORY_URL`, default `http://localhost:3111`). `.claude/settings.json` enables the `superpowers` and
`ui-ux-pro-max` plugins and denies agents *reading* secret files. Read [CLAUDE.md](CLAUDE.md) before you
start: the SDD is binding, work is test-first, and secrets are never committed.

## Contributing

How to set up, test and submit a change: [CONTRIBUTING.md](CONTRIBUTING.md). The project follows
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md); vulnerabilities are reported privately as described in
[SECURITY.md](SECURITY.md).

## Security at a glance

Static bearer tokens stored as SHA-256 hashes with four ordered roles; every mutating call audited; cloud
credentials from mounted files or the platform secret store, write-only through the API (never in the database or
an API response); AI payloads redacted and guest console
output screened as untrusted; non-root, read-only-root-filesystem containers with default-deny network
policies. Report vulnerabilities privately — see [docs/Security.md](docs/Security.md) §14.

## Trademarks and affiliation

Seamless Migrate is an independent open source project. It is not affiliated with, sponsored by,
or endorsed by Red Hat, Inc., IBM, the OpenStack Foundation d/b/a Open Infrastructure Foundation
("OpenInfra"), Broadcom Inc., or the upstream os-migrate project. Product and project names appear
in this repository only to describe what Seamless Migrate works with.

- Red Hat, Red Hat Enterprise Linux, OpenShift and Ansible are trademarks or registered trademarks
  of Red Hat, Inc. or its subsidiaries in the United States and other countries.
- The OpenStack word mark is a trademark of the OpenStack Foundation d/b/a Open Infrastructure
  Foundation.
- VMware, vSphere and vCenter are trademarks or registered trademarks of Broadcom Inc. and/or its
  subsidiaries.
- Kubernetes is a registered trademark of The Linux Foundation.
- All other trademarks are the property of their respective owners.

## Upstream attribution

Seamless Migrate builds on [os-migrate](https://github.com/os-migrate/os-migrate), licensed under
the Apache License 2.0. Upstream copyright and license notices are retained, and files modified by
this project carry a notice stating that they were changed. Seamless Migrate is distributed under
the same license; see [LICENSE](LICENSE).

## Support

Seamless Migrate is not a Red Hat product and is not covered by any Red Hat subscription or support
agreement. Please do not open Red Hat support cases for it. Report bugs in
[GitHub Issues](https://github.com/imransetiadi/seamless-vms-migration/issues) and vulnerabilities
as described in [docs/Security.md](docs/Security.md).

Where this README calls a guest OS "supported", it refers to Red Hat's virt-v2v support matrix for
the conversion step, not to support for Seamless Migrate itself.

The software is provided "AS IS", without warranties or conditions of any kind (Apache License 2.0,
sections 7 and 8). A migration can stop or change production workloads: rehearse it outside
production and keep verified backups before you finalize.

---

## Collection reference (os-migrate)

> **Note:** This section reproduces the upstream os-migrate README for reference only. Statements
> about Red Hat support, Red Hat Automation Hub and certified content apply to the upstream
> `os_migrate.os_migrate` collection published by the os-migrate project, not to Seamless Migrate.

The remainder of this file is the upstream **os-migrate 1.0.5** collection README, kept for reference. The
collection is the data mover underneath Seamless Migrate and can still be used on its own.

### OS Migrate: OpenStack to OpenStack migration tooling
OS Migrate is an open source toolbox for parallel cloud migration
between OpenStack clouds.

[![consistency-functional](https://github.com/os-migrate/os-migrate/actions/workflows/consistency-functional.yml/badge.svg?branch=main)](https://github.com/os-migrate/os-migrate/actions/workflows/consistency-functional.yml)
[![container-image-build](https://github.com/os-migrate/os-migrate/actions/workflows/container-image-build.yml/badge.svg?branch=main)](https://github.com/os-migrate/os-migrate/actions/workflows/container-image-build.yml)
[![docs-build](https://github.com/os-migrate/os-migrate/actions/workflows/docs-build.yml/badge.svg?branch=main)](https://github.com/os-migrate/os-migrate/actions/workflows/docs-build.yml)
<img src="https://img.shields.io/badge/Python-v3.10+-blue.svg">
<img src="https://img.shields.io/badge/Ansible-v2.16+-blue.svg">
<a href="https://opensource.org/licenses/Apache-2.0">
  <img src="https://img.shields.io/badge/License-Apache2.0-blue.svg">
</a>

### Description

Parallel cloud migration is a way to
modernize an OpenStack deployment. Instead of upgrading an OpenStack
cluster in place, a second OpenStack cluster is deployed alongside,
and tenant content is migrated from the original cluster to the new
one. As hardware resources free up in the original cluster, they can
be gradually added to the new cluster.

OS Migrate provides a framework for exporting and importing resources
between two clouds. It's a collection of Ansible playbooks that
provide the basic functionality, but may not fit each use case out of
the box. You can craft custom playbooks using the OS Migrate
collection pieces (roles and modules) as building blocks.

OS Migrate strictly uses the official OpenStack API and does not
utilize direct database access or other methods to export or import
data. The Ansible playbooks contained in OS Migrate are idempotent. If
a command fails, you can retry with the same command.


### Requirements

- **Ansible**: 2.16.0 or higher
- **Python**: 3.10 or higher
- **Python Dependencies**: See `requirements.txt` for runtime dependencies including:
  - openstacksdk >= 4.5.0
  - python-openstackclient >= 7.0
  - passlib >= 1.7.4
  - PyYAML >= 6.0.2
- **Binary Dependencies**: See `bindep.txt` for system package requirements


### Installation

#### For Red Hat Customers (Recommended)

This collection is available through Red Hat Automation Hub as certified content for Ansible Automation Platform subscribers. To install from Automation Hub:

1. Configure your `ansible.cfg` to use Automation Hub:
   ```ini
   [galaxy]
   server_list = automation_hub

   [galaxy_server.automation_hub]
   url=https://console.redhat.com/api/automation-hub/content/published/
   token=<your_token_here>
   ```

2. Install the collection:
   ```bash
   ansible-galaxy collection install os_migrate.os_migrate
   ```

You can also include it in a `requirements.yml` file:
```yaml
collections:
  - name: os_migrate.os_migrate
```

Then install with:
```bash
ansible-galaxy collection install -r requirements.yml
```

#### For Community Users

Community users can install from Ansible Galaxy:

```bash
ansible-galaxy collection install os_migrate.os_migrate
```

To upgrade to the latest version:
```bash
ansible-galaxy collection install os_migrate.os_migrate --upgrade
```

To install a specific version:
```bash
ansible-galaxy collection install os_migrate.os_migrate:==1.0.0
```

See [using Ansible collections](https://docs.ansible.com/ansible/devel/user_guide/collections_using.html) for more details.

#### Post-Installation Setup

After installation, ensure you have:
- Valid OpenStack credentials for both source and destination environments
- Network connectivity between your Ansible control node and OpenStack API endpoints
- Sufficient permissions to create and manage OpenStack resources in both environments

### Overview

With OS Migrate you can test a migration from one existing openstack deployment to another existing openstack deployment. You can also test with a single existing openstack deployment migrating from one project to another. These docs cover the testing of a single existing openstack deployment migrating from one project to another scenario.

The concepts and prerequisites are the same for other deployments.

#### Prerequisites for Migration

- Source environment credentials
- Destination environment credentials
- Existing images in both source and destination environments
- Flavors
- Public network
- Space requirements
    - 2 images totalling 1.25 GB
    - 1 volume totalling 1 GB in source environment
    - 2 volumes totalling 6 GB in destination environment
    - 2 VMs totalling 35 GB disk usage in each environment


### Workflow

Below are the steps required to satisfy the above requirements and run a migration end-to-end in a test environment, migrating resources from one project to another.


#### Create source environment and destination environment projects and users
```yaml
# Create the src user in the default domain with password 'redhat'
openstack user create --domain default --password redhat src

# Create the src project
openstack project create --domain default src

# Assign src user a 'member' role in the src project
openstack role add \
--user src --user-domain default \
--project src --project-domain default member

# Confirm role assignment was successful
openstack role assignment list --project src

# Create the dst user in the default domain with password 'redhat'
openstack user create --domain default --password redhat dst

# Create the dst project
openstack project create --domain default dst

# Assign dst user a 'member' role in the src project
openstack role add \
--user dst --user-domain default \
--project dst --project-domain default member

# Confirm role assignment was successful
openstack role assignment list --project dst
```


#### Create images
```yaml
# Download images
wget https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-20230704.1.x86_64.qcow2
wget http://download.cirros-cloud.net/0.4.0/cirros-0.4.0-x86_64-disk.img

# Create images in glance from these downloads
openstack image create --public --disk-format qcow2 --file \
    CentOS-Stream-GenericCloud-9-20230704.1.x86_64.qcow2 CentOS-Stream-GenericCloud-9-20230704.1.x86_64.qcow2
openstack image create --public --disk-format raw --file cirros-0.4.0-x86_64-disk.img cirros-0.4.0-x86_64-disk.img
```


#### Create flavors
```yaml
openstack flavor create --public \
--ram 256 --disk 5 --vcpus 1 --rxtx-factor 1 m1.xtiny

openstack flavor create --public \
--ram 2048 --disk 30 --vcpus 2 --rxtx-factor 1 m1.large
```


#### Create public network
If your OpenStack environment doesn't have a public network created yet, you'll need to create one. The parameters below should work if you're deploying your OpenStack environment with Infrared Virsh plugin. If you deployed using something else, you may need to adjust the parameters.

```yaml
openstack network create \
     --mtu 1500 \
     --external \
     --provider-network-type flat \
     --provider-physical-network datacentre \
     public

openstack subnet create \
    --network public \
    --gateway 10.0.0.1 \
    --subnet-range 10.0.0.0/24 \
    --allocation-pool start=10.0.0.150,end=10.0.0.190 \
    public
```


### Running a migration
Copy the above config to file custom-config.yaml in the local directory of your local os-migrate source.

```yaml
os_migrate_src_auth:
  auth_url: http://10.0.0.131:5000/v3
  password: redhat
  project_domain_name: Default
  project_name: src
  user_domain_name: Default
  username: src
os_migrate_src_region_name: regionOne
os_migrate_dst_auth:
  auth_url: http://10.0.0.131:5000/v3
  password: redhat
  project_domain_name: Default
  project_name: dst
  user_domain_name: Default
  username: dst
os_migrate_dst_region_name: regionOne

os_migrate_data_dir: /root/os_migrate/local/migrate-data

os_migrate_conversion_host_ssh_user: cloud-user
os_migrate_src_conversion_external_network_name: nova
os_migrate_dst_conversion_external_network_name: nova
os_migrate_conversion_flavor_name: m1.large
os_migrate_conversion_image_name: CentOS-Stream-GenericCloud-8-20220913.0.x86_64.qcow2

os_migrate_src_osm_server_flavor: m1.xtiny
os_migrate_src_osm_server_image: cirros-0.4.0-x86_64-disk.img
os_migrate_src_osm_router_external_network: nova

os_migrate_src_validate_certs: False
os_migrate_dst_validate_certs: False

os_migrate_src_release: 16
os_migrate_dst_release: 16

os_migrate_src_conversion_net_mtu: 1400
os_migrate_dst_conversion_net_mtu: 1400
```

Run the migration suite using the above steps.
```yaml
OS_MIGRATE_E2E_TEST_ARGS='-e @/root/os_migrate/local/custom-config.yaml' ./toolbox/run make test-e2e-tenant
```


### Related Information

For more useful information see; optional tags/variables to add to run a migration [end-to-end](https://os-migrate.github.io/os-migrate/devel/dev-env-setup.html#optional-tags-to-pass-to-e2e-tests), demo on using os-migrate [here](https://www.youtube.com/watch?v=1AQKTcZi85A) and streamlining [VM Migrations](https://www.redhat.com/en/events/webinar/stream-os-migrate) on Red Hat OpenStack.


### Contributing
As an open source project, OS Migrate welcomes contributions from the community at large. The following guide provides information on how to add a new role to the project and where additional testing or documentation artifacts should be added. This isn't an exhaustive reference and is a living document subject to change as needed when the project formalizes any practice or pattern. See the
[OS Migrate developer documentation](https://os-migrate.github.io/os-migrate/devel/README.html).


### Support

This collection is maintained by the Red Hat OpenStack Migration team.

By ensuring correct connectivity, installation, user ACLs, and host setup, most migration issues can be avoided.

#### Customer Support

As Red Hat Ansible Certified Content, this collection is entitled to support through the Ansible Automation Platform (AAP) using the **Create issue** button on the top right corner of the [Automation Hub](https://console.redhat.com/ansible/automation-hub/).

#### Community Support

If a support case cannot be opened with Red Hat and the collection has been obtained either from Galaxy or GitHub, community help is available through:

- **GitHub Issues**: Open a bug report or feature request at [https://github.com/os-migrate/os-migrate/issues](https://github.com/os-migrate/os-migrate/issues)
- **Ansible Forum**: Get community assistance at [https://forum.ansible.com/](https://forum.ansible.com/)


### Release Notes and Roadmap

#### Red Hat Customers

For Red Hat customers using this collection through Ansible Automation Hub, release information and distributions are available at:
[https://console.redhat.com/ansible/automation-hub/repo/published/os_migrate/os_migrate/distributions/](https://console.redhat.com/ansible/automation-hub/repo/published/os_migrate/os_migrate/distributions/)

#### Community Users

For community users who obtained this collection from Galaxy or GitHub, changelog information is available at:
[https://github.com/os-migrate/os-migrate/blob/main/CHANGELOG.rst](https://github.com/os-migrate/os-migrate/blob/main/CHANGELOG.rst)

### Related Information

For detailed guides, prerequisites, and troubleshooting, please see the [OS Migrate Documentation](https://os-migrate.github.io/documentation/).

### License Information

[Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0.txt)
