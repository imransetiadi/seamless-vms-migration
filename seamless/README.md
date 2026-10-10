# Seamless Migrate — control plane (`seamless_migrate`)

The control plane plans, validates, estimates, orchestrates and exposes (REST + SSE, RBAC) VM
migrations from RHOSP 17.1, community OpenStack and VMware vSphere to Red Hat OpenStack Services
on OpenShift (RHOSO) 18.0. The Ansible collection at the repository root (os-migrate, extended
with the warm path) remains the data mover; the control plane drives it through
`ansible-playbook`.

The binding specification is [`docs/SDD.md`](../docs/SDD.md) (section numbers below refer to it).

## Layout

| Module | Responsibility |
|---|---|
| `config.py` | `Settings.from_env()` — every `SEAMLESS_*` variable (§15.1, §18) |
| `domain/` | enums, Pydantic models (§4) and the migration state machine (§5) |
| `store.py`, `events.py` | SQLAlchemy Core store (PostgreSQL 16 / SQLite) and the event bus (§11, §4.3) |
| `planning/` | estimator (§9.1), strategy selector (§9.2), pre-flight (§9.3), wave planner (§9.4) |
| `providers/` | OpenStack/RHOSO (openstacksdk), VMware (pyVmomi) and demo providers (§10) |
| `ai/` | Jev MCP client, bounded advisor, agentmemory client, knowledge service (§14) |
| `executors/` | simulated, Ansible and storage-handover executors (§7) |
| `orchestrator.py`, `verification.py` | asyncio FSM driver and post-cutover verification (§8, §7.5) |
| `api/`, `security/` | FastAPI REST + SSE, bearer-token RBAC, secrets handling (§12, §13) |
| `stats.py`, `metrics.py` | `/stats` aggregates and the Prometheus exposition (§12, §18) |
| `cli.py`, `demo.py` | `seamless` CLI and demo seeding (§15) |

## Development

```bash
cd seamless
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev,jev,openstack,vmware]'
.venv/bin/pytest -q                                               # SQLite
SEAMLESS_TEST_PG_URL=postgresql+psycopg://… .venv/bin/pytest -q   # + PostgreSQL store tests
.venv/bin/ruff check .
```

Live integration tests are skipped unless enabled: `SEAMLESS_LIVE_JEV=1` (needs
`TYPESAFE_API_KEY` and `npx`) and `SEAMLESS_LIVE_MEMORY=1` (needs `SEAMLESS_MEMORY_URL`).

## Running

```bash
seamless serve --demo                    # simulated providers/executor; auth off on loopback
seamless token create --name sari --role approver   # prints the token once + tokens.yaml entry
seamless plan apply -f plan.yaml         # PlanCreate as YAML; matched by id, else by name
seamless plan validate PLAN_ID           # findings + estimates; exit 3 when blocked
seamless plan start PLAN_ID
seamless estimate -f vms.yaml --slo 600 --link-mbps 1000 --scan-mibps 515   # scan rate from bench_blocksync
seamless status --plan PLAN_ID
```

`serve` refuses `SEAMLESS_AUTH_DISABLED=true` unless it binds to a loopback address
(`127.0.0.1`, `::1`, `localhost`). The demo seeds three providers and the plans
"Finance apps (RHOSP 17.1 → RHOSO)" (cuts over automatically, ~10 % simulated failures exercise
rollback) and "DC2 VMware exit" (waits for approvals) — idempotently.

### Security notes

* Only SHA-256 hashes of API tokens are stored (`SEAMLESS_TOKENS_FILE`); failed authentication
  emits `auth.denied` (never the token).
* Credentials are never stored or returned: OpenStack credentials come from `SEAMLESS_CLOUDS_YAML`
  by cloud name, VMware credentials from `SEAMLESS_SECRETS_DIR/<name>/{username,password}` or
  `SEAMLESS_SECRET_<NAME>_USERNAME/_PASSWORD`. Ansible receives them only through a 0600
  `secrets.yml` removed after every run (together with the `clouds.yaml` os-migrate writes).
* The Jev stdio child gets only `PATH`, `HOME` and the Jev provider keys; payloads to Jev and
  agentmemory are redacted; guest console output is screened and never sent to agentmemory.

## Container image

Built from the repository root (it installs this package with the `openstack,vmware,jev`
extras, `ansible-core`, the collection, `os_migrate.vmware_migration_kit`, Node 22 for `npx`,
and copies `dashboard/dist` when present). It runs as UID 1001 with data in `/var/lib/seamless`.

```bash
podman build --ignorefile seamless/.containerignore -f seamless/Containerfile -t seamless:0.1.0 .
docker build -f seamless/Containerfile -t seamless:0.1.0 .   # reads Containerfile.dockerignore
```
