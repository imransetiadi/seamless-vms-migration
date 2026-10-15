# Releasing Seamless Migrate on GitHub

This is how a Seamless Migrate release is prepared, gated, tagged and published as a
[GitHub Release](https://github.com/imransetiadi/seamless-vms-migration/releases). A release is one
annotated tag `vX.Y.Z` on `main` plus a GitHub Release that carries the release notes and the
artifacts built from that tag. The release manager is the repository owner, or a maintainer the owner
names. Agents prepare the release commits and notes and never tag or publish on their own.

The release plan is in [PRD §9](docs/PRD.md). The gates come from [QASuite §13](docs/QASuite.md), and
the deployment the artifacts serve is in [SDD §17](docs/SDD.md).

---

## 1. What a release contains

| Component | Version lives in | Artifact |
|---|---|---|
| Control plane (`seamless/`) | `seamless/pyproject.toml` (`version`) and `seamless/src/seamless_migrate/__init__.py` (`__version__`, printed by `seamless --version` and returned by `GET /api/v1/health`) | container image `seamless-migrate:X.Y.Z`, built from `seamless/Containerfile` |
| Dashboard (`dashboard/`) | `dashboard/package.json` and `dashboard/package-lock.json` | `dashboard/dist`, baked into the image |
| Deployment (`deploy/`) | `deploy/compose/compose.yaml` (`${SEAMLESS_VERSION:-X.Y.Z}`, documented in `deploy/compose/README.md`), `newTag` and the `app.kubernetes.io/version` label in `deploy/openshift/kustomization.yaml`, `newTag` in `deploy/kubernetes/kustomization.yaml` | the manifests, in the release's source archive |
| Image labels | `org.opencontainers.image.version` in `seamless/Containerfile` | the label `docker inspect` shows on the image |
| os-migrate collection (repository root) | `galaxy.yml` (`version`) and `plugins/module_utils/const.py` (`OS_MIGRATE_VERSION`); `tests/unit/test_version.py` keeps them equal | `os_migrate-os_migrate-<collection version>.tar.gz` from `make build` |

The image carries the control plane, the built dashboard and this collection (SDD §17), so one image
tag stands for one product release.

## 2. Versions and tags

- **Product version:** [Semantic Versioning](https://semver.org/) `MAJOR.MINOR.PATCH`, shared by the
  control plane, the dashboard and the deployment. Before 1.0.0, a MINOR release may change behaviour
  or the API, and its notes must say how to upgrade. A PATCH release only fixes.
- **Collection version:** its own line, today 1.1.0 (see `CHANGELOG.rst`). Bump it only when the
  collection changed since its last version, and name it in every product release's notes.
- **Tags:** `vX.Y.Z` for a final release and `vX.Y.Z-rc.N` for a release candidate. They are annotated
  and made on `main` after the release PR merged. The collection's bare `X.Y.Z` tags follow upstream
  os-migrate's habit ([docs/src/devel/releasing.rst](docs/src/devel/releasing.rst)) and are not used
  here, so the two version lines never collide.
- **Never move, delete or reuse a published tag.** A wrong release is fixed by the next PATCH release.
- **Signing:** from 1.0.0 every release tag is signed (PRD §9, "signed releases"), with
  `git tag -s` and a GPG or SSH signing key the owner published. Signing earlier releases is
  recommended.

## 3. Release gates

A tag is cut only when every gate below holds, or a waiver recorded in the release report covers it.

**Every release candidate and every final release**

- [ ] CI is green on the release commit of `main`. The `ci` workflow runs the `control-plane`,
      `collection`, `dashboard`, `security`, `manifests` and `image` jobs, the last one with trivy
      (no fixable HIGH or CRITICAL) and a CycloneDX SBOM.
- [ ] `make seamless-check` is green on a clean checkout of that commit.
- [ ] No secret anywhere: `gitleaks dir -c .gitleaks-tree.toml --redact --no-banner .` and
      `gitleaks git --redact --no-banner .` are clean (CLAUDE.md §7).
- [ ] `CHANGELOG.rst` has the collection's section when the collection changed, and the release notes
      (§6) are written.

**Each release candidate** (QASuite §13.5): the lab matrix — from 0.2.0 the Tier 1 community destinations
too (QASuite §7.1 M7–M11, SDD §21.1) — data integrity, the G1 and G2 performance gates, the full resilience suite, DAST and manual accessibility run against the RC tag. Their result is
a signed release report: environment descriptor, commit, result per case, defect list, waivers.

**The final release** (QASuite §13.2): all exit criteria hold. That means the unit suites, coverage of
at least 85 %, the demo E2E, the lab, integrity, the performance gates, security (Security.md §11 and
§12), resilience, no open S1 or S2 defect, and documentation reconciled with the measured numbers.

## 4. Step by step

The commands run from the repository root. `NEW` is the version being released and `OLD` the current
one.

### 4.1 Prepare the release PR

```bash
git checkout main && git pull --ff-only
OLD=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' seamless/src/seamless_migrate/__init__.py)
NEW=0.2.0                      # or 0.2.0-rc.1 for a release candidate
git checkout -b release/v$NEW

# product version
perl -pi -e "s/^version = \"\Q$OLD\E\"/version = \"$NEW\"/" seamless/pyproject.toml
perl -pi -e "s/^__version__ = \"\Q$OLD\E\"/__version__ = \"$NEW\"/" seamless/src/seamless_migrate/__init__.py
(cd dashboard && npm version --no-git-tag-version "$NEW")
perl -pi -e "s/SEAMLESS_VERSION:-\Q$OLD\E/SEAMLESS_VERSION:-$NEW/" deploy/compose/compose.yaml
perl -pi -e "s/newTag: \Q$OLD\E/newTag: $NEW/" deploy/openshift/kustomization.yaml deploy/kubernetes/kustomization.yaml
perl -pi -e "s{app.kubernetes.io/version: \Q$OLD\E}{app.kubernetes.io/version: $NEW}" deploy/openshift/kustomization.yaml
perl -pi -e "s/image.version=\"\Q$OLD\E\"/image.version=\"$NEW\"/" seamless/Containerfile

# what pins the version and must follow by hand: the health payload the control-plane tests expect,
# the Compose README's table of defaults
grep -n "$OLD" seamless/tests/test_api_core.py deploy/compose/README.md
```

Then read every remaining `git grep -n "$OLD"` hit, and change only those that name the *current*
release. The SDD, the PRD, QASuite and Performance.md mention 0.1.0 as history and scope, so never
bulk-replace them. The SDD and the PRD belong to the controller and change in their own commit
(CLAUDE.md §1).

When the collection changed, bump it as well. Follow the version edits of
[docs/src/devel/releasing.rst](docs/src/devel/releasing.rst): `galaxy.yml`, `plugins/module_utils/const.py`
and any `os_migrate_version:` in `tests/`. Then give `CHANGELOG.rst` the new section.

Check the result:

```bash
make seamless-test                 # control plane
make dashboard-build               # dashboard (npm ci + build); cd dashboard && npm test
# collection unit tests and ansible-test sanity, as CI runs them (QASuite §14.2; make test-ansible-units is upstream's podman path)
PYTHONPATH="$COLL" ANSIBLE_COLLECTIONS_PATH="$COLL" .venv/bin/python -m pytest tests/unit -q -rs
PATH="$PWD/.venv/bin:$PATH" scripts/collection-sanity.sh
make seamless-check                # the full local gate
```

Commit with `chore(release): vX.Y.Z` and open a PR to `main`. Agents end the message with the trailer
from CLAUDE.md §2.7. Merge with a merge commit once CI is green, and never push to `main` directly.

### 4.2 Tag the merged commit

```bash
git checkout main && git pull --ff-only
git log -1 --oneline                        # the merge commit of the release PR
git tag -a "v$NEW" -m "Seamless Migrate $NEW"     # git tag -s ... once releases are signed
git push origin "v$NEW"
```

### 4.3 Build the artifacts from the tag

Always build from a clean checkout of the tag, never from a working tree with local changes. On this
project's developer machines every `docker` command targets the dedicated Colima profile: use
`docker --context colima-seamless …`, never the default context (CLAUDE.md §6).

```bash
git clone --branch "v$NEW" https://github.com/imransetiadi/seamless-vms-migration.git release-$NEW
cd release-$NEW

# collection tarball: CI builds it from the tracked files on every run (job `collection`, artifact
# `collection-tarball`, audit D-04) — download that artifact of the release commit's run, or build it
# the same way locally; `make build` is upstream's path through its builder container
gh run download --name collection-tarball --dir dist   # the run of the release commit
ls os_migrate-os_migrate-*.tar.gz

(cd dashboard && npm ci && npm run build)    # the image copies dashboard/dist
docker --context colima-seamless build -f seamless/Containerfile -t seamless-migrate:$NEW \
  --build-arg "SOURCE_COMMIT=$(git rev-parse HEAD)" .
docker run --rm seamless-migrate:$NEW --version  # prints $NEW: the image's entrypoint is the seamless CLI
```

The `ci` workflow builds the same image on `main` but pushes it nowhere. The release manager pushes it
to the project's registry and records its digest in the release notes. The manifests can then pin
the image by digest, as `deploy/openshift/kustomization.yaml` suggests for production:

```bash
docker tag seamless-migrate:$NEW <registry>/seamless-migrate:$NEW
docker push <registry>/seamless-migrate:$NEW     # note the sha256 digest it prints
```

Scan the released image and write its SBOM with [trivy](https://trivy.dev/), as the `image` job of CI
does for the image it builds:

```bash
trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 seamless-migrate:$NEW
trivy image --format cyclonedx --output sbom.cdx.json seamless-migrate:$NEW
```

Without trivy at hand, the `sbom` artifact of the CI run on the release commit describes the image CI
built from the same commit. Its base layers may be newer than the released image's, so say which one
you attach:

```bash
gh run list --workflow ci --branch main --limit 5        # find the run of the release commit
gh run download <run-id> --name sbom                     # sbom.cdx.json
```

Then write the checksums. Use `shasum -a 256` on macOS and `sha256sum` on Linux:

```bash
sha256sum os_migrate-os_migrate-*.tar.gz sbom.cdx.json > SHA256SUMS
```

### 4.4 Publish the GitHub Release

Create it as a draft, read it on GitHub, then publish:

```bash
gh release create "v$NEW" \
  --repo imransetiadi/seamless-vms-migration \
  --verify-tag \
  --draft \
  --title "Seamless Migrate $NEW" \
  --notes-file release-notes-$NEW.md \
  os_migrate-os_migrate-*.tar.gz sbom.cdx.json SHA256SUMS
#   add --prerelease for an RC (vX.Y.Z-rc.N), which never becomes "Latest"

gh release edit "v$NEW" --repo imransetiadi/seamless-vms-migration --draft=false
```

`--verify-tag` refuses to create the release if the tag was not pushed, so a release can never
point at a commit GitHub would make up. GitHub adds the source archives (zip and tar.gz) of the tag
itself.

### 4.5 After publishing

- Download the assets once and check them: `gh release download "v$NEW" && sha256sum -c SHA256SUMS`.
- Smoke the released image as the `image` job of CI does. It starts in demo mode on SQLite, and
  its health must name the version:

  ```bash
  docker run -d --name smoke -p 127.0.0.1:18080:8080 -e SEAMLESS_DEMO=true seamless-migrate:$NEW serve
  curl -fsS http://127.0.0.1:18080/api/v1/health     # {"status":"ok","version":"<NEW>",...}
  docker rm -f smoke
  ```
- Announce the release with a link to its notes, and keep the PRD §9 release plan current through the
  controller.

## 5. Patch releases

A PATCH release fixes the last MINOR line without the newer work of `main`:

```bash
git checkout -b stable/0.2 v0.2.0            # once per MINOR line; then push it
git push origin stable/0.2
git checkout -b fix/<topic> stable/0.2
git cherry-pick -x <fix commit(s) already merged to main>
```

Open the PR against `stable/0.2`, bump PATCH as in §4.1, then tag the merge commit of that branch,
`v0.2.1`, and publish it as in §4.2–§4.5. Every fix lands on `main` first, so a later MINOR release
never loses it.

## 6. Release notes template

Save as `release-notes-X.Y.Z.md` (it is not committed), fill it from the merged PRs, the commit log
(`git log --oneline vOLD..vNEW`) and `CHANGELOG.rst`, and use it with `--notes-file`.

```markdown
## Seamless Migrate X.Y.Z

**Type:** final release | release candidate N
**Commit:** <full sha> · **Collection:** os_migrate.os_migrate <collection version>
**Image:** <registry>/seamless-migrate:X.Y.Z (sha256:<digest>)

### Highlights
- <what an operator gains, one line each, with the PRD FR or SDD section>

### Upgrade notes and breaking changes
- <what to do before or after upgrading: settings, data, API, manifests>

### Fixes
- <user-visible fixes; the review or issue id>

### Security
- <fixed findings, scans run (gitleaks, trivy, SBOM attached), waivers>

### Known issues and waivers
- <open defects with workaround; waivers from the release report>

### Tested with
- <the source and destination platforms of the lab run (QASuite §7, e.g. M1 RHOSP 17.1 → RHOSO 18.0, M7 RHOSP 17.1 →
  community 2026.2 Hibiscus), with each community destination's support tier (SDD §21.1)>
- <the Python, ansible-core, openstacksdk, Node and PostgreSQL versions CI and the lab used>

### Verify
sha256sum -c SHA256SUMS
```

GitHub's "Generate release notes" button can draft the PR list in the web form. Edit its result into
this structure rather than publishing it as it stands.

## 7. What never goes into a release

- No secret, key, token, `clouds.yaml`, kubeconfig, `.env`, `tokens.yaml` or customer data, in
  notes or assets (CLAUDE.md §7). If one ever ships, rotate it first, then delete the asset, then
  tell the owner.
- No artifact built from anything but a clean checkout of the tag. The collection build skips
  git-ignored local files (`galaxy.yml` `build_ignore`, enforced by `tests/unit/test_collection_build.py`),
  but a clean checkout is the rule.
- **Ansible Galaxy:** the collection is a modified os-migrate. This repository does not publish it
  to Galaxy under upstream's `os_migrate` namespace, so its tarball is attached to the GitHub
  Release instead. Publishing to Galaxy (`scripts/publish.sh`) is for the namespace's owners, by
  agreement with upstream; see *Upstream attribution* and *Trademarks* in the [README](README.md).

## 8. Not automated yet

The repository has no release workflow. `ci.yml` runs with `contents: read` and builds the image
without pushing it, so every step above is manual on purpose. Automation could come later, in its
own reviewed change:

- a `.github/release.yml` that groups the generated notes by PR labels;
- a workflow on `push: tags: ['v*']` that builds the tarball and the image from the tag, pushes the
  image, attaches the tarball, the SBOM and `SHA256SUMS` to a draft release, and leaves publishing to
  the release manager. It needs `permissions: contents: write`, and registry credentials as repository
  secrets, never in the workflow file.

## 9. Upgrading a deployment

The schema is created and extended by the control plane itself (`Store.create_schema()` at every start adds
only what is missing, SDD §11); there is no migration tool yet (audit B-04), so the order of operations is
what protects the data. For every MINOR, and for a PATCH that touches `store.py`:

1. **Back up first**: a run of the backup CronJob (`oc create job --from=cronjob/seamless-pg-backup pre-upgrade`,
   `deploy/openshift/backup/README.md`) and a snapshot of the `seamless-data` PVC; on Compose the `pg_dump`
   recipe of `deploy/compose/README.md`. Do the restore drill of that README before the first upgrade of a
   production deployment.
2. **Quiesce**: pause every running plan from the dashboard (no cutover in flight), then scale the control
   plane to 0 (`Recreate` keeps a single writer; a rolling update would start the new version beside the old).
3. **Roll the image**: set the new tag or digest in the kustomization (or `SEAMLESS_VERSION` on Compose) and
   apply; with the two-role PostgreSQL setup of Security.md §9.4, start the new version once with the owner
   role's URL so `create_schema()` can add indexes, then switch back to the application role.
4. **Verify**: `GET /api/v1/health` reports the new `version` and `db: ok`; the dashboard lists the plans;
   resume the paused plans.
5. **Roll back** if step 4 fails: the previous tag plus the dump of step 1 (the restore runbook).

Release notes of a MINOR say which step needs more than this (a new Secret key, a new NetworkPolicy rule).
