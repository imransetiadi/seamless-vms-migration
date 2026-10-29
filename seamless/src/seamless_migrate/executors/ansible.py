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
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import signal
import sys
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..ai.redaction import redact_cut, redact_tail
from ..config import Settings
from ..conversion_key import installation_key, run_key
from ..domain.enums import ProviderKind, Strategy, SyncPassKind
from ..domain.fsm import source_stopped
from ..domain.models import (
    Mappings,
    Migration,
    Nic,
    NicOverride,
    Plan,
    Provider,
    SyncPass,
    VMRef,
    next_pass_number,
    utcnow,
)
from ..planning.gpu import gpu_flavor, gpu_names, vm_gpus
from ..planning.kit import kit_vm_name
from ..planning.networks import (
    kit_ipv4,
    kit_network_key,
    kit_network_value,
    kit_subnet,
    policy_profile_of,
    port_refusal_at_run,
    resolve_network,
)
from ..planning.preflight import ceph_pools
from ..providers.base import ProviderError
from ..providers.vmware import parse_endpoint
from ..security.host_keys import (
    SIDE_ALIASES,
    VMWARE_ALIAS,
    KnownHost,
    fingerprint,
    trusted_keys,
    write_known_hosts,
)
from ..security.secret_store import ceph_key_secret_name
from ..security.secrets import (
    SecretNotFound,
    openstack_cloud_entry,
    read_secret,
    remove_quietly,
    resolve,
    resolve_private_key,
    write_secret_file,
)
from .base import (
    HostKeys,
    NoHostKeys,
    PermanentStepError,
    ProcessGroups,
    StepContext,
    StepName,
    StepResult,
    TransientStepError,
    outlive_cancel,
)

log = logging.getLogger(__name__)

KIT_PLAYBOOK = "os_migrate.vmware_migration_kit.migration"


def destination_server_name(m: Migration) -> str:
    """The name the migration's destination server gets: the VM's own from os-migrate, the kit's
    name of it from the VMware migration kit (SDD §7.2)."""
    return kit_vm_name(m.vm.name) if m.strategy in VMWARE else m.vm.name


def kit_port_name(vm_name: str) -> re.Pattern[str]:
    """The name the kit gives a port it creates for one of the VM's NICs:
    "{kit name}-NIC-{n}-VLAN-{network}" (SDD §7.2)."""
    return re.compile(re.escape(kit_vm_name(vm_name)) + r"-NIC-\d+-VLAN-")


#: the control plane's mark of a vmware_warm kit copy that completed (SDD §7.2, review RV30-01)
COPIED = {"seamless_copied": "true"}
#: its mark once a cutover's kit run may have written to it - no completed copy (SDD §7.2,
#: reviews RV32A-01, RV32A-02)
CUTOVER_COPY = {"seamless_copied": "cutover"}


