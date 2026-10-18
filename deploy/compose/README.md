# Seamless Migrate — Docker Compose stack (Linux, macOS with Colima, Podman)

Implements [SDD §17.1](../../docs/SDD.md). The stack runs the control plane, its dashboard and PostgreSQL 16
(and, optionally, a Jev MCP sidecar) on one host. The Make targets pick the engine by that host:

* **Linux** (Ubuntu, Debian, RHEL, Rocky Linux, AlmaLinux, Fedora): the machine's own Docker Engine, the Docker
  context `default`. No Colima, and no Node.js: the image builds the dashboard itself.
* **macOS**: the Colima profile **`seamless`**, through the Docker context **`colima-seamless`**. The targets
  pass it explicitly and start Colima with `--activate=false`, so your active Docker context, your `default`
  Colima profile and any other context are never changed.
* **Podman**, or another Docker context (Docker Desktop, a remote engine): see
  [Other engines and hosts](#other-engines-and-hosts).

| Service | Image | Reachable from | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | `seamless` only (internal network) | volume `pgdata`, data checksums, **password required for every connection** (`--auth-local/--auth-host=scram-sha-256`), healthcheck `pg_isready`, **not published** |
| `jev` *(profile `ai`)* | `node:22-alpine` → `npx -y @jkudish/jev-mcp@0.14.1 --http` | `seamless` only (never published; on `frontend` because it needs npm and the Jev provider API) | bearer token `JEV_MCP_AUTH_TOKEN`, key `TYPESAFE_API_KEY`, `/health` probe |
| `seamless` | built from [`seamless/Containerfile`](../../seamless/Containerfile) (context = repo root) | **`127.0.0.1:8080` only** | tokens file mounted read-only, volume `seamless-data` at `/data`, non-root, `cap_drop: ALL` |

Networks: `backend` (`internal: true`: PostgreSQL ↔ control plane, no route out) and `frontend` (control plane
↔ Jev, and the way out to cloud APIs, vCenter, conversion hosts and the host's agentmemory at
`http://host.docker.internal:3111`).

## Prerequisites

* **Linux:** Docker Engine 20.10+ with the Compose plugin v2.20+ (`depends_on.required`) from Docker's
  repository, plus `git`, `make`, `curl` and `openssl`:

  ```bash
  # RHEL 8/9/10, Rocky Linux, AlmaLinux
  sudo dnf -y install dnf-plugins-core git make openssl   # curl is there already (curl or RHEL 9's curl-minimal)
  sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
  sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  # Ubuntu, Debian: Docker's apt repository (https://docs.docker.com/engine/install/ubuntu/ or /debian/), then
  sudo apt-get install -y git make curl openssl docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  # then, on every distribution
  sudo systemctl enable --now docker       # start it now and at boot
  sudo usermod -aG docker "$USER"          # log out and back in; this account runs every make target, without sudo
  ```

  The `docker` group grants root-level rights on the machine: add only the account that runs Seamless.
  About 4 CPUs, 6 GiB of RAM and 20 GiB of disk.
* **macOS:** [Colima](https://github.com/abiosoft/colima), the Docker CLI with the Compose plugin (v2.20+), GNU
  `make`, `openssl`: `brew install colima docker docker-compose`.
* Optional: `TYPESAFE_API_KEY` in the environment of the shell that runs `compose-init.sh` (enables the Jev
  sidecar; never type it on a command line — `read -rs TYPESAFE_API_KEY && export TYPESAFE_API_KEY`), a running
  host agentmemory (`npx -y @agentmemory/agentmemory@0.9.30`, REST on `localhost:3111`).

## First run

```bash
make seamless-colima-up      # macOS only: starts profile "seamless", 4 CPU / 6 GiB / 40 GiB (idempotent; keeps your context)
scripts/compose-init.sh      # writes deploy/compose/.env (0600) and tokens.yaml; prints the admin token ONCE
make seamless-up             # or: make seamless-demo   (simulated clouds, SEAMLESS_DEMO=true)
```

The first build takes several minutes. When the build context has no built dashboard (`dashboard/dist`), the
image builds one with its own Node 22; `make dashboard-build` beforehand only makes later builds faster.

Then open <http://127.0.0.1:8080/> and sign in at `/login` with the admin token. On a Linux server, tunnel to it
from your workstation first (`ssh -L 8080:127.0.0.1:8080 you@server`). Or call the API:

```bash
read -rs TOKEN && export TOKEN                           # paste the token compose-init.sh printed; nothing echoes
hdr=$(mktemp -d)/auth && (umask 077 && printf 'Authorization: Bearer %s\n' "$TOKEN" > "$hdr")
curl -fsS http://127.0.0.1:8080/api/v1/health            # {"status":"ok",...,"db":"ok","orchestrator":{"running":true,...}}
curl -fsS http://127.0.0.1:8080/api/v1/ready             # same body; HTTP 503 while the DB or the orchestrator loop is down
curl -fsS -H @"$hdr" http://127.0.0.1:8080/api/v1/me
curl -N   -H @"$hdr" http://127.0.0.1:8080/api/v1/events/stream    # live SSE
rm -rf "${hdr%/auth}"                                    # when you are done
```

The token never goes on a command line, where `ps` shows it to every user of the host and shell history keeps it
([Security.md](../../docs/Security.md) §7.2): `read -rs` takes it without echo, `printf` - a shell builtin - writes the
header into a private file (`mktemp -d` is mode 0700), and curl reads it from there (`-H @file`).

`compose-init.sh` is idempotent (existing files are kept). `--force` regenerates both files:

* **`.env`**: new `JEV_MCP_AUTH_TOKEN`; **kept** are `POSTGRES_PASSWORD` (the database volume was initialized with
  it), API keys already stored, lines you added yourself (for example `SEAMLESS_HOST_PORT`), a removed
  `SEAMLESS_MEMORY_URL` (memory stays disabled) and your Jev on/off choice. `--rotate-db-password` is only for a
  brand-new database (`make seamless-reset CONFIRM=yes` first).
* **`tokens.yaml`**: a **new admin token** is issued — the old one stops working, every other entry of the old file
  is dropped (recreate them with `seamless token create`), and sessions or scripts holding the old token must
  sign in again. Add `--keep-token` to regenerate `.env` only and leave `tokens.yaml` and the admin token alone.

After `--force`, recreate the stack so the control plane reloads the files: `make seamless-down seamless-up`.

## Other engines and hosts

| Engine | How | Notes |
|---|---|---|
| Docker Engine on Linux (default there) | `make seamless-up` | context `default`, and `DOCKER_HOST` is honoured (a rootless engine). The targets add `compose.linux-host.yaml`, so `host.docker.internal` (the default agentmemory host) reaches the host. agentmemory there must listen beyond `127.0.0.1` (SDD §17.1) |
| Docker via Colima (default on macOS) | `make seamless-colima-up && make seamless-up` | context `colima-seamless`; your active context is never changed |
| Another Docker context | `make seamless-up SEAMLESS_DOCKER_CONTEXT=desktop-linux` | Docker Desktop, a remote engine |
| Podman 4.7+ | `make seamless-up SEAMLESS_ENGINE=podman` | `podman compose` with docker-compose as provider (recommended) or podman-compose; the targets poll `/api/v1/health` instead of `--wait` |

Every target takes the same variables (`make seamless-logs SEAMLESS_ENGINE=podman`, `make seamless-down …`).
With Podman, the host is `host.containers.internal` (set `SEAMLESS_MEMORY_URL` in `.env` for agentmemory);
rootless Podman keeps the named volumes in your user's storage. On SELinux hosts the token mount is
relabelled (`selinux: z`). If you changed `SEAMLESS_HOST_PORT` in `.env`, pass it to make too so the health
wait polls the right port.

## Make targets

| Target | What it does |
|---|---|
| `seamless-colima-up` | macOS: `colima start seamless --activate=false --cpu 4 --memory 6 --disk 40` (keeps your active Docker context), prints the daemon info and the still-active context. Linux: says that Colima is not needed |
| `seamless-init` | runs `scripts/compose-init.sh` |
| `seamless-up` | builds and starts the stack, waits until healthy (`up -d --build --wait`) |
| `seamless-demo` | like `seamless-up` with `SEAMLESS_DEMO=true`: fake providers, simulated executor, seeded plans |
| `seamless-down` | removes the containers, keeps volumes (also stops the `jev` sidecar) |
| `seamless-ps` / `seamless-logs` | status / follow logs (`SEAMLESS_SERVICE=seamless` to filter) |
| `seamless-reset` | **deletes** containers and volumes; needs `CONFIRM=yes` |
| `seamless-test` | `cd seamless && .venv/bin/pytest -q` (export `SEAMLESS_TEST_PG_URL` to include PostgreSQL) |
| `seamless-check` | CI's checks that need only the venv and the dashboard's `node_modules`: control-plane tests with the coverage gate, ruff, DEMO-07, collection tests, playbook syntax and ansible-lint, the installed scanners, dashboard unit tests and build — not all of CI: it leaves out the Playwright suite and the browser journeys, the PostgreSQL store tests, the dependency audits, trivy's configuration scan and the manifest and image jobs (SDD §17.1; the repository README says how to run each) |
| `dashboard-build` | `cd dashboard && npm ci && npm run build` |

Variables: `SEAMLESS_DOCKER_CONTEXT` (default `default` on Linux, `colima-$(SEAMLESS_COLIMA_PROFILE)` on macOS),
`SEAMLESS_HOST_OS` (default: `uname -s`), `SEAMLESS_ENGINE` (`docker` or `podman`), `SEAMLESS_COLIMA_PROFILE`
(default `seamless`), `SEAMLESS_COLIMA_CPU/MEMORY/DISK`, `SEAMLESS_PROJECT` (default `seamless`),
`SEAMLESS_EXTRA_COMPOSE_FILE`. Before using Docker, every target checks that the engine answers and that its
Compose plugin is v2.20+, and says how to fix either on the host it runs on.

## Configuration

`.env` (generated; edit only the toggles) holds `POSTGRES_PASSWORD`, `JEV_MCP_AUTH_TOKEN`, optionally
`TYPESAFE_API_KEY`, `COMPOSE_PROFILES`, `SEAMLESS_JEV_MODE`, `SEAMLESS_MEMORY_URL`, `SEAMLESS_MEMORY_SECRET`.
Only the variables the Compose file wires under `environment:` reach the container (there is no `env_file`); every one of them can be overridden from `.env` or the shell. The
Compose file wires explicitly:

| Variable | Default here | Meaning |
|---|---|---|
| `SEAMLESS_DEMO`, `_DEMO_SPEED`, `_DEMO_SEED`, `_DEMO_FAILURE_RATE` | `false`, `60`, `42`, `0.1` | demo mode |
| `SEAMLESS_MAX_CONCURRENT_MIGRATIONS` / `_CUTOVERS`, `SEAMLESS_TICK_S`, `SEAMLESS_MAX_STEP_RETRIES`, `SEAMLESS_STEP_TIMEOUT_S`, `SEAMLESS_AUTH_LOCKOUT_PER_MINUTE` | `10`, `3`, `1.0`, `2`, `0` (no step ceiling), `60` | orchestrator / auth lockout |
| `SEAMLESS_JEV_MODE` | `http` (`off` when no key at init) | Jev advisor (SDD §14.1); URL `http://jev:8080/mcp` is fixed |
| `SEAMLESS_JEV_TIMEOUT_S`, `SEAMLESS_JEV_MIN_CONFIDENCE` | `20`, `0.6` | advisor bounds |
| `SEAMLESS_MEMORY_URL`, `SEAMLESS_MEMORY_SECRET` | host agentmemory, none | passed through only when defined; delete the line to disable memory |
| `SEAMLESS_MEMORY_PROJECT`, `SEAMLESS_MEMORY_REDACT_NAMES` | `seamless-migrate`, `false` | memory project id / privacy ([MEMORY.md](../../docs/MEMORY.md) §3) |
| `SEAMLESS_SECRET_STORE`, `SEAMLESS_SECRETS_DIR` | `files`, `/var/run/secrets/seamless` | provider credentials and conversion-host keys entered in the dashboard (Providers → Add or Edit): 0600 files in the `seamless-secrets` volume — back it up like a secret; `make seamless-reset` deletes it |
| `SEAMLESS_CORS_ORIGINS`, `SEAMLESS_METRICS_PUBLIC`, `SEAMLESS_LOG_LEVEL`, `SEAMLESS_LOG_JSON` | empty, `false`, `INFO`, `true` | misc (JSON logs for forwarders, SDD §15.1) |
| `SEAMLESS_HOST_PORT`, `SEAMLESS_VERSION` | `8080`, `0.1.0-rc.2` | published loopback port, image tag |

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
    user: "1000:0"                     # your uid from `id -u`, group 0: bind mounts keep the host's ownership
                                       # (on Linux and Colima alike), so a 0600 file is unreadable for another uid
    environment:
      SEAMLESS_CLOUDS_YAML: /etc/openstack/clouds.yaml
    volumes:
      - type: bind
        source: /home/<you>/.config/openstack/clouds.yaml    # macOS: /Users/<you>/.config/openstack/clouds.yaml
        target: /etc/openstack/clouds.yaml
        read_only: true
        bind:
          create_host_path: false
          selinux: z                   # RHEL, Rocky Linux, AlmaLinux, Fedora: relabel it; ignored elsewhere
```

`make seamless-up SEAMLESS_EXTRA_COMPOSE_FILE=deploy/compose/compose.local.yaml`. VMware credentials: set
`SEAMLESS_SECRET_<NAME>_USERNAME` / `_PASSWORD` in the override's `environment` (SDD §13.3), or mount a
directory laid out as `<name>/{username,password}` at `/var/run/secrets/seamless`. Prefer Keystone
application credentials ([Security.md](../../docs/Security.md) §7).

## Data, backup, reset

* Volumes: `seamless_pgdata` (system of record: plans, migrations, events), `seamless_seamless-data`
  (run directories) and `seamless_seamless-secrets` (provider credentials and conversion-host keys entered
  in the dashboard, 0600 files). `make seamless-down` keeps them; `make seamless-reset CONFIRM=yes` deletes
  them. Back up the secrets volume like a secret, separately from the database dump, or re-enter the
  credentials after a restore.
* Switching between demo and real use: reset first — demo seeds fake providers and plans into the database.
* Every PostgreSQL connection needs a password, also inside the container; the container's own
  `POSTGRES_PASSWORD` is used so the secret never appears on a command line. The `initdb` options only apply
  to a **new** `pgdata` volume (`make seamless-reset CONFIRM=yes` to adopt them).
* Backup — the dump contains plan data: write it **outside the repository**, readable only by you:

  ```bash
  umask 077 && mkdir -p "$HOME/seamless-backups"
  # bash; in zsh run `setopt SH_WORD_SPLIT` first so that $C splits into words.
  # Linux: plain `docker compose`; macOS: `docker --context colima-seamless compose`
  C="docker compose -p seamless -f deploy/compose/compose.yaml --env-file deploy/compose/.env"
  $C exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U seamless seamless' \
    > "$HOME/seamless-backups/seamless-$(date +%Y%m%d-%H%M).sql"
  ```

  An interactive shell: `$C exec -it postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U seamless seamless'`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Linux: `docker not found`, `Docker Engine does not answer` or `permission denied … docker.sock` | install Docker Engine (Prerequisites), `sudo systemctl enable --now docker`, and add your account to the `docker` group (log out and back in) |
| `Docker's Compose plugin is missing` / `older than 2.20` | install `docker-compose-plugin` from Docker's repository; distribution packages (`docker-compose` v1, `podman-docker`) do not provide it |
| SELinux (RHEL family): a mounted file is `Permission denied` although its mode allows it | add `selinux: z` to that bind mount, as the token file has (an AVC denial in `ausearch -m avc -ts recent`) |
| macOS: `Docker context 'colima-seamless' not found` / `profile is not running` | `make seamless-colima-up` |
| `POSTGRES_PASSWORD is not set` | run `scripts/compose-init.sh` (or `make seamless-init`) |
| `bind source path does not exist: …/tokens.yaml` | `.env`/`tokens.yaml` missing: run the init script (Compose never creates a directory in its place) |
| `password authentication failed for user "seamless"` | `.env` password differs from the one the `pgdata` volume was initialized with (or a stray `POSTGRES_PASSWORD` in your shell when you ran `docker compose` yourself — see Precedence). Restore the old `.env`, or `make seamless-reset CONFIRM=yes` and init again |
| `Permission denied: '/data/...'` | the image creates `/data` owned by its non-root user (UID 1001, Containerfile), so a volume created by a current image is writable; a volume first mounted by an older image keeps root ownership — one-off fix: `docker run --rm -v seamless_seamless-data:/data busybox chown -R 1001:0 /data` (macOS: `docker --context colima-seamless run …`) |
| `jev` unhealthy for ~1 min after first start | `npx` downloads the pinned package; wait for `start_period` (90 s) and check `make seamless-logs SEAMLESS_SERVICE=jev` |
| Memory calls fail from the container | `docker run --rm --add-host host.docker.internal:host-gateway busybox wget -qO- http://host.docker.internal:3111/agentmemory/livez` (macOS: `docker --context colima-seamless run --rm busybox wget …`); start the host agentmemory (on Linux listening beyond `127.0.0.1`), or delete `SEAMLESS_MEMORY_URL` from `.env` |
| Port 8080 already in use | set `SEAMLESS_HOST_PORT=8081` in `.env` |
| `http://localhost:8080/` shows another app (or its 404) although the stack is healthy | the stack listens on `127.0.0.1` only and a browser opens `localhost` on `::1` first, where another program answers (`lsof -nP -iTCP:8080 -sTCP:LISTEN`); `make seamless-up`/`seamless-demo` print a note then. Open `http://127.0.0.1:8080/`, stop that program, or set `SEAMLESS_HOST_PORT` |
| `exec: "python3": executable file not found` in the health check | the image must provide `python3` on `PATH` (UBI Python base does) |

## Security notes

* Only `seamless` is published, and only on the loopback interface. To use it from another machine, tunnel
  (`ssh -L 8080:127.0.0.1:8080 you@server`); do not change the bind.
* `.env` is mode 0600 and never mounted into a container. `tokens.yaml` holds SHA-256 hashes of 256-bit
  tokens only; it is mode 0644 so that the container's non-root user, another uid than yours, can read it
  through the bind mount (set `TOKENS_FILE_MODE=600` and run the container as your uid if you prefer 0600).
* **`docker compose config` prints resolved secrets** (database password, Jev tokens and key). Never paste
  its output into a ticket or chat; the Make targets do not print it.
* `.env`, `tokens.yaml` and `compose.local.yaml` are git-ignored and listed in `.dockerignore`, so they never
  enter an image build context. Keep backups and dumps outside the repository tree. See
  [Security.md](../../docs/Security.md) for the threat model.
