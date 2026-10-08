# Seamless Migrate — Docker Compose stack on a dedicated Colima profile

Implements [SDD §17.1](../../docs/SDD.md). The stack runs the control plane, its dashboard and PostgreSQL 16
(and, optionally, a Jev MCP sidecar) on the Colima profile **`seamless`**. Everything is addressed through the
Docker context **`colima-seamless`**: the Make targets pass it explicitly and start Colima with
`--activate=false`, so your active Docker context, your `default` Colima profile and any other context are
never changed.

| Service | Image | Reachable from | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | `seamless` only (internal network) | volume `pgdata`, data checksums, **password required for every connection** (`--auth-local/--auth-host=scram-sha-256`), healthcheck `pg_isready`, **not published** |
| `jev` *(profile `ai`)* | `node:22-alpine` → `npx -y @jkudish/jev-mcp@0.14.1 --http` | `seamless` only (never published; on `frontend` because it needs npm and the Jev provider API) | bearer token `JEV_MCP_AUTH_TOKEN`, key `TYPESAFE_API_KEY`, `/health` probe |
| `seamless` | built from [`seamless/Containerfile`](../../seamless/Containerfile) (context = repo root) | **`127.0.0.1:8080` only** | tokens file mounted read-only, volume `seamless-data` at `/data`, non-root, `cap_drop: ALL` |

Networks: `backend` (`internal: true`: PostgreSQL ↔ control plane, no route out) and `frontend` (control plane
↔ Jev, and the way out to cloud APIs, vCenter, conversion hosts and the host's agentmemory at
`http://host.docker.internal:3111`).

## Prerequisites