def kit_volumes(vm_name: str, volumes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The volumes the kit's next run takes as the VM's own: named "{kit name}-{disk key}" with
    the metadata osm true - none for a VM without a kit name (SDD §7.2)."""
    kit = kit_vm_name(vm_name)
    if not kit:
        return []
    own = re.compile(re.escape(kit) + r"-[0-9]+")
    return [v for v in volumes if own.fullmatch(str(v.get("name") or "")) and _marked(v, "osm")]


def _marked(volume: Mapping[str, Any], key: str) -> bool:
    """Whether the volume's metadata ``key`` reads true: the kit's ``osm`` and ``converted``, the
    control plane's ``seamless_copied`` (SDD §7.2)."""
    return (volume.get("metadata") or {}).get(key) == "true"


#: Cinder states from which a volume cannot be attached - and so not synced - but is deleted
#: without a reset (SDD §7.2, review RV30-07)
UNATTACHABLE = frozenset({"error", "error_restoring", "error_extending", "error_managing"})


def _cutover_copy(volume: Mapping[str, Any]) -> bool:
    """Whether a cutover's kit run may have written to the kit volume: its copy mark reads
    'cutover' (SDD §7.2, reviews RV32A-01, RV32A-02)."""
    return (volume.get("metadata") or {}).get("seamless_copied") == CUTOVER_COPY["seamless_copied"]


def _resumable(volume: Mapping[str, Any], *, cutover: bool = False) -> bool:
    """A kit volume a vmware_warm run syncs onto by CBT: not converted, its copy completed (marked
    copied) - or, for a cutover retried inside the outage that may keep them, marked by that
    outage's cutover - and attachable (SDD §7.2, reviews RV30-01, RV30-07, RV32A-01, RV32A-02)."""
    return (
        not _marked(volume, "converted")
        and (_marked(volume, "seamless_copied") or (cutover and _cutover_copy(volume)))
        and str(volume.get("status") or "") not in UNATTACHABLE
    )


SERVER_TYPE = "openstack.compute.Server"
OPENSTACK = frozenset({Strategy.cold, Strategy.warm})
VMWARE = frozenset({Strategy.vmware_cold, Strategy.vmware_warm})
WARM_CHUNK_SIZE = 4194304
WARM_WORKERS = 4
#: a run's link entry's bound without a step timeout: 7 days (SDD §26.6)
LINK_EXPIRY_S = 7 * 24 * 3600
TRANSIENT = re.compile(r"(?i)timeout|http 503|connection reset")
#: a conversion host's key, or the link, refused in a run's output (SDD §6.6, §7.2, §26.5): the
#: collection's two refusals, and ssh's own words - also as Ansible's unreachable message carries
#: them. Read before TRANSIENT: such a run fails permanently, whatever else its output holds.
REFUSED = re.compile(
    r"host key refused: |link refused by the source conversion host|Host key verification failed\."
    r"|host key is known for \S+ and you have requested strict checking"
    r"|no matching host key type found"
)
#: what a refusal names (SDD §6.6, §7.2): the collection's host key refusal - its side, its alias
#: and its server when it knew it -, its link refusal - the source host's alias -, and the
#: inventory name of a host Ansible's ssh could not reach
_KEY_REFUSAL = re.compile(
    r"host key refused: the (source|destination) conversion host "
    r"\(([^,\s)]+)(?:, server (\S+))? at "
)
_LINK_REFUSAL = re.compile(r"link refused by the source conversion host \(([^\s)]+) at ")
_UNREACHABLE = re.compile(r"^fatal: \[([^\]]+)\]: UNREACHABLE! ")
#: the fingerprint of the key a host offered: as ssh prints it - its line breaks escaped in
#: Ansible's JSON -, or as the collection's refusal names it
_OFFERED = re.compile(
    r"(?:key sent by the remote host is(?:\s|\\[rn])+|presented the \S+ key )"
    r"(SHA256:[A-Za-z0-9+/]{43})"
)
#: Playbook output lines that start the source-stop task (SDD §7.2 downtime clock): the warm
#: role's "Stop the source server", the cold role's "Perform workload stop …", and the kit's
#: "Migrate Guest from Vmware using nbdkit <vm>" - kit 2.2.7 has no power-off task, its migrate
#: module powers the VM off inside that one (review RV26-03); a power-off or shut-down task too.
STOP_TASK = re.compile(
    r"(?i)^TASK \[[^\]]*(?:stop the source server|perform workload stop|power[ _-]?off|shut ?down"
    r"|migrate guest from vmware using nbdkit)"
)
#: Result lines of an Ansible task (default stdout callback).
_RESULT_LINE = re.compile(r"^(ok|changed|failed|fatal|skipping|unreachable):")
#: Private key for an existing conversion host (``ssh_key_secret``), written per run.
CONVERSION_KEY_FILE = "conversion-ssh.key"
#: Longest playbook output line read whole (a module result dict can be large); longer lines
#: are read in pieces of this size.
_OUTPUT_LINE_LIMIT = 2**22
_RESOURCE = re.compile(r"^[a-z_]+$")
ENV_PASSTHROUGH = frozenset(
    {
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME",
        "VIRTUAL_ENV", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "SSH_AUTH_SOCK",
    }
)  # fmt: skip
#: ``ANSIBLE_*`` variables of the control plane's environment that never reach a playbook: they
#: change what the default callback prints (the downtime clock and the failure detail parse it),
#: write the full output elsewhere, or load an arbitrary ansible.cfg (SDD §10).
ANSIBLE_ENV_DENIED = frozenset(
    {
        "ANSIBLE_VAULT_PASSWORD", "ANSIBLE_VAULT_PASSWORD_FILE", "ANSIBLE_CONFIG",
        "ANSIBLE_VERBOSITY", "ANSIBLE_DEBUG", "ANSIBLE_LOG_PATH", "ANSIBLE_STDOUT_CALLBACK",
        "ANSIBLE_CALLBACKS_ENABLED", "ANSIBLE_CALLBACK_WHITELIST", "ANSIBLE_CALLBACK_PLUGINS",
        "ANSIBLE_DISPLAY_ARGS_TO_STDOUT", "ANSIBLE_DISPLAY_SKIPPED_HOSTS", "ANSIBLE_NOCOLOR",
        "ANSIBLE_FORCE_COLOR", "ANSIBLE_HOME", "ANSIBLE_LOCAL_TEMP", "ANSIBLE_REMOTE_TEMP",
        "ANSIBLE_RETRY_FILES_ENABLED",
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


def import_floating_ip_mode(plan: Plan) -> str:
    """The import's floating_ip_mode for the plan (SDD §7.2, §8.3): skip under keep_address - the
    control plane moves the floating IPs -, else the collection's auto."""
    return "skip" if plan.network_cutover.floating_ips == "keep_address" else "auto"


def set_port_creation_mode(
    doc: dict[str, Any], mode: str, profile: str = "legacy", floating_ip_mode: str = "auto"
) -> dict[str, Any]:
    """Return a copy of an exported ``workloads.yml`` whose servers recreate their ports in
    ``mode`` (``plan.port_creation_mode``, SDD §7.2), whose destination check judges another
    project's network by the destination's policy ``profile`` (SDD §6.6, §21.5), and whose
    import treats floating IPs in ``floating_ip_mode`` (§7.2, §8.3)."""
    out = copy.deepcopy(doc)
    for resource in out.get("resources") or []:
        if resource.get("type") != SERVER_TYPE:
            continue
        if not isinstance(resource.get("_migration_params"), dict):
            resource["_migration_params"] = {}
        resource["_migration_params"]["port_creation_mode"] = mode
        resource["_migration_params"]["policy_profile"] = profile
        resource["_migration_params"]["floating_ip_mode"] = floating_ip_mode
    return out


def ports_without_security_groups(doc: dict[str, Any]) -> list[str]:
    """The networks of server ports exported without their security groups, by an os-migrate
    from before SDD §6.6's rule: Neutron would give such a port the project's default group."""
    missing: list[str] = []
    for resource in doc.get("resources") or []:
        if resource.get("type") != SERVER_TYPE:
            continue
        for port in (resource.get("params") or {}).get("ports") or []:
            params = port.get("params") or {}
            if params.get("security_group_refs") is None:
                missing.append(str((params.get("network_ref") or {}).get("name") or "?"))
    return missing


def _ip_version(address: str) -> int | None:
    try:
        return ipaddress.ip_address(address).version
    except ValueError:
        return None


def apply_nic_overrides(doc: dict[str, Any], entries: Mapping[str, NicOverride]) -> dict[str, Any]:
    """Return a copy of an exported ``workloads.yml`` whose server ports with an entry (by MAC,
    lower case) go to the entry's network, their addresses - the entry's, else their own - as
    address-only fixed IP references that Neutron places by subnet (SDD §7.2, §6.6).

    A floating IP on a port's former address stays there when the port keeps the address, else
    moves to its new address of the same IP version: os-migrate attaches it by that address.
    ``PermanentStepError`` when one that must move finds no such address or several, or when an
    entry's MAC is no exported port's (the VM's NICs changed since validation): raised after the
    export, before the import stops the source.
    """
    out = copy.deepcopy(doc)
    applied: set[str] = set()
    for resource in out.get("resources") or []:
        if resource.get("type") != SERVER_TYPE:
            continue
        server = resource.get("params") or {}
        moved: dict[str, tuple[str, list[str]]] = {}  # former address -> (MAC, new addresses)
        for port in server.get("ports") or []:
            params = port.get("params") or {}
            mac = str(params.get("mac_address") or "").lower()
            entry = entries.get(mac)
            if entry is None:
                continue
            applied.add(mac)
            former = [ref.get("ip_address") for ref in params.get("fixed_ips_refs") or []]
            if entry.network:
                params["network_ref"] = {**(params.get("network_ref") or {}), "name": entry.network}
            addresses = entry.fixed_ips if entry.fixed_ips is not None else former
            params["fixed_ips_refs"] = [{"ip_address": address} for address in addresses]
            # what a warm cutover exports again for, should the entry be removed (SDD §7.2)
            if not isinstance(port.get("_migration_params"), dict):
                port["_migration_params"] = {}
            port["_migration_params"]["nic_override"] = True
            for address in former:
                moved[str(address)] = (mac, list(addresses))
        for fip in server.get("floating_ips") or []:
            params = fip.get("params") or {}
            address = str(params.get("fixed_ip_address") or "")
            if address not in moved:
                continue
            mac, addresses = moved[address]
            if address in addresses:
                # the port keeps it (NicOverride writes addresses canonically, as Neutron does):
                # os-migrate attaches the floating IP there
                continue
            version = _ip_version(address)
            same = [a for a in addresses if _ip_version(a) == version]
            if len(same) != 1:
                count = (
                    f"{len(same)} IPv{version} addresses" if same else f"no IPv{version} address"
                )
                raise PermanentStepError(
                    f"the floating IP on {address} cannot follow NIC {mac}: its override gives it "
                    f"{count}; give the NIC exactly one, so the floating IP moves to it"
                )
            params["fixed_ip_address"] = same[0]
    unknown = sorted(set(entries) - applied)
    if unknown:
        raise PermanentStepError(
            "the export has no port with the MAC of the NIC override(s) "
            f"{', '.join(unknown)}: the VM's NICs changed since validation; validate the plan again"
        )
    return out


def _overridden_ports(doc: dict[str, Any]) -> bool:
    """Whether a server port of the export carries a NIC override applied at the export (marked
    ``_migration_params.nic_override``, SDD §7.2)."""
    return any(
        bool((port.get("_migration_params") or {}).get("nic_override"))
        for resource in doc.get("resources") or []
        if resource.get("type") == SERVER_TYPE
        for port in (resource.get("params") or {}).get("ports") or []
    )


def _scoped_ref(ref: dict[str, Any], network: dict[str, Any]) -> dict:
    # os-migrate searches the credential's own project for %auth%, and every network it sees
    # for a reference without a project (SDD §7.2)
    scope = "%auth%" if network.get("own") else None
    return {**ref, "project_name": scope, "domain_name": scope}


def _resolved_ref(
    ref: dict[str, Any], networks: Sequence[dict[str, Any]], what: str, where: str
) -> dict:
    name = ref.get("name")
    network, why = resolve_network(networks, str(name))
    if network is None:
        # the destination provider ``where`` names (SDD §1 naming)
        raise PermanentStepError(
            f"{what} cannot go to network {name!r} of {where}: "
            + (why or f"there is no network named {name!r}")
        )
    return _scoped_ref(ref, network)


def resolve_network_refs(
    doc: dict[str, Any],
    networks: Sequence[dict[str, Any]],
    deferred: list[str] | None = None,
    where: str = "the destination",
) -> dict[str, Any]:
    """Return a copy of an exported ``workloads.yml`` whose server ports and floating IPs name the
    RHOSO network the rule of SDD §7.3 step 0 resolves (``network_details``): the credential's own
    keeps ``%auth%`` as the reference's project and domain, another project's - the only one of
    its name - neither; every fixed IP reference becomes address-only, so Neutron finds the
    address in that network's subnets (SDD §7.2, §6.6). ``PermanentStepError`` names a port whose
    network does not resolve: raised before the import stops the source. Given ``deferred`` (a
    warm pre-copy or sync run, which makes no port), the failure is added there instead and the
    port keeps its references as exported. A floating IP's network that does not resolve keeps
    its reference: in auto mode a detached floating IP with the address needs no network, and
    the destination check judges it before the stop."""
    out = copy.deepcopy(doc)
    for resource in out.get("resources") or []:
        if resource.get("type") != SERVER_TYPE:
            continue
        server = resource.get("params") or {}
        for port in server.get("ports") or []:
            params = port.get("params") or {}
            what = f"the port {params.get('mac_address')} of {server.get('name')}"
            try:
                ref = _resolved_ref(params.get("network_ref") or {}, networks, what, where)
            except PermanentStepError as exc:
                if deferred is None:
                    raise
                # the cutover resolves it again and stops there, before the source stops
                deferred.append(str(exc))
                continue
            params["network_ref"] = ref
            params["fixed_ips_refs"] = [
                {"ip_address": ref["ip_address"]}
                for ref in params.get("fixed_ips_refs") or []
                if ref.get("ip_address")
            ]
        for fip in server.get("floating_ips") or []:
            params = fip.get("params") or {}
            ref = params.get("floating_network_ref")
            network = resolve_network(networks, str(ref.get("name")))[0] if ref else None
            if network is not None:
                params["floating_network_ref"] = _scoped_ref(ref, network)
    return out


#: Beside the export: what its rewrite applied (SDD §7.2 export record).
EXPORT_RECORD = "rewrite.json"


def export_record(
    mappings: Mappings, nic_overrides: Mapping[str, NicOverride] | None, name: str | None
) -> dict[str, Any]:
    """The export record (SDD §7.2): the mappings a rewrite applies - the plan's overlaid with the
    migration's resolved ones -, the VM's NIC override entries and the exported server's name, by
    which each run selects its workload (a renamed VM is exported again, review lead A). The
    rewrite applies no GPU mapping - an OpenStack GPU VM's GPU goes with its flavor mapping - so
    `gpus` is left out, and a record written before it existed still matches (SDD §22.4)."""
    return {
        "mappings": mappings.model_dump(mode="json", exclude={"gpus"}),
        "nic_overrides": {
            mac: entry.model_dump(mode="json") for mac, entry in (nic_overrides or {}).items()
        },
        "name": name,
    }


def _read_export_record(osm: Path) -> dict[str, Any] | None:
    try:
        return json.loads((osm / EXPORT_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _apply_port_mode(
    osm: Path,
    mode: str,
    cutover: bool,
    nic_overrides: bool = False,
    networks: Sequence[dict[str, Any]] | None = None,
    record: dict[str, Any] | None = None,
    exports: bool = False,
    deferred: list[str] | None = None,
    profile: str = "legacy",
    where: str = "the destination",
    floating_ip_mode: str = "auto",
) -> bool:
    """Write the plan's port_creation_mode and floating_ip_mode and the destination's policy
    ``profile`` into an
    existing export before a run, and resolve its
    network references again with the destination's ``networks`` when given, unless the run
    replaces the export: it ``exports`` anyway (the cold cutover), or True is returned (SDD §7.2).
    A warm pass passes ``deferred``, where a port network that does not resolve is added.

    True when the run must export the workload again: a cutover in "neutron" mode whose ports were
    exported without their security groups, or of a VM with NIC overrides, or that had some at the
    export (a port marked ``nic_override``: an entry removed since); or, given the plan's
    ``record``, an export whose record differs from it or that has none. The export replaces the
    server in place, so the file stays for a rollback should the new export fail.
    """
    path = osm / "workloads.yml"
    if not path.exists():
        return False
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    reexport = (
        cutover
        and (
            (mode == "neutron" and bool(ports_without_security_groups(doc)))
            or nic_overrides
            or _overridden_ports(doc)
        )
    ) or (record is not None and _read_export_record(osm) != record)
    doc = set_port_creation_mode(doc, mode, profile, floating_ip_mode)
    # a new export is resolved after it instead: the one it replaces may no longer resolve
    if networks and not (reexport or exports):
        doc = resolve_network_refs(doc, networks, deferred, where)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return reexport


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


def declared_gpu_profile(vm: VMRef, source: Provider) -> str | None:
    """The vGPU profile the VM's source provider declares for its flavor (`Provider.gpu_profiles`,
    SDD §22.3), with which a cutover reads its GPUs as §9.3's GPU rows do (§7.2, §22.2); None for a
    VMware VM, whose provider declares none."""
    return source.gpu_profiles.get(vm.flavor) if vm.flavor else None


def gpu_mapping_refusal(
    vm: VMRef,
    plan: Plan,
    source_kind: ProviderKind | str,
    inventory: Any,
    where: str = "the destination",
    *,
    declared_profile: str | None = None,
) -> str | None:
    """Why a GPU VM's mapping does not apply to the destination's flavors ``inventory`` read for
    the run (SDD §7.2, §22.4): the plan maps its GPUs to no single flavor - a VMware VM's GPU
    mappings, an OpenStack VM's mapping of its flavor, never a same-named flavor -, the flavor it
    names does not exist, or it asks for no GPU - none of the inventory's gpu_flavors (§22.3) -,
    and the server would lose its GPU. The VM's GPUs are read with ``declared_profile``, the
    profile its source declares for its flavor (`declared_gpu_profile`). None for a VM without a
    GPU and for a mapping that applies; ``where`` names the destination provider (SDD §1
    naming)."""
    gpus = vm_gpus(vm, declared_profile)
    if not gpus:
        return None
    what = f"the GPUs of {vm.name} ({gpu_names(gpus)})"
    flavor = gpu_flavor(vm, plan, source_kind, declared_profile)
    if flavor is None:
        if ProviderKind(source_kind) == ProviderKind.vmware:
            return f"the plan's GPU mappings map {what} to no single flavor of {where}"
        # the import, or the handover's step 8, would take the same-named flavor (§22.4)
        return (
            f"the plan maps no flavor to {vm.flavor!r}, which gives {what}: map it to a GPU flavor "
            f"of {where}"
        )

    def listed(key: str) -> set[str]:
        return {str(f.get("name")) for f in getattr(inventory, key, None) or []}

    if flavor not in listed("flavors"):
        why = f"there is no flavor named {flavor!r}"
    elif flavor not in listed("gpu_flavors"):
        why = "it asks for no GPU, and the server would lose its GPU"
    else:
        return None
    return f"{what} cannot go to flavor {flavor!r} of {where}: {why}"


def classify_failure(output: str) -> type[Exception]:
    """Permanent when the failure output names a refused host key or link, whatever else it holds
    (SDD §7.2, §26.5); else transient (retried) when it matches a transient pattern."""
    if REFUSED.search(output):
        return PermanentStepError
    return TransientStepError if TRANSIENT.search(output[-8000:]) else PermanentStepError


@dataclass(frozen=True)
class _Refusal:
    """What a failed run's output names of a conversion host it refused (SDD §7.2): each None
    when the output does not say."""

    #: source or destination
    side: str | None
    #: the alias or inventory name the output names it by
    name: str | None
    server_id: str | None
    #: the SHA256 fingerprint of the key the host offered, as ssh prints it
    offered: str | None
    #: the link the source host refused, not a host key
    link: bool = False


def refusal_in(output: str) -> _Refusal | None:
    """What ``output`` names of the conversion host whose key, or link, it refuses (SDD §6.6,
    §7.2): the collection's refusal, else ssh's own words in Ansible's unreachable message, by
    the host's inventory name; None when it refuses nothing. Nothing of the output is kept but
    the side, the names, a server id and a fingerprint."""
    if not REFUSED.search(output):
        return None
    offered = _OFFERED.search(output)
    fingerprint = offered.group(1) if offered else None
    key = _KEY_REFUSAL.search(output)
    if key:
        return _Refusal(key.group(1), key.group(2), key.group(3), fingerprint)
    link = _LINK_REFUSAL.search(output)
    if link:
        return _Refusal("source", link.group(1), None, None, link=True)
    for line in output.splitlines():
        unreachable = _UNREACHABLE.match(line)
        if unreachable and REFUSED.search(line):
            return _Refusal(None, unreachable.group(1), None, fingerprint)
    return _Refusal(None, None, None, fingerprint)


def plan_dir(settings: Settings, plan_id: str) -> Path:
    return Path(settings.data_dir) / "plans" / plan_id


def run_dir(settings: Settings, plan_id: str, migration_id: str) -> Path:
    return plan_dir(settings, plan_id) / "migrations" / migration_id


def _common_openstack_vars(
    source: Provider, destination: Provider, osm: Path, plan_osm: Path, conversion_key: Path
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "os_migrate_data_dir": str(osm),
        # the installation's, the one key the managed hosts accept - a run's, until it exists, the
        # key an earlier version made for its plan -; the link key is the plan's (SDD §7.2
        # "Conversion key", issue #112)
        "os_migrate_conversion_keypair_private_path": str(conversion_key),
        "os_migrate_conversion_link_keypair_private_path": str(
            plan_osm / "conversion" / "link-ssh.key"
        ),
        "os_migrate_src_validate_certs": source.verify_tls,
        "os_migrate_dst_validate_certs": destination.verify_tls,
        "os_migrate_warm_chunk_size": WARM_CHUNK_SIZE,
        "os_migrate_warm_workers": WARM_WORKERS,
        # Security.md SEC-04: conversion hosts never accept password SSH logins
        "os_migrate_conversion_host_ssh_user_enable_password_access": False,
        # each run makes its own link between the conversion hosts and removes it when it ends,
        # whatever the host key setting; the content install sets sshd for it (SDD §7.2, §26.6)
        "os_migrate_conversion_link_per_run": True,
    }
    ssh_user = "cloud-user"
    for side, provider in (("src", source), ("dst", destination)):
        if provider.ca_cert_path:
            out[f"os_migrate_{side}_ca_cert"] = provider.ca_cert_path
        host = provider.conversion_host
        # each conversion host is reached as its own provider's user (SDD §6.5, audit EX-08)
        out[f"os_migrate_{side}_conversion_host_ssh_user"] = (
            host.ssh_user if host else None
        ) or "cloud-user"
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


#: the run's Ceph files beside secrets.yml (SDD §7.2, §28.4): the minimal ceph.conf and the keyring
CEPH_CONF_FILE = "ceph.conf"
CEPH_KEYRING_FILE = "ceph.keyring"
#: seconds rbd may run on the source conversion host for one query (SDD §28.5)
EXTENT_QUERY_TIMEOUT_S = 300


def ceph_tracking(plan: Plan, source: Provider, settings: Settings) -> dict[str, Any] | None:
    """What a warm plan that tracks extents gets of its source's Ceph access (SDD §7.2, §28.4):
    the access, its key and the tracked pools - None when the plan does not track, or the source
    has no access, no key or no pool of its fsid (every pass scans; the findings of §28.9 say so).
    The key is read for the run's keyring alone: it reaches no variable and no log line."""
    if not plan.extent_tracking or source.ceph is None:
        return None
    pools = ceph_pools(source)
    if not pools:
        return None
    try:
        key = read_secret(ceph_key_secret_name(source.id), settings).get("key")
    except SecretNotFound:
        key = None
    if not key:
        return None
    return {"access": source.ceph, "key": key, "pools": pools}


def ceph_run_files(ctx: StepContext) -> list[tuple[Path, str]]:
    """The run's ceph.conf and keyring, 0600 beside secrets.yml and deleted with it (SDD §7.2),
    for a warm step of a plan whose tracking is on; [] otherwise."""
    tracking = ceph_tracking(ctx.plan, ctx.source, ctx.settings)
    if tracking is None or ctx.migration.strategy != Strategy.warm:
        return []
    access = tracking["access"]
    rdir = run_dir(ctx.settings, ctx.plan.id, ctx.migration.id)
    conf = f"[global]\nfsid = {access.fsid}\nmon_host = {','.join(access.monitors)}\n"
    keyring = f"[client.{access.client}]\n\tkey = {tracking['key']}\n"
    return [(rdir / CEPH_CONF_FILE, conf), (rdir / CEPH_KEYRING_FILE, keyring)]


def _extent_vars(step: StepName, ctx: StepContext) -> dict[str, Any]:
    """The roles' extent tracking variables of a warm pass (SDD §6.5, §7.2): check for a pre-copy
    or sync run, read for the cutover, with the Ceph files, user, pools and timeout; {} when the
    plan passes no Ceph file."""
    files = ceph_run_files(ctx)
    if not files or step not in (StepName.PRECOPY, StepName.SYNC, StepName.CUTOVER):
        return {}
    tracking = ceph_tracking(ctx.plan, ctx.source, ctx.settings)
    assert tracking is not None  # ceph_run_files found it
    out: dict[str, Any] = {
        "os_migrate_warm_ceph_conf": str(files[0][0]),
        "os_migrate_warm_ceph_keyring": str(files[1][0]),
        "os_migrate_warm_ceph_client": tracking["access"].client,
        "os_migrate_warm_ceph_pools": dict(tracking["pools"]),
        "os_migrate_warm_extent_query_timeout": EXTENT_QUERY_TIMEOUT_S,
    }
    if step == StepName.CUTOVER:
        out["os_migrate_warm_extents_final"] = "read"
    else:
        out["os_migrate_warm_extents_precopy"] = "check"
    return out


def build_vars(
    step: StepName, ctx: StepContext, network_map: dict[str, str] | None = None
) -> dict[str, Any]:
    """Non-secret extra-vars for running ``step`` (credentials go to ``secrets.yml``); a VMware
    run's ``network_map`` is what ``_vmware_network_map`` resolved, and a GPU VM's ``flavor_uuid``
    the GPU flavor its plan names (SDD §7.2, §22.4)."""
    step = StepName(step)
    m = ctx.migration
    rdir = run_dir(ctx.settings, ctx.plan.id, m.id)
    if m.strategy in VMWARE:
        host, _ = parse_endpoint(ctx.source.endpoint)
        mappings = ctx.plan.mappings
        out: dict[str, Any] = {
            "vcenter_hostname": host,
            "vms_list": [m.vm.name],
            # every NIC by the kit's key to the RHOSO network and subnet Seamless resolved: the kit
            # drops a NIC its map lacks, and without the map sends every NIC to an unset variable
            "network_map": dict(network_map or {}),
            "used_mapped_networks": True,
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
        flavor = gpu_flavor(m.vm, ctx.plan, ctx.source.kind)
        if flavor:
            # the kit creates a GPU VM's server with its GPU flavor, never its own best match:
            # every run gets it - a mapping to an empty name names none, and the cutover refuses
            # it (SDD §7.2, §22.4, G7 review)
            out["flavor_uuid"] = flavor
        return out
    out = _common_openstack_vars(
        ctx.source,
        ctx.destination,
        rdir / "osm",
        plan_dir(ctx.settings, ctx.plan.id) / "osm",
        run_key(ctx.settings, ctx.plan.id),
    )
    out["os_migrate_workloads_filter"] = workload_filter(m.vm.name)
    # the link's entry stops admitting its key once the run cannot be running any more: the
    # step timeout when one is set, else 7 days - the bound of an entry a crash left (SDD §26.6)
    out["os_migrate_conversion_link_expiry_s"] = (
        math.ceil(ctx.settings.step_timeout_s) if ctx.settings.step_timeout_s > 0 else LINK_EXPIRY_S
    )
    out["os_migrate_workload_stop_before_migration"] = (
        m.strategy == Strategy.cold and step == StepName.CUTOVER
    )
    # SDD §6.4: keep mapped volume types (apply_mappings rewrote them to RHOSO types)
    out["os_migrate_workloads_preserve_volume_type"] = bool(ctx.plan.mappings.volume_types)
    # a warm pass of a plan that tracks extents (SDD §7.2, §28.4, plan task CE7)
    out.update(_extent_vars(step, ctx))
    # the cleanup after a cancel is the only step that asks for delete_dest_volumes (SDD §5.1)
    cancel_cleanup = step == StepName.ROLLBACK and bool(ctx.options.get("delete_dest_volumes"))
    if (
        m.strategy == Strategy.cold
        and step == StepName.ROLLBACK
        and m.downtime_started_at
        and not cancel_cleanup
    ):
        # the cold path records no destination server id: the rollback matches by name — only
        # after the stop, since the role creates the server after it; before it a same-named
        # server is not this migration's (a cutover refused or skipped for a collision, SDD §7.2)
        out["os_migrate_rollback_match_by_name"] = True
    if (
        m.strategy == Strategy.cold
        and step == StepName.CUTOVER
        and _leftover(rdir / "osm", m.vm.source_id)
    ):
        # the cleanup before the export removes what an earlier attempt left, and leaves the
        # source as it is: a retry may keep it stopped (SDD §7, COLD-02, the owner's choice)
        out["os_migrate_rollback_delete_dest_volumes"] = True
        out["os_migrate_rollback_start_source"] = False
    if cancel_cleanup:
        # it touches only what the migration recorded of its own: its rollback removed its server
        # and ports and started its source, and the VM it released may since be another plan's,
        # whose server, ports and stopped source are not this migration's (SDD §5.1, review RV-02)
        out["os_migrate_rollback_start_source"] = False
        out["os_migrate_rollback_delete_ports"] = False
    if step == StepName.ROLLBACK and (
        cancel_cleanup
        or (m.strategy == Strategy.cold and _cold_volumes_recorded(rdir / "osm", m.vm.source_id))
    ):
        # cleanup after a cancel: the destination volumes of the abandoned pass go too; and a
        # cold rollback deletes the volumes the cold import recorded - a cold retry never reuses
        # them, its transfer creates new ones (SDD §6.3, §7, the owner's choice, 2026-10-14)
        out["os_migrate_rollback_delete_dest_volumes"] = True
    return out


def _cold_volumes_recorded(osm: Path, server_id: str) -> bool:
    """The cold import recorded destination volumes it created for the workload (SDD §6.3)."""
    return bool((_read_warm_state(osm, server_id) or {}).get("dest_volumes"))


def _leftover(osm: Path, server_id: str) -> bool:
    """What an earlier attempt of the migration left with no destination server - a warm
    pre-copy's passes or pending source snapshot, or the volumes a cold transfer that failed or was
    cut short recorded: a cold re-run never reuses them, so its cutover removes them first (SDD
    §7). A record that names a server is a resumed cutover's own."""
    state = _read_warm_state(osm, server_id) or {}
    return bool(
        state.get("passes") or state.get("pending_snapshot") or state.get("dest_volumes")
    ) and not state.get("destination_server_id")


class _Unsafe(str):
    """A string Ansible must read verbatim: written with the ``!unsafe`` tag."""


class _AnsibleInputDumper(yaml.SafeDumper):
    """``safe_dump`` that writes :class:`_Unsafe` strings as ``!unsafe`` scalars."""


_AnsibleInputDumper.add_representer(
    _Unsafe, lambda dumper, value: dumper.represent_scalar("!unsafe", str(value))
)


def _mark_unsafe(value: Any) -> Any:
    if isinstance(value, str):
        return _Unsafe(value)
    if isinstance(value, dict):
        return {
            (_Unsafe(key) if isinstance(key, str) else key): _mark_unsafe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mark_unsafe(item) for item in value]
    return value


def ansible_yaml(data: dict[str, Any], *, sort_keys: bool = True) -> str:
    """YAML for the files Ansible reads (``-e @vars.yml``, ``-e @secrets.yml``, the inventory).

    Ansible templates every string it reads, so a VM name (chosen by whoever runs the source VM),
    a mapping value or a password holding ``{{ … }}`` or ``{% … %}`` would run as Jinja on the
    control plane, where ``lookup('pipe', …)`` executes commands next to every cloud credential.
    Every string — every value, and every key of a nested mapping, which ``dict2items`` turns into
    a value (a source network's name keys ``network_map``) — is written ``!unsafe`` so Ansible
    keeps it verbatim (SDD §7.2, Security.md C1-05); only the top-level keys, the variable names
    (the inventory's group names), stay plain.
    """
    marked = {key: _mark_unsafe(value) for key, value in data.items()}
    return yaml.dump(marked, Dumper=_AnsibleInputDumper, sort_keys=sort_keys)


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
    return ansible_yaml(doc, sort_keys=False)


def conversion_key_path(ctx: StepContext) -> Path | None:
    """Where the run writes the ``ssh_key_secret`` private key (None when not configured)."""
    host = ctx.destination.conversion_host
    if host is None or not host.ssh_key_secret:
        return None
    return run_dir(ctx.settings, ctx.plan.id, ctx.migration.id) / CONVERSION_KEY_FILE


#: a pinned key type's host key algorithms where they are not the type's own name: an RSA key
#: signs with SHA-2, never ssh-rsa, which OpenSSH 8.8 and later disable (SDD §6.5, §26.4)
_HOST_KEY_ALGORITHMS = {"ssh-rsa": ("rsa-sha2-512", "rsa-sha2-256")}


def pinned_ssh_args(known_hosts: Path, alias: str, keys: Sequence[str]) -> str:
    """The ssh options that check a host's key against the run's ``known_hosts`` alone, under
    ``alias``, as conv_host_inventory.yml gives an OpenStack conversion host (SDD §6.5, §26.4):
    strict checking, the alias as HostKeyAlias, and the pinned ``keys``' algorithms, each once in
    the keys' order - none when nothing is pinned."""
    algorithms: list[str] = []
    for key in keys:
        kind = key.split()[0]
        algorithms += [a for a in _HOST_KEY_ALGORITHMS.get(kind, (kind,)) if a not in algorithms]
    options = [
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={known_hosts}",
        "GlobalKnownHostsFile=/dev/null",
        f"HostKeyAlias={alias}",
        "CheckHostIP=no",
        "UpdateHostKeys=no",
        "VerifyHostKeyDNS=no",
        "HashKnownHosts=yes",
        *([f"HostKeyAlgorithms={','.join(algorithms)}"] if algorithms else []),
        "IdentitiesOnly=yes",
    ]
    return " ".join(f"-o {option}" for option in options)


def build_inventory(ctx: StepContext, trust: _Trust | None = None) -> str:
    """Inventory: ``migrator`` (local); VMware adds the RHOSO ``conversion_host``.

    The host is reached with the ``ssh_key_secret`` key when configured (the kit has no
    prestage step that could generate one), else with the installation's conversion key (SDD
    §7.2 "Conversion key"). Given the run's ``trust`` - host keys checked -, its key is checked
    against the run's known_hosts under the group's name, as an OpenStack host's is (SDD §7.2
    "Conversion host keys", §26.7).
    """
    host = ctx.destination.conversion_host
    if ctx.migration.strategy in VMWARE and host is not None and host.address:
        key = conversion_key_path(ctx) or run_key(ctx.settings, ctx.plan.id)
        entry: dict[str, Any] = {
            "ansible_ssh_user": host.ssh_user,
            "ansible_ssh_private_key_file": str(key),
        }
        if trust is not None:
            keys = [k for h in trust.hosts if h.alias == VMWARE_ALIAS for k in h.keys]
            entry["ansible_ssh_common_args"] = pinned_ssh_args(
                trust.known_hosts, VMWARE_ALIAS, keys
            )
            # Ansible puts no StrictHostKeyChecking=no of its own before those options
            entry["ansible_ssh_host_key_checking"] = True
        return _inventory({host.address: entry})
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
            entry = openstack_cloud_entry(destination, settings)
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
            entry = openstack_cloud_entry(provider, settings)
            out[f"os_migrate_{side}_auth"] = entry["auth"]
            if entry.get("auth_type"):
                out[f"os_migrate_{side}_auth_type"] = entry["auth_type"]
            region = provider.region or entry.get("region_name")
            if region:
                out[f"os_migrate_{side}_region_name"] = region
        return out
    except SecretNotFound as exc:
        raise PermanentStepError(f"credentials unavailable: {exc}") from None


def _warm_sync_pass(entry: dict[str, Any], m: Migration, started: Any) -> SyncPass:
    """A pass of the warm state file as the migration records it."""
    return SyncPass(
        number=int(entry.get("number") or next_pass_number(m)),
        kind=SyncPassKind(entry.get("kind") or "delta"),
        started_at=entry.get("started_at") or started,
        ended_at=entry.get("ended_at") or utcnow(),
        bytes_scanned=int(entry.get("bytes_scanned") or 0),
        bytes_changed=int(entry.get("bytes_changed") or 0),
        bytes_transferred=int(entry.get("bytes_transferred") or 0),
        duration_s=entry.get("duration_s"),
        # how the pass used each disk's extents, as the sync recorded it (SDD §4.2, §28.5)
        extents=entry.get("extents") or {},
    )


def workload_file_stem(name: str) -> str:
    """The stem of a VM's progress and log files, as ``import_workload_prelim`` names them
    (SDD §7.2): ``%`` -> ``%25``, ``/`` -> ``%2F``, NUL -> ``%00``, nothing else changes."""
    return name.replace("%", "%25").replace("/", "%2F").replace("\0", "%00")


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


class Deferred(list[str]):
    """What a warm pre-copy or sync run meets that only the cutover's ports refuse (SDD §7.2):
    each logged once, and all of it kept for the pass's cutover_refusals."""

    def __init__(self) -> None:
        super().__init__()
        self.logged = 0


def _read_refusals(path: Path) -> list[str]:
    """What the warm role recorded that the cutover will refuse (SDD §6.6): none when it wrote no
    list of messages."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(problem) for problem in found] if isinstance(found, list) else []


def _read_warm_state(osm: Path, server_id: str) -> dict[str, Any] | None:
    path = osm / "workload_warm" / f"{server_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def recorded_copied_data(settings: Settings, plan_id: str, migration: Migration) -> bool | None:
    """What the workload's record (SDD §6.3) says of the data the migration copied: whether it
    holds destination volumes or a pending source snapshot; None without a record (the simulator,
    VMware, a storage handover, nothing run yet) or for an id the collection would refuse as a
    file name. An unreadable record counts as data kept: its cleanup reports what it finds (SDD
    §5.1, §12)."""
    if not _STATE_FILE_ID.match(migration.vm.source_id):
        return None
    path = (
        run_dir(settings, plan_id, migration.id)
        / "osm"
        / "workload_warm"
        / f"{migration.vm.source_id}.json"
    )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return True
    return bool(record.get("dest_volumes") or record.get("pending_snapshot"))


def recorded_destination_volumes(
    settings: Settings, plan_id: str, migration: Migration
) -> bool | None:
    """Whether the workload's record (SDD §6.3) holds destination volumes - an attempt created
    them, and the next one uses them again (SDD §9.3, review RV38A-01); None without a record or
    for an id the collection would refuse as a file name. An unreadable record counts as volumes
    held, as recorded_copied_data counts it as data kept."""
    if not _STATE_FILE_ID.match(migration.vm.source_id):
        return None
    path = (
        run_dir(settings, plan_id, migration.id)
        / "osm"
        / "workload_warm"
        / f"{migration.vm.source_id}.json"
    )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return True
    return bool(record.get("dest_volumes"))


#: a source id the collection accepts as a state file name (warm_migration._SAFE_SERVER_ID)
_STATE_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _ensure_record(osm: Path, server_id: str, server_name: str) -> None:
    """Write the workload's record - no volume, pass or server - before a cold cutover's
    playbooks when it has none: the file is the sign that the migration records its workload, so
    its rollbacks never take a same-named server it does not record for the migration's (SDD §6.3,
    §7, review RV3-03). An id the collection would refuse as a file name writes nothing."""
    if not _STATE_FILE_ID.match(server_id):
        return
    path = osm / "workload_warm" / f"{server_id}.json"
    if path.exists():
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "server_id": server_id,
        "server_name": server_name,
        "transfer_uuid": None,
        "dest_volumes": {},
        "passes": [],
        "pending_snapshot": None,
        "destination_server_id": None,
    }
    # synced into place: a missing record would let a later rollback take a same-named server
    # by its name (review RV4-07)
    _write_synced_json(path, record)


def _write_synced_json(path: Path, data: Any) -> None:
    """As WarmState.save: mode 0600, synced, renamed into place - then the directory is synced
    too, so the rename outlives a crash."""
    tmp = path.with_name(f".{path.name}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        # some filesystems (network or FUSE mounts) cannot sync a directory: the file is written
        # and synced all the same, and failing every step there would gain nothing
        log.warning("could not sync %s after writing %s: %s", path.parent, path.name, exc)


#: the VMware rollback's records of what a server found only by name held (SDD §7.2, RV30-05):
#: one file per attempt, never written over another (review RV35A-02), one level below the guest
#: directories the kit creates in the run directory, {VM name}, so no VM's name is its path
#: (review RV32A-08)
KEPT_DIR = Path(".seamless")
KEPT_PREFIX = "rollback-kept"
#: the single record earlier builds wrote in the run directory - read too, by a migration in flight
#: across the upgrade; the one they wrote in KEPT_DIR carries the prefix, read with the others
LEGACY_KEPT_RECORD = "rollback-kept.json"


class _UnreadableKeepRecord(Exception):
    """The VMware rollback's keep record cannot be read: what to keep is unknown."""


def _kept_records(rdir: Path) -> list[Path]:
    """Every keep record of the run directory: the one an earlier build wrote there - never the
    kit's guest directory of a VM so named -, then each in .seamless/. Raises OSError for a
    listing that fails."""
    legacy = rdir / LEGACY_KEPT_RECORD
    paths = [legacy] if legacy.is_file() else []
    try:
        names = sorted(entry.name for entry in os.scandir(rdir / KEPT_DIR))
    except FileNotFoundError:
        names = []
    return paths + [
        rdir / KEPT_DIR / name
        for name in names
        if name.startswith(KEPT_PREFIX) and name.endswith(".json")
    ]


def _read_kept(rdir: Path) -> set[str]:
    """The volumes earlier attempts of the VMware rollback kept: what the servers they found only
    by the kit's name held before they deleted them (SDD §7.2, review RV30-05). Raises
    _UnreadableKeepRecord for a record - or a listing of them - that cannot be read (review
    RV32A-08)."""
    try:
        paths = _kept_records(rdir)
    except OSError as exc:
        raise _UnreadableKeepRecord(f"{rdir / KEPT_DIR} cannot be listed: {exc}") from None
    kept: set[str] = set()
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise _UnreadableKeepRecord(f"{path} cannot be read: {exc}") from None
        if not isinstance(data, dict) or not isinstance(data.get("volumes") or [], list):
            raise _UnreadableKeepRecord(f"{path} is no keep record")
        kept |= {str(volume) for volume in data.get("volumes") or []}
    return kept


def _record_kept(rdir: Path, server_id: str, held: Collection[str]) -> None:
    """What a server found only by the kit's name held, in a record of its own - never over
    another, so a damaged one stays as it is and every other still counts (SDD §7.2, reviews
    RV34A-03, RV35A-02)."""
    directory = rdir / KEPT_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9-]", "_", server_id)[:64]
    path = directory / f"{KEPT_PREFIX}.{name}.{uuid.uuid4().hex}.json"
    _write_synced_json(path, {"server_id": server_id, "volumes": sorted(held)})


def _remove_kept(rdir: Path) -> None:
    """A rollback that succeeded: a later one starts from what it finds (review RV29-03) - every
    record removed, never the kit's guest directory of a VM so named."""
    with contextlib.suppress(OSError):
        for path in _kept_records(rdir):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)


async def stop_process_group(proc: asyncio.subprocess.Process) -> None:
    """Stop a playbook started in its own session: SIGTERM to its process group, then SIGKILL."""
    for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), grace)
            return
        except TimeoutError:
            continue


def _collections_path(settings: Settings) -> str:
    base = Path(settings.data_dir) / "collections"
    link = base / "ansible_collections" / "os_migrate" / "os_migrate"
    root = Path(settings.collection_root)
    if (root / "galaxy.yml").is_file() and not link.exists():
        link.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            link.symlink_to(root.resolve(), target_is_directory=True)
    existing = os.environ.get("ANSIBLE_COLLECTIONS_PATH") or (
        "~/.ansible/collections:/usr/share/ansible/collections"
    )
    return f"{base}:{existing}"


def ansible_env(settings: Settings) -> dict[str, str]:
    """The environment of every ansible-playbook the control plane runs - a step's and a hook's
    (SDD §7.2, §10): the passthrough variables and the allowed ``ANSIBLE_*``, never a secret, a
    token or a cloud credential of its own environment."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ENV_PASSTHROUGH or (k.startswith("ANSIBLE_") and k not in ANSIBLE_ENV_DENIED)
    }
    home = Path(settings.data_dir) / ".ansible"
    checking = settings.ansible_host_key_checking
    env.update(
        {
            "ANSIBLE_COLLECTIONS_PATH": _collections_path(settings),
            "ANSIBLE_HOME": str(home),
            "ANSIBLE_LOCAL_TEMP": str(home / "tmp"),
            # modules of the local connection run from remote_tmp, which Ansible derives from
            # the passwd home of the UID (/opt/app-root/src in the image: not writable)
            "ANSIBLE_REMOTE_TEMP": str(home / "tmp"),
            "ANSIBLE_RETRY_FILES_ENABLED": "0",
            "ANSIBLE_NOCOLOR": "1",
            # the downtime clock reads the stop task's result line: never hide skipped hosts
            "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "True",
            # SDD §15.1: the setting decides, never the control plane's own environment
            "ANSIBLE_HOST_KEY_CHECKING": "True" if checking else "False",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


@dataclass(frozen=True)
class _Playbook:
    name: str
    stops_source: bool = False


@dataclass
class _RunTrace:
    """What a run's output showed: whether a playbook's source-stop task started - on VMware the
    kit's migrate task, the only one that writes to a volume (SDD §7.2, review RV34A-02) - and
    what a failed playbook's output names of a conversion host it refused (SDD §7.2)."""

    stop_task_started: bool = False
    refusal: _Refusal | None = None


#: a run's known_hosts, in its run directory (SDD §7.2 "Conversion host keys", §26.4)
KNOWN_HOSTS_FILE = "known_hosts"
#: a run's own ControlPersist directory, in its run directory (SDD §7.2 "Conversion host keys")
CONTROL_PATH_DIR = "cp"
#: the role's report of the servers it created and found, in a pre-staging's osm/ (SDD §6.5)
HOSTS_REPORT = Path("conversion") / "hosts.json"
#: each side's short name, as the collection's variables name it
_SIDE_KEY = {"source": "src", "destination": "dst"}


def _host_key_file(rdir: Path, side: str) -> Path:
    """Where the private half of the key a pre-staging injects into a side's conversion host waits
    for the role (SDD §7.2, §26.3): ``host-key-src.key``, ``host-key-dst.key``."""
    return rdir / f"host-key-{_SIDE_KEY[side]}.key"


def _write_host_key(path: Path) -> str:
    """An ed25519 host key pair made in memory (SDD §7.2, §26.3): its private half written to
    ``path`` alone, 0600, in OpenSSH's format with its final newline - a leftover of a run a crash
    cut short replaced -, its public half returned, ``ssh-ed25519 <base64>``. Nothing of the
    private half is logged or kept."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    key = Ed25519PrivateKey.generate()
    remove_quietly(path)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()))
    os.chmod(path, 0o600)
    return key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()


def _read_hosts_report(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """The servers the role reported it created and found, each by its side's alias (SDD §6.5):
    none of either when the run wrote no report, or one that cannot be read - logged by the
    failure's kind alone."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}
    except (OSError, ValueError) as exc:
        log.warning("the conversion host report %s cannot be read: %s", path, type(exc).__name__)
        return {}, {}

    def servers(key: str) -> dict[str, str]:
        listed = report.get(key) if isinstance(report, dict) else None
        return {
            str(entry["alias"]): str(entry["id"])
            for entry in (listed if isinstance(listed, list) else [])
            if isinstance(entry, dict) and entry.get("alias") and entry.get("id")
        }

    return servers("created"), servers("found")


def _host_label(provider: Provider, side: str) -> str:
    """What names a side's conversion host when nothing found it: its name, else its address,
    else its side's alias - the collection's default name (SDD §7.2, §10)."""
    host = provider.conversion_host
    if host is None:
        return SIDE_ALIASES[side]
    return host.name or host.address or SIDE_ALIASES[side]


@dataclass
class _Host:
    """A conversion host a run reaches, as the run checks its key (SDD §7.2, §26.4)."""

    #: its provider's role, source or destination
    side: str
    #: what ssh checks its key under: its inventory name, os_migrate_conv_src or _dst, or the
    #: VMware inventory group's, conversion_host
    alias: str
    provider: Provider
    #: what the run found it by - its name, else its address -, or what names it when nothing
    #: did (SDD §10)
    name: str
    #: the server it resolves to; None when it resolves to none, or was not looked up
    server_id: str | None
    #: what the run's known_hosts holds for it: its declared keys, else its pin (SDD §10), or the
    #: key the run injects into a host the role creates
    known: KnownHost
    #: a host a pre-staging's role creates: where the private half of its injected key waits
    private: Path | None = None
    #: the public half of the key injected into it, once made
    injected: str | None = None
    #: the server the role reported it created with that key
    created: str | None = None

    @property
    def keys(self) -> list[str]:
        """The keys the run's known_hosts holds for it, without comments."""
        return trusted_keys(self.known)


@dataclass
class _Trust:
    """What a run checks its conversion hosts' keys against, while host keys are checked (SDD
    §7.2 "Conversion host keys", §26.4): its known_hosts file and the hosts it holds."""

    known_hosts: Path
    hosts: list[_Host]

    def variables(self) -> dict[str, Any]:
        """The variables that pin the collection's hosts to the run's file (SDD §6.5), and name
        the injected keys' private halves to the role - each side's own variable: the role's own
        name, an extra var, would reach both sides."""
        out: dict[str, Any] = {"os_migrate_conversion_host_known_hosts": str(self.known_hosts)}
        for host in self.hosts:
            if host.private is not None:
                side = _SIDE_KEY[host.side]
                out[f"os_migrate_{side}_conversion_host_key_private_path"] = str(host.private)
        return out

    def secrets(self) -> list[Path]:
        """The injected keys' private halves, removed after the run whatever its outcome."""
        return [host.private for host in self.hosts if host.private is not None]

    def named(self, refusal: _Refusal) -> _Host | None:
        """The host a refusal names (SDD §7.2): by its alias - the collection's message, an
        inventory name -, else by its side; else a VMware run's one host; else None."""
        for host in self.hosts:
            if refusal.name is not None and host.alias == refusal.name:
                return host
        for host in self.hosts:
            if refusal.side is not None and host.side == refusal.side:
                return host
        if [host.alias for host in self.hosts] == [VMWARE_ALIAS]:
            return self.hosts[0]
        return None

    def write(self) -> None:
        """The injected keys made, each private half in its file and its public half under its
        host's alias, then the run's known_hosts, 0600, written whole - all before the run's
        playbooks (SDD §7.2, §10)."""
        for host in self.hosts:
            if host.private is not None:
                host.injected = _write_host_key(host.private)
                host.known = KnownHost(host.alias, None, (host.injected,))
        write_known_hosts(self.known_hosts, [host.known for host in self.hosts])


# -- executor -----------------------------------------------------------------------------------
class AnsibleExecutor:
    name = "ansible"

    def __init__(
        self,
        settings: Settings,
        *,
        providers: Any = None,
        poll_s: float = 1.0,
        groups: ProcessGroups | None = None,
    ) -> None:
        self.settings = settings
        self._providers = providers
        self.poll_s = poll_s
        #: the book each playbook's process group is registered with while it runs (SDD §7.1)
        self.groups = groups if groups is not None else ProcessGroups()

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
        prestage_flavors: list[str] | None = None,
        prestage_keypairs: list[str] | None = None,
        rekey_conversion_hosts: Mapping[str, Path | str] | None = None,
        host_keys: HostKeys | None = None,
    ) -> None:
        """Pre-stage the plan's resources, then deploy the conversion hosts - first re-keying
        each side ``rekey_conversion_hosts`` names, logged in to with the earlier plan's key its
        keypair holds (SDD §7.2 "Conversion key"). ``host_keys`` is the orchestrator's book of
        the run's conversion host keys (SDD §7.2 "Conversion host keys")."""
        if source.kind == ProviderKind.vmware:
            return  # the kit prepares its own conversion host
        rekey = dict(rekey_conversion_hosts or {})
        unknown = sorted(set(rekey) - {"src", "dst"})
        if unknown:
            raise PermanentStepError(f"no conversion host side is called {unknown!r}")
        pdir = plan_dir(self.settings, plan.id)
        osm = pdir / "osm"
        playbooks: list[_Playbook] = []
        for resource in plan.prestage_resources:
            if not _RESOURCE.match(resource):
                raise PermanentStepError(f"invalid prestage resource {resource!r}")
            if resource == "flavors" and not prestage_flavors:
                # only the plan's flavors RHOSO lacks, never os-migrate's every-flavor default:
                # its import rewrites a same-named destination flavor's extra specs (SDD §7.2)
                continue
            if resource == "keypairs" and not prestage_keypairs:
                continue  # only the plan's keypairs the destination credential lacks (SDD §7.2)
            playbooks += [_Playbook(f"export_{resource}.yml"), _Playbook(f"import_{resource}.yml")]
        if deploy_conversion_hosts is None:
            hosts = [source.conversion_host, destination.conversion_host]
            deploy_conversion_hosts = all(hosts) and any(h.manage for h in hosts if h)
        if deploy_conversion_hosts and rekey:
            # the hosts must accept the installation's key before the deploy logs in with it
            playbooks.append(_Playbook("rekey_conversion_hosts.yml"))
        if deploy_conversion_hosts:
            playbooks.append(_Playbook("deploy_conversion_hosts.yml"))
        # a pre-staging always passes the installation's key: the orchestrator resolved it, and
        # the deploy generates it when none exists yet (SDD §7.2)
        variables = _common_openstack_vars(
            source, destination, osm, osm, installation_key(self.settings)
        )
        # no static link: no plan's link key goes to the hosts - each run makes its own (SDD §7.2,
        # §26.6)
        variables["os_migrate_link_conversion_hosts"] = False
        # the content install puts the Ceph client on the source host of a tracked plan (SDD §6.5)
        variables["os_migrate_conversion_host_ceph_client"] = (
            ceph_tracking(plan, source, self.settings) is not None
        )
        if deploy_conversion_hosts:
            for side, old_key in sorted(rekey.items()):
                variables[f"os_migrate_{side}_conversion_rekey_private_path"] = str(old_key)
                # the deploy's keypair task replaces the key the side's keypair holds
                variables[f"os_migrate_{side}_conversion_keypair_state"] = "replace"
        for key, names in (
            ("os_migrate_flavors_filter", prestage_flavors),
            ("os_migrate_keypairs_filter", prestage_keypairs),
        ):
            if names:
                variables[key] = [{"regex": "^" + re.escape(name) + "$"} for name in names]
        book = host_keys or NoHostKeys()
        trust = None
        if deploy_conversion_hosts and self.settings.ansible_host_key_checking:
            # the deploy reaches both conversion hosts: their keys are settled before it, and a
            # host its role creates gets a key injected (SDD §7.2 "Conversion host keys")
            hosts = []
            for side, provider in (("source", source), ("destination", destination)):
                host = provider.conversion_host
                managed = host.manage if host is not None else True
                creates = _host_key_file(pdir, side) if managed else None
                hosts.append(
                    await self._host_of(side, provider, SIDE_ALIASES[side], book, creates=creates)
                )
            trust = _Trust(pdir / KNOWN_HOSTS_FILE, hosts)
        injecting = trust is not None and bool(trust.secrets())
        report = osm / HOSTS_REPORT
        if injecting:
            # a run whose role reaches no server task leaves an earlier run's report (SDD §6.5)
            await asyncio.to_thread(remove_quietly, report)
        trace = _RunTrace()
        try:
            await self._execute(
                pdir,
                osm,
                variables,
                source,
                destination,
                _inventory(),
                playbooks,
                None,
                None,
                trace=trace,
                trust=trust,
            )
        except BaseException as exc:
            if injecting:
                # a server the role created before the failure holds the key: pinned all the same
                assert trust is not None
                await outlive_cancel(self._pin_created(trust, report, book))
            if trust is not None and trace.refusal is not None:
                refusal = trace.refusal
                host = await outlive_cancel(self._key_refused(refusal, trust, book))
                if host is not None and host.created is not None and not refusal.link:
                    # the server holds the key the run injected unless its image's cloud-init
                    # left it out (SDD §7.2, §26.3)
                    raise PermanentStepError(
                        f"the conversion host {host.name}, created with an injected host key, "
                        "does not present it: check its image's cloud-init"
                    ) from exc
            raise
        if injecting:
            assert trust is not None
            refusal = await self._pin_created(trust, report, book)
            if refusal is not None:
                raise PermanentStepError(refusal)

    async def _pin_created(self, trust: _Trust, report: Path, book: HostKeys) -> str | None:
        """After a pre-staging's run, whatever its outcome (SDD §7.2, §26.3, §26.4): the role's
        report read, each server it created with the key the run injected is pinned with it
        through ``book`` - source injected, with conversion_host.key_pinned. A server it found
        instead does not hold that key and is never pinned with it: the refusal of such a host
        is returned - a host without a key -, unless the provider's pin is that server's."""
        created, found = await asyncio.to_thread(_read_hosts_report, report)
        refusal = None
        for host in trust.hosts:
            if host.injected is None:
                continue
            if host.alias in created:
                host.created = created[host.alias]
                await book.pin_injected(host.provider, host.side, host.created, [host.injected])
                continue
            pin = host.provider.conversion_host_pin
            server = found.get(host.alias)
            if server is not None and (pin is None or pin.server_id != server) and not refusal:
                refusal = (
                    f"host key unknown: no key is pinned or declared for the {host.side} "
                    f"conversion host {host.name}"
                )
        return refusal

    # -- the conversion hosts' keys ----------------------------------------------------------
    @staticmethod
    async def _key_refused(refusal: _Refusal, trust: _Trust, book: HostKeys) -> _Host | None:
        """conversion_host.key_refused through ``book`` for what a run's output refused (SDD §4.3,
        §7.2): the provider and side of the host it names, the server it names - else the one
        the host resolved to, or its role created -, the fingerprints the run pinned for it and
        the one the host offered, each None when the run cannot know it; never a line of the
        output. The host it names, if any. An event that cannot be written is logged: the run's
        failure is what the step reports."""
        host = trust.named(refusal)
        side = host.side if host is not None else refusal.side
        server = refusal.server_id
        if host is not None:
            server = server or host.server_id or host.created
        data = {
            "provider_id": host.provider.id if host is not None else None,
            "side": side,
            "server_id": server,
            "pinned": [fingerprint(key) for key in host.keys] if host is not None else None,
            "offered": refusal.offered,
        }
        what = f"the {side} conversion host" if side else "a conversion host"
        if host is not None:
            what += f" of provider {host.provider.id}"
        if server:
            what += f" (server {server})"
        if refusal.link:
            message = f"{what} refused the run's link"
        else:
            message = f"the run refused the host key of {what}"
        try:
            await book.refused(message, data)
        except Exception as exc:  # noqa: BLE001 - the run's own failure is reported
            log.warning("conversion_host.key_refused was not recorded: %s", type(exc).__name__)
        return host

    async def _run_trust(self, ctx: StepContext, step: StepName, rdir: Path) -> _Trust | None:
        """The conversion hosts a run reaches and the keys it checks them against, while
        SEAMLESS_ANSIBLE_HOST_KEY_CHECKING is on; None while it is off - 0.4.0's runs (SDD §7.2
        "Conversion host keys", §26.4). An OpenStack run reaches both hosts, a VMware kit run the
        destination's host of its inventory, under the group's name."""
        if not self.settings.ansible_host_key_checking:
            return None
        if Strategy(ctx.migration.strategy) in VMWARE:
            host = ctx.destination.conversion_host
            reached = (
                [("destination", ctx.destination, VMWARE_ALIAS)]
                if host is not None and host.address
                else []
            )
        else:
            reached = [
                ("source", ctx.source, SIDE_ALIASES["source"]),
                ("destination", ctx.destination, SIDE_ALIASES["destination"]),
            ]
        book = ctx.host_keys or NoHostKeys()
        rollback = ctx.log if step == StepName.ROLLBACK else None
        hosts = [
            await self._host_of(side, provider, alias, book, rollback=rollback)
            for side, provider, alias in reached
        ]
        return _Trust(rdir / KNOWN_HOSTS_FILE, hosts)

    async def _host_of(
        self,
        side: str,
        provider: Provider,
        alias: str,
        book: HostKeys,
        *,
        rollback: Callable[[str], Awaitable[None]] | None = None,
        creates: Path | None = None,
    ) -> _Host:
        """The side's conversion host as the run checks it (SDD §7.2, §10, §26.4): its declared
        keys, else the pin of the server it resolves to - looked up as the provider check finds
        it; a lookup that fails is a transient failure. A host without a pin has its console read
        once through ``book``, which pins what it shows (§26.8). A host the run then has no key
        for is refused before any playbook: "host key unknown", or "conversion host replaced"
        when its pin is another server's (§26.5). Given ``rollback`` - a rollback's log: it must
        start the source whatever its hosts' keys - nothing is refused or read, and a lookup that
        fails is logged: the host then resolves to no server, and no pin applies. Given
        ``creates`` - a pre-staging whose deploy manages the host - the host is looked up by its
        name alone, as the role looks it up, and one the role will create - no server bears its
        name - gets a key injected, its private half to wait at ``creates`` (§26.3)."""
        host = provider.conversion_host
        declared = tuple(host.ssh_host_keys) if host is not None else ()
        name = _host_label(provider, side)
        if declared:
            return _Host(side, alias, provider, name, None, KnownHost(alias, None, declared))
        try:
            if creates is None:
                name, server = await self._find_conversion_host(provider, side)
            else:
                # the role looks its server up by its exact name, and creates it when none bears
                # it (SDD §6.5)
                name = (host.name if host is not None else None) or SIDE_ALIASES[side]
                found = await self.providers.get(provider).find_server(name)
                if found is None:
                    known = KnownHost(alias, None)
                    return _Host(side, alias, provider, name, None, known, private=creates)
                server = str(found)
        except ProviderError as exc:
            failure = f"could not look up the {side} conversion host {name} in {provider.label}"
            if rollback is None:
                raise TransientStepError(f"{failure}: {exc}") from exc
            await rollback(f"{failure} ({exc}): no pin applies to it in this run")
            server = None
        if rollback is None and server is not None and provider.conversion_host_pin is None:
            # a host without a pin: its console, read once, pins what it shows (SDD §26.8)
            provider = await book.pin_from_console(provider)
        pin = provider.conversion_host_pin
        declared = tuple(provider.conversion_host.ssh_host_keys) if provider.conversion_host else ()
        known = KnownHost(alias, server, declared, pin)
        checked = _Host(side, alias, provider, name, server, known)
        if rollback is None and not checked.keys:
            if pin is not None and server is not None and pin.server_id != server:
                raise PermanentStepError(
                    f"conversion host replaced: {name} is now server {server}; its pin belongs "
                    f"to server {pin.server_id}"
                )
            raise PermanentStepError(
                f"host key unknown: no key is pinned or declared for the {side} conversion host "
                f"{name}"
            )
        return checked

    async def _find_conversion_host(self, provider: Provider, side: str) -> tuple[str, str | None]:
        """The server the side's conversion host resolves to, as the provider check finds it
        (SDD §10): by its name - its side's alias when it has none -, else by its address; with
        what found it, or what names it when nothing did. Raises ProviderError."""
        host = provider.conversion_host
        impl = self.providers.get(provider)
        name = (host.name if host is not None else None) or SIDE_ALIASES[side]
        server = await impl.find_server(name)
        if server is not None:
            return name, str(server)
        if host is not None and host.address:
            server = await impl.server_of_address(host.address)
            if server is not None:
                return host.address, str(server)
        return _host_label(provider, side), None

    # -- steps ---------------------------------------------------------------------------------
    def _playbooks(
        self,
        step: StepName,
        strategy: Strategy,
        osm: Path,
        reexport: bool = False,
        leftover: bool = False,
    ) -> list[_Playbook]:
        exported = (osm / "workloads.yml").exists() and not reexport
        export = [] if exported else [_Playbook("export_workloads.yml")]
        if strategy == Strategy.cold:
            if step == StepName.CUTOVER:
                # what an earlier attempt left goes before the stop, right after the export: the
                # cleanup selects the workload by its name of now (SDD §7, review lead A)
                cleanup = [_Playbook("rollback_workloads.yml")] if leftover else []
                return [
                    _Playbook("export_workloads.yml"),
                    *cleanup,
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
        # the conversion hosts' keys first: nothing of the run happens before them (SDD §7.2)
        trust = await self._run_trust(ctx, step, rdir)
        reexport = False
        networks: list[dict[str, Any]] | None = None
        # a warm pre-copy or sync run makes no port: what only the cutover's ports would meet is
        # logged, and the cutover refuses it before the source stops (SDD §7.2)
        deferred: Deferred | None = (
            Deferred() if step in (StepName.PRECOPY, StepName.SYNC) else None
        )
        if strategy in (Strategy.cold, Strategy.warm):
            vm_id = ctx.migration.vm.source_id
            nics = ctx.plan.nic_overrides.get(vm_id)
            # a warm cutover re-run that finishes the recorded destination server runs on the
            # export as it is: its ports exist, and the plan cannot change while the source is
            # stopped (SDD §7.2)
            finishing = (
                strategy == Strategy.warm
                and step == StepName.CUTOVER
                and bool(
                    ((await asyncio.to_thread(_read_warm_state, osm, vm_id)) or {}).get(
                        "destination_server_id"
                    )
                )
            )
            judged = step != StepName.ROLLBACK and not finishing
            if judged:
                inventory = await self._destination_inventory(ctx, deferred)
                # the import creates the server with the flavor the export names: a GPU VM's
                # mapping must still apply to the destination's flavors (SDD §7.2, §22.4)
                self._refuse_gpu_mapping(ctx, inventory, deferred)
                # none listed (an older provider): nothing to resolve against
                networks = (
                    None
                    if inventory is None
                    else list(getattr(inventory, "network_details", None) or [])
                )
            mappings = effective_mappings(ctx.plan.mappings, ctx.migration.resolved_mappings)
            reexport = await asyncio.to_thread(
                _apply_port_mode,
                osm,
                ctx.plan.port_creation_mode,
                step == StepName.CUTOVER and not finishing,
                bool(nics),
                networks,
                export_record(mappings, nics, ctx.migration.vm.name) if judged else None,
                any(p.name == "export_workloads.yml" for p in self._playbooks(step, strategy, osm)),
                deferred,
                policy_profile_of(ctx.destination.capabilities, ctx.destination),
                ctx.destination.label,
                import_floating_ip_mode(ctx.plan),
            )
            await self._log_deferred(ctx, deferred)
        leftover = (
            strategy == Strategy.cold
            and step == StepName.CUTOVER
            and await asyncio.to_thread(_leftover, osm, ctx.migration.vm.source_id)
        )
        playbooks = self._playbooks(step, strategy, osm, reexport=reexport, leftover=leftover)
        if not playbooks:
            raise PermanentStepError(f"step {step} does not apply to strategy {strategy}")
        if step == StepName.CUTOVER:
            await self._refuse_name_collision(ctx)
        if strategy in VMWARE and step in (StepName.CUTOVER, StepName.PRECOPY, StepName.SYNC):
            volumes = await self._refuse_stale_kit_volumes(ctx, strategy)
            if strategy == Strategy.vmware_warm:
                cutover = step == StepName.CUTOVER and await self._keeps_cutover_copies(
                    ctx, volumes
                )
                await self._delete_cut_off_kit_copies(ctx, volumes, cutover=cutover)
        if strategy == Strategy.cold and step == StepName.CUTOVER:
            await asyncio.to_thread(
                _ensure_record, osm, ctx.migration.vm.source_id, ctx.migration.vm.name
            )
        # the kit powers the VM off at the cutover: its networks are settled before it runs, and so
        # is the GPU flavor it creates a GPU VM's server with (SDD §7.2, §22.4)
        network_map = None
        if strategy in VMWARE:
            inventory = await self._kit_inventory(ctx)
            self._refuse_gpu_mapping(ctx, inventory, deferred)
            network_map = await self._vmware_network_map(ctx, inventory, deferred)
            await self._log_deferred(ctx, deferred)
        passes_before = 0
        if strategy == Strategy.warm:
            state_before = await asyncio.to_thread(
                _read_warm_state, osm, ctx.migration.vm.source_id
            )
            passes_before = len((state_before or {}).get("passes") or [])
        started = utcnow()
        refusals_file = osm / "workload_warm" / f"{ctx.migration.vm.source_id}.refusals.json"
        if deferred is not None and strategy == Strategy.warm:
            # the role writes it during the pass: an earlier pass's is not this one's (SDD §6.6)
            remove_quietly(refusals_file)
        marked: list[str] = []  # the copies this cutover marked 'cutover'
        trace = _RunTrace()
        try:
            if strategy == Strategy.vmware_warm and step == StepName.CUTOVER:
                try:
                    await self._mark_cutover_copies(ctx, marked)
                except ProviderError as exc:
                    raise TransientStepError(
                        f"could not mark the kit's copies of {ctx.migration.vm.name} in "
                        f"{ctx.destination.label} before its cutover: {exc}"
                    ) from exc
            stop_skipped = await self._execute(
                rdir,
                osm,
                build_vars(step, ctx, network_map),
                ctx.source,
                ctx.destination,
                build_inventory(ctx, trust),
                playbooks,
                ctx,
                effective_mappings(ctx.plan.mappings, ctx.migration.resolved_mappings),
                networks,
                deferred,
                trace=trace,
                trust=trust,
            )
        except BaseException:
            if marked and not trace.stop_task_started:
                # the kit never reached its migrate task, the only one that writes to a volume:
                # what this run marked is a completed copy still (SDD §7.2, review RV34A-02)
                await outlive_cancel(self._mark_copies_back(ctx, marked))
            if trust is not None and trace.refusal is not None:
                book = ctx.host_keys or NoHostKeys()
                await outlive_cancel(self._key_refused(trace.refusal, trust, book))
            raise
        if stop_skipped and strategy == Strategy.cold:
            # the cold role skips every task (the stop included) when the destination already
            # has a server with the workload's name: nothing was migrated
            raise PermanentStepError(
                f"import_workloads.yml skipped {ctx.migration.vm.name}: "
                f"{ctx.destination.label} already has a server with that name and nothing was "
                "migrated; delete or rename it, or roll the migration back"
            )
        if strategy == Strategy.vmware_warm and step in (StepName.PRECOPY, StepName.SYNC):
            await self._mark_kit_copies(ctx)
        result = await self._result(step, strategy, ctx, osm, started, passes_before)
        if deferred is not None:
            # SDD §7.2: what this pass met that the cutover refuses before the source stops -
            # what the executor deferred and, on the OpenStack warm path, the destination check's
            found = list(deferred)
            if strategy == Strategy.warm:
                found += await asyncio.to_thread(_read_refusals, refusals_file)
            result.cutover_refusals = list(dict.fromkeys(found))
        return result

    async def _refuse_name_collision(self, ctx: StepContext) -> None:
        """Before the first cutover attempt, refuse a destination server that carries the name the
        migration's server will get (the kit's name of a VMware VM, SDD §7.2).

        The cold role would skip the migration (and report success), the kit would fail after
        powering the VM off, and a later rollback by name would delete a server this migration
        never created. A retry after a stop (``downtime_started_at`` set) or with a recorded
        destination server is a resume, not a collision.
        """
        m = ctx.migration
        if m.destination_server_id or m.downtime_started_at is not None:
            return
        name = destination_server_name(m)
        try:
            existing = await self.providers.get(ctx.destination).find_server(name)
        except ProviderError as exc:
            # an unanswered lookup is not "no such server": refuse before anything is stopped
            # (a duplicate name is a lookup error too)
            raise TransientStepError(
                f"could not check {ctx.destination.label} for a server named {name}: {exc}"
            ) from exc
        if existing:
            raise PermanentStepError(
                f"a server named {name} already exists in {ctx.destination.label} "
                f"({existing}) and this migration did not create it; delete or rename it before "
                "the cutover"
            )

    async def _refuse_stale_kit_volumes(
        self, ctx: StepContext, strategy: Strategy
    ) -> list[dict[str, Any]]:
        """Before a VMware kit run - so before any clock - refuse while a volume of the VM's kit
        name holds what the run would take as migrated: one marked converted, or for a cold run
        any, onto which the kit refuses to copy. A rollback that could not delete it ended failed
        with the source running, and the run would boot the destination from it beside the
        source (SDD §7.2, review RV29-01). A converted volume is the kit's own of this outage
        while the source has not run since its stop: the clock open, the VM reading off and
        vCenter recording no power-on of it since (review RV30-03). Returns the VM's kit volumes
        it read."""
        m = ctx.migration
        if not kit_vm_name(m.vm.name):
            return []
        where = ctx.destination.label
        try:
            volumes = kit_volumes(m.vm.name, await self.providers.get(ctx.destination).volumes())
        except ProviderError as exc:
            # an unanswered lookup is not "no such volume": refuse before the kit runs
            raise TransientStepError(
                f"could not check {where} for the kit's volumes of {m.vm.name}: {exc}"
            ) from exc
        stale = [v for v in volumes if strategy == Strategy.vmware_cold or _marked(v, "converted")]
        if not stale:
            return volumes
        if source_stopped(m) and all(_marked(v, "converted") for v in stale):
            try:
                vm = await self.providers.get(ctx.source).get_vm(m.vm.source_id)
            except ProviderError as exc:
                raise TransientStepError(
                    f"could not read {m.vm.name} from {ctx.source.label}: {exc}"
                ) from exc
            if vm.power_state == "stopped":
                # off now - and not run since the clock opened? vCenter's events say; its power
                # state alone does not, a VM run and shut down again reads off too (review RV30-03)
                try:
                    ran = await self.providers.get(ctx.source).powered_on_since(
                        m.vm.source_id, m.downtime_started_at
                    )
                except ProviderError as exc:
                    raise TransientStepError(
                        f"could not read the power-ons of {m.vm.name} from {ctx.source.label}: "
                        f"{exc}"
                    ) from exc
                if not ran:
                    # converted in this outage, the source off since: the kit is right
                    return volumes
        named = ", ".join(
            f"{v['id']} ({v.get('name')}"
            + (
                f", attached to {', '.join(map(str, v['attachments']))}"
                if v.get("attachments")
                else ""
            )
            + ")"
            for v in stale
        )
        # the way out first: Migration.error keeps 1000 characters, its event 300 (review RV30-08)
        raise PermanentStepError(
            "roll the migration back again, which deletes the kit's volumes it may, or delete them "
            f"in {where}: the kit would take a converted one as migrated and boot the new server "
            f"from it, and refuses to copy a cold run onto any - {where} holds the kit's volume "
            f"{named} of {m.vm.name}"
        )

    async def _delete_cut_off_kit_copies(
        self, ctx: StepContext, volumes: list[dict[str, Any]], *, cutover: bool = False
    ) -> None:
        """Before a vmware_warm kit run, delete each first copy cut off: a kit volume of the VM
        neither converted nor marked copied. The kit wrote its changeID before the copy it never
        finished, and would sync onto it only what the source changed since; deleted, the disk is
        copied in full (SDD §7.2, review RV30-01). A marked one Cinder cannot attach goes too
        (review RV30-07), and one an earlier cutover marked - unless ``cutover``: a cutover retried
        inside the outage keeps them (reviews RV32A-01, RV32A-02). The destination's conversion host
        holding one - a killed run's - lets go of it first, of a copy kept too (review RV32A-03);
        one another server holds stays."""
        dst = self.providers.get(ctx.destination)
        host: list[str | None] = []  # looked up once, for the first copy held
        try:
            for volume in volumes:
                if _marked(volume, "converted"):
                    continue
                kept = _resumable(volume, cutover=cutover)
                holders = [str(holder) for holder in volume.get("attachments") or []]
                if holders:
                    if not host:
                        host.append(await self._conversion_host_server(ctx, dst))
                    if host[0] is None or any(holder != host[0] for holder in holders):
                        continue
                    # a pass cut off during its sync, or the kit's own detach that failed, left
                    # it attached: the kit attaches it again, which Nova refuses (review RV32A-03)
                    await dst.detach_volume(host[0], volume["id"])
                if kept:
                    continue
                await dst.delete_volume(volume["id"])
                await ctx.log(
                    f"deleted the kit's volume {volume['id']} ({volume.get('name')}): a first "
                    "copy cut off, which the kit copies again in full"
                )
        except ProviderError as exc:
            raise TransientStepError(
                f"could not delete a first copy cut off of {ctx.migration.vm.name} in "
                f"{ctx.destination.label}: {exc}"
            ) from exc

    async def _keeps_cutover_copies(self, ctx: StepContext, volumes: list[dict[str, Any]]) -> bool:
        """Whether a vmware_warm cutover retried inside this outage keeps the kit volumes an earlier
        attempt marked 'cutover' for its CBT sync (SDD §7.2, reviews RV32A-01, RV32A-02): no
        server booted from them - the migration records none, none carries the destination
        server's name - and the kit converted the volume of the VM's first disk in vCenter's device
        order, so that conversion succeeded on its first try: the kit converted no other disk,
        powered the VM off and then only synced the rest. The refusal before let a converted
        volume through only with the clock open, the VM off and no power-on of it since."""
        m = ctx.migration
        if not source_stopped(m) or m.destination_server_id:
            return False
        if not any(_cutover_copy(v) for v in volumes):
            return False
        converted = {str(v.get("name")) for v in volumes if _marked(v, "converted")}
        if not converted:
            return False
        try:
            if await self.providers.get(ctx.destination).find_server(destination_server_name(m)):
                return False
            keys = await self.providers.get(ctx.source).disk_keys(m.vm.source_id)
        except ProviderError as exc:
            # an unanswered read is no answer: nothing deleted, nothing run (SDD §7.2)
            raise TransientStepError(
                f"could not check whether the cutover of {m.vm.name} keeps its copies: {exc}"
            ) from exc
        return bool(keys) and f"{kit_vm_name(m.vm.name)}-{keys[0]}" in converted

    async def _mark_cutover_copies(self, ctx: StepContext, marked: list[str] | None = None) -> None:
        """Mark each of the VM's kit copies marked completed 'cutover', no completed copy for any
        later run or rollback (SDD §7.2, reviews RV32A-01, RV32A-02): right before a vmware_warm
        cutover's kit run - it converts a disk in place and boots a server that writes to every
        one, which no CBT sync undoes -, and in a rollback once a server booted from them. Each
        one marked is added to ``marked``. Raises ProviderError; each caller fails its attempt as
        transient."""
        dst = self.providers.get(ctx.destination)
        for volume in kit_volumes(ctx.migration.vm.name, await dst.volumes()):
            if _marked(volume, "seamless_copied"):
                await dst.set_volume_metadata(volume["id"], dict(CUTOVER_COPY))
                if marked is not None:
                    marked.append(volume["id"])

    async def _mark_copies_back(self, ctx: StepContext, marked: Sequence[str]) -> None:
        """A vmware_warm cutover kit run that ended before the kit's migrate task wrote to no
        volume: the copies it marked 'cutover' are completed copies still, marked so again for the
        retry's CBT sync. A marking back that fails is logged and leaves the rest 'cutover' (SDD
        §7.2, review RV34A-02)."""
        dst = self.providers.get(ctx.destination)
        for index, volume_id in enumerate(marked):
            try:
                await dst.set_volume_metadata(volume_id, dict(COPIED))
            except ProviderError as exc:
                await ctx.log(
                    f"could not mark the kit's copies {', '.join(marked[index:])} of "
                    f"{ctx.migration.vm.name} completed again: {exc}; a later run copies them "
                    "in full"
                )
                return

    async def _mark_kit_copies(self, ctx: StepContext) -> None:
        """After a vmware_warm pass the kit completed, mark each of the VM's kit volumes that is
        not converted and that no server holds: its copy completed, and a later run keeps it for
        its CBT sync (SDD §7.2, review RV30-01). A marking that fails fails the pass as
        transient: its retry copies the unmarked volumes in full."""
        dst = self.providers.get(ctx.destination)
        try:
            for volume in kit_volumes(ctx.migration.vm.name, await dst.volumes()):
                if (
                    _marked(volume, "converted")
                    or _marked(volume, "seamless_copied")
                    or volume.get("attachments")
                ):
                    continue
                await dst.set_volume_metadata(volume["id"], dict(COPIED))
        except ProviderError as exc:
            raise TransientStepError(
                f"could not mark the kit's copies of {ctx.migration.vm.name} in "
                f"{ctx.destination.label}: {exc}"
            ) from exc

    async def _result(
        self,
        step: StepName,
        strategy: Strategy,
        ctx: StepContext,
        osm: Path,
        started: Any,
        passes_before: int = 0,
    ) -> StepResult:
        m = ctx.migration
        if step == StepName.ROLLBACK:
            return StepResult(details={"source_running": True})
        if strategy == Strategy.warm:
            state = await asyncio.to_thread(_read_warm_state, osm, m.vm.source_id)
            if not state or not state.get("passes"):
                raise PermanentStepError("the warm state file has no recorded pass")
            if (
                len(state["passes"]) <= passes_before
                and step == StepName.CUTOVER
                and state.get("destination_server_id")
            ):
                # a re-run after an attempt that ran the final pass and created the server,
                # then failed: the role finished that server (SDD §6.5), the cutover is done -
                # with the final pass when the migration does not list it yet (SDD §7.2)
                last = state["passes"][-1]
                listed = {p.number for p in m.sync_passes}
                final = None
                if last.get("kind") == "final" and int(last.get("number") or 0) not in listed:
                    final = _warm_sync_pass(last, m, started)
                return StepResult(
                    sync_pass=final, destination_server_id=state["destination_server_id"]
                )
            if len(state["passes"]) <= passes_before:
                # the role skipped the workload (prelim reported no change): re-reporting the
                # previous pass would feed a phantom pass into convergence and the estimate
                raise PermanentStepError(
                    f"the playbook recorded no new pass for {m.vm.name} (the workload was "
                    f"skipped); check {ctx.destination.label} for a same-named server"
                )
            sync_pass = _warm_sync_pass(state["passes"][-1], m, started)
            if step == StepName.CUTOVER:
                server = state.get("destination_server_id") or await self._lookup_server(ctx)
                return StepResult(sync_pass=sync_pass, destination_server_id=server)
            return StepResult(sync_pass=sync_pass)
        if step == StepName.CUTOVER:
            server = await self._lookup_server(ctx)
            if strategy != Strategy.cold:
                return StepResult(destination_server_id=server)
            # SDD §7.2: the copy the state file reported as progress is the cutover's full pass -
            # the VM's used bytes, what its 100 % stands for - so bytes_transferred stays the
            # listed passes (§5.5 invariant 2); the copy dominates the step's duration
            ended = utcnow()
            copied = m.bytes_total or m.vm.used_bytes
            return StepResult(
                sync_pass=SyncPass(
                    number=next_pass_number(m),
                    kind=SyncPassKind.full,
                    started_at=started,
                    ended_at=ended,
                    bytes_scanned=m.vm.disk_bytes,
                    bytes_changed=copied,
                    bytes_transferred=copied,
                    duration_s=(ended - started).total_seconds(),
                ),
                destination_server_id=server,
            )
        # vmware_warm CBT passes: the kit does not report byte counts to the control plane
        ended = utcnow()
        number = next_pass_number(m)
        return StepResult(
            sync_pass=SyncPass(
                number=number,
                kind=SyncPassKind.full if number == 1 else SyncPassKind.delta,
                started_at=started,
                ended_at=ended,
                duration_s=(ended - started).total_seconds(),
            )
        )

    async def _destination_inventory(self, ctx: StepContext, deferred: Deferred | None) -> Any:
        """The destination's inventory for an OpenStack run, read once for it (SDD §7.2): the
        networks of its network references, and the flavors a GPU VM's mapping must still apply to
        (§22.4). None when it cannot be read - logged: the references stay as exported, and the
        destination check (§6.6) still looks them up before the source stops -, except at a GPU
        VM's cutover, which then fails as transient before any playbook: it never creates the
        server on a mapping it could not judge. A pre-copy or sync run (``deferred``) creates no
        server and goes on."""
        vm, where = ctx.migration.vm, ctx.destination.label
        try:
            return await self.providers.get(ctx.destination).inventory()
        except ProviderError as exc:
            gpu = bool(vm_gpus(vm, declared_gpu_profile(vm, ctx.source)))
            if gpu and deferred is None:
                raise TransientStepError(
                    f"could not read the flavors of {where} for the GPU flavor of {vm.name}: {exc}"
                ) from exc
            await ctx.log(
                f"could not read the networks of {where} ({exc}): the network references stay as "
                "exported"
            )
            if gpu:
                await ctx.log(
                    f"the GPU flavor of {vm.name} is not judged in this pass: the cutover reads "
                    f"the flavors of {where} again before the source stops"
                )
            return None

    @staticmethod
    def _refuse_gpu_mapping(ctx: StepContext, inventory: Any, deferred: list[str] | None) -> None:
        """Before the source stops, refuse a GPU VM whose mapping no longer applies to the
        destination's flavors ``inventory`` read for the run (SDD §7.2, §22.4): a permanent
        failure - or, in a pre-copy or sync run (``deferred``), which creates no server, a problem
        the pass records for its cutover. Without an inventory nothing is judged: a pass that
        could not read it leaves that to its cutover."""
        if inventory is None:
            return
        vm = ctx.migration.vm
        refusal = gpu_mapping_refusal(
            vm,
            ctx.plan,
            ctx.source.kind,
            inventory,
            ctx.destination.label,
            declared_profile=declared_gpu_profile(vm, ctx.source),
        )
        if refusal is None:
            return
        if deferred is None:
            raise PermanentStepError(refusal)
        deferred.append(refusal)

    async def _kit_inventory(self, ctx: StepContext) -> Any:
        """The destination's inventory for a kit run, read once for it: the networks, subnets and
        token roles of the network map, and the flavors a GPU VM's mapping must still apply to (SDD
        §7.2, §22.4). One that cannot be read is a transient failure, before the kit runs."""
        try:
            return await self.providers.get(ctx.destination).inventory()
        except ProviderError as exc:
            raise TransientStepError(
                f"could not read the networks of {ctx.destination.label}: {exc}"
            ) from exc

    async def _vmware_network_map(
        self, ctx: StepContext, inventory: Any, deferred: list[str] | None = None
    ) -> dict[str, str]:
        """The migration kit's network_map (SDD §7.2): each NIC of the VM by the kit's key to
        "{network id},{subnet id}" of the RHOSO network its port group resolves to - the plan's
        mapping of it, else the same name, by the rule of §7.3 step 0 against the networks of the
        run's ``inventory`` - and of the subnet that holds its IPv4 guest addresses. Refused before
        the kit runs: a network that does not resolve, one the credential may not put a port on
        (§6.6), addresses the kit's port cannot take, two NICs one key would send to different
        values - a NIC with no IPv4 address takes any of its network's -, a MAC or address another
        port of the network holds; ports that cannot be read are a transient failure. Given
        ``deferred`` (a pre-copy or sync run, in which the kit makes no port), the network the
        credential may not use and the held MAC or address are added there instead and the NIC is
        mapped, and ports that cannot be read are logged."""
        dst = self.providers.get(ctx.destination)
        networks = list(getattr(inventory, "network_details", None) or [])
        subnets = list(getattr(inventory, "subnets", None) or [])
        # the token's roles read now with the inventory, as the handover's step 0 and the
        # collection read them; the check report's only when they cannot be read - a report new
        # credentials reset names none (SDD §7.2, §10, review RV19-01)
        live = getattr(inventory, "roles", None)
        roles = live if live is not None else ctx.destination.capabilities.get("roles")
        vm = ctx.migration.vm
        out: dict[str, str] = {}
        problems: list[str] = []
        bare: list[tuple[str, str, str]] = []

        def claim(what: str, key: str, value: str, any_subnet: bool = False) -> None:
            have = out.setdefault(key, value)
            # a value is "{network id},{subnet id}", or the network's id alone
            if have != value and not (
                any_subnet and have.partition(",")[0] == value.partition(",")[0]
            ):
                problems.append(
                    f"{what} shares the migration kit's key {key!r} with another NIC that goes to "
                    f"{have}, not {value}"
                )

        for nic in vm.nics:
            # the plan's mapping of its port group, else the same name: the kit keeps the guest's
            # network settings, so no NIC override moves it
            name = ctx.plan.mappings.networks.get(nic.network, nic.network)
            what = f"the NIC {nic.mac or '?'} of {vm.name}"
            network, why = resolve_network(networks, name)
            if network is None:
                problems.append(
                    f"{what} cannot go to network {name!r} of {ctx.destination.label}: "
                    + (why or f"there is no network named {name!r}")
                )
                continue
            profile = policy_profile_of(ctx.destination.capabilities, ctx.destination)
            # no roles to judge, and a port there needs one: never let through (RV19-01)
            refused = port_refusal_at_run(network, roles, profile, where=ctx.destination.label)
            if refused:
                problem = (
                    f"{what} cannot go to network {name!r} of {ctx.destination.label} "
                    f"(project {network.get('project_id')}): {refused}"
                )
                if deferred is None:
                    problems.append(problem)
                    continue
                # the kit makes the port at the cutover only: the NIC is mapped meanwhile
                deferred.append(problem)
            own = [s for s in subnets if s.get("network_id") == network.get("id")]
            subnet, why = kit_subnet(nic, own)
            if why:
                problems.append(f"{what} on {name!r}: {why}")
                continue
            try:
                held = await self._held_by_another_port(
                    dst, str(network["id"]), nic, ctx.destination.label
                )
            except TransientStepError as exc:
                if deferred is None:
                    raise
                await ctx.log(
                    f"{what} on {name!r}: {exc}; the cutover reads them again before the power-off"
                )
                held = None
            if held:
                if deferred is None:
                    problems.append(f"{what} on {name!r}: {held}")
                    continue
                deferred.append(f"{what} on {name!r}: {held}")
            key, value = kit_network_key(nic), kit_network_value(network, subnet)
            if key is None:
                # the kit would drop a distributed port group's NIC that a name keys
                problems.append(
                    f"{what} was validated before the migration kit's key of its port group was "
                    "recorded: validate the plan again"
                )
            elif kit_ipv4(nic.fixed_ips):
                claim(what, key, value)
            else:
                # needs no particular subnet: it takes the one another NIC of its key needs
                bare.append((what, key, value))
        for what, key, value in bare:
            claim(what, key, value, any_subnet=True)
        if problems:
            raise PermanentStepError("; ".join(problems))
        return out

    @staticmethod
    async def _log_deferred(ctx: StepContext, deferred: Deferred | None) -> None:
        """Log, once each, what a warm pass did not refuse and the cutover will (SDD §7.2); the
        list keeps it for the pass's cutover_refusals."""
        if deferred is None:
            return
        for problem in deferred[deferred.logged :]:
            await ctx.log(f"the cutover will refuse this before the source stops: {problem}")
        deferred.logged = len(deferred)

    @staticmethod
    async def _held_by_another_port(
        dst: Any, network_id: str, nic: Nic, where: str = "the destination"
    ) -> str | None:
        """What another port of the network holds of what the kit asks Neutron for after the
        power-off: the NIC's MAC, else one of its IPv4 addresses (SDD §7.2); a failed read names
        the destination provider, ``where`` (SDD §1 naming)."""
        try:
            if nic.mac:
                for port in await dst.ports(network_id=network_id, mac_address=nic.mac.lower()):
                    return f"port {port['id']} holds its MAC {nic.mac}"
            for address in kit_ipv4(nic.fixed_ips):
                for port in await dst.ports(network_id=network_id, ip_address=address):
                    return f"port {port['id']} holds {address}"
        except ProviderError as exc:
            raise TransientStepError(f"could not read the ports of {where}: {exc}") from exc
        return None

    @staticmethod
    async def _delete_kit_ports(dst: Any, nics: list[Nic], vm_name: str) -> list[str]:
        """Delete the migration kit's ports of the VM (SDD §7.2): Nova only unbinds a port it did
        not create when it deletes the server, and the port would keep the NIC's MAC and addresses.
        They are the unbound ports with one of the NICs' MACs and the kit's name form for this VM;
        a port bound to a server, or another VM's that shares a MAC, is never deleted (EX-07)."""
        own = kit_port_name(vm_name)
        deleted: list[str] = []
        for mac in sorted({nic.mac.lower() for nic in nics if nic.mac}):
            for port in await dst.ports(mac_address=mac):
                if port.get("device_id") or not own.match(str(port.get("name") or "")):
                    continue
                await dst.delete_port(port["id"])
                deleted.append(str(port["id"]))
        return deleted

    async def _delete_kit_volumes(
        self, ctx: StepContext, dst: Any, server_id: str | None, kept: Collection[str] = ()
    ) -> list[str]:
        """Delete the kit's volumes of the VM that its next run would take as this VM's own (SDD
        §7.2, review RV28-01): the kit finds a VM's volume by its name, ``<kit name>-<disk key>``,
        and the metadata ``osm``; one it marked ``converted`` it takes as migrated - no copy, no
        power-off, the new server booted from the first attempt's disk while the source runs
        again - and onto any other a vmware_cold run refuses to copy. A vmware_warm volume not
        converted stays when marked copied and attachable - the next pass syncs it by CBT -, and
        one not marked, a first copy cut off, goes (reviews RV30-01, RV30-07); with a recorded
        server - the kit booted it from all of them - every one goes (review RV30-02). The
        destination's conversion host holding one is the kit's copy a cut-off run left: detached
        first (review RV29-02). A volume another server holds is never deleted, nor one of
        ``kept`` - what a server found only by name held (review RV29-03) -, nor any of a VM
        without a kit name."""
        m = ctx.migration
        if not kit_vm_name(m.vm.name):
            return []
        deleted: list[str] = []
        conversion_host: list[str | None] = []  # looked up once, for the first volume held
        for volume in kit_volumes(m.vm.name, await dst.volumes()):
            if str(volume["id"]) in kept:
                continue
            holders = [str(holder) for holder in volume.get("attachments") or []]
            if any(holder != server_id for holder in holders):
                if not conversion_host:
                    conversion_host.append(await self._conversion_host_server(ctx, dst))
                host = conversion_host[0]
                if host is not None and host in holders:
                    await dst.detach_volume(host, volume["id"])
                    holders = [holder for holder in holders if holder != host]
                if any(holder != server_id for holder in holders):
                    continue
            if m.strategy != Strategy.vmware_cold and server_id is None and _resumable(volume):
                # a warm copy that completed, from which no recorded server booted: the next pass
                # syncs it by CBT (reviews RV30-01, RV30-02, RV30-07)
                continue
            await dst.delete_volume(volume["id"])
            deleted.append(str(volume["id"]))
        return deleted

    @staticmethod
    async def _conversion_host_server(ctx: StepContext, dst: Any) -> str | None:
        """The destination conversion host's Nova server: by its name, else the server holding
        its address - fixed, or floating through Neutron's floating IP (SDD §7.2, reviews RV29-02,
        RV30-06)."""
        host = ctx.destination.conversion_host
        if host is None:
            return None
        if host.name:
            found = await dst.find_server(host.name)
            if found:
                return str(found)
        if host.address:
            found = await dst.server_of_address(host.address)
            if found:
                return str(found)
        return None

    async def _lookup_server(self, ctx: StepContext) -> str | None:
        name = destination_server_name(ctx.migration)
        try:
            return await self.providers.get(ctx.destination).find_server(name)
        except ProviderError as exc:
            await ctx.log(
                f"could not look up the server named {name} in {ctx.destination.label}: {exc}"
            )
            return None

    async def _vmware_rollback(self, ctx: StepContext) -> StepResult:
        m = ctx.migration
        vm = m.vm
        if ctx.options.get("delete_dest_volumes"):
            return await self._vmware_cancel_cleanup(ctx)
        rdir = run_dir(self.settings, ctx.plan.id, m.id)
        try:
            dst = self.providers.get(ctx.destination)
            recorded = m.destination_server_id
            named = None
            # what an earlier attempt kept of a server it then deleted (review RV30-05); a record
            # that cannot be read never keeps the source stopped: what to keep is unknown, so this
            # attempt deletes no kit volume (SDD §7.2, review RV32A-08)
            unreadable = unrecorded = None
            try:
                kept = await asyncio.to_thread(_read_kept, rdir)
            except _UnreadableKeepRecord as exc:
                kept, unreadable = set(), str(exc)
                await ctx.log(f"rollback: {unreadable}; every kit volume of {vm.name} stays")
            if source_stopped(m):
                # the kit records nothing in the control plane before the cutover powers the
                # VM off; a same-named server after that point was created by this migration
                # (_refuse_name_collision ruled out a pre-existing one before the attempt) - also
                # beside a recorded one, maybe an earlier attempt's that a failed rollback already
                # deleted while the new attempt's cutover did not record its own (review RV32A-07).
                # Only while the clock is open: an earlier attempt's power-on closed it only after
                # every server it found was gone, a verification only with the recorded one
                # running - a server of the kit's name then is another VM's (review RV34A-01)
                found = await dst.find_server(kit_vm_name(vm.name))
                if found and found != recorded:
                    named = found
                    # it may be another plan's VM the kit names alike (owner question q26): what
                    # it holds stays, its deletion detaching it notwithstanding (review RV29-03) -
                    # read from Nova's record, as no Cinder call comes before the power-on, and
                    # recorded before the deletion: a retried attempt no longer finds the server
                    held = set(await dst.server_volumes(named))
                    kept |= held
                    holds = ", ".join(sorted(held)) or "no volume"
                    try:
                        # a record of its own, also after a read that failed (review RV35A-02)
                        await asyncio.to_thread(_record_kept, rdir, named, held)
                    except OSError as exc:
                        # never keeps the source stopped: this attempt deletes no kit volume
                        # (SDD §7.2, §8, review RV35A-03)
                        unrecorded = f"could not record what {named} holds ({holds}): {exc}"
                        await ctx.log(
                            f"rollback: {unrecorded}; every kit volume of {vm.name} stays"
                        )
                    else:
                        await ctx.log(f"rollback: what {named} holds stays: {holds}")
            servers = [server for server in (recorded, named) if server]
            for server in servers:
                await dst.delete_server(server)
            server_id = recorded or named
            # the source next: only the server's deletion comes before it - two live copies on
            # a stretched network -, and the destination's clean-up never keeps it stopped - the
            # kit's ports and volumes matter only to the next attempt, which a failure there
            # retries with the source running (SDD §7.2, §8, reviews RV29-01, RV30-04)
            source = self.providers.get(ctx.source)

            async def power_on_and_close() -> None:
                await source.power_on(vm.source_id)
                # it runs: the clock closes now, whatever fails after (SDD §5.2, review RV30-03)
                await ctx.mark_downtime_end()

            # one unit, which a cancel - the step timeout, a shutdown - waits out: a power-on
            # vCenter finishes never leaves the clock open while the VM runs (SDD §5.2, review
            # RV38A-04)
            await outlive_cancel(power_on_and_close())
            if servers or kept or unreadable:
                # a server booted from the kit's volumes: what this rollback then cannot delete
                # or keeps is no completed copy for a later run (SDD §7.2, review RV32A-01)
                await self._mark_cutover_copies(ctx)
            deleted_ports = await self._delete_kit_ports(dst, vm.nics, vm.name)
            deleted_volumes = (
                []
                if unreadable or unrecorded
                else await self._delete_kit_volumes(ctx, dst, m.destination_server_id, kept)
            )
        except ProviderError as exc:
            raise TransientStepError(f"rollback: {exc}") from exc
        # done: a later rollback starts from what it finds (review RV29-03)
        await asyncio.to_thread(_remove_kept, rdir)
        return StepResult(
            details={
                "source_running": True,
                "deleted_server": server_id,
                "deleted_servers": servers,
                "deleted_ports": deleted_ports,
                "deleted_volumes": deleted_volumes,
            }
        )

    async def _vmware_cancel_cleanup(self, ctx: StepContext) -> StepResult:
        """A cancel's cleanup on VMware deletes only a server the migration recorded: no lookup by
        name, no kit port or volume, no power-on - the VM it released may since be another plan's
        (SDD §5.1, §7.2, review RV-01, RV-02). The kit's volumes of the VM it keeps it names: the
        kit names them after the VM, so another plan's migration of it takes them as its own, and
        the operator deletes them once none migrates it; a read that fails is named and fails
        nothing, the server being gone (review RV42B, lead NP-2). One a server holds is that
        server's disk - another plan's cut-over server of the VM, another VM the kit names alike
        -, never named for deletion (review RV43B-01); one the destination's conversion host alone
        holds is the kit's copy a pass the cancel cut off left there: named as attached to it, left
        attached - another plan's pass of the VM may sync onto it (review RV44B-01). A conversion
        host it cannot look up leaves each held volume one of a holder it could not tell
        (`unsure_volumes`), named with the error (`unsure_why`), never unnamed (review RV45B-02)."""
        server_id = ctx.migration.destination_server_id
        dst = self.providers.get(ctx.destination)
        if server_id:
            try:
                await dst.delete_server(server_id)
            except ProviderError as exc:
                raise TransientStepError(f"rollback: {exc}") from exc
        details: dict[str, Any] = {"deleted_server": server_id, "deleted_ports": []}
        try:
            volumes = await dst.volumes()
        except ProviderError as exc:
            return StepResult(
                details={**details, "kept_volumes": None, "kept_volumes_unread": str(exc)}
            )
        kept: list[dict[str, Any]] = []
        unsure: list[dict[str, Any]] = []  # held by a server it could not tell
        conversion_host: list[str | None] = []  # looked up once, for the first kit volume held
        lookup_error: str | None = None
        for volume in kit_volumes(ctx.migration.vm.name, volumes):
            entry: dict[str, Any] = {"id": str(volume["id"]), "name": str(volume.get("name"))}
            holders = [str(holder) for holder in volume.get("attachments") or []]
            if holders:
                if not conversion_host:
                    try:
                        conversion_host.append(await self._conversion_host_server(ctx, dst))
                    except ProviderError as exc:
                        conversion_host.append(None)
                        lookup_error = str(exc)
                if lookup_error is not None:
                    unsure.append(entry)  # the conversion host's copy, or another server's disk
                    continue
                if conversion_host[0] is None or holders != [conversion_host[0]]:
                    continue
                entry["on_conversion_host"] = True
            kept.append(entry)
        details["kept_volumes"] = kept
        if unsure:
            details["unsure_volumes"] = unsure
            details["unsure_why"] = lookup_error
        return StepResult(details=details)

    async def _finalize(self, ctx: StepContext) -> StepResult:
        if not ctx.options.get("delete_source"):
            return StepResult(details={"source_deleted": False})
        if ctx.source.kind == ProviderKind.vmware:
            return StepResult(
                details={
                    "source_deleted": False,
                    "reason": "deleting a VMware source VM is not automated: delete it in vCenter",
                }
            )
        try:
            await self.providers.get(ctx.source).delete_server(ctx.migration.vm.source_id)
        except ProviderError as exc:
            raise TransientStepError(f"finalize: {exc}") from exc
        return StepResult(details={"source_deleted": True})

    # -- process plumbing ----------------------------------------------------------------------
    def _env(self) -> dict[str, str]:
        if not self.settings.ansible_host_key_checking:
            # SEC-02 (Security.md §6.3) is open: a conversion host made for the migration has no
            # key to compare with; said once per run (SDD §15.1), not once per playbook
            log.warning(
                "the conversion hosts' SSH host keys are not verified "
                "(SEAMLESS_ANSIBLE_HOST_KEY_CHECKING is off)"
            )
        return ansible_env(self.settings)

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
        networks: Sequence[dict[str, Any]] | None = None,
        deferred: list[str] | None = None,
        *,
        trace: _RunTrace | None = None,
        trust: _Trust | None = None,
    ) -> bool:
        """Run the playbooks; True when the source-stop task of one of them was skipped. What the
        output showed is kept in ``trace``; ``trust``, while host keys are checked, is what the
        run checks its conversion hosts' keys against (SDD §7.2)."""
        vars_path = workdir / "vars.yml"
        secrets_path = workdir / "secrets.yml"
        inventory_path = workdir / "inventory.yml"
        key_path = conversion_key_path(ctx) if ctx is not None else None
        cleanup = (secrets_path, osm / "clouds.yaml", workdir / "clouds.yaml")
        if key_path is not None:
            cleanup += (key_path,)
        # the Ceph files of a tracked warm plan go with the run, like secrets.yml (SDD §7.2)
        ceph_files = ceph_run_files(ctx) if ctx is not None else []
        cleanup += tuple(path for path, _ in ceph_files)
        control: Path | None = None
        if trust is not None:
            variables = {**variables, **trust.variables()}
            # the injected keys' private halves go with the run, whatever its outcome (SDD §7.2)
            cleanup += tuple(trust.secrets())
            # Ansible's ssh masters, named by address and not by alias, of this run alone: an
            # earlier run's would carry its connections without a check of the key (SDD §7.2)
            control = workdir / CONTROL_PATH_DIR

        def prepare() -> None:
            osm.mkdir(parents=True, exist_ok=True, mode=0o700)
            if trust is not None:
                # before any playbook: no ssh of the run meets a host without its keys
                trust.write()
            if control is not None:
                shutil.rmtree(control, ignore_errors=True)
                control.mkdir(mode=0o700)
            write_secret_file(vars_path, ansible_yaml(variables))
            write_secret_file(inventory_path, inventory)
            secret_vars = build_secret_vars(source, destination, self.settings)
            write_secret_file(secrets_path, ansible_yaml(secret_vars))
            for path, content in ceph_files:
                write_secret_file(path, content)
            if key_path is not None:
                secret = destination.conversion_host.ssh_key_secret  # type: ignore[union-attr]
                try:
                    write_secret_file(key_path, resolve_private_key(secret, self.settings))
                except SecretNotFound as exc:
                    raise PermanentStepError(f"conversion host key unavailable: {exc}") from None

        stop_skipped = False
        try:
            await asyncio.to_thread(prepare)
            env = await asyncio.to_thread(self._env)
            if control is not None:
                env["ANSIBLE_SSH_CONTROL_PATH_DIR"] = str(control)
                env.pop("ANSIBLE_SSH_CONTROL_PATH", None)
            for playbook in playbooks:
                if playbook.name == "export_workloads.yml":
                    # written again once the rewrite of the new export finishes (SDD §7.2)
                    remove_quietly(osm / EXPORT_RECORD)
                stop_skipped |= await self._run_playbook(
                    playbook,
                    workdir,
                    inventory_path,
                    vars_path,
                    secrets_path,
                    ctx,
                    osm,
                    env,
                    trace=trace,
                )
                if playbook.name == "export_workloads.yml" and (ctx or mappings) is not None:
                    planned = ctx.migration.vm.source_id if ctx is not None else None
                    mode = ctx.plan.port_creation_mode if ctx is not None else None
                    nics = ctx.plan.nic_overrides.get(planned or "") if ctx is not None else None
                    volumes = (
                        [d.id for d in ctx.migration.vm.disks if d.kind == "volume"]
                        if ctx is not None
                        else None
                    )
                    await asyncio.to_thread(
                        self._rewrite_workloads,
                        osm,
                        mappings,
                        planned,
                        mode,
                        nics,
                        networks,
                        deferred,
                        volumes,
                        policy_profile_of(destination.capabilities, destination),
                        destination.label,
                        import_floating_ip_mode(ctx.plan) if ctx is not None else "auto",
                    )
                    if ctx is not None:
                        await self._log_deferred(ctx, deferred)
        finally:
            # synchronous on purpose: runs even when the step task is being cancelled
            remove_quietly(*cleanup)
            if control is not None:
                shutil.rmtree(control, ignore_errors=True)
        return stop_skipped

    @staticmethod
    def _rewrite_workloads(
        osm: Path,
        mappings: Mappings | None,
        planned_id: str | None = None,
        port_mode: str | None = None,
        nic_overrides: Mapping[str, NicOverride] | None = None,
        networks: Sequence[dict[str, Any]] | None = None,
        deferred: list[str] | None = None,
        validated_volumes: Sequence[str] | None = None,
        profile: str = "legacy",
        where: str = "the destination",
        floating_ip_mode: str = "auto",
    ) -> None:
        """Keep only the planned server of the export, check that it still lists every volume
        the migration validated (``validated_volumes``), apply the mappings, the VM's NIC
        overrides and the plan's port_creation_mode, and resolve the network references against
        the destination's ``networks`` - a warm pass adds a port network that does not resolve
        to ``deferred`` -; once all of it succeeded, write the export record (SDD §7.2)."""
        path = osm / "workloads.yml"
        if not path.exists():
            raise PermanentStepError("export_workloads.yml did not produce workloads.yml")
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        resources = doc.get("resources") or []
        servers = [r for r in resources if r.get("type") == SERVER_TYPE]
        if not servers:
            raise PermanentStepError("the exported workloads.yml contains no server")
        if planned_id is not None:
            # os-migrate exports by name: a same-named server of the project that is not in the
            # plan must never be imported, stopped or migrated
            def server_id(resource: dict[str, Any]) -> Any:
                return (resource.get("_info") or {}).get("id")

            if not any(server_id(r) == planned_id for r in servers):
                exported = ", ".join(str(server_id(r)) for r in servers)
                raise PermanentStepError(
                    f"the export holds no server with the planned id {planned_id} "
                    f"(exported: {exported})"
                )
            doc["resources"] = [
                r for r in resources if r.get("type") != SERVER_TYPE or server_id(r) == planned_id
            ]
        if mappings is not None:
            doc = apply_mappings(doc, mappings)
        if port_mode is not None:
            doc = set_port_creation_mode(doc, port_mode, profile, floating_ip_mode)
        # written first: a rollback reads the planned server from it, should an override fail
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        if validated_volumes:
            # a volume detached from the VM since - by an operator, or by an earlier attempt's
            # export that failed after it detached the data volumes - would be left out of the
            # import, and the migration would succeed without that disk (review RV6-01)
            kept = [r for r in doc.get("resources") or [] if r.get("type") == SERVER_TYPE]
            listed = {
                (volume.get("_info") or {}).get("id")
                for resource in kept
                for volume in (resource.get("params") or {}).get("volumes") or []
            }
            lacking = [v for v in validated_volumes if v not in listed]
            if lacking:
                raise PermanentStepError(
                    "the export lists none of the volume(s) " + ", ".join(lacking) + " the VM "
                    "had at validation: attach them to the source VM again (an earlier attempt "
                    "may have left them detached) and retry (SDD §7.2)"
                )
        if nic_overrides:
            doc = apply_nic_overrides(doc, nic_overrides)
            path.write_text(yaml.safe_dump(doc, sort_keys=False))
        if networks:
            doc = resolve_network_refs(doc, networks, deferred, where)
            path.write_text(yaml.safe_dump(doc, sort_keys=False))
        missing = ports_without_security_groups(doc) if port_mode == "neutron" else []
        if missing:
            # written first: a rollback reads the planned server from it
            raise PermanentStepError(
                "os-migrate exported the ports on " + ", ".join(missing) + " without their "
                "security groups: recreated through Neutron they would get the project's default "
                "group. Update the os-migrate collection, or recreate the ports with Nova."
            )
        exported = [r for r in doc.get("resources") or [] if r.get("type") == SERVER_TYPE]
        name = (exported[0].get("params") or {}).get("name") if exported else None
        record = export_record(mappings or Mappings(), nic_overrides, name)
        (osm / EXPORT_RECORD).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

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
        *,
        trace: _RunTrace | None = None,
    ) -> bool:
        """Run one playbook; True when its source-stop task was seen but skipped."""
        name = playbook.name
        watch_stop = playbook.stops_source and ctx is not None
        stop_seen_at: Any = None  # the stop task started; its result line confirms it ran
        stop_skipped = False
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
                limit=_OUTPUT_LINE_LIMIT,
            )
        except OSError as exc:
            raise PermanentStepError(f"cannot run {self.settings.ansible_playbook}: {exc}") from exc
        # its session's process group, until it ended: a deposition kills it (SDD §7.1, §25.4)
        self.groups.add(proc.pid)
        tail = asyncio.create_task(self._tail(ctx, osm)) if ctx is not None else None
        lines: deque[str] = deque(maxlen=400)
        try:
            assert proc.stdout is not None
            eof = False
            while not eof:
                try:
                    raw = await proc.stdout.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    raw, eof = exc.partial, True
                except asyncio.LimitOverrunError:
                    # a line above the stream limit (a huge module result) is read in pieces
                    # instead of failing the step; readline() would discard it
                    raw = await proc.stdout.read(_OUTPUT_LINE_LIMIT)
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                lines.append(line)
                if ctx is not None and line.strip():
                    await ctx.log(line)  # whole: the orchestrator redacts, then cuts (SDD §4)
                if watch_stop and STOP_TASK.match(line):
                    # SDD §7.2: the downtime clock starts when the source-stop task starts…
                    watch_stop = False
                    stop_seen_at = utcnow()
                    if trace is not None:
                        trace.stop_task_started = True
                elif stop_seen_at is not None and line.startswith("TASK ["):
                    # …the next task began without a result that ran: the stop was skipped
                    stop_seen_at = None
                    stop_skipped = True
                elif stop_seen_at is not None and _RESULT_LINE.match(line):
                    # …but only once its result line shows it ran (a skipped task, e.g. with
                    # data_copy: false, prints the TASK header too and stops nothing); with a
                    # loop, keep waiting while items are skipped
                    if not line.startswith("skipping:"):
                        await ctx.mark_downtime_start(at=stop_seen_at)  # type: ignore[union-attr]
                        stop_seen_at = None
                    elif "(item=" not in line:
                        stop_seen_at = None
                        stop_skipped = True
            returncode = await proc.wait()
            if stop_seen_at is not None and ctx is not None:
                # the playbook ended without a result line for the task: assume it ran
                await ctx.mark_downtime_start(at=stop_seen_at)
        except asyncio.CancelledError:
            if stop_seen_at is not None and ctx is not None:
                # SDD §7.2: cancelled while the stop task ran - the source may be stopped, so the
                # clock opens at the task's start (the rollback starts it, a cancel is refused)
                await outlive_cancel(ctx.mark_downtime_start(at=stop_seen_at))
            raise
        finally:
            try:
                if tail is not None:
                    tail.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await tail
                if proc.returncode is None:
                    await self._stop_process(proc)
            finally:
                self.groups.discard(proc.pid)
        if returncode != 0:
            output = "\n".join(lines)
            detail = next(
                (ln for ln in reversed(lines) if "fatal" in ln.lower() or "error" in ln.lower()),
                lines[-1] if lines else "",
            )
            if ctx is None:
                # prestage has no migration to log to: keep the tail in the service log
                log.warning(
                    "%s failed (exit %s); last lines:\n%s",
                    name,
                    returncode,
                    redact_tail(output, 4000),
                )
            if trace is not None:
                # for the run's conversion_host.key_refused (SDD §7.2)
                trace.refusal = refusal_in(output)
            raise classify_failure(output)(
                f"{name} failed (exit {returncode}): {redact_cut(detail, 500)}"
            )
        return stop_skipped

    @staticmethod
    async def _stop_process(proc: asyncio.subprocess.Process) -> None:
        await stop_process_group(proc)

    async def _tail(self, ctx: StepContext | None, osm: Path) -> None:
        if ctx is None:
            return
        state_file = osm / "workload_logs" / f"{workload_file_stem(ctx.migration.vm.name)}.state"
        total = ctx.migration.bytes_total or ctx.migration.vm.used_bytes
        last: float | None = None
        while True:
            await asyncio.sleep(self.poll_s)
            pct = await asyncio.to_thread(_read_progress, state_file)
            if pct is not None and pct != last:
                last = pct
                await ctx.report_progress(pct, int(total * pct / 100), total)
