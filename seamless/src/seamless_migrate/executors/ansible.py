"""Ansible executor: drives the os-migrate collection and vmware-migration-kit (SDD §7.2).

Each migration runs in ``{data_dir}/plans/{plan_id}/migrations/{migration_id}/`` with its own
os-migrate data dir (``osm/``). Credentials reach ``ansible-playbook`` only through a 0600
``secrets.yml`` that is deleted (with the ``clouds.yaml`` os-migrate writes) after every run.
Progress is read by tailing the os-migrate ``state_file`` and the warm state file.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
import signal
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import Settings
from ..domain.enums import ProviderKind, Strategy, SyncPassKind
from ..domain.models import Mappings, Plan, Provider, SyncPass, utcnow
from ..providers.base import ProviderError
from ..providers.vmware import parse_endpoint
from ..security.secrets import (
    SecretNotFound,
    load_cloud_auth,
    remove_quietly,
    resolve,
    resolve_private_key,
    write_secret_file,
)
from .base import PermanentStepError, StepContext, StepName, StepResult, TransientStepError

log = logging.getLogger(__name__)

KIT_PLAYBOOK = "os_migrate.vmware_migration_kit.migration"
SERVER_TYPE = "openstack.compute.Server"
OPENSTACK = frozenset({Strategy.cold, Strategy.warm})
VMWARE = frozenset({Strategy.vmware_cold, Strategy.vmware_warm})
WARM_CHUNK_SIZE = 4194304
WARM_WORKERS = 4
TRANSIENT = re.compile(r"(?i)timeout|http 503|connection reset")
#: Playbook output lines that start the source-stop task (SDD §7.2 downtime clock): the warm
#: role's "Stop the source server", the cold role's "Perform workload stop …", the kit's power-off.
STOP_TASK = re.compile(
    r"(?i)^TASK \[[^\]]*(?:stop the source server|perform workload stop|power[ _-]?off|shut ?down)"
)
#: Result lines of an Ansible task (default stdout callback).
_RESULT_LINE = re.compile(r"^(ok|changed|failed|fatal|skipping|unreachable):")
#: Private key for an existing conversion host (``ssh_key_secret``), written per run.
CONVERSION_KEY_FILE = "conversion-ssh.key"
_RESOURCE = re.compile(r"^[a-z_]+$")
ENV_PASSTHROUGH = frozenset(
    {
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME",
        "VIRTUAL_ENV", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "SSH_AUTH_SOCK",
    }
)  # fmt: skip


# -- pure helpers ---------------------------------------------------------------------------------
def workload_filter(name: str) -> list[dict[str, str]]:
    """os-migrate selects workloads by name with ``re.search``: anchor and escape it."""
    return [{"regex": "^" + re.escape(name) + "$"}]


def _rename(ref: Any, table: dict[str, str]) -> None:
    if isinstance(ref, dict) and ref.get("name") in table:
        ref["name"] = table[ref["name"]]


def _map_projects(node: Any, projects: dict[str, str]) -> None:
    if isinstance(node, dict):
        if node.get("project_name") in projects:
            node["project_name"] = projects[node["project_name"]]
        for value in node.values():
            _map_projects(value, projects)
    elif isinstance(node, list):
        for value in node:
            _map_projects(value, projects)


def apply_mappings(doc: dict[str, Any], mappings: Mappings) -> dict[str, Any]:
    """Return a copy of an exported ``workloads.yml`` rewritten with the plan mappings."""
    out = copy.deepcopy(doc)
    for resource in out.get("resources") or []:
        if resource.get("type") != SERVER_TYPE:
            continue
        params = resource.get("params") or {}
        _rename(params.get("flavor_ref"), mappings.flavors)
        for port in params.get("ports") or []:
            port_params = port.get("params") or {}
            _rename(port_params.get("network_ref"), mappings.networks)
            for fixed in port_params.get("fixed_ips_refs") or []:
                _rename((fixed.get("subnet_ref") or {}).get("network_ref"), mappings.networks)
        for fip in params.get("floating_ips") or []:
            _rename((fip.get("params") or {}).get("floating_network_ref"), mappings.networks)
        for volume in params.get("volumes") or []:
            vparams = volume.get("params") or {}
            if vparams.get("volume_type") in mappings.volume_types:
                vparams["volume_type"] = mappings.volume_types[vparams["volume_type"]]
        boot = (resource.get("_migration_params") or {}).get("boot_volume_params") or {}
        if boot.get("volume_type") in mappings.volume_types:
            boot["volume_type"] = mappings.volume_types[boot["volume_type"]]
        _map_projects(params, mappings.projects)
    return out


def effective_mappings(plan_mappings: Mappings, resolved: Mappings | None) -> Mappings:
    """Plan mappings overlaid with what preflight resolved for the migration (SDD §7.2).

    Explicit plan mappings win; resolved entries only fill the gaps.
    """
    resolved = resolved or Mappings()
    return Mappings(
        networks={**resolved.networks, **plan_mappings.networks},
        flavors={**resolved.flavors, **plan_mappings.flavors},
        volume_types={**resolved.volume_types, **plan_mappings.volume_types},
        projects={**resolved.projects, **plan_mappings.projects},
    )


def classify_failure(output: str) -> type[Exception]:
    """Transient (retried) when the failure output matches a transient pattern."""
    return TransientStepError if TRANSIENT.search(output[-8000:]) else PermanentStepError


def plan_dir(settings: Settings, plan_id: str) -> Path:
    return Path(settings.data_dir) / "plans" / plan_id


def run_dir(settings: Settings, plan_id: str, migration_id: str) -> Path:
    return plan_dir(settings, plan_id) / "migrations" / migration_id


def _common_openstack_vars(
    source: Provider, destination: Provider, osm: Path, plan_osm: Path
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "os_migrate_data_dir": str(osm),
        "os_migrate_conversion_keypair_private_path": str(plan_osm / "conversion" / "ssh.key"),
        "os_migrate_conversion_link_keypair_private_path": str(
            plan_osm / "conversion" / "link-ssh.key"
        ),
        "os_migrate_src_validate_certs": source.verify_tls,
        "os_migrate_dst_validate_certs": destination.verify_tls,
        "os_migrate_warm_chunk_size": WARM_CHUNK_SIZE,
        "os_migrate_warm_workers": WARM_WORKERS,
        # Security.md SEC-04: conversion hosts never accept password SSH logins
        "os_migrate_conversion_host_ssh_user_enable_password_access": False,
    }
    ssh_user = "cloud-user"
    for side, provider in (("src", source), ("dst", destination)):
        if provider.ca_cert_path:
            out[f"os_migrate_{side}_ca_cert"] = provider.ca_cert_path
        host = provider.conversion_host
        if host is None:
            continue
        ssh_user = host.ssh_user or ssh_user
        if host.ssh_allowed_cidr:
            # SEC-03: one security group rule for both clouds; the destination's CIDR wins
            out["os_migrate_conversion_secgroup_remote_ip_prefix"] = host.ssh_allowed_cidr
        out[f"os_migrate_{side}_conversion_host_name"] = host.name or f"os_migrate_conv_{side}"
        out[f"os_migrate_deploy_{side}_conversion_host"] = host.manage
        optional = {
            "flavor_name": host.flavor,
            "external_network_name": host.external_network,
            "image_name": host.image,
            "floating_ip_address": host.address,
        }
        for key, value in optional.items():
            if value:
                out[f"os_migrate_{side}_conversion_{key}"] = value
    out["os_migrate_conversion_host_ssh_user"] = ssh_user
    return out


def build_vars(step: StepName, ctx: StepContext) -> dict[str, Any]:
    """Non-secret extra-vars for running ``step`` (credentials go to ``secrets.yml``)."""
    step = StepName(step)
    m = ctx.migration
    rdir = run_dir(ctx.settings, ctx.plan.id, m.id)
    if m.strategy in VMWARE:
        host, _ = parse_endpoint(ctx.source.endpoint)
        mappings = ctx.plan.mappings
        out: dict[str, Any] = {
            "vcenter_hostname": host,
            "vms_list": [m.vm.name],
            "network_map": dict(mappings.networks),
            "used_mapped_networks": bool(mappings.networks),
            "use_fixed_ips": True,
            "os_migrate_vmw_data_dir": str(rdir),
            "already_deploy_conversion_host": True,
            "copy_openstack_credentials_to_conv_host": False,
            "os_migrate_tear_down": False,
            "vmware_insecure": not ctx.source.verify_tls,
            "openstack_insecure": not ctx.destination.verify_tls,
            "cbt_sync": m.strategy == Strategy.vmware_warm,
            "cutover": step == StepName.CUTOVER,
        }
        targets = set(mappings.volume_types.values())
        if len(targets) == 1:
            out["cinder_volume_type"] = targets.pop()
        return out
    out = _common_openstack_vars(
        ctx.source, ctx.destination, rdir / "osm", plan_dir(ctx.settings, ctx.plan.id) / "osm"
    )
    out["os_migrate_workloads_filter"] = workload_filter(m.vm.name)
    out["os_migrate_workload_stop_before_migration"] = (
        m.strategy == Strategy.cold and step == StepName.CUTOVER
    )
    # SDD §6.4: keep mapped volume types (apply_mappings rewrote them to RHOSO types)
    out["os_migrate_workloads_preserve_volume_type"] = bool(ctx.plan.mappings.volume_types)
    if m.strategy == Strategy.cold and step == StepName.ROLLBACK:
        # the cold path records no destination server id: the rollback matches by name
        out["os_migrate_rollback_match_by_name"] = True
    return out


def _inventory(conversion_host: dict[str, Any] | None = None) -> str:
    doc: dict[str, Any] = {
        "migrator": {
            "hosts": {
                "localhost": {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                }
            }
        }
    }
    if conversion_host:
        doc["conversion_host"] = {"hosts": conversion_host}
    return yaml.safe_dump(doc, sort_keys=False)


def conversion_key_path(ctx: StepContext) -> Path | None:
    """Where the run writes the ``ssh_key_secret`` private key (None when not configured)."""
    host = ctx.destination.conversion_host
    if host is None or not host.ssh_key_secret:
        return None
    return run_dir(ctx.settings, ctx.plan.id, ctx.migration.id) / CONVERSION_KEY_FILE


def build_inventory(ctx: StepContext) -> str:
    """Inventory: ``migrator`` (local); VMware adds the RHOSO ``conversion_host``.

    The host is reached with the ``ssh_key_secret`` key when configured (the kit has no
    prestage step that could generate one), else with the plan's conversion keypair.
    """
    host = ctx.destination.conversion_host
    if ctx.migration.strategy in VMWARE and host is not None and host.address:
        key = conversion_key_path(ctx) or (
            plan_dir(ctx.settings, ctx.plan.id) / "osm" / "conversion" / "ssh.key"
        )
        return _inventory(
            {
                host.address: {
                    "ansible_ssh_user": host.ssh_user,
                    "ansible_ssh_private_key_file": str(key),
                }
            }
        )
    return _inventory()


def build_secret_vars(
    source: Provider, destination: Provider, settings: Settings
) -> dict[str, Any]:
    """Credentials for ``secrets.yml`` (resolved from clouds.yaml / the secrets dir)."""
    try:
        if source.kind == ProviderKind.vmware:
            if not source.credentials_secret:
                raise SecretNotFound(f"{source.id} has no credentials_secret")
            creds = resolve(source.credentials_secret, settings)
            entry = load_cloud_auth(destination.cloud or "", settings)
            dst_cloud = {"auth": entry["auth"]}
            for key in ("region_name", "interface", "identity_api_version", "auth_type"):
                if entry.get(key):
                    dst_cloud[key] = entry[key]
            if destination.region:
                dst_cloud["region_name"] = destination.region
            out: dict[str, Any] = {
                "vcenter_username": creds["username"],
                "vcenter_password": creds["password"],
                "dst_cloud": dst_cloud,
            }
            if creds.get("datacenter"):
                out["vcenter_datacenter"] = creds["datacenter"]
            return out
        out = {}
        for side, provider in (("src", source), ("dst", destination)):
            entry = load_cloud_auth(provider.cloud or "", settings)
            out[f"os_migrate_{side}_auth"] = entry["auth"]
            if entry.get("auth_type"):
                out[f"os_migrate_{side}_auth_type"] = entry["auth_type"]
            region = provider.region or entry.get("region_name")
            if region:
                out[f"os_migrate_{side}_region_name"] = region
        return out
    except SecretNotFound as exc:
        raise PermanentStepError(f"credentials unavailable: {exc}") from None


def _read_progress(path: Path) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    values = (
        [float(v) for v in data.values() if isinstance(v, int | float)]
        if isinstance(data, dict)
        else []
    )
    return round(sum(values) / len(values), 2) if values else None


def _read_warm_state(osm: Path, server_id: str) -> dict[str, Any] | None:
    path = osm / "workload_warm" / f"{server_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class _Playbook:
    name: str
    stops_source: bool = False


# -- executor -----------------------------------------------------------------------------------
class AnsibleExecutor:
    name = "ansible"

    def __init__(self, settings: Settings, *, providers: Any = None, poll_s: float = 1.0) -> None:
        self.settings = settings
        self._providers = providers
        self.poll_s = poll_s

    @property
    def providers(self) -> Any:
        if self._providers is None:
            from ..providers.registry import ProviderRegistry

            self._providers = ProviderRegistry(self.settings)
        return self._providers

    def supports(self, strategy: Strategy) -> bool:
        return Strategy(strategy) in OPENSTACK | VMWARE

    # -- prestage ------------------------------------------------------------------------------
    async def prestage(
        self,
        plan: Plan,
        source: Provider,
        destination: Provider,
        *,
        deploy_conversion_hosts: bool | None = None,
    ) -> None:
        if source.kind == ProviderKind.vmware:
            return  # the kit prepares its own conversion host
        pdir = plan_dir(self.settings, plan.id)
        osm = pdir / "osm"
        playbooks: list[_Playbook] = []
        for resource in plan.prestage_resources:
            if not _RESOURCE.match(resource):
                raise PermanentStepError(f"invalid prestage resource {resource!r}")
            playbooks += [_Playbook(f"export_{resource}.yml"), _Playbook(f"import_{resource}.yml")]
        if deploy_conversion_hosts is None:
            hosts = [source.conversion_host, destination.conversion_host]
            deploy_conversion_hosts = all(hosts) and any(h.manage for h in hosts if h)
        if deploy_conversion_hosts:
            playbooks.append(_Playbook("deploy_conversion_hosts.yml"))
        variables = _common_openstack_vars(source, destination, osm, osm)
        await self._execute(
            pdir, osm, variables, source, destination, _inventory(), playbooks, None, None
        )

    # -- steps ---------------------------------------------------------------------------------
    def _playbooks(self, step: StepName, strategy: Strategy, osm: Path) -> list[_Playbook]:
        exported = (osm / "workloads.yml").exists()
        export = [] if exported else [_Playbook("export_workloads.yml")]
        if strategy == Strategy.cold:
            if step == StepName.CUTOVER:
                return [
                    _Playbook("export_workloads.yml"),
                    _Playbook("import_workloads.yml", stops_source=True),
                ]
            if step == StepName.ROLLBACK:
                return [_Playbook("rollback_workloads.yml")]
        elif strategy == Strategy.warm:
            if step in (StepName.PRECOPY, StepName.SYNC):
                return [*export, _Playbook("import_workloads_precopy.yml")]
            if step == StepName.CUTOVER:
                return [*export, _Playbook("import_workloads_cutover.yml", stops_source=True)]
            if step == StepName.ROLLBACK:
                return [_Playbook("rollback_workloads.yml")]
        elif strategy == Strategy.vmware_warm and step in (StepName.PRECOPY, StepName.SYNC):
            return [_Playbook(KIT_PLAYBOOK)]
        elif strategy in VMWARE and step == StepName.CUTOVER:
            return [_Playbook(KIT_PLAYBOOK, stops_source=True)]
        return []

    async def run(self, step: StepName, ctx: StepContext) -> StepResult:
        step = StepName(step)
        strategy = Strategy(ctx.migration.strategy)
        if not self.supports(strategy):
            raise PermanentStepError(f"the Ansible executor does not handle {strategy}")
        if step == StepName.FINALIZE:
            return await self._finalize(ctx)
        if step == StepName.ROLLBACK and strategy in VMWARE:
            return await self._vmware_rollback(ctx)
        rdir = run_dir(self.settings, ctx.plan.id, ctx.migration.id)
        osm = rdir / "osm"
        playbooks = self._playbooks(step, strategy, osm)
        if not playbooks:
            raise PermanentStepError(f"step {step} does not apply to strategy {strategy}")
        started = utcnow()
        await self._execute(
            rdir,
            osm,
            build_vars(step, ctx),
            ctx.source,
            ctx.destination,
            build_inventory(ctx),
            playbooks,
            ctx,
            effective_mappings(ctx.plan.mappings, ctx.migration.resolved_mappings),
        )
        return await self._result(step, strategy, ctx, osm, started)

    async def _result(
        self, step: StepName, strategy: Strategy, ctx: StepContext, osm: Path, started: Any
    ) -> StepResult:
        m = ctx.migration
        if step == StepName.ROLLBACK:
            return StepResult(details={"source_running": True})
        if strategy == Strategy.warm:
            state = await asyncio.to_thread(_read_warm_state, osm, m.vm.source_id)
            if not state or not state.get("passes"):
                raise PermanentStepError("the warm state file has no recorded pass")
            last = state["passes"][-1]
            sync_pass = SyncPass(
                number=int(last.get("number") or len(m.sync_passes) + 1),
                kind=SyncPassKind(last.get("kind") or "delta"),
                started_at=last.get("started_at") or started,
                ended_at=last.get("ended_at") or utcnow(),
                bytes_scanned=int(last.get("bytes_scanned") or 0),
                bytes_changed=int(last.get("bytes_changed") or 0),
                bytes_transferred=int(last.get("bytes_transferred") or 0),
                duration_s=last.get("duration_s"),
            )
            if step == StepName.CUTOVER:
                server = state.get("destination_server_id") or await self._lookup_server(ctx)
                return StepResult(sync_pass=sync_pass, destination_server_id=server)
            return StepResult(sync_pass=sync_pass)
        if step == StepName.CUTOVER:
            return StepResult(destination_server_id=await self._lookup_server(ctx))
        # vmware_warm CBT passes: the kit does not report byte counts to the control plane
        ended = utcnow()
        number = len(m.sync_passes) + 1
        return StepResult(
            sync_pass=SyncPass(
                number=number,
                kind=SyncPassKind.full if number == 1 else SyncPassKind.delta,
                started_at=started,
                ended_at=ended,
                duration_s=(ended - started).total_seconds(),
            )
        )

    async def _lookup_server(self, ctx: StepContext) -> str | None:
        try:
            return await self.providers.get(ctx.destination).find_server(ctx.migration.vm.name)
        except ProviderError as exc:
            await ctx.log(f"could not look up the destination server: {exc}")
            return None

    async def _vmware_rollback(self, ctx: StepContext) -> StepResult:
        vm = ctx.migration.vm
        try:
            dst = self.providers.get(ctx.destination)
            server_id = ctx.migration.destination_server_id or await dst.find_server(vm.name)
            if server_id:
                await dst.delete_server(server_id)
            await self.providers.get(ctx.source).power_on(vm.source_id)
        except ProviderError as exc:
            raise TransientStepError(f"rollback: {exc}") from exc
        return StepResult(details={"source_running": True, "deleted_server": server_id})

    async def _finalize(self, ctx: StepContext) -> StepResult:
        if not ctx.options.get("delete_source"):
            return StepResult(details={"source_deleted": False})
        if ctx.source.kind == ProviderKind.vmware:
            return StepResult(
                details={
                    "source_deleted": False,
                    "reason": "deleting VMware source VMs is not automated in 0.1.0",
                }
            )
        try:
            await self.providers.get(ctx.source).delete_server(ctx.migration.vm.source_id)
        except ProviderError as exc:
            raise TransientStepError(f"finalize: {exc}") from exc
        return StepResult(details={"source_deleted": True})

    # -- process plumbing ----------------------------------------------------------------------
    def _collections_path(self) -> str:
        base = Path(self.settings.data_dir) / "collections"
        link = base / "ansible_collections" / "os_migrate" / "os_migrate"
        root = Path(self.settings.collection_root)
        if (root / "galaxy.yml").is_file() and not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileExistsError):
                link.symlink_to(root.resolve(), target_is_directory=True)
        existing = os.environ.get("ANSIBLE_COLLECTIONS_PATH") or (
            "~/.ansible/collections:/usr/share/ansible/collections"
        )
        return f"{base}:{existing}"

    def _env(self) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if k in ENV_PASSTHROUGH or (k.startswith("ANSIBLE_") and k != "ANSIBLE_VAULT_PASSWORD")
        }
        home = Path(self.settings.data_dir) / ".ansible"
        env.update(
            {
                "ANSIBLE_COLLECTIONS_PATH": self._collections_path(),
                "ANSIBLE_HOME": str(home),
                "ANSIBLE_LOCAL_TEMP": str(home / "tmp"),
                "ANSIBLE_RETRY_FILES_ENABLED": "0",
                "ANSIBLE_NOCOLOR": "1",
                # the downtime clock reads the stop task's result line: never hide skipped hosts
                "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "True",
                "ANSIBLE_HOST_KEY_CHECKING": env.get("ANSIBLE_HOST_KEY_CHECKING", "False"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    async def _execute(
        self,
        workdir: Path,
        osm: Path,
        variables: dict[str, Any],
        source: Provider,
        destination: Provider,
        inventory: str,
        playbooks: Sequence[_Playbook],
        ctx: StepContext | None,
        mappings: Mappings | None,
    ) -> None:
        vars_path = workdir / "vars.yml"
        secrets_path = workdir / "secrets.yml"
        inventory_path = workdir / "inventory.yml"
        key_path = conversion_key_path(ctx) if ctx is not None else None
        cleanup = (secrets_path, osm / "clouds.yaml", workdir / "clouds.yaml")
        if key_path is not None:
            cleanup += (key_path,)

        def prepare() -> None:
            osm.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_secret_file(vars_path, yaml.safe_dump(variables, sort_keys=True))
            write_secret_file(inventory_path, inventory)
            secret_vars = build_secret_vars(source, destination, self.settings)
            write_secret_file(secrets_path, yaml.safe_dump(secret_vars, sort_keys=True))
            if key_path is not None:
                secret = destination.conversion_host.ssh_key_secret  # type: ignore[union-attr]
                try:
                    write_secret_file(key_path, resolve_private_key(secret, self.settings))
                except SecretNotFound as exc:
                    raise PermanentStepError(f"conversion host key unavailable: {exc}") from None

        try:
            await asyncio.to_thread(prepare)
            env = await asyncio.to_thread(self._env)
            for playbook in playbooks:
                await self._run_playbook(
                    playbook, workdir, inventory_path, vars_path, secrets_path, ctx, osm, env
                )
                if playbook.name == "export_workloads.yml" and mappings is not None:
                    await asyncio.to_thread(self._rewrite_workloads, osm, mappings)
        finally:
            # synchronous on purpose: runs even when the step task is being cancelled
            remove_quietly(*cleanup)

    @staticmethod
    def _rewrite_workloads(osm: Path, mappings: Mappings) -> None:
        path = osm / "workloads.yml"
        if not path.exists():
            raise PermanentStepError("export_workloads.yml did not produce workloads.yml")
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not any(r.get("type") == SERVER_TYPE for r in doc.get("resources") or []):
            raise PermanentStepError("the exported workloads.yml contains no server")
        path.write_text(yaml.safe_dump(apply_mappings(doc, mappings), sort_keys=False))

    def _playbook_arg(self, name: str) -> str:
        if name.endswith(".yml"):
            return str(Path(self.settings.collection_root) / "playbooks" / name)
        return name  # fully qualified collection playbook

    async def _run_playbook(
        self,
        playbook: _Playbook,
        workdir: Path,
        inventory: Path,
        variables: Path,
        secrets: Path,
        ctx: StepContext | None,
        osm: Path,
        env: dict[str, str],
    ) -> None:
        name = playbook.name
        watch_stop = playbook.stops_source and ctx is not None
        stop_seen_at: Any = None  # the stop task started; its result line confirms it ran
        cmd = [
            self.settings.ansible_playbook,
            "-i", str(inventory),
            self._playbook_arg(name),
            "-e", f"@{variables}",
            "-e", f"@{secrets}",
        ]  # fmt: skip
        if ctx is not None:
            await ctx.log(f"running {name}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workdir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                limit=2**20,
            )
        except OSError as exc:
            raise PermanentStepError(f"cannot run {self.settings.ansible_playbook}: {exc}") from exc
        tail = asyncio.create_task(self._tail(ctx, osm)) if ctx is not None else None
        lines: deque[str] = deque(maxlen=400)
        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                lines.append(line)
                if ctx is not None and line.strip():
                    await ctx.log(line[:2000])
                if watch_stop and STOP_TASK.match(line):
                    # SDD §7.2: the downtime clock starts when the source-stop task starts…
                    watch_stop = False
                    stop_seen_at = utcnow()
                elif stop_seen_at is not None and line.startswith("TASK ["):
                    # …the next task began without a result that ran: the stop was skipped
                    stop_seen_at = None
                elif stop_seen_at is not None and _RESULT_LINE.match(line):
                    # …but only once its result line shows it ran (a skipped task, e.g. with
                    # data_copy: false, prints the TASK header too and stops nothing); with a
                    # loop, keep waiting while items are skipped
                    if not line.startswith("skipping:"):
                        await ctx.mark_downtime_start(at=stop_seen_at)  # type: ignore[union-attr]
                        stop_seen_at = None
                    elif "(item=" not in line:
                        stop_seen_at = None
            returncode = await proc.wait()
            if stop_seen_at is not None and ctx is not None:
                # the playbook ended without a result line for the task: assume it ran
                await ctx.mark_downtime_start(at=stop_seen_at)
        finally:
            if tail is not None:
                tail.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tail
            if proc.returncode is None:
                await self._stop_process(proc)
        if returncode != 0:
            output = "\n".join(lines)
            detail = next(
                (ln for ln in reversed(lines) if "fatal" in ln.lower() or "error" in ln.lower()),
                lines[-1] if lines else "",
            )
            raise classify_failure(output)(f"{name} failed (exit {returncode}): {detail[:500]}")

    @staticmethod
    async def _stop_process(proc: asyncio.subprocess.Process) -> None:
        for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, sig)
            try:
                await asyncio.wait_for(proc.wait(), grace)
                return
            except TimeoutError:
                continue

    async def _tail(self, ctx: StepContext | None, osm: Path) -> None:
        if ctx is None:
            return
        state_file = osm / "workload_logs" / f"{ctx.migration.vm.name}.state"
        total = ctx.migration.bytes_total or ctx.migration.vm.used_bytes
        last: float | None = None
        while True:
            await asyncio.sleep(self.poll_s)
            pct = await asyncio.to_thread(_read_progress, state_file)
            if pct is not None and pct != last:
                last = pct
                await ctx.report_progress(pct, int(total * pct / 100), total)
