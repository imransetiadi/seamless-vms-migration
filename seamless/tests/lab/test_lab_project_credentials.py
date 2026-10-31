"""Lab verification of per-tenant credentials (QASuite LAB-TC01 and LAB-TC03; SDD §27.10, plan task
TC11) on RHOSO 18.0 and a Tier 1 community series, each cloud with two tenant projects whose own
Keystone users made an application credential with ``member`` (Operations.md §7.5). Skipped unless
the lab is configured; never runs in CI - ``tests/lab/test_lab_rehearsal.py`` runs it there through
the real provider and executor code against two in-process clouds.

    SEAMLESS_LAB_CLOUDS_YAML=~/lab/clouds.yaml \\
    SEAMLESS_LAB_SOURCE_CLOUD=rhosp17 SEAMLESS_LAB_DEST_CLOUD=rhoso18 \\
    SEAMLESS_LAB_TENANTS=~/lab/tenants.json \\
    .venv/bin/pytest -m lab tests/lab/test_lab_project_credentials.py -v -s

The clouds.yaml entries are the providers' own credentials, of a project that is none of the
tenants'. SEAMLESS_LAB_TENANTS - a JSON list, or the path of a file that holds it - names the
project pairs, two or more: a source and a destination project each, with the application
credential each project's user made, and a disposable server of the source project that boots
from a volume (the volumes are what the passes copy and LAB-TC01 finds in the destination project):

    [{"name": "tenant-a",
      "source": {"project": "<project id>", "credential": "~/lab/src-tenant-a.json",
                 "server": "<id of a disposable boot-from-volume server of the project>"},
      "destination": {"project": "<project id>", "credential": "~/lab/dst-tenant-a.json"},
      "mappings": {"networks": {"tenant-a-net": "tenant-a-net"}}},
     {"name": "tenant-b", ...}]

A credential file is what `openstack application credential create ... -f json` printed
(Operations.md §7.5 item 2). Its id and its secret go to the lab's own secret store as
PUT .../projects/{project_id}/credentials writes them, and neither is ever printed: each playbook
runs through the lab's recorder, an ansible-playbook of its own that notes whom a run's files sign
in as - by labels, a pair's project credential or a provider's own - and whether a provider's own
secret is in any of them, then runs the real one (SEAMLESS_LAB_ANSIBLE_PLAYBOOK, else
ansible-playbook from the PATH).

Read-only by default: the checks, each pair's pre-staging - its conversion hosts deployed in its two
projects with their credentials and pinned with the keys it injects (SDD §26.3, §27.5) - and a warm
pre-copy pass per pair; the source servers keep running, and the pre-copy's volumes go again as a
cancel's cleanup removes them. SEAMLESS_LAB_DESTRUCTIVE=1 adds each pair's warm and cold cutover,
each rolled back after its check (the source server stops meanwhile and runs again after), and
LAB-TC03's cutover with the expired credential, which must stop nothing. Use disposable servers
only.

LAB-TC03 needs SEAMLESS_LAB_TC03_CREDENTIAL: a second application credential of the first pair's
destination project, made by that project's user with an expiry a few minutes on
(`openstack application credential create tc03 --role member --expiration <UTC time> -f json`).
Run it on its own soon after (`-k tc03`): it waits for the expiry, at most
SEAMLESS_LAB_EXPIRY_WAIT_S (1800) seconds.

The lab's secret store and the digests its recorder tells the providers' own secrets by are
removed after the module; the recorder's notes stay beside them (`runs.jsonl`, labels alone).

Optional: SEAMLESS_LAB_DEST_KIND (rhoso; openstack for a community series);
SEAMLESS_LAB_CONVERSION_HOSTS, a JSON {"source": {...}, "destination": {...}} of
ConversionHostConfig - the hosts' name, image, flavor, external_network, ssh_user -;
SEAMLESS_LAB_DATA_DIR, a data directory kept from one run to the next - the store with the hosts'
pins, the installation key they accept -, without which a run needs each tenant project's
conversion hosts deleted first: a new run's pre-staging creates and pins them.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from seamless_migrate import __version__
from seamless_migrate.api.app import build_services
from seamless_migrate.config import Settings, find_repo_root
from seamless_migrate.credentials import cutover_hold, implementation
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy
from seamless_migrate.domain.models import (
    ConversionHostConfig,
    Mappings,
    Migration,
    Plan,
    ProjectCredential,
    Provider,
    VMRef,
    to_utc,
    utcnow,
)
from seamless_migrate.executors.ansible import run_dir
from seamless_migrate.executors.base import (
    PermanentStepError,
    StepName,
    StepResult,
    TransientStepError,
)
from seamless_migrate.providers.openstack import connect as openstack_connect
from seamless_migrate.security.secret_store import FileSecretStore, project_secret_name
from seamless_migrate.security.secrets import load_cloud_auth
from seamless_migrate.store import NotFound, Store
from tests.executor_support import Recorder, make_ctx
from tests.factories import make_migration, make_plan

pytestmark = pytest.mark.lab

ENV = (
    "SEAMLESS_LAB_CLOUDS_YAML",
    "SEAMLESS_LAB_SOURCE_CLOUD",
    "SEAMLESS_LAB_DEST_CLOUD",
    "SEAMLESS_LAB_TENANTS",
)
#: the lab's providers, by side
SIDES = {"source": "lab-source", "destination": "lab-destination"}
#: what the provider check reads of a cloud, recorded with the results (QASuite §13.1e)
VERSION_KEYS = (
    "compute_microversion",
    "block_storage_microversion",
    "release_range",
    "release",
    "support_tier",
    "policy_profile",
    "project",
)
#: what a project credential's check read, recorded with the results (SDD §10)
ENTRY_KEYS = (
    "project_name",
    "domain",
    "user_id",
    "roles",
    "restricted",
    "expires_at",
    "checked_at",
)

#: the lab's ansible-playbook: whom a run's files sign in as, by the labels of the digests of the
#: credentials' ids and users, never a value, and whether a provider's own secret is in any of them;
#: then the real one, with the same arguments, answering as it answers
RECORDER = textwrap.dedent(
    """\
    #!{python}
    import hashlib, json, os, subprocess, sys
    from pathlib import Path
    import yaml

    REAL, LOG, LABELS = {real!r}, {log!r}, {labels!r}

    class Loader(yaml.SafeLoader):
        pass  # as Ansible reads the run's files: a !unsafe scalar is a plain string

    Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))

    def digest(value):
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    def load(path):
        try:
            return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=Loader)
        except (OSError, yaml.YAMLError):
            return None

    def scalars(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from scalars(value)
        elif isinstance(node, list):
            for value in node:
                yield from scalars(value)
        elif node is not None and not isinstance(node, bool):
            yield str(node)

    def who(auth, labels):
        if not isinstance(auth, dict):
            return None
        ident = auth.get("application_credential_id") or auth.get("username")
        return labels.get(digest(ident), "an unknown credential") if ident else "no credential"

    argv = sys.argv[1:]
    code = subprocess.call([REAL, *argv])
    known = json.loads(Path(LABELS).read_text(encoding="utf-8"))
    labels, own = known["labels"], set(known["own"])
    files = {{"inventory.yml": argv[argv.index("-i") + 1]}}
    files.update({{os.path.basename(a[1:]): a[1:] for a in argv if a.startswith("@")}})
    docs = {{name: load(path) for name, path in files.items()}}
    # the clouds.yaml os-migrate's prelude wrote from the run's auth variables, before the
    # executor deletes it
    data_dir = (docs.get("vars.yml") or {{}}).get("os_migrate_data_dir") or "."
    clouds = load(Path(str(data_dir)) / "clouds.yaml")
    if clouds is not None:
        docs["clouds.yaml"] = clouds
    secrets = docs.get("secrets.yml") or {{}}
    entries = ((clouds or {{}}).get("clouds") or {{}}) if isinstance(clouds, dict) else {{}}
    sides = ("src", "dst")
    record = {{
        "playbook": os.path.basename(argv[argv.index("-i") + 2]),
        "exit": code,
        "secrets": {{s: who(secrets.get("os_migrate_" + s + "_auth"), labels) for s in sides}},
        "keys": {{s: sorted(secrets.get("os_migrate_" + s + "_auth") or {{}}) for s in sides}},
        "clouds_yaml": None if clouds is None else {{
            s: who((entries.get(s) or {{}}).get("auth"), labels) for s in sides}},
        "own_secret_in": sorted(
            name for name, doc in docs.items() if any(digest(v) in own for v in scalars(doc))),
    }}
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\\n")
    sys.exit(code)
    """
)


def _digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _time(text: str) -> datetime:
    """A time as the check report writes it (``utc_text``), as a UTC datetime."""
    return to_utc(datetime.fromisoformat(str(text).replace("Z", "+00:00")))


@dataclass
class Credential:
    """An application credential as `openstack application credential create -f json` printed it
    (Operations.md §7.5 item 2): its id and its secret, printed nowhere - in no repr either -, and
    its expiry as Keystone printed it."""

    credential_id: str = field(repr=False)
    secret: str = field(repr=False)
    expires_at: str | None = None


def _credential(path: Path) -> Credential:
    """The credential a file holds, whatever case the client names its keys in."""
    printed = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    read = {str(key).lower().replace(" ", "_"): value for key, value in printed.items()}
    missing = [key for key in ("id", "secret") if not read.get(key)]
    assert not missing, f"{path} holds no {' or '.join(missing)} of an application credential"
    return Credential(str(read["id"]), str(read["secret"]), read.get("expires_at"))


@dataclass
class Pair:
    """A project pair (SDD §27.4): a source project with a disposable server, a destination
    project, each with the application credential its user made, and its plans' mappings."""

    name: str
    source_project: str
    server_id: str
    destination_project: str
    source_credential: Credential
    destination_credential: Credential
    mappings: Mappings

    def project(self, side: str) -> str:
        return self.source_project if side == "source" else self.destination_project


def _pairs(value: str) -> list[Pair]:
    """SEAMLESS_LAB_TENANTS: a JSON list of the project pairs, or the path of a file holding it."""
    listed = value.lstrip().startswith("[")
    text = value if listed else Path(value).expanduser().read_text(encoding="utf-8")
    pairs = []
    for index, entry in enumerate(json.loads(text), 1):
        source, destination = entry["source"], entry["destination"]
        pairs.append(
            Pair(
                name=str(entry.get("name") or f"pair-{index}"),
                source_project=str(source["project"]),
                server_id=str(source["server"]),
                destination_project=str(destination["project"]),
                source_credential=_credential(Path(source["credential"])),
                destination_credential=_credential(Path(destination["credential"])),
                mappings=Mappings(**(entry.get("mappings") or {})),
            )
        )
    return pairs


async def _until(expiry: datetime) -> None:
    """Wait until a credential's expiry has passed, a minute more for the clocks of the lab's host
    and of Keystone (the rehearsal expires the credential at once instead)."""
    left = (expiry - utcnow()).total_seconds() + 60
    if left > 0:
        await asyncio.sleep(left)


def _commit() -> str:
    """The Seamless commit the lab runs, for the record (QASuite §13.1e); unknown where no git or
    no checkout tells it."""
    try:
        found = subprocess.run(
            ["git", "-C", str(find_repo_root()), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return found.stdout.strip() or "unknown"


def _close(conn: Any) -> None:
    with contextlib.suppress(Exception):
        conn.close()


@dataclass
class Lab:
    """The lab's clouds as two providers holding each pair's project credentials, the control
    plane that acts on them - the provider check, the provider registry, the executors and the
    orchestrator's book of the conversion hosts' pins over a store of its own -, and the record of
    the runs. ``connect`` opens a connection as Seamless does, for the lab's own reads, and
    ``wait_for_expiry`` waits for a credential's expiry: the rehearsal injects its doubles."""

    settings: Settings
    pairs: list[Pair]
    services: Any
    recorder_log: Path
    labels: dict[str, str] = field(repr=False)
    tc03_credential: Credential | None = None
    connect: Callable[..., Any] = openstack_connect
    wait_for_expiry: Callable[[datetime], Awaitable[None]] = _until
    #: the label of the credential each provider holds now for a project, by (side, project)
    _written: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)
    _started: bool = field(default=False, repr=False)
    _ready: dict[str, VMRef] = field(default_factory=dict, repr=False)
    _plans: dict[tuple[str, str], Plan] = field(default_factory=dict, repr=False)

    @property
    def store(self) -> Any:
        return self.services.store

    @property
    def orch(self) -> Any:
        return self.services.orchestrator

    @property
    def registry(self) -> Any:
        return self.orch.providers

    @property
    def executor(self) -> Any:
        return self.orch.executors.ansible

    @property
    def destructive(self) -> bool:
        return os.environ.get("SEAMLESS_LAB_DESTRUCTIVE") == "1"

    def provider(self, side: str) -> Provider:
        """The side's provider as the store holds it now: its checks, its project credentials and
        their hosts' pins."""
        return self.store.get("provider", SIDES[side], Provider)

    def credentials(self) -> list[Credential]:
        held = [c for p in self.pairs for c in (p.source_credential, p.destination_credential)]
        return held + ([self.tc03_credential] if self.tc03_credential is not None else [])

    def write_credential(self, side: str, project: str, credential: Credential) -> None:
        """An admin's write of the side's credential for ``project`` (SDD §12, §13.3), as
        PUT .../projects/{project_id}/credentials makes it: the secret in the store, the entry with
        its time - the project's conversion host pin kept - and that project's entry of the check
        report dropped."""
        provider = self.provider(side)
        name = project_secret_name(provider.id, project)
        values = {
            "application_credential_id": credential.credential_id,
            "application_credential_secret": credential.secret,
        }
        FileSecretStore(self.settings.secrets_dir).write(name, values, {})
        held = provider.project_credentials.get(project)
        entry = ProjectCredential(
            secret=name,
            updated_at=utcnow(),
            conversion_host_pin=held.conversion_host_pin if held is not None else None,
        )
        caps = dict(provider.capabilities)
        if isinstance(caps.get("projects"), dict):
            caps["projects"] = {p: e for p, e in caps["projects"].items() if p != project}
        credentials = {**provider.project_credentials, project: entry}
        updated = provider.model_copy(
            update={"project_credentials": credentials, "capabilities": caps}
        )
        self.store.put("provider", updated)
        self._written[(side, project)] = self.labels[_digest(credential.credential_id)]

    async def check(self, side: str, projects: list[str] | None = None) -> Provider:
        """The provider check (SDD §10): its own credential's, then each project credential's
        sign-in - those of ``projects`` alone, as a validation's check signs in."""
        return await self.orch.check_provider(SIDES[side], "lab", projects=projects)

    async def start(self) -> None:
        """Once: each pair's two credentials written, as an admin writes them, and both providers
        checked - each project's entry ok, with ``member`` (SDD §27.7) -; the clouds' series and
        policy profiles, the commit and the entries printed for the record (QASuite §13.1e)."""
        if self._started:
            return
        for pair in self.pairs:
            self.write_credential("source", pair.source_project, pair.source_credential)
            self.write_credential(
                "destination", pair.destination_project, pair.destination_credential
            )
        print(f"\nSeamless {__version__}, commit {_commit()}")
        for side in SIDES:
            checked = await self.check(side)
            caps = checked.capabilities
            assert checked.status == "ok", (
                f"the {side} provider's own check failed: {checked.status_message}"
            )
            print(f"{side}: " + json.dumps({key: caps.get(key) for key in VERSION_KEYS}))
            tenants = sorted({pair.project(side) for pair in self.pairs})
            own = (caps.get("project") or {}).get("id")
            assert own not in tenants, (
                f"the {side} provider's own credential is of project {own}, a tenant's: give the "
                "providers' own credentials a project of their own"
            )
            for project in tenants:
                entry = (caps.get("projects") or {}).get(project) or {}
                print(f"  project {project}: " + json.dumps({k: entry.get(k) for k in ENTRY_KEYS}))
                assert entry.get("status") == "ok", (
                    f"the {side} credential of project {project} failed its check: "
                    f"{entry.get('message')}"
                )
                assert "member" in (entry.get("roles") or []), (
                    f"the {side} credential of project {project} holds {entry.get('roles')}, "
                    "without member (SDD §27.7)"
                )
        self._started = True

    def plan(self, pair: Pair, strategy: Strategy) -> Plan:
        """The pair's plan of ``strategy`` - LAB-TC01's warm and cold plans -, naming its two
        projects, stored as a plan is: the book of a run's conversion host pins reads it to find
        the credential they belong to (SDD §7.2, §26.4)."""
        key = (pair.name, str(strategy))
        if key not in self._plans:
            plan = make_plan(
                name=f"LAB-TC {pair.name} {strategy}",
                source_provider_id=SIDES["source"],
                destination_provider_id=SIDES["destination"],
                vm_ids=[pair.server_id],
                source_project=pair.source_project,
                destination_project=pair.destination_project,
                mappings=pair.mappings,
                prestage_resources=[],
            )
            self.store.put("plan", plan)
            self._plans[key] = plan
        return self._plans[key]

    def migration(self, pair: Pair, strategy: Strategy, vm: VMRef) -> Migration:
        return make_migration(plan_id=self.plan(pair, strategy).id, vm=vm, strategy=strategy)

    async def ready(self, pair: Pair) -> VMRef:
        """Once per pair: its source server read with its source project's credential - a VM of
        that project, as a plan's VMs all are (SDD §27.5) -, and the pair pre-staged: its
        conversion hosts deployed in its two projects with their credentials and pinned on them
        with the keys the pre-staging injects (SDD §7.2, §26.3, §26.4)."""
        if pair.name in self._ready:
            return self._ready[pair.name]
        await self.start()
        source, destination = self.provider("source"), self.provider("destination")
        vm = await implementation(self.registry, source, pair.source_project).get_vm(pair.server_id)
        assert vm.project_id == pair.source_project, (
            f"server {pair.server_id} is of project {vm.project_id}, not of {pair.name}'s source "
            f"project {pair.source_project}: a plan's VMs are all of its source project (SDD §27.5)"
        )
        plan = self.plan(pair, Strategy.warm)
        before, started = len(self.runs()), time.monotonic()
        await self.executor.prestage(
            plan,
            source,
            destination,
            deploy_conversion_hosts=True,
            host_keys=self.orch.host_keys(plan.id),
        )
        runs = self.runs()[before:]
        took = time.monotonic() - started
        print(f"{pair.name}: pre-staged in {took:.0f} s, {[r['playbook'] for r in runs]}")
        self.assert_project_credentials(runs, pair)
        for side in SIDES:
            pin = self.provider(side).project_credentials[pair.project(side)].conversion_host_pin
            assert pin is not None, (
                f"{pair.name}: no conversion host is pinned on its {side} project credential "
                "(SDD §26.3, §26.4)"
            )
            print(f"{pair.name}: the {side} conversion host, server {pin.server_id} ({pin.source})")
        self._ready[pair.name] = vm
        return vm

    async def run(
        self,
        pair: Pair,
        migration: Migration,
        step: StepName,
        *,
        recorder: Recorder | None = None,
        **options: Any,
    ) -> StepResult:
        """One step of the pair's migration through the real executor, with the providers as the
        store holds them and the orchestrator's book of the run's conversion host pins; what it
        recorded kept on the migration as the orchestrator keeps it, and each run of it holding
        the pair's two project credentials alone."""
        plan = self.plan(pair, migration.strategy)
        sides = self.provider("source"), self.provider("destination")
        ctx, rec = make_ctx(plan, migration, *sides, self.settings, recorder, **options)
        ctx.host_keys = self.orch.host_keys(plan.id, migration.id)
        before, started = len(self.runs()), time.monotonic()
        result = await self.executor.run(step, ctx)
        runs = [run["playbook"] for run in self.runs()[before:]]
        took = time.monotonic() - started
        print(f"{pair.name} {migration.strategy} {step}: {took:.0f} s, {runs}")
        self.assert_project_credentials(self.runs()[before:], pair)
        if result.sync_pass is not None:
            migration.sync_passes.append(result.sync_pass)
        if result.destination_server_id:
            migration.destination_server_id = result.destination_server_id
        if rec.downtime_at and migration.downtime_started_at is None:
            migration.downtime_started_at = rec.downtime_at[0]
        return result

    def runs(self) -> list[dict[str, Any]]:
        """The recorder's notes, one per playbook run, in order."""
        if not self.recorder_log.exists():
            return []
        lines = self.recorder_log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def assert_project_credentials(self, runs: list[dict[str, Any]], pair: Pair) -> None:
        """Each run's files sign in with the pair's two project credentials alone - those the
        providers hold now -, no provider's own credential and none of its secrets: no admin
        credential reaches a run's files (SDD §7.2, §27.8). Labels, never a value."""
        expected = {
            "src": self._written[("source", pair.source_project)],
            "dst": self._written[("destination", pair.destination_project)],
        }
        for run in runs:
            playbook = run["playbook"]
            assert run["secrets"] == expected, (
                f"{playbook}: its secrets.yml signs in as {run['secrets']}, not as {expected} "
                "(SDD §7.2, §27.5)"
            )
            assert run["clouds_yaml"] in (None, expected), (
                f"{playbook}: the clouds.yaml os-migrate wrote signs in as {run['clouds_yaml']}, "
                f"not as {expected}"
            )
            assert run["own_secret_in"] == [], (
                f"{playbook}: a provider's own secret is in its {', '.join(run['own_secret_in'])} "
                "(SDD §27.8)"
            )

    def recorded_volumes(self, pair: Pair, migration: Migration) -> list[str]:
        """The destination volumes the workload's record holds (``dest_volumes``, SDD §6.3), as the
        warm passes made them."""
        path = (
            run_dir(self.settings, self.plan(pair, migration.strategy).id, migration.id)
            / "osm"
            / "workload_warm"
            / f"{migration.vm.source_id}.json"
        )
        record = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        held = (record.get("dest_volumes") or {}).values()
        return [str(volume["dest_id"]) for volume in held if volume.get("dest_id")]

    def volume_projects(self, volumes: list[str]) -> list[list[str | None]]:
        """Each volume's project, as the destination provider's own credential reads it: the
        lab's own read, beside the product's (Cinder lets an admin read another project's volume by
        id)."""
        conn = self.connect(self.provider("destination"), self.settings)
        try:
            return [[v, conn.block_storage.get_volume(v).project_id] for v in volumes]
        finally:
            _close(conn)

    def landed(self, server_id: str) -> dict[str, list[list[str | None]]]:
        """The projects of a destination server, of each volume attached to it, of each of its
        ports and of each floating IP on them, as the destination provider's own credential reads
        them - the lab's own read (Nova, Cinder and Neutron let an admin read another project's by
        id, SDD §27.6)."""
        conn = self.connect(self.provider("destination"), self.settings)
        try:
            server = conn.compute.get_server(server_id)
            attached = [
                v.get("id") if isinstance(v, Mapping) else getattr(v, "id", None)
                for v in server.attached_volumes or []
            ]
            ports = list(conn.network.ports(device_id=server_id))
            return {
                "server": [[server_id, server.project_id]],
                "volumes": [[v, conn.block_storage.get_volume(v).project_id] for v in attached],
                "ports": [[port.id, port.project_id] for port in ports],
                "floating_ips": [
                    [ip.floating_ip_address, ip.project_id]
                    for port in ports
                    for ip in conn.network.ips(port_id=port.id)
                ],
            }
        finally:
            _close(conn)

    def token_expiry(self, side: str, project: str) -> str | None:
        """When a token of the side's credential for ``project`` expires, as Keystone issued it -
        capped at the application credential's expiry since the fix of CVE-2022-2447 (SDD §27.7,
        `TODO(LAB): confirm`): recorded, not judged."""
        conn = self.connect(self.provider(side), self.settings, project_id=project)
        try:
            expires = conn.session.auth.get_access(conn.session).expires
            return expires.isoformat() if expires is not None else None
        finally:
            _close(conn)

    def seen_by(self, project: str, server_id: str, volumes: list[str]) -> list[str]:
        """What of a destination server, of its volumes and of its ports the destination
        provider's credential for ``project`` reads - by id, Nova's and Cinder's 404 for another
        project's, and in Neutron's port list, which holds the token's project's alone (SDD
        §27.6)."""
        import openstack.exceptions

        conn = self.connect(self.provider("destination"), self.settings, project_id=project)
        reads = [("server", lambda: conn.compute.get_server(server_id))]
        reads += [(f"volume {v}", lambda v=v: conn.block_storage.get_volume(v)) for v in volumes]
        seen = []
        try:
            for what, read in reads:
                with contextlib.suppress(openstack.exceptions.NotFoundException):
                    read()
                    seen.append(what)
            return seen + [f"port {port.id}" for port in conn.network.ports(device_id=server_id)]
        finally:
            _close(conn)

    async def landed_in(self, pair: Pair, server_id: str | None, name: str) -> None:
        """LAB-TC01's check of a cutover's server ``name``: it, each volume it holds, each of its
        ports and each floating IP on them are of the pair's destination project (SDD §27.6); the
        pair's destination credential finds it by its name, the destination provider's own
        credential - whose list is its own project's - does not, nor does another pair's, whose
        credential reads neither the server nor its volumes nor its ports."""
        assert server_id, f"{pair.name}: the cutover recorded no destination server"
        landed = await asyncio.to_thread(self.landed, server_id)
        print(f"{pair.name}: landed {json.dumps(landed)}")
        wrong = {
            kind: [held for held in found if held[1] != pair.destination_project]
            for kind, found in landed.items()
        }
        assert not any(wrong.values()), (
            f"{pair.name}: outside its destination project {pair.destination_project}: {wrong}"
        )
        assert landed["volumes"] and landed["ports"], (
            f"{pair.name}: server {server_id} holds no volume or no port: {landed}"
        )
        destination = self.provider("destination")
        mine = implementation(self.registry, destination, pair.destination_project)
        found = await mine.find_server(name)
        assert found == server_id, (
            f"{pair.name}: its destination credential finds {found} by the name {name}, not "
            f"{server_id}"
        )
        own = await self.registry.get(destination).find_server(name)
        assert own is None, f"the destination provider's own project holds {name}: {own}"
        volumes = [volume for volume, _ in landed["volumes"]]
        for other in self.pairs:
            if other is not pair:
                theirs = implementation(self.registry, destination, other.destination_project)
                held = await theirs.find_server(name)
                assert held is None, f"{other.name}'s destination project holds {name}: {held}"
                project = other.destination_project
                seen = await asyncio.to_thread(self.seen_by, project, server_id, volumes)
                assert seen == [], f"{other.name}'s destination credential reads {seen}"

    async def source_power(self, pair: Pair) -> str:
        """The pair's source server's power state, as the source provider's own credential reads
        it by id (SDD §27.1)."""
        vm = await self.registry.get(self.provider("source")).get_vm(pair.server_id)
        return str(vm.power_state)


def _install_recorder(
    base: Path, real: str, labels: Mapping[str, str], own: set[str]
) -> tuple[Path, Path]:
    """The recorder and its labels file, 0700 and 0600, in ``base``: the recorder's path and its
    log's."""
    log, known = base / "runs.jsonl", base / "labels.json"
    known.write_text(json.dumps({"labels": dict(labels), "own": sorted(own)}), encoding="utf-8")
    known.chmod(0o600)
    script = base / "recorder"
    script.write_text(
        RECORDER.format(python=sys.executable, real=real, log=str(log), labels=str(known)),
        encoding="utf-8",
    )
    script.chmod(stat.S_IRWXU)
    return script, log


@pytest.fixture(scope="module")
def lab(tmp_path_factory) -> Iterator[Lab]:
    missing = [name for name in ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"lab not configured: {', '.join(missing)}")
    pytest.importorskip("openstack")
    base = tmp_path_factory.mktemp("lab-tc")
    configured = _configured(base)
    try:
        yield configured
    finally:
        # the copies of the credentials and the digests of the providers' own secrets go
        configured.services.store.dispose()
        shutil.rmtree(base / "secrets", ignore_errors=True)
        (base / "labels.json").unlink(missing_ok=True)


def _configured(base: Path) -> Lab:
    """The lab of SEAMLESS_LAB_*, its files in ``base``: its pairs, its providers in a store of its
    own - kept in SEAMLESS_LAB_DATA_DIR when it is set -, its recorder and the control plane."""
    pairs = _pairs(os.environ["SEAMLESS_LAB_TENANTS"])
    assert len(pairs) >= 2, "SEAMLESS_LAB_TENANTS names one project pair: LAB-TC01 takes two"
    tc03 = os.environ.get("SEAMLESS_LAB_TC03_CREDENTIAL")
    data_dir = os.environ.get("SEAMLESS_LAB_DATA_DIR")
    settings = Settings(
        clouds_yaml=Path(os.environ["SEAMLESS_LAB_CLOUDS_YAML"]).expanduser(),
        data_dir=Path(data_dir).expanduser() if data_dir else base / "data",
        secrets_dir=base / "secrets",
        collection_root=find_repo_root(),
        ansible_playbook=str(base / "recorder"),
        auth_disabled=True,
    )
    # the recorder's labels: each pair's credentials and each provider's own, by the digest of the
    # id or user a run's auth names; the providers' own secrets by their digests
    labels: dict[str, str] = {}
    for pair in pairs:
        labels[_digest(pair.source_credential.credential_id)] = f"{pair.name} source credential"
        labels[_digest(pair.destination_credential.credential_id)] = (
            f"{pair.name} destination credential"
        )
    expiring = _credential(Path(tc03)) if tc03 else None
    if expiring is not None:
        labels[_digest(expiring.credential_id)] = f"{pairs[0].name} TC03 credential"
    own: set[str] = set()
    clouds = {
        "source": os.environ["SEAMLESS_LAB_SOURCE_CLOUD"],
        "destination": os.environ["SEAMLESS_LAB_DEST_CLOUD"],
    }
    for side, cloud in clouds.items():
        auth = load_cloud_auth(cloud, settings)["auth"]
        ident = auth.get("application_credential_id") or auth.get("username")
        if ident:
            labels[_digest(ident)] = f"the {side} provider's own credential"
        secret = ("password", "application_credential_secret", "token")
        own |= {_digest(auth[key]) for key in secret if auth.get(key)}
    real = os.environ.get("SEAMLESS_LAB_ANSIBLE_PLAYBOOK") or "ansible-playbook"
    _, log = _install_recorder(base, real, labels, own)
    services = build_services(settings, Store(settings.db_url))
    hosts = json.loads(os.environ.get("SEAMLESS_LAB_CONVERSION_HOSTS") or "{}")
    kinds = {
        "source": ProviderKind.openstack,
        "destination": ProviderKind(os.environ.get("SEAMLESS_LAB_DEST_KIND") or "rhoso"),
    }
    for side, cloud in clouds.items():
        default = {"name": f"seamless-conv-{'src' if side == 'source' else 'dst'}"}
        provider = Provider(
            id=SIDES[side],
            name=f"lab {side}",
            kind=kinds[side],
            role=ProviderRole(side),
            # informational: the connection comes from the clouds.yaml entry
            endpoint=f"clouds.yaml:{cloud}",
            cloud=cloud,
            conversion_host=ConversionHostConfig(**hosts.get(side, default)),
        )
        with contextlib.suppress(NotFound):
            # a data directory kept from an earlier run: its credentials' host pins stay (§26.4)
            kept = services.store.get("provider", provider.id, Provider)
            provider = provider.model_copy(update={"project_credentials": kept.project_credentials})
        services.store.put("provider", provider)
    return Lab(settings, pairs, services, log, labels, expiring)


async def _stops_nothing(lab: Lab, pair: Pair, migration: Migration, step: StepName) -> None:
    """A step of the pair's migration with its expired destination credential: it fails before
    anything of it - for good, as a credential Keystone rejects fails it (SDD §7.1) -, no playbook
    runs, no downtime starts, the source keeps running, and the failure names the provider and
    the project, never an application credential."""
    rec, before = Recorder(), len(lab.runs())
    try:
        await lab.run(pair, migration, step, recorder=rec)
    except PermanentStepError as exc:
        failure = str(exc)
    except TransientStepError as exc:
        raise AssertionError(
            f"the {step} failed as transient - tried again, not failed for good as a credential "
            f"Keystone rejects fails it (SDD §7.1): {exc}"
        ) from None
    else:
        raise AssertionError(
            f"the {step} ran with the expired credential: no sign-in refused it first (SDD §7.1)"
        )
    print(f"{pair.name}: the {step} refused: {failure}")
    ran = [run["playbook"] for run in lab.runs()[before:]]
    assert ran == [], f"the {step} ran {ran} before it failed"
    assert rec.downtime_marks == 0, f"the {step} started the downtime clock"
    label = lab.provider("destination").label
    assert label in failure and pair.destination_project in failure, (
        f"the failure names neither {label} nor project {pair.destination_project}: {failure}"
    )
    leaked = any(c.credential_id in failure or c.secret in failure for c in lab.credentials())
    assert not leaked, "the failure names an application credential"
    power = await lab.source_power(pair)
    assert power == "running", f"the source server is {power} after the refused {step}"


async def test_lab_tc01_a_member_plan_lands_in_its_destination_project(lab: Lab):
    """LAB-TC01: a warm and a cold plan per project pair, each naming its source and its
    destination project, their credentials with ``member``: the pre-copy's volumes and -
    SEAMLESS_LAB_DESTRUCTIVE=1 - each cutover's server, volumes, ports and floating IPs land in the
    plan's destination project, none in the provider's own project or another tenant's, and every
    run's files hold the plan's two project credentials alone, no admin credential (SDD §7.2,
    §27.5, §27.6, §27.8); each rollback runs the source server again."""
    await lab.start()
    for pair in lab.pairs:
        vm = await lab.ready(pair)
        warm = lab.migration(pair, Strategy.warm, vm)
        await lab.run(pair, warm, StepName.PRECOPY)
        volumes = lab.recorded_volumes(pair, warm)
        assert volumes, f"{pair.name}: the pre-copy recorded no destination volume (SDD §6.3)"
        held = await asyncio.to_thread(lab.volume_projects, volumes)
        print(f"{pair.name}: the pre-copy's volumes {held}")
        assert all(project == pair.destination_project for _, project in held), (
            f"{pair.name}: the pre-copy's volumes outside its destination project "
            f"{pair.destination_project}: {held}"
        )
        if lab.destructive:
            cutover = await lab.run(pair, warm, StepName.CUTOVER)
            await lab.landed_in(pair, cutover.destination_server_id, vm.name)
            await lab.run(pair, warm, StepName.ROLLBACK)
            assert await lab.source_power(pair) == "running", f"{pair.name}: the source is down"
        # what the warm passes made goes as a cancel's cleanup removes it (SDD §5.1)
        await lab.run(pair, warm, StepName.ROLLBACK, delete_dest_volumes=True)
        if lab.destructive:
            cold = lab.migration(pair, Strategy.cold, vm)
            cutover = await lab.run(pair, cold, StepName.CUTOVER)
            await lab.landed_in(pair, cutover.destination_server_id, vm.name)
            await lab.run(pair, cold, StepName.ROLLBACK)
            assert await lab.source_power(pair) == "running", f"{pair.name}: the source is down"
    if not lab.destructive:
        pytest.skip("set SEAMLESS_LAB_DESTRUCTIVE=1 to cut each pair's server over, warm and cold")


async def test_lab_tc03_an_expired_credential_stops_nothing(lab: Lab):
    """LAB-TC03: the first pair's destination credential replaced, as an admin writes it, by one
    that expires within minutes: its check reads the expiry and the cutover gate holds the pair's
    cutover (SDD §5.4 rule 6, §27.7); once it expired, a sync pass fails before anything of it -
    for good, as Keystone rejects the credential, never with the provider's credential instead
    (§7.1, §27.5) - and, SEAMLESS_LAB_DESTRUCTIVE=1, so does the cutover, before the source stops;
    with the pair's own credential written again and checked, the gate lets go and the next pass
    runs. The check's read of the expiry and the token's are recorded (SDD §27.7, `TODO(LAB)`)."""
    expiring = lab.tc03_credential
    if expiring is None:
        pytest.skip(
            "set SEAMLESS_LAB_TC03_CREDENTIAL to a credential of the first pair's destination "
            "project that expires within minutes"
        )
    await lab.start()
    pair = lab.pairs[0]
    vm = await lab.ready(pair)
    plan, migration = lab.plan(pair, Strategy.warm), lab.migration(pair, Strategy.warm, vm)
    await lab.run(pair, migration, StepName.PRECOPY)  # with the pair's own credentials
    # an admin writes the expiring credential: its check reads the expiry, the gate holds
    lab.write_credential("destination", pair.destination_project, expiring)
    checked = await lab.check("destination", [pair.destination_project])
    entry = checked.capabilities["projects"][pair.destination_project]
    assert entry["status"] == "ok", (
        "the TC03 credential failed its check - expired already? Make another and run LAB-TC03 "
        f"at once: {entry['message']}"
    )
    assert entry["expires_at"] is not None, "the check read no expiry of it (SDD §27.7)"
    expiry = _time(entry["expires_at"])
    token = await asyncio.to_thread(lab.token_expiry, "destination", pair.destination_project)
    print(
        f"{pair.name}: the TC03 credential expires at {entry['expires_at']} as its check read it, "
        f"at {expiring.expires_at} as Keystone printed it; a token of it expires at {token}"
    )
    held = cutover_hold(plan, Strategy.warm, lab.provider("source"), checked, utcnow())
    assert held is not None and pair.destination_project in held and "within 24 hours" in held, (
        f"the gate does not hold the cutover for the credential's expiry: {held}"
    )
    print(f"{pair.name}: the gate holds the cutover: {held}")
    wait = float(os.environ.get("SEAMLESS_LAB_EXPIRY_WAIT_S") or "1800")
    if (expiry - utcnow()).total_seconds() > wait:
        pytest.skip(
            f"the TC03 credential expires more than {wait:g} s on: make one that ends sooner"
        )
    await lab.wait_for_expiry(expiry)
    await _stops_nothing(lab, pair, migration, StepName.SYNC)
    if lab.destructive:
        await _stops_nothing(lab, pair, migration, StepName.CUTOVER)
    # the pair's own credential written again and checked: the gate lets go, the next pass runs
    lab.write_credential("destination", pair.destination_project, pair.destination_credential)
    checked = await lab.check("destination", [pair.destination_project])
    held = cutover_hold(plan, Strategy.warm, lab.provider("source"), checked, utcnow())
    assert held is None, f"the gate still holds the cutover with the pair's own credential: {held}"
    again = await lab.run(pair, migration, StepName.SYNC)
    assert again.sync_pass is not None, "the next pass recorded no pass"
    # what the warm passes made goes as a cancel's cleanup removes it (SDD §5.1)
    await lab.run(pair, migration, StepName.ROLLBACK, delete_dest_volumes=True)
    if not lab.destructive:
        pytest.skip("set SEAMLESS_LAB_DESTRUCTIVE=1 to try the cutover with the expired one too")