* [Colima](https://github.com/abiosoft/colima), Docker CLI with the Compose plugin (v2.20+ for `depends_on.required`),
  GNU `make`, `openssl`. On macOS: `brew install colima docker docker-compose`.
* Optional: `TYPESAFE_API_KEY` in the environment of the shell that runs `compose-init.sh` (enables the Jev
  sidecar; never type it on a command line — `read -rs TYPESAFE_API_KEY && export TYPESAFE_API_KEY`), a running
  host agentmemory (`npx -y @agentmemory/agentmemory@0.9.30`, REST on `localhost:3111`).

## First run

```bash
make seamless-colima-up      # starts profile "seamless": 4 CPU / 6 GiB / 40 GiB (idempotent; your active Docker context is kept)
make dashboard-build         # optional: bake the UI into the image (served at /)
scripts/compose-init.sh      # writes deploy/compose/.env (0600) and tokens.yaml; prints the admin token ONCE
make seamless-up             # or: make seamless-demo   (simulated clouds, SEAMLESS_DEMO=true)
```

Then open <http://127.0.0.1:8080/> and sign in at `/login` with the admin token, or call the API:

```bash
export TOKEN='smg_...'                                   # the token printed by compose-init.sh
curl -fsS http://127.0.0.1:8080/api/v1/health            # {"status":"ok",...,"db":"ok","orchestrator":{"running":true,...}}
curl -fsS http://127.0.0.1:8080/api/v1/ready             # same body; HTTP 503 while the DB or the orchestrator loop is down
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/me
curl -N   -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/events/stream    # live SSE
```

`compose-init.sh` is idempotent (existing files are kept). `--force` regenerates both files:

* **`.env`**: new `JEV_MCP_AUTH_TOKEN`; **kept** are `POSTGRES_PASSWORD` (the database volume was initialized with
  it), API keys already stored, lines you added yourself (for example `SEAMLESS_HOST_PORT`), a removed
  `SEAMLESS_MEMORY_URL` (memory stays disabled) and your Jev on/off choice. `--rotate-db-password` is only for a
  brand-new database (`make seamless-reset CONFIRM=yes` first).
* **`tokens.yaml`**: a **new admin token** is issued — the old one stops working, every other entry of the old file
  is dropped (recreate them with `seamless token create`), and sessions or scripts holding the old token must
  sign in again. Add `--keep-token` to regenerate `.env` only and leave `tokens.yaml` and the admin token alone.

After `--force`, recreate the stack so the control plane reloads the files: `make seamless-down seamless-up`.

## Make targets

| Target | What it does |
|---|---|
| `seamless-colima-up` | `colima start seamless --activate=false --cpu 4 --memory 6 --disk 40` (keeps your active Docker context), prints the daemon info and the still-active context |
| `seamless-init` | runs `scripts/compose-init.sh` |
| `seamless-up` | builds and starts the stack, waits until healthy (`up -d --build --wait`) |
| `seamless-demo` | like `seamless-up` with `SEAMLESS_DEMO=true`: fake providers, simulated executor, seeded plans |
| `seamless-down` | removes the containers, keeps volumes (also stops the `jev` sidecar) |
| `seamless-ps` / `seamless-logs` | status / follow logs (`SEAMLESS_SERVICE=seamless` to filter) |
| `seamless-reset` | **deletes** containers and volumes; needs `CONFIRM=yes` |
| `seamless-test` | `cd seamless && .venv/bin/pytest -q` (export `SEAMLESS_TEST_PG_URL` to include PostgreSQL) |
| `dashboard-build` | `cd dashboard && npm ci && npm run build` |

Variables: `SEAMLESS_COLIMA_PROFILE` (default `seamless`), `SEAMLESS_COLIMA_CPU/MEMORY/DISK`,
`SEAMLESS_DOCKER_CONTEXT` (default `colima-$(SEAMLESS_COLIMA_PROFILE)`), `SEAMLESS_PROJECT` (default `seamless`),
`SEAMLESS_EXTRA_COMPOSE_FILE`.

## Configuration

`.env` (generated; edit only the toggles) holds `POSTGRES_PASSWORD`, `JEV_MCP_AUTH_TOKEN`, optionally
`TYPESAFE_API_KEY`, `COMPOSE_PROFILES`, `SEAMLESS_JEV_MODE`, `SEAMLESS_MEMORY_URL`, `SEAMLESS_MEMORY_SECRET`.
Any variable of [SDD §15.1](../../docs/SDD.md) can be overridden from `.env` or the shell; the ones the
Compose file wires explicitly:

| Variable | Default here | Meaning |
|---|---|---|
| `SEAMLESS_DEMO`, `_DEMO_SPEED`, `_DEMO_SEED`, `_DEMO_FAILURE_RATE` | `false`, `60`, `42`, `0.1` | demo mode |
| `SEAMLESS_MAX_CONCURRENT_MIGRATIONS` / `_CUTOVERS`, `SEAMLESS_TICK_S`, `SEAMLESS_MAX_STEP_RETRIES` | `10`, `3`, `1.0`, `2` | orchestrator |
| `SEAMLESS_JEV_MODE` | `http` (`off` when no key at init) | Jev advisor (SDD §14.1); URL `http://jev:8080/mcp` is fixed |
| `SEAMLESS_JEV_TIMEOUT_S`, `SEAMLESS_JEV_MIN_CONFIDENCE` | `20`, `0.6` | advisor bounds |
| `SEAMLESS_MEMORY_URL`, `SEAMLESS_MEMORY_SECRET` | host agentmemory, none | passed through only when defined; delete the line to disable memory |
| `SEAMLESS_MEMORY_PROJECT`, `SEAMLESS_MEMORY_REDACT_NAMES` | `seamless-migrate`, `false` | memory project id / privacy ([MEMORY.md](../../docs/MEMORY.md) §3) |
| `SEAMLESS_CORS_ORIGINS`, `SEAMLESS_METRICS_PUBLIC`, `SEAMLESS_LOG_LEVEL`, `SEAMLESS_LOG_JSON` | empty, `false`, `INFO`, `false` | misc |
| `SEAMLESS_HOST_PORT`, `SEAMLESS_VERSION` | `8080`, `0.1.0` | published loopback port, image tag |

Authentication is always on (`SEAMLESS_AUTH_DISABLED=false`): the process listens on `0.0.0.0` inside the
container, which the CLI only permits with authentication enabled (SDD §13.1). **Demo mode therefore also needs
the admin token.**

**Precedence.** Compose lets a variable exported in your shell win over `--env-file`. The Make targets therefore
unset the variables that `.env` owns (`POSTGRES_PASSWORD`, `JEV_MCP_AUTH_TOKEN`, `TYPESAFE_API_KEY`,
`COMPOSE_PROFILES`, `SEAMLESS_JEV_MODE`, `SEAMLESS_MEMORY_URL`, `SEAMLESS_MEMORY_SECRET`) and pin the project name,
so a stray `POSTGRES_PASSWORD` in your shell cannot break the database login; every other variable
(`SEAMLESS_HOST_PORT=8081 make seamless-up`) can still be overridden from the shell. When you call
`docker compose` yourself, prefix it with `env -u POSTGRES_PASSWORD -u JEV_MCP_AUTH_TOKEN -u TYPESAFE_API_KEY
-u COMPOSE_PROFILES -u SEAMLESS_JEV_MODE -u SEAMLESS_MEMORY_URL -u SEAMLESS_MEMORY_SECRET`.

### Enabling or disabling the Jev sidecar

Enabled when `.env` contains `COMPOSE_PROFILES=ai` **and** `SEAMLESS_JEV_MODE=http` (written by
`compose-init.sh` when `TYPESAFE_API_KEY` was in your environment). To switch on later, put the key in your
shell environment (`read -rs TYPESAFE_API_KEY && export TYPESAFE_API_KEY`) and run
`scripts/compose-init.sh --force --keep-token` (keeps the admin token; a plain `--force` also rotates it), or edit
those two lines (and `TYPESAFE_API_KEY`) in `.env`; then `make seamless-down seamless-up`. Without Jev everything
works with deterministic rules (SDD §14.2).

### Using real clouds

The stack starts without any cloud credentials. To migrate for real, mount your `clouds.yaml` through a
git-ignored override file (never bake credentials into an image or compose.yaml):

```yaml
# deploy/compose/compose.local.yaml   (git-ignored)
services:
  seamless:
    user: "501:0"                      # your uid from `id -u`: Colima bind mounts keep host ownership,
                                       # so a 0600 file is unreadable for a different container uid
    environment:
      SEAMLESS_CLOUDS_YAML: /etc/openstack/clouds.yaml
    volumes:
      - type: bind
        source: /Users/<you>/.config/openstack/clouds.yaml
        target: /etc/openstack/clouds.yaml
        read_only: true
        bind:
          create_host_path: false
```

`make seamless-up SEAMLESS_EXTRA_COMPOSE_FILE=deploy/compose/compose.local.yaml`. VMware credentials: set
`SEAMLESS_SECRET_<NAME>_USERNAME` / `_PASSWORD` in the override's `environment` (SDD §13.3), or mount a
directory laid out as `<name>/{username,password}` at `/var/run/secrets/seamless`. Prefer Keystone
application credentials ([Security.md](../../docs/Security.md) §7).

## Data, backup, reset

* Volumes: `seamless_pgdata` (system of record: plans, migrations, events) and `seamless_seamless-data`
  (run directories). `make seamless-down` keeps them; `make seamless-reset CONFIRM=yes` deletes them.
* Switching between demo and real use: reset first — demo seeds fake providers and plans into the database.
* Every PostgreSQL connection needs a password, also inside the container; the container's own
  `POSTGRES_PASSWORD` is used so the secret never appears on a command line. The `initdb` options only apply
  to a **new** `pgdata` volume (`make seamless-reset CONFIRM=yes` to adopt them).
* Backup — the dump contains plan data: write it **outside the repository**, readable only by you:

  ```bash
  umask 077 && mkdir -p "$HOME/seamless-backups"
  # bash; in zsh run `setopt SH_WORD_SPLIT` first so that $C splits into words
  C="docker --context colima-seamless compose -p seamless -f deploy/compose/compose.yaml --env-file deploy/compose/.env"
  $C exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U seamless seamless' \
    > "$HOME/seamless-backups/seamless-$(date +%Y%m%d-%H%M).sql"
  ```

  An interactive shell: `$C exec -it postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U seamless seamless'`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Docker context 'colima-seamless' not found` / `profile is not running` | `make seamless-colima-up` |
| `POSTGRES_PASSWORD is not set` | run `scripts/compose-init.sh` (or `make seamless-init`) |
| `bind source path does not exist: …/tokens.yaml` | `.env`/`tokens.yaml` missing: run the init script (Compose never creates a directory in its place) |
| `password authentication failed for user "seamless"` | `.env` password differs from the one the `pgdata` volume was initialized with (or a stray `POSTGRES_PASSWORD` in your shell when you ran `docker compose` yourself — see Precedence). Restore the old `.env`, or `make seamless-reset CONFIRM=yes` and init again |
| `Permission denied: '/data/...'` | the image must own `/data` for its non-root user (Containerfile); one-off fix: `docker --context colima-seamless run --rm -v seamless_seamless-data:/data busybox chown -R <uid>:0 /data` |
| `jev` unhealthy for ~1 min after first start | `npx` downloads the pinned package; wait for `start_period` (90 s) and check `make seamless-logs SEAMLESS_SERVICE=jev` |
| Memory calls fail from the container | `docker --context colima-seamless run --rm busybox wget -qO- http://host.docker.internal:3111/agentmemory/livez`; start the host agentmemory, or delete `SEAMLESS_MEMORY_URL` from `.env` |
| Port 8080 already in use | set `SEAMLESS_HOST_PORT=8081` in `.env` |
| `exec: "python3": executable file not found` in the health check | the image must provide `python3` on `PATH` (UBI Python base does) |

## Security notes

* Only `seamless` is published, and only on the loopback interface. To use it from another machine, tunnel
  (`ssh -L 8080:127.0.0.1:8080 mac-host`); do not change the bind.
* `.env` is mode 0600 and never mounted into a container. `tokens.yaml` holds SHA-256 hashes of 256-bit
  tokens only; it is mode 0644 so that the container's non-root user can read it through the Colima bind
  mount (set `TOKENS_FILE_MODE=600` and run the container as your uid if you prefer 0600).
* **`docker compose config` prints resolved secrets** (database password, Jev tokens and key). Never paste
  its output into a ticket or chat; the Make targets do not print it.
* `.env`, `tokens.yaml` and `compose.local.yaml` are git-ignored and listed in `.dockerignore`, so they never
  enter an image build context. Keep backups and dumps outside the repository tree. See
  [Security.md](../../docs/Security.md) for the threat model.
