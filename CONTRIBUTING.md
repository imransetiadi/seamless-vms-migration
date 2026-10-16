# Contributing to Seamless Migrate

Thank you for helping! Seamless Migrate moves virtual machines from Red Hat OpenStack Platform 17.1,
community OpenStack and VMware vSphere into Red Hat OpenStack Services on OpenShift (RHOSO) 18.0 or community
OpenStack 2023.1 Antelope – 2026.2 Hibiscus (docs/SDD.md §21).
People run it against production workloads, so we value correctness, safety and clear evidence over
speed. Every contribution counts: bug reports, documentation, tests, reviews and code.

English is the project language for code, commits and pull requests. Issues are welcome in English
or Bahasa Indonesia.

## Ways to contribute

- **Report a bug** or a migration that behaved unexpectedly (see [Reporting bugs](#reporting-bugs)).
- **Improve the docs**: quick starts, troubleshooting, guest OS notes, translations.
- **Pick up an issue** labelled `good first issue` or `help wanted`. Comment on it first so nobody
  duplicates your work.
- **Review pull requests.** Testing a PR in your own lab and reporting the result is very valuable.
- **Share migration results** (source, destination, strategy, guest OS, measured downtime). They
  improve the guest OS support table and the downtime model.

## Before you start

1. Read [CLAUDE.md](CLAUDE.md), the working agreement for both humans and agents.
2. The [SDD](docs/SDD.md) is binding. If your change alters behaviour, the API, the state machine,
   the security model or the data path in a way the SDD does not describe, open an issue first and
   propose the SDD change. Implementation follows the agreed design.
3. Work test-first: write or update a failing test that captures the behaviour, then make it pass.
4. For anything bigger than a small fix, open an issue first so we can agree on the approach before
   you invest your time.

## Ground rules for data

- **Never commit secrets**: credentials, tokens, keys, kubeconfigs, `clouds.yaml` with real values,
  or `.env` files. Pre-commit runs gitleaks, and CI scans as well.
- **Never commit data from real environments**: customer names, real hostnames, internal domains,
  public IP addresses, VM names, logs or console output.
- **Use documentation-safe values** in docs, tests and examples: private ranges (`10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`), the RFC 5737 documentation ranges (`192.0.2.0/24`,
  `198.51.100.0/24`, `203.0.113.0/24`) and `example.com` / `example.org` domains.
- **Redact logs** before you attach them to an issue.
- **No vendor logos.** Do not add Red Hat, OpenShift, OpenStack, VMware or other vendor logos to the
  repository, the docs or the dashboard. Refer to products by name only to describe compatibility;
  see [Trademarks and affiliation](README.md#trademarks-and-affiliation).

## Where things live

| Path | Contents |
| --- | --- |
| `plugins/`, `roles/`, `playbooks/` | The os-migrate Ansible collection plus the warm migration path |
| `seamless/` | Control plane: `src/seamless_migrate/`, `tests/`, `Containerfile` |
| `dashboard/` | React 18 + Vite + TypeScript dashboard |
| `deploy/compose/`, `deploy/openshift/`, `deploy/kubernetes/` | Docker Compose stack and Kustomize manifests |
| `tests/` | Collection tests (`unit/`, `sanity/`, `func/`, `perf/`); `e2e/` holds the demo-stack scripts and the upstream end-to-end playbooks |
| `docs/` | PRD, SDD, MEMORY, QASuite, Security, Performance, implementation plans (`superpowers/`), advisor notes (`advisor/`) and the upstream os-migrate documentation sources (`src/`) |

## Development setup

You need Colima (or any Docker host, or Podman 4.7+), the Docker CLI with the Compose plugin, GNU
`make` and `openssl`. Control-plane work needs Python 3.11+, collection work Python 3.10+ and
Ansible 2.16+, and dashboard work Node.js 22+ with npm.

### Local stack (Docker Compose)

```sh
make seamless-colima-up      # start the dedicated Colima profile "seamless"
scripts/compose-init.sh      # once: generates .env and tokens.yaml and prints the admin API token once
make seamless-demo           # full stack with simulated clouds
make seamless-logs           # follow the logs
make seamless-down           # stop the stack and keep the data
```

To use another engine, add `SEAMLESS_DOCKER_CONTEXT=default` (Docker) or `SEAMLESS_ENGINE=podman`
(Podman) to the `make` targets. Details and troubleshooting are in
[deploy/compose/README.md](deploy/compose/README.md).

### Control plane without containers

```sh
cd seamless
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,jev,collection]'
.venv/bin/pytest -q                # SQLite; set SEAMLESS_TEST_PG_URL to also test against PostgreSQL
.venv/bin/seamless serve --demo    # API at http://127.0.0.1:8080; the dashboard too once dashboard/dist is built
```

### Dashboard

```sh
make dashboard-build
cd dashboard && npx playwright install chromium && npm run test:e2e   # browser smoke test in mock mode
```

### Checks

```sh
pipx install pre-commit   # or: pip install pre-commit (not part of the dev extra)
pre-commit install        # gitleaks, ruff, shellcheck and actionlint on every commit
make seamless-check       # the tests, linters and scanners CI runs, with the 85 % coverage gate
```

`make seamless-check` leaves out the browser smoke test (`npm run test:e2e`), the manifest
validation, the container image scan, and the PostgreSQL store tests unless `SEAMLESS_TEST_PG_URL`
is set. CI runs the first two and the PostgreSQL tests on every pull request, and the image scan on
`main` and nightly.

If you touch a `Containerfile` or anything under `deploy/`, also run `make deploy-runtime-check`,
which runs the images the same way the manifests do.

The AI features are optional. You do not need Jev or agentmemory for development or tests, and
everything works with Jev turned off.

## Making a change

- Fork the repository and create a topic branch from `main`, for example `fix/warm-delta-timeout`
  or `docs/podman-quickstart`.
- Keep pull requests small and focused: one logical change per PR.
- Write commit messages in the [Conventional Commits](https://www.conventionalcommits.org/) style:
  `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`.
- Sign off every commit (see [Licensing and sign-off](#licensing-and-sign-off)).
- Update the documentation in the same PR: the README for user-facing behaviour, and the relevant
  file in `docs/` for design, security or performance changes.
- If you change migration behaviour (strategies, the downtime model, rollback and finalize, guest
  OS handling), describe how you tested it and in which environment.

## Pull request checklist

- [ ] The PR links to an issue (required for anything beyond a small fix).
- [ ] Tests were written first or updated, and `make seamless-check` passes locally.
- [ ] Coverage stays at or above the 85 % gate.
- [ ] The documentation is updated, including the SDD if the design changed.
- [ ] No secrets, customer data, real hostnames or IP addresses, or vendor logos.
- [ ] Every commit is signed off (`git commit -s`).
- [ ] Any AI assistance is disclosed in the PR description.

## Reporting bugs

Search the existing issues first. A good bug report includes:

- the Seamless Migrate version or commit;
- the source platform (Red Hat OpenStack Platform 17.1, community OpenStack, Kolla-Ansible or
  VMware vCenter) and the destination (RHOSO 18.0, or community OpenStack with its release series and
  deployment tool, e.g. 2026.2 Hibiscus on Kolla-Ansible);
- the migration strategy: `cold`, `warm`, `storage_handover`, `vmware_cold` or `vmware_warm`;
- the guest OS and its version;
- what you expected, what happened, and the steps to reproduce it;
- redacted logs, or a redacted audit export from `seamless events export`.

Never include credentials, tokens or customer data in an issue.

## Security issues

Do not open public issues for vulnerabilities. Report them privately as described in section 14
of [docs/Security.md](docs/Security.md).

## Working with upstream os-migrate

The Ansible collection in this repository (`plugins/`, `roles/`, `playbooks/`) builds on
[os-migrate](https://github.com/os-migrate/os-migrate).

- If a fix or feature would also help os-migrate users, consider proposing it upstream as well, and
  link the upstream PR in your PR here.
- When you modify a file that came from upstream, keep its existing license header. If the file
  does not have one yet, add a comment line `Modified by the Seamless Migrate project, <year>.`

## AI-assisted contributions

This project is developed with AI assistance (see [CLAUDE.md](CLAUDE.md) and `.claude/`), and
AI-assisted contributions are welcome under these conditions:

- You understand every line you submit, and you can explain and maintain it. You are the author
  and you are responsible for it.
- The same tests, checks and review apply as for any other change.
- Mention in the PR description which tool you used and what you used it for.
- Never paste secrets, customer data or private infrastructure details into an AI tool.
- Agents working in this repository must follow [CLAUDE.md](CLAUDE.md). The MCP servers in
  `.mcp.json` (Jev and agentmemory) are optional.

## Licensing and sign-off

Seamless Migrate is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree
that your contributions are licensed under the same license.

We use the [Developer Certificate of Origin](https://developercertificate.org/) (DCO). Sign off
each commit to certify that you wrote the change, or otherwise have the right to submit it under
the project's license:

```sh
git commit -s -m "fix: handle an empty volume list in the warm pre-copy"
```

This adds a `Signed-off-by: Your Name <you@example.com>` line to the commit. Use your real name and
an email address where you can be reached. If you contribute on behalf of your employer, make sure
you are allowed to. Pull requests with commits that are not signed off will be asked to amend them.

## Code of conduct

Be respectful, assume good intent, and keep feedback about the work, not the person. Harassment
and discrimination are not tolerated, and maintainers may remove comments or contributors that
break these expectations. The full text is [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md); report
concerns privately to the maintainer named in [CONTRIBUTORS.md](CONTRIBUTORS.md).
