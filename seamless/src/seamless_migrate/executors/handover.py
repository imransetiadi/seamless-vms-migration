"""Storage-handover executor for clouds sharing a Ceph cluster or a NetApp ONTAP SVM (SDD §7.3).

Cutover per VM (SDD §7.3): check while the VM runs that every volume can be handed over
(references per driver family, §7.3.1; Cinder's unmanage rules; Nova microversion 2.85), stop the
source, record its definition, set ``delete_on_termination=false`` on every attachment, delete the
source server, ``os-unmanage`` the now available volumes, ``manage`` the RBD images, ONTAP files or
LUNs in RHOSO, restore their boot properties and boot the destination server from them.
Every completed sub-step is journaled in ``handover-journal.json`` so a crash resumes without
repeating work; rollback reverses the journal (6 → 3) and recreates the source server.
No data is copied in either direction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Collection
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from ..ai.redaction import redact_cut
from ..config import Settings
from ..domain.enums import Strategy
from ..domain.models import Mappings, Plan, Provider, VMRef, utcnow
from ..planning.ipam import address_refusal, requested_ips
from ..planning.networks import policy_profile_of, port_refusal_at_run
from ..providers.base import microversion_text
from ..providers.registry import connection_fingerprint
from ..storage import (
    StorageError,
    manage_reference,
    resolve_destination,
    split_host,
    storage_family,
)
from .ansible import classify_failure, effective_mappings, run_dir
from .base import PermanentStepError, StepContext, StepName, StepResult, outlive_cancel

log = logging.getLogger(__name__)
T = TypeVar("T")

JOURNAL_FILE = "handover-journal.json"
DEFINITION_FILE = "source-server.json"
#: the first disk of each other bus — virtio-scsi, IDE (legacy guests), Xen: the last guess
#: for the boot volume when neither Nova's root device nor Cinder's bootable flag names one
#: (SDD §7.3 step 2)
OTHER_ROOT_DEVICES = ("/dev/sda", "/dev/hda", "/dev/xvda")
# PUT os-volume_attachments may change delete_on_termination from this compute microversion on
KEEP_MICROVERSION = "2.85"


def _checked(response: Any, what: str) -> Any:
    """Raise for an error answer: openstacksdk's raw Proxy calls (post/put/get) do not
    (``Proxy.request(raise_exc=False)``), so a refusal would otherwise pass silently."""
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        detail = redact_cut(str(getattr(response, "text", "") or "").strip(), 300)
        detail = detail.replace("\n", " ")
        raise RuntimeError(f"{what}: HTTP {status}: {detail}")
    return response


def _microversion(value: Any) -> tuple[int, int]:
    """A microversion as (major, minor): keystoneauth's parsed tuple, (2, 96), or text, "2.96";
    (0, 0) when it is neither (review RV18-02)."""
    try:
        major, minor = microversion_text(value).split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return (0, 0)


# volume_image_metadata keys that decide how the guest boots (SDD §7.3 step 7)
BOOT_PROPERTY_PREFIXES = ("hw_", "os_", "img_")
BOOT_PROPERTY_KEYS = frozenset({"architecture"})


def boot_properties(metadata: Any) -> dict[str, str]:
    """Firmware, machine type, buses, NIC model and OS hints of a volume's image metadata."""
    if not isinstance(metadata, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in metadata.items()
        if str(k).startswith(BOOT_PROPERTY_PREFIXES) or str(k) in BOOT_PROPERTY_KEYS
    }


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _cinder_bootable(volume: Any) -> bool:
    value = _attr(volume, "is_bootable")
    if isinstance(value, str):  # Cinder's API answers "true"/"false"
        return value.strip().lower() == "true"
    return bool(value)


def _mark_boot(attachments: list[dict[str, Any]], root_device: str) -> None:
    """Mark the attachment the server boots from (SDD §7.3 step 2): the one at ``root_device``, else
    the Cinder-bootable volume with the lowest device — the inventory's rule for ``Disk.bootable`` —
    else the first disk of another bus. With none of them, no attachment is marked."""
    for att in attachments:
        att["boot"] = att["device"] == root_device
    if any(att["boot"] for att in attachments):
        return
    flagged = sorted((a for a in attachments if a["bootable"]), key=lambda a: a["device"] or "")
    if flagged:
        flagged[0]["boot"] = True
        return
    for device in OTHER_ROOT_DEVICES:
        for att in attachments:
            if att["device"] == device:
                att["boot"] = True
                return


def _not_found(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 404 or "NotFound" in type(exc).__name__


def _existing_server(conn: Any, server_id: str) -> Any:
    """The server, or ``None`` when it is already gone (HTTP 404)."""
    try:
        return conn.compute.get_server(server_id)
    except Exception as exc:
        if _not_found(exc):
            return None
        raise


def _destination_network(conn: Any, name: str, cloud: str = "the destination") -> Any:
    """A port's RHOSO network as step 8 finds it (SDD §7.3 step 0): the credential's own network of
    that name, else the only network of that name it sees - the admin credential sees every
    project's, and a name can be in several."""
    own = list(conn.network.networks(name=name, project_id=conn.current_project_id))
    if len(own) == 1:
        return own[0]
    if own:
        raise PermanentStepError(
            f"{cloud} has {len(own)} networks named {name!r} in the credential's project"
        )
    seen = list(conn.network.networks(name=name))
    if len(seen) == 1:
        return seen[0]
    if not seen:
        raise PermanentStepError(f"{cloud} has no network named {name!r}")
    projects = ", ".join(sorted({str(_attr(n, "project_id")) for n in seen}))
    raise PermanentStepError(
        f"{cloud} has {len(seen)} networks named {name!r}, none in the credential's project "
        f"(projects {projects})"
    )


def _token_roles(conn: Any) -> set[str] | None:
    """The role names of the credential's token, or None when it cannot tell (SDD §6.6)."""
    try:
        return set(conn.session.auth.get_access(conn.session).role_names or [])
    except Exception:  # an auth plugin without access info: that half is not judged
        log.debug("could not read the token's roles", exc_info=True)
        return None


def port_description(source_port_id: str) -> str:
    """The description of a destination port created through Neutron for a source port: how a
    resume finds it again and a rollback deletes it (SDD §6.6, §7.3 step 8). The collection's
    ``server_port.port_description`` writes the same."""
    return f"os-migrate:{source_port_id}"


def _existing_port(conn: Any, port_id: str) -> Any:
    """The port, or ``None`` when it is gone (HTTP 404): Nova deletes the ports it created."""
    try:
        return conn.network.get_port(port_id)
    except Exception as exc:
        if _not_found(exc):
            return None
        raise


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def _in_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """``asyncio.to_thread`` that no cancel cuts short (SDD §7.3): a call in flight cannot be
    stopped, so a cancelled step ends only once its call returned and journaled what it did, and
    the rollback that follows reads the whole journal (audit EX-01)."""
    return await outlive_cancel(asyncio.to_thread(fn, *args, **kwargs))


def _recreated_vm(vm: VMRef, server_id: str, new_ids: dict[str, str]) -> dict[str, Any]:
    """``vm`` as the rollback recreated it: the new server's id, each disk managed back by its new
    volume id (SDD §7.3)."""
    disks = [d.model_copy(update={"id": new_ids.get(d.id, d.id)}) for d in vm.disks]
    return vm.model_copy(update={"source_id": server_id, "disks": disks}).model_dump(mode="json")


class _Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"done": {}, "order": []}

    def done(self, key: str) -> bool:
        return key in self.data["done"]

    def get(self, key: str) -> dict[str, Any]:
        return self.data["done"].get(key) or {}

    def mark(self, key: str, **info: Any) -> None:
        self.data["done"][key] = {"at": utcnow().isoformat(), **info}
        self.data["order"].append(key)
        _write_json(self.path, self.data)

    def archive(self, label: str) -> None:
        if self.path.exists():
            stamp = utcnow().strftime("%Y%m%dT%H%M%S")
            self.path.rename(self.path.with_name(f"handover-journal.{label}-{stamp}.json"))


class HandoverExecutor:
    name = "storage_handover"

    def __init__(
        self,
        settings: Settings,
        conn_factory: Callable[[Provider], Any] | None = None,
        *,
        poll_s: float = 2.0,
        wait_s: int = 900,
    ) -> None:
        self.settings = settings
        self._conn_factory = conn_factory or self._default_conn
        #: per provider id: the fingerprint a connection was built for, and the connection
        self._conns: dict[str, tuple[str, Any]] = {}
        self._conns_lock = asyncio.Lock()
        self.poll_s = poll_s
        self.wait_s = wait_s

    def _default_conn(self, provider: Provider) -> Any:
        from ..providers.openstack import connect

        return connect(provider, self.settings)

    def supports(self, strategy: Strategy) -> bool:
        return Strategy(strategy) == Strategy.storage_handover

    async def prestage(self, plan: Plan, source: Provider, destination: Provider) -> None:
        return None  # resources are prestaged by the Ansible executor

    def run_dir(self, ctx: StepContext) -> Path:
        return run_dir(self.settings, ctx.plan.id, ctx.migration.id)

    async def _conn(self, provider: Provider) -> Any:
        """The provider's connection, built again once how it is reached changed - a credential
        rotated while a plan runs (SDD §13.3) - as the provider registry does (audit EX-14)."""
        fingerprint = connection_fingerprint(provider, self.settings)
        async with self._conns_lock:
            cached = self._conns.get(provider.id)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            conn = await _in_thread(self._conn_factory, provider)
            self._conns[provider.id] = (fingerprint, conn)
        close = getattr(cached[1], "close", None) if cached is not None else None
        if callable(close):
            with contextlib.suppress(Exception):
                close()  # the replaced connection must not keep its session
        return conn

    async def _call(self, what: str, fn: Callable[[], T]) -> T:
        try:
            return await _in_thread(fn)
        except (PermanentStepError, asyncio.CancelledError):
            raise
        except Exception as exc:
            message = f"{what}: {exc}"
            # retried or not by the error alone: the label names the destination provider, whose
            # name could hold a transient pattern ("... Timeout DC", review RV20-12)
            raise classify_failure(str(exc))(message) from exc

    async def run(self, step: StepName, ctx: StepContext) -> StepResult:
        step = StepName(step)
        if step == StepName.CUTOVER:
            return await self._cutover(ctx)
        if step == StepName.ROLLBACK:
            return await self._rollback(ctx)
        if step == StepName.FINALIZE:
            return StepResult(
                details={"source_deleted": True, "note": "the source server was removed at cutover"}
            )
        raise PermanentStepError(f"step {step} does not apply to storage_handover")

    # -- cutover ---------------------------------------------------------------------------------
    def _capture_definition(
        self, conn: Any, server_id: str, port_creation_mode: str = "nova"
    ) -> dict[str, Any]:
        server = conn.compute.get_server(server_id)
        flavor = _attr(server, "flavor") or {}
        networks = []
        for port in conn.network.ports(device_id=server_id):
            network = conn.network.get_network(_attr(port, "network_id"))
            fixed_ips = [
                {"subnet_id": ip.get("subnet_id"), "ip_address": ip.get("ip_address")}
                for ip in _attr(port, "fixed_ips") or []
            ]
            # every port with its id, MAC, addresses, security group ids and port-security flag,
            # for the rollback (SDD §7.3 step 2): Nova applies a server's groups only to the ports
            # it creates. The destination keeps the first address on the mapped network, or every
            # one through Neutron (step 8)
            entry = {
                "network_id": _attr(port, "network_id"),
                "network": _attr(network, "name"),
                "fixed_ip": fixed_ips[0]["ip_address"] if fixed_ips else None,
                "port_id": _attr(port, "id"),
                "mac_address": _attr(port, "mac_address"),
                "fixed_ips": fixed_ips,
                "security_group_ids": list(_attr(port, "security_group_ids") or []),
                "port_security_enabled": _attr(port, "is_port_security_enabled"),
            }
            if port_creation_mode == "neutron":
                # the destination looks them up by name in its own project (step 8)
                entry["security_groups"] = sorted(
                    {
                        str(_attr(conn.network.get_security_group(g), "name"))
                        for g in entry["security_group_ids"]
                    }
                )
            networks.append(entry)
        attachments = []
        for att in conn.compute.volume_attachments(server):
            volume = conn.block_storage.get_volume(_attr(att, "volume_id"))
            device = _attr(att, "device")
            dot = _attr(att, "delete_on_termination")
            attachments.append(
                {
                    "volume_id": _attr(volume, "id"),
                    "device": device,
                    "boot": False,  # decided below, once every attachment is known
                    "bootable": _cinder_bootable(volume),
                    "name": _attr(volume, "name"),
                    "size": _attr(volume, "size"),
                    "volume_type": _attr(volume, "volume_type"),
                    "host": _attr(volume, "host"),
                    "image_metadata": boot_properties(_attr(volume, "volume_image_metadata")),
                    "delete_on_termination": None if dot is None else bool(dot),
                }
            )
        # Nova's root device (an admin attribute; /dev/vda when not shown), as in the inventory
        _mark_boot(attachments, _attr(server, "root_device_name") or "/dev/vda")
        attachments.sort(key=lambda a: (a["boot"], a["device"] or ""))  # data first, boot last
        groups = _attr(server, "security_groups") or []
        return {
            "server_id": server_id,
            "name": _attr(server, "name"),
            "flavor": _attr(flavor, "original_name")
            or _attr(flavor, "name")
            or _attr(flavor, "id"),
            "metadata": dict(_attr(server, "metadata") or {}),
            "security_groups": sorted({_attr(g, "name") for g in groups if _attr(g, "name")}),
            "availability_zone": _attr(server, "availability_zone"),
            "networks": networks,
            "attachments": attachments,
            "port_creation_mode": port_creation_mode,
        }

    @staticmethod
    def _pools(conn: Any, where: str) -> dict[str, dict[str, Any]]:
        """The Cinder pools of ``where`` - "the source", or the destination provider by its label
        (SDD §1 naming, review RV21-05) - by name."""
        try:
            pools = list(conn.block_storage.backend_pools())
        except Exception as exc:
            raise PermanentStepError(
                f"cannot read the Cinder pools of {where} (scheduler-stats/get_pools, admin): "
                f"{exc}; the manage reference depends on the driver family (SDD §7.3.1)"
            ) from exc
        return {str(_attr(p, "name")): dict(_attr(p, "capabilities") or {}) for p in pools}

    def _resolve_storage(
        self,
        src: Any,
        dst: Any,
        attachments: list[dict[str, Any]],
        backend_map: dict[str, str],
        cloud: str = "the destination",
    ) -> dict[str, dict[str, Any]]:
        """Family, source pool and destination host of every volume (§7.3 step 0, §7.3.1); a
        refusal names the destination provider, ``cloud`` (SDD §1 naming)."""
        missing = sorted(
            {
                a["volume_type"] or "<default>"
                for a in attachments
                if (a["volume_type"] or "") not in backend_map
            }
        )
        if missing:
            raise PermanentStepError(f"no handover backend for volume type(s): {missing}")
        src_pools = self._pools(src, "the source")
        dst_backends = [
            {"pool": name, "family": storage_family(caps)}
            for name, caps in self._pools(dst, cloud).items()
        ]
        storage: dict[str, dict[str, Any]] = {}
        for att in attachments:
            vid, host = att["volume_id"], att.get("host")
            if not host or host not in src_pools:
                raise PermanentStepError(
                    f"volume {vid}: Cinder pool {host!r} is not listed by the source "
                    "(admin credentials show os-vol-host-attr:host)"
                )
            family = storage_family(src_pools[host])
            src_pool = split_host(host)[1]
            try:
                dst_host = resolve_destination(
                    family, src_pool, backend_map[att["volume_type"] or ""], dst_backends, cloud
                )
            except StorageError as exc:
                raise PermanentStepError(f"volume {vid}: {exc}") from None
            storage[vid] = {"family": family, "src_pool": src_pool, "dst_host": dst_host}
        return storage

    def _readiness(
        self,
        src: Any,
        dst: Any,
        server_id: str,
        backend_map: dict[str, str],
        plan: Plan | None = None,
        mappings: Mappings | None = None,
        profile: str = "legacy",
        cloud: str = "the destination",
        reported_roles: Collection[str] | None = None,
    ) -> dict[str, Any]:
        """Step 0 as a read-only report: what each volume would become, and every problem; with the
        plan, also what would make step 6 or 8 fail on a port, a security group, the flavor or a
        volume type - ``mappings`` as those steps apply them, the plan's by default; ``profile``
        is the destination's policy profile (§21.5), ``reported_roles`` the roles its check
        report names."""
        mode = plan.port_creation_mode if plan is not None else "nova"
        definition = self._capture_definition(src, server_id, mode)
        attachments = definition["attachments"]
        problems: list[str] = []
        if plan is not None:
            problems.extend(
                self._port_problems(
                    dst, definition["networks"], plan, server_id, profile, cloud, reported_roles
                )
            )
            problems.extend(self._group_problems(dst, definition, cloud))
            problems.extend(
                self._lookup_problems(dst, definition, mappings or plan.mappings, cloud)
            )
        local = self._local_disks(src, src.compute.get_server(server_id))
        if local:
            # validation rules a handover out for them, but a resize since then shows only here
            problems.append(
                "the server has " + ", ".join(local) + " on its hypervisor, which deleting it at "
                "step 4 would lose: a storage handover moves Cinder volumes only - validate the "
                "plan again for a cold or warm migration"
            )
        if attachments and not any(a["boot"] for a in attachments):
            # the destination server could not be created, nor the source again on rollback
            problems.append(
                "cannot tell which volume the server boots from: no volume is at its root device, "
                "Cinder marks none bootable and none is the first disk of a bus"
            )
        missing = sorted(
            {
                a["volume_type"] or "<default>"
                for a in attachments
                if (a["volume_type"] or "") not in backend_map
            }
        )
        if missing:
            problems.append(f"no handover backend for volume type(s): {missing}")
        src_pools: dict[str, dict[str, Any]] | None = None
        dst_backends: list[dict[str, Any]] = []
        try:
            src_pools = self._pools(src, "the source")
            dst_backends = [
                {"pool": name, "family": storage_family(caps)}
                for name, caps in self._pools(dst, cloud).items()
            ]
        except PermanentStepError as exc:
            problems.append(str(exc))
        volumes: list[dict[str, Any]] = []
        for att in attachments:
            vid, host = att["volume_id"], att.get("host")
            entry: dict[str, Any] = {
                "volume_id": vid,
                "volume_type": att["volume_type"],
                "family": None,
                "source_pool": host,
                "destination_host": None,
                "reference": None,
                "delete_on_termination": att.get("delete_on_termination"),
            }
            target = backend_map.get(att["volume_type"] or "")
            if src_pools is not None and target:
                if not host or host not in src_pools:
                    problems.append(
                        f"volume {vid}: Cinder pool {host!r} is not listed by the source"
                    )
                else:
                    family = storage_family(src_pools[host])
                    entry["family"] = family
                    try:
                        dst_host = resolve_destination(
                            family, split_host(host)[1], target, dst_backends, cloud
                        )
                        entry["destination_host"] = dst_host
                        entry["reference"] = manage_reference(
                            family, split_host(dst_host)[1], f"volume-{vid}"
                        )
                    except StorageError as exc:
                        problems.append(f"volume {vid}: {exc}")
            volumes.append(entry)
        problems.extend(self._unmanage_blockers(src, attachments))
        return {"server_id": server_id, "volumes": volumes, "problems": problems}

    async def readiness(
        self,
        source: Provider,
        destination: Provider,
        server_id: str,
        backend_map: dict[str, str],
        plan: Plan | None = None,
        mappings: Mappings | None = None,
    ) -> dict[str, Any]:
        """Read-only handover check of one server (SDD §7.3 step 0): per volume its driver
        family, source pool, destination host and manage reference, and every problem that would
        refuse the handover. Changes nothing; the lab runs it before a real cutover."""
        src = await self._conn(source)
        dst = await self._conn(destination)
        return await self._call(
            "check the handover readiness",
            lambda: self._readiness(
                src,
                dst,
                server_id,
                backend_map,
                plan,
                mappings,
                policy_profile_of(destination.capabilities, destination),
                destination.label,
                destination.capabilities.get("roles"),
            ),
        )

    @staticmethod
    def _local_disks(conn: Any, server: Any) -> list[str]:
        """The server's disks on its hypervisor, read as the inventory reads them (SDD §4.2): a
        root disk from an image, its flavor's ephemeral (GiB) and swap (MiB) disks - from the
        flavor Nova embeds, looked up by id where it embeds none (below compute 2.47)."""
        flavor = _attr(server, "flavor", default={}) or {}
        ephemeral, swap = _attr(flavor, "ephemeral"), _attr(flavor, "swap")
        if ephemeral is None and swap is None and _attr(flavor, "id"):
            full = conn.compute.get_flavor(_attr(flavor, "id"))
            ephemeral, swap = _attr(full, "ephemeral"), _attr(full, "swap")
        disks = []
        if _attr(_attr(server, "image", default={}) or {}, "id"):
            disks.append("a root disk from an image")
        if int(ephemeral or 0) > 0:
            disks.append(f"an ephemeral disk of {int(ephemeral)} GiB")
        if int(swap or 0) > 0:
            disks.append(f"a swap disk of {int(swap)} MiB")
        return disks

    @staticmethod
    def _port_problems(
        dst: Any,
        networks: list[dict[str, Any]],
        plan: Plan,
        vm_id: str,
        profile: str = "legacy",
        cloud: str = "the destination",
        reported_roles: Collection[str] | None = None,
    ) -> list[str]:
        """What would make step 8 fail on a port, or put it where it must not go, found while the
        VM still runs (§7.3 step 0): its RHOSO network does not resolve with step 8's lookup, the
        credential may not put a port there, or an address step 8 would give it is one Neutron
        refuses there (§6.6)."""
        neutron = plan.port_creation_mode == "neutron"
        # the token's roles read for the run - the destination's check report's when they cannot
        # be read; when neither names any, a port that needs a role is refused, as the kit refuses
        # it (SDD §7.3 step 0, §7.2, review RV20-07)
        roles = _token_roles(dst)
        if roles is None and reported_roles is not None:
            roles = set(reported_roles)
        problems: list[str] = []
        for n in networks:
            target, addresses = HandoverExecutor._destination_nic(plan, vm_id, n)
            where = f"port {n.get('port_id')} ({n.get('mac_address')})"
            try:
                network = _destination_network(dst, target, cloud)
            except PermanentStepError as exc:
                problems.append(f"{where}: {exc}")
                continue
            # another project's network only when shared or external, and from a role the
            # destination's policy profile counts: the admin credential sees every project's
            # private networks (§6.6, §21.5)
            project = _attr(network, "project_id")
            why_not = port_refusal_at_run(
                {
                    "own": project == dst.current_project_id,
                    "shared": bool(_attr(network, "is_shared", False)),
                    "external": bool(_attr(network, "is_router_external", False)),
                },
                roles,
                profile,
                where=cloud,
            )
            if why_not:
                problems.append(f"{where} on {target!r} of {cloud} (project {project}): {why_not}")
            net_id = _attr(network, "id")
            subnets = list(dst.network.subnets(network_id=net_id))
            # Nova keeps one address per port (step 8)
            given = addresses if neutron else addresses[:1]
            for address in given:
                why = address_refusal(address, subnets, neutron=neutron, mac=n.get("mac_address"))
                if why:
                    problems.append(f"{where} on {target!r} of {cloud}: {address} {why}")
            # what another port of the network holds: Neutron refuses its address, and with the MAC
            # kept the MAC - all but the earlier attempt's own port, which step 8 uses again
            mine = (
                {
                    _attr(p, "id")
                    for p in HandoverExecutor._earlier_ports(dst, n, network, addresses)
                }
                if neutron and n.get("port_id")
                else set()
            )
            for address in given:
                holders = [
                    _attr(p, "id")
                    for p in dst.network.ports(
                        network_id=net_id, fixed_ips=[f"ip_address={address}"]
                    )
                    if address in [ip.get("ip_address") for ip in _attr(p, "fixed_ips") or []]
                    and _attr(p, "id") not in mine
                ]
                if holders:
                    problems.append(
                        f"{where} on {target!r} of {cloud}: {address} is held by port {holders[0]}"
                    )
            mac = str(n.get("mac_address") or "").lower()
            if neutron and mac:
                holders = [
                    _attr(p, "id")
                    for p in dst.network.ports(network_id=net_id, mac_address=mac)
                    if str(_attr(p, "mac_address") or "").lower() == mac
                    and _attr(p, "id") not in mine
                ]
                if holders:
                    problems.append(
                        f"{where} on {target!r} of {cloud}: MAC {mac} is held by port {holders[0]}"
                    )
        return problems

    @staticmethod
    def _group_problems(
        dst: Any, definition: dict[str, Any], cloud: str = "the destination"
    ) -> list[str]:
        """The security groups step 8 asks for by name that are not exactly one group of the
        credential's project, found while the VM still runs (§7.3 step 0): the server's, which
        Nova looks up by name (``default`` aside), and each port's, which ``definition`` names in
        "neutron" mode only (step 2) and step 8 looks up for the port it creates. Step 8 runs after
        the source server was deleted."""
        askers: dict[str, list[str]] = {}
        for name in definition.get("security_groups") or []:
            if name != "default":
                askers.setdefault(name, []).append("the server")
        for n in definition["networks"]:
            for name in n.get("security_groups") or []:
                askers.setdefault(name, []).append(f"port {n.get('port_id')}")
        problems: list[str] = []
        for name, who in askers.items():
            found = list(dst.network.security_groups(name=name, project_id=dst.current_project_id))
            if len(found) != 1:
                many = f"{len(found)} security groups" if found else "no security group"
                problems.append(
                    f"{', '.join(who)}: {cloud} has {many} named {name!r} in the credential's "
                    "project"
                )
        return problems

    @staticmethod
    def _lookup_problems(
        dst: Any, definition: dict[str, Any], mappings: Mappings, cloud: str = "the destination"
    ) -> list[str]:
        """The flavor step 8 and the volume types step 6 would ask RHOSO for and it lacks, found
        while the VM still runs (§7.3 step 0): both steps run after the source server was deleted.
        Each is chosen as those steps choose it - the mapping, else the source's own name."""
        problems: list[str] = []
        source = definition["flavor"]
        flavor = mappings.flavors.get(source, source)
        if flavor and dst.compute.find_flavor(flavor, ignore_missing=True) is None:
            problems.append(
                f"the server's flavor {source!r}: {cloud} has no flavor named {flavor!r}"
            )
        known = {str(_attr(t, "name")) for t in dst.block_storage.types()}
        missing: dict[str, list[str]] = {}
        for att in definition["attachments"]:
            vtype = mappings.volume_types.get(att["volume_type"] or "", att["volume_type"])
            if vtype and vtype not in known:
                missing.setdefault(vtype, []).append(str(att["volume_id"]))
        for vtype, volumes in missing.items():
            which = f"volume {volumes[0]}" if len(volumes) == 1 else f"volumes {', '.join(volumes)}"
            problems.append(f"{which}: {cloud} has no volume type named {vtype!r}")
        return problems

    @staticmethod
    def _unmanage_blockers(src: Any, attachments: list[dict[str, Any]]) -> list[str]:
        """What would make step 3 or 5 fail, found while the VM still runs (§7.3 step 0).

        Cinder refuses to unmanage encrypted volumes, volumes with snapshots and volumes in a
        group (``volume.api.API.delete(unmanage_only=True)``, Wallaby and later); keeping a volume
        when its server is deleted needs compute microversion 2.85.
        """
        problems: list[str] = []
        version = _attr(src.compute.get_endpoint_data(), "max_microversion")
        if _microversion(version) < _microversion(KEEP_MICROVERSION):
            problems.append(
                "the source compute API supports microversion "
                f"{microversion_text(version) or 'unknown'}; keeping the volumes when the source "
                f"server is deleted needs {KEEP_MICROVERSION}"
            )
        for att in attachments:
            vid = att["volume_id"]
            volume = src.block_storage.get_volume(vid)
            if _attr(volume, "encryption_key_id") or _attr(volume, "is_encrypted") is True:
                problems.append(f"volume {vid} is encrypted: Cinder cannot unmanage it")
            if _attr(volume, "group_id") or _attr(volume, "consistency_group_id"):
                problems.append(f"volume {vid} belongs to a group: remove it from the group first")
            snapshots = len(
                list(src.block_storage.snapshots(details=False, all_projects=True, volume_id=vid))
            )
            if snapshots:
                problems.append(
                    f"volume {vid} has {snapshots} snapshot(s): Cinder cannot unmanage it "
                    "until they are deleted"
                )
        return problems

    def _set_delete_on_termination(
        self, conn: Any, server_id: str, volume_id: str, value: bool
    ) -> None:
        """PUT os-volume_attachments (microversion 2.85) and read it back (§7.3 step 3)."""
        url = f"/servers/{server_id}/os-volume_attachments/{volume_id}"
        _checked(
            conn.compute.put(
                url,
                json={"volumeAttachment": {"volumeId": volume_id, "delete_on_termination": value}},
                microversion=KEEP_MICROVERSION,
            ),
            f"Nova refused delete_on_termination={value} on {volume_id}",
        )
        shown = _checked(
            conn.compute.get(url, microversion=KEEP_MICROVERSION),
            f"reading the attachment of {volume_id}",
        ).json()["volumeAttachment"]
        if shown.get("delete_on_termination") is not value:
            raise PermanentStepError(
                f"Nova did not set delete_on_termination={value} on {volume_id} "
                f"(it reports {shown.get('delete_on_termination')!r})"
            )

    @staticmethod
    def _set_boot_properties(conn: Any, volume_id: str, metadata: dict[str, str]) -> None:
        _checked(
            conn.block_storage.post(
                f"/volumes/{volume_id}/action",
                json={"os-set_image_metadata": {"metadata": metadata}},
            ),
            f"Cinder refused the boot properties of {volume_id}",
        )

    def _unmanage(self, conn: Any, volume_id: str, journal: _Journal, sent: str) -> None:
        """``os-unmanage``, journaled as ``sent`` once Cinder accepts it (SDD §7.3): after a crash
        during the wait a resume waits again, or goes on when Cinder no longer knows the volume."""
        try:
            volume = conn.block_storage.get_volume(volume_id)
        except Exception as exc:
            if journal.done(sent) and _not_found(exc):
                return
            raise
        if not journal.done(sent):
            _checked(
                conn.block_storage.post(f"/volumes/{volume_id}/action", json={"os-unmanage": None}),
                f"Cinder refused to unmanage {volume_id}",
            )
            journal.mark(sent)
        conn.block_storage.wait_for_delete(volume, interval=self.poll_s, wait=self.wait_s)

    def _manage(
        self,
        conn: Any,
        host: str,
        ref: dict[str, str],
        name: str | None,
        volume_type: str | None,
        bootable: bool,
        journal: _Journal,
        sent: str,
    ) -> str:
        """``manage``, journaled as ``sent`` with the new volume id once Cinder accepts it (SDD
        §7.3): after a crash during the wait a resume waits for that volume instead of managing
        the object again, which the first manage renamed."""
        if journal.done(sent):
            volume_id = journal.get(sent)["volume_id"]
            conn.block_storage.wait_for_status(
                conn.block_storage.get_volume(volume_id),
                status="available",
                failures=["error", "error_managing"],
                interval=self.poll_s,
                wait=self.wait_s,
            )
            return str(volume_id)
        body: dict[str, Any] = {
            "host": host,
            "ref": ref,
            "name": name,
            "bootable": bootable,
        }
        if volume_type:
            body["volume_type"] = volume_type
        response = _checked(
            conn.block_storage.post("/manageable_volumes", json={"volume": body}),
            f"Cinder refused to manage {ref} on {host}",
        )
        volume_id = response.json()["volume"]["id"]
        journal.mark(sent, volume_id=str(volume_id))
        conn.block_storage.wait_for_status(
            conn.block_storage.get_volume(volume_id),
            status="available",
            failures=["error", "error_managing"],
            interval=self.poll_s,
            wait=self.wait_s,
        )
        return str(volume_id)

    @staticmethod
    def _bdm(
        attachments: list[dict[str, Any]], ids: dict[str, str], keep_original: bool = False
    ) -> list[dict[str, Any]]:
        """Block-device mapping in device order; the destination never deletes its volumes, a
        recreated source keeps the journaled delete_on_termination."""
        ordered = sorted(attachments, key=lambda a: (not a["boot"], a["device"] or ""))
        return [
            {
                "boot_index": 0 if a["boot"] else -1,
                "uuid": ids[a["volume_id"]],
                "source_type": "volume",
                "destination_type": "volume",
                "delete_on_termination": bool(a.get("delete_on_termination"))
                if keep_original
                else False,
            }
            for a in ordered
        ]

    @staticmethod
    def _source_ports(
        conn: Any, networks: list[dict[str, Any]], journal: _Journal
    ) -> list[dict[str, str]]:
        """The source server's NICs for its recreation (SDD §7.3 rollback): a port a user created
        survives the server's deletion unbound and is used again; a port Nova created is gone and
        is recreated with its MAC, every fixed IP, its security groups and its port-security flag
        (journaled, so a resume does not create it twice). A definition journaled without ports
        keeps its network and first address."""
        nics: list[dict[str, str]] = []
        for n in networks:
            if not n.get("port_id"):
                nic = {"uuid": n["network_id"]}
                if n.get("fixed_ip"):
                    nic["fixed_ip"] = n["fixed_ip"]
                nics.append(nic)
                continue
            key = f"rb:port:{n['port_id']}"
            if not journal.done(key):
                port = _existing_port(conn, n["port_id"])
                if port is not None and not _attr(port, "device_id"):
                    port_id = n["port_id"]
                else:
                    fixed_ips = [{k: v for k, v in ip.items() if v} for ip in n["fixed_ips"]]
                    # never an address the source's Neutron derives from the kept MAC itself:
                    # sent, it is refused or allocated twice (SLAAC, DHCPv6-stateless, §7.3)
                    subnets = list(conn.network.subnets(network_id=n["network_id"]))
                    attrs: dict[str, Any] = {
                        "network_id": n["network_id"],
                        "mac_address": n["mac_address"],
                        "fixed_ips": requested_ips(fixed_ips, subnets),
                    }
                    # a definition journaled before step 2 recorded them has none
                    if n.get("security_group_ids") is not None:
                        attrs["security_group_ids"] = n["security_group_ids"]
                    if n.get("port_security_enabled") is not None:
                        attrs["is_port_security_enabled"] = n["port_security_enabled"]
                    created = conn.network.create_port(**attrs)
                    port_id = str(_attr(created, "id"))
                journal.mark(key, port_id=port_id)
            nics.append({"port": journal.get(key)["port_id"]})
        return nics

    @staticmethod
    def _earlier_ports(
        conn: Any, n: dict[str, Any], network: Any, addresses: list[str]
    ) -> list[Any]:
        """The unbound port an earlier attempt of step 8 created for the journaled port ``n``, which
        step 8 uses again: its description, on the target network, in the credential's project,
        with the MAC and exactly the addresses. The admin credential sees every project's ports,
        and a cold or warm migration of the same VM may have left one (§7.3)."""
        description = port_description(n["port_id"])
        mac = str(n.get("mac_address") or "").lower()
        # not counting the addresses Neutron derives itself on SLAAC or DHCPv6-stateless subnets:
        # it adds them to every port it creates (§6.6)
        subnets = list(conn.network.subnets(network_id=_attr(network, "id")))
        wanted = sorted(
            ip["ip_address"]
            for ip in requested_ips([{"ip_address": a} for a in addresses], subnets)
        )
        return [
            p
            for p in conn.network.ports(description=description)
            if _attr(p, "description") == description
            and not _attr(p, "device_id")
            and _attr(p, "network_id") == _attr(network, "id")
            and str(_attr(p, "mac_address") or "").lower() == mac
            and sorted(
                ip.get("ip_address")
                for ip in requested_ips(list(_attr(p, "fixed_ips") or []), subnets)
            )
            == wanted
            and _attr(p, "project_id") == conn.current_project_id
        ]

    @staticmethod
    def _destination_nic(plan: Plan, vm_id: str, n: dict[str, Any]) -> tuple[str, list[str]]:
        """A journaled port's RHOSO network and addresses (SDD §7.3 step 8): its NIC override's
        network, else the mapped one; the override's addresses, else its own."""
        entry = plan.nic_override(vm_id, n.get("mac_address"))
        network = (entry.network if entry is not None else None) or plan.mappings.networks.get(
            n["network"], n["network"]
        )
        if entry is not None and entry.fixed_ips is not None:
            return network, list(entry.fixed_ips)
        own = [ip["ip_address"] for ip in n.get("fixed_ips") or []]
        # a definition journaled before every address was: its first one
        return network, own or ([n["fixed_ip"]] if n.get("fixed_ip") else [])

    @staticmethod
    def _destination_ports(
        conn: Any,
        definition: dict[str, Any],
        plan: Plan,
        vm_id: str,
        journal: _Journal,
        cloud: str = "the destination",
    ) -> list[dict[str, str]]:
        """Each port created first through Neutron on the mapped network with its MAC, every fixed
        IP, its security groups and its port-security flag (SDD §7.3 step 8), journaled: a resume
        boots on the journaled port, or on the one a create sent before a crash made - unbound,
        with its description, on the target network, in the credential's project, with the MAC and
        exactly the addresses. Nova applies a server's security groups only to ports it creates."""
        nics: list[dict[str, str]] = []
        for n in definition["networks"]:
            key = f"dst_port:{n['port_id']}"
            if not journal.done(key):
                description = port_description(n["port_id"])
                target, addresses = HandoverExecutor._destination_nic(plan, vm_id, n)
                network = _destination_network(conn, target, cloud)
                project_id = conn.current_project_id
                earlier = HandoverExecutor._earlier_ports(conn, n, network, addresses)
                if earlier:
                    port = earlier[0]
                else:
                    subnets = list(conn.network.subnets(network_id=_attr(network, "id")))
                    attrs: dict[str, Any] = {
                        "network_id": _attr(network, "id"),
                        "mac_address": n["mac_address"],
                        # by address: Neutron picks the destination subnet that holds it; never
                        # one Neutron derives from the MAC itself (SLAAC, DHCPv6-stateless, §6.6)
                        "fixed_ips": requested_ips([{"ip_address": a} for a in addresses], subnets),
                        "security_group_ids": [
                            _attr(
                                conn.network.find_security_group(
                                    name, ignore_missing=False, project_id=project_id
                                ),
                                "id",
                            )
                            for name in n.get("security_groups") or []
                        ],
                        "description": description,
                    }
                    if n.get("port_security_enabled") is not None:
                        attrs["is_port_security_enabled"] = n["port_security_enabled"]
                    port = conn.network.create_port(**attrs)
                journal.mark(key, port_id=str(_attr(port, "id")))
            nics.append({"port": journal.get(key)["port_id"]})
        return nics

    def _create_server(
        self,
        conn: Any,
        definition: dict[str, Any],
        flavor: str,
        networks: list[dict[str, Any]],
        bdm: list[dict[str, Any]],
        journal: _Journal,
        sent: str,
    ) -> str:
        """Create a server, journaled as ``sent`` with its id once Nova accepts it (SDD §7.3): after
        a crash or a timeout while Nova builds it a resume waits for that server instead of
        creating a second one on the same volumes; one Nova no longer knows is created again."""
        if journal.done(sent):
            server = _existing_server(conn, journal.get(sent)["server_id"])
            if server is not None:
                conn.compute.wait_for_server(
                    server,
                    status="ACTIVE",
                    failures=["ERROR"],
                    interval=self.poll_s,
                    wait=self.wait_s,
                )
                return str(_attr(server, "id"))
        flavor_obj = conn.compute.find_flavor(flavor, ignore_missing=False)
        params: dict[str, Any] = {
            "name": definition["name"],
            "flavor_id": _attr(flavor_obj, "id"),
            "networks": networks,
            "block_device_mapping": bdm,
            "metadata": definition.get("metadata") or {},
        }
        if definition.get("security_groups"):
            params["security_groups"] = [{"name": n} for n in definition["security_groups"]]
        server = conn.compute.create_server(**params)
        journal.mark(sent, server_id=str(_attr(server, "id")))
        conn.compute.wait_for_server(
            server, status="ACTIVE", failures=["ERROR"], interval=self.poll_s, wait=self.wait_s
        )
        return str(_attr(server, "id"))

    async def _cutover(self, ctx: StepContext) -> StepResult:
        rdir = self.run_dir(ctx)
        journal = await _in_thread(_Journal, rdir / JOURNAL_FILE)
        src = await self._conn(ctx.source)
        dst = await self._conn(ctx.destination)
        # the plan's mappings overlaid with the flavor preflight matched: what steps 6 and 8 apply
        # (SDD §7.2)
        mappings = effective_mappings(ctx.plan.mappings, ctx.migration.resolved_mappings)
        backend_map = ctx.plan.handover.backend_map
        server_id = ctx.migration.vm.source_id

        if not journal.data["order"]:
            # step 0: everything that can be checked while the VM still runs (§7.3, §7.3.1)
            report = await self.readiness(
                ctx.source, ctx.destination, server_id, backend_map, ctx.plan, mappings
            )
            if report["problems"]:
                raise PermanentStepError(
                    "storage handover refused: " + "; ".join(report["problems"])
                )

        try:
            if not journal.done("stop_source"):

                def stop() -> None:
                    server = src.compute.get_server(server_id)
                    if str(_attr(server, "status", "")).upper() != "SHUTOFF":
                        src.compute.stop_server(server)
                        # Nova accepted it: should the wait fail, a rollback starts the source
                        journal.mark("sent:stop_source")
                    src.compute.wait_for_server(
                        server, status="SHUTOFF", interval=self.poll_s, wait=self.wait_s
                    )

                await self._call("stop source server", stop)
                await _in_thread(journal.mark, "stop_source")
        except (Exception, asyncio.CancelledError):
            # SDD §7.3 step 1: the stop Nova accepted may have stopped the source - the clock opens
            # at its request, so the rollback starts the source again and a cancel is refused
            sent = journal.get("sent:stop_source")
            if sent:
                at = datetime.fromisoformat(sent["at"])
                await outlive_cancel(ctx.mark_downtime_start(at=at))
            raise
        await outlive_cancel(ctx.mark_downtime_start())

        definition_path = rdir / DEFINITION_FILE
        if not journal.done("save_definition"):
            mode = ctx.plan.port_creation_mode
            definition = await self._call(
                "record source definition",
                lambda: self._capture_definition(src, server_id, mode),
            )
            definition["storage"] = await _in_thread(
                self._resolve_storage,
                src,
                dst,
                definition["attachments"],
                backend_map,
                ctx.destination.label,
            )
            await _in_thread(_write_json, definition_path, definition)
            await _in_thread(journal.mark, "save_definition")
        definition = await _in_thread(_read_json, definition_path)
        attachments: list[dict[str, Any]] = definition["attachments"]

        # step 3: keep every volume when the server is deleted (runs journaled before this order
        # had detached and unmanaged volumes already: nothing to keep for those)
        for att in attachments:
            vid = att["volume_id"]
            if journal.done(f"detach:{vid}") or journal.done(f"unmanage_src:{vid}"):
                continue
            key = f"keep:{vid}"
            if not journal.done(key):
                await self._call(
                    f"keep {vid} when the source server is deleted",
                    lambda v=vid: self._set_delete_on_termination(src, server_id, v, False),
                )
                await _in_thread(journal.mark, key)

        # step 4: delete the source server; its volumes become available
        if not journal.done("delete_source"):

            def delete_source() -> None:
                server = _existing_server(src, server_id)
                if server is not None:
                    src.compute.delete_server(server, ignore_missing=True)
                    src.compute.wait_for_delete(server, interval=self.poll_s, wait=self.wait_s)
                for att in attachments:
                    if journal.done(f"unmanage_src:{att['volume_id']}"):
                        continue
                    src.block_storage.wait_for_status(
                        src.block_storage.get_volume(att["volume_id"]),
                        status="available",
                        failures=["error"],
                        interval=self.poll_s,
                        wait=self.wait_s,
                    )

            await self._call("delete the source server", delete_source)
            await _in_thread(journal.mark, "delete_source")

        # step 5: unmanage at the source (Cinder refuses attached volumes)
        for att in attachments:
            key = f"unmanage_src:{att['volume_id']}"
            if not journal.done(key):
                await self._call(
                    f"unmanage {att['volume_id']} at the source",
                    lambda vid=att["volume_id"]: self._unmanage(
                        src, vid, journal, f"sent:unmanage_src:{vid}"
                    ),
                )
                await _in_thread(journal.mark, key)

        storage: dict[str, dict[str, Any]] = definition.get("storage") or {}
        dest_ids: dict[str, str] = {}
        for att in attachments:
            key = f"manage_dst:{att['volume_id']}"
            if not journal.done(key):
                vtype = att["volume_type"] or ""
                ref = storage.get(att["volume_id"])
                if ref:
                    host = ref["dst_host"]
                    manage_ref = manage_reference(
                        ref["family"], split_host(host)[1], f"volume-{att['volume_id']}"
                    )
                else:  # a definition journaled before §7.3.1: RBD
                    host = backend_map[vtype]
                    manage_ref = {"source-name": f"volume-{att['volume_id']}"}
                dest_id = await self._call(
                    f"manage {att['volume_id']} in {ctx.destination.label}",
                    lambda a=att, t=vtype, h=host, r=manage_ref: self._manage(
                        dst,
                        h,
                        r,
                        a["name"],
                        mappings.volume_types.get(t, a["volume_type"]),
                        a["boot"],
                        journal,
                        f"sent:manage_dst:{a['volume_id']}",
                    ),
                )
                await _in_thread(journal.mark, key, dest_id=dest_id)
            dest_ids[att["volume_id"]] = journal.get(key)["dest_id"]

        for att in attachments:
            metadata = att.get("image_metadata") or {}
            key = f"meta_dst:{att['volume_id']}"
            if metadata and not journal.done(key):
                await self._call(
                    f"restore the boot properties of {att['volume_id']} in {ctx.destination.label}",
                    lambda v=dest_ids[att["volume_id"]], m=metadata: self._set_boot_properties(
                        dst, v, m
                    ),
                )
                await _in_thread(journal.mark, key)

        if not journal.done("create_server"):

            def create() -> str:
                # the mode the definition was recorded in (step 2), as a resume must keep it
                vm_id = ctx.migration.vm.source_id
                if definition.get("port_creation_mode") == "neutron":
                    networks = self._destination_ports(
                        dst, definition, ctx.plan, vm_id, journal, ctx.destination.label
                    )
                else:
                    networks = []
                    for n in definition["networks"]:
                        target, addresses = self._destination_nic(ctx.plan, vm_id, n)
                        network = _destination_network(dst, target, ctx.destination.label)
                        net = {"uuid": _attr(network, "id")}
                        # Nova keeps one address per port: its override's, else its first
                        if addresses:
                            net["fixed_ip"] = addresses[0]
                        networks.append(net)
                flavor = mappings.flavors.get(definition["flavor"], definition["flavor"])
                return self._create_server(
                    dst,
                    definition,
                    flavor,
                    networks,
                    self._bdm(attachments, dest_ids),
                    journal,
                    "sent:create_server",
                )

            new_id = await self._call(f"create the server in {ctx.destination.label}", create)
            await _in_thread(journal.mark, "create_server", server_id=new_id)
        return StepResult(
            destination_server_id=journal.get("create_server")["server_id"],
            details={"journal": list(journal.data["order"])},
        )

    # -- rollback --------------------------------------------------------------------------------
    async def _rollback(self, ctx: StepContext) -> StepResult:
        rdir = self.run_dir(ctx)
        journal = await _in_thread(_Journal, rdir / JOURNAL_FILE)
        src = await self._conn(ctx.source)
        server_id = ctx.migration.vm.source_id
        definition_path = rdir / DEFINITION_FILE
        if not journal.data["order"]:
            # a rollback that archived its journal before the migration recorded what it did - a
            # stop or a failed save in between - reports the VM it recreated again (audit EX-04)
            recreated = await _in_thread(self._archived_outcome, rdir, ctx.migration.vm)
            if recreated is not None:
                return StepResult(details={"source_running": True, "vm": recreated})
            return StepResult(details={"source_running": True, "note": "nothing to roll back"})
        if not await _in_thread(definition_path.exists):
            # stopped but nothing else happened yet: just start the source again
            if not journal.done("rb:start_source"):
                await self._call("start the source server", lambda: self._start(src, server_id))
                await _in_thread(journal.mark, "rb:start_source")
            await _in_thread(journal.archive, "rolled-back")
            return StepResult(details={"source_running": True})

        dst = await self._conn(ctx.destination)
        definition = await _in_thread(_read_json, definition_path)
        attachments: list[dict[str, Any]] = definition["attachments"]

        # a create only sent before a crash or a timeout left a server too (SDD §7.3)
        dest_created = journal.get("create_server") or journal.get("sent:create_server")
        if dest_created and not journal.done("rb:delete_destination"):
            dest_server = dest_created["server_id"]

            def delete_destination() -> None:
                server = _existing_server(dst, dest_server)
                if server is not None:
                    dst.compute.delete_server(server, ignore_missing=True)
                    dst.compute.wait_for_delete(server, interval=self.poll_s, wait=self.wait_s)

            await self._call(f"delete the server in {ctx.destination.label}", delete_destination)
            await _in_thread(journal.mark, "rb:delete_destination")

        if definition.get("port_creation_mode") == "neutron":
            # step 8's ports through Neutron: Nova keeps them when it deletes their server, and a
            # cutover can fail between them and the server (SDD §7.3 rollback)
            deleted_server = (dest_created or {}).get("server_id")
            for n in definition["networks"]:
                key = f"rb:delete_dst_port:{n['port_id']}"
                if journal.done(key):
                    continue
                journaled_id = journal.get(f"dst_port:{n['port_id']}").get("port_id")

                def delete_ports(n: dict[str, Any] = n, journaled: Any = journaled_id) -> None:
                    description = port_description(n["port_id"])
                    ports = list(dst.network.ports(description=description))
                    if journaled and journaled not in {_attr(p, "id") for p in ports}:
                        port = _existing_port(dst, journaled)
                        ports += [port] if port is not None else []
                    for port in ports:
                        ours = _attr(port, "id") == journaled or (
                            _attr(port, "description") == description
                        )
                        bound = _attr(port, "device_id")
                        if ours and (not bound or bound == deleted_server):
                            dst.network.delete_port(port, ignore_missing=True)

                await self._call(
                    f"delete the port made for {n['port_id']} in {ctx.destination.label}",
                    delete_ports,
                )
                await _in_thread(journal.mark, key)

        def managed_at_rhoso(vid: str) -> str | None:
            """The RHOSO volume a manage created, confirmed or only sent before a crash (§7.3)."""
            if journal.done(f"manage_dst:{vid}"):
                return str(journal.get(f"manage_dst:{vid}")["dest_id"])
            if journal.done(f"sent:manage_dst:{vid}"):
                return str(journal.get(f"sent:manage_dst:{vid}")["volume_id"])
            return None

        for att in reversed(attachments):
            dest_id = managed_at_rhoso(att["volume_id"])
            key = f"rb:unmanage_dst:{att['volume_id']}"
            if dest_id and not journal.done(key):
                await self._call(
                    f"unmanage {dest_id} in {ctx.destination.label}",
                    lambda v=dest_id, k=key: self._unmanage(dst, v, journal, f"sent:{k}"),
                )
                await _in_thread(journal.mark, key)

        new_ids: dict[str, str] = {}
        for att in reversed(attachments):
            vid = att["volume_id"]
            if not journal.done(f"unmanage_src:{vid}"):
                if not journal.done(f"sent:unmanage_src:{vid}"):
                    new_ids[vid] = vid  # never unmanaged: still the source's volume
                    continue
                # sent before a crash: let Cinder finish it, then manage back as any other
                await self._call(
                    f"finish the unmanage of {vid} at the source",
                    lambda v=vid: self._unmanage(src, v, journal, f"sent:unmanage_src:{v}"),
                )
                await _in_thread(journal.mark, f"unmanage_src:{vid}")
            key = f"rb:manage_src:{vid}"
            if not journal.done(key):
                dest_id = managed_at_rhoso(vid)
                source_name = f"volume-{dest_id}" if dest_id else f"volume-{vid}"
                ref = (definition.get("storage") or {}).get(vid)
                back = (
                    manage_reference(ref["family"], ref["src_pool"], source_name)
                    if ref
                    else {"source-name": source_name}
                )
                new_id = await self._call(
                    f"manage {source_name} back at the source",
                    lambda a=att, r=back, k=key: self._manage(
                        src,
                        a["host"],
                        r,
                        a["name"],
                        a["volume_type"],
                        a["boot"],
                        journal,
                        f"sent:{k}",
                    ),
                )
                await _in_thread(journal.mark, key, volume_id=new_id)
            new_ids[vid] = journal.get(key)["volume_id"]
            metadata = att.get("image_metadata") or {}
            meta_key = f"rb:meta_src:{vid}"
            if metadata and not journal.done(meta_key):
                await self._call(
                    f"restore the boot properties of {new_ids[vid]} at the source",
                    lambda v=new_ids[vid], m=metadata: self._set_boot_properties(src, v, m),
                )
                await _in_thread(journal.mark, meta_key)

        any_unmanaged = any(journal.done(f"unmanage_src:{a['volume_id']}") for a in attachments)
        source_gone = journal.done("delete_source") or (
            await _in_thread(_existing_server, src, server_id) is None
        )
        new_server_id = server_id
        if any_unmanaged or source_gone:
            if not journal.done("delete_source") and not journal.done("rb:delete_stale_source"):

                def delete_stale() -> None:
                    server = _existing_server(src, server_id)
                    if server is not None:
                        src.compute.delete_server(server, ignore_missing=True)
                        src.compute.wait_for_delete(server, interval=self.poll_s, wait=self.wait_s)

                await self._call("delete the stale source server", delete_stale)
                await _in_thread(journal.mark, "rb:delete_stale_source")
            if not journal.done("rb:create_source"):
                networks = await self._call(
                    "recreate the source ports",
                    lambda: self._source_ports(src, definition["networks"], journal),
                )
                created = await self._call(
                    "recreate the source server",
                    lambda: self._create_server(
                        src,
                        definition,
                        definition["flavor"],
                        networks,
                        self._bdm(attachments, new_ids, keep_original=True),
                        journal,
                        "sent:rb:create_source",
                    ),
                )
                await _in_thread(journal.mark, "rb:create_source", server_id=created)
            new_server_id = journal.get("rb:create_source")["server_id"]
        else:
            # the server still exists: give back the delete_on_termination step 3 changed
            for att in attachments:
                vid = att["volume_id"]
                key = f"rb:dot:{vid}"
                if (
                    journal.done(f"keep:{vid}")
                    and att.get("delete_on_termination") is True
                    and not journal.done(key)
                ):
                    await self._call(
                        f"restore delete_on_termination of {vid}",
                        lambda v=vid: self._set_delete_on_termination(src, server_id, v, True),
                    )
                    await _in_thread(journal.mark, key)
            for att in attachments:
                key = f"rb:attach:{att['volume_id']}"
                if journal.done(f"detach:{att['volume_id']}") and not journal.done(key):
                    await self._call(
                        f"reattach {att['volume_id']}",
                        lambda a=att: src.compute.create_volume_attachment(
                            server_id, volume_id=a["volume_id"], device=a["device"]
                        ),
                    )
                    await _in_thread(journal.mark, key)
            if not journal.done("rb:start_source"):
                await self._call("start the source server", lambda: self._start(src, server_id))
                await _in_thread(journal.mark, "rb:start_source")

        details: dict[str, Any] = {"source_running": True}
        if new_server_id != server_id or any(k != v for k, v in new_ids.items()):
            details["vm"] = _recreated_vm(ctx.migration.vm, new_server_id, new_ids)
        await _in_thread(journal.archive, "rolled-back")
        return StepResult(details=details)

    @staticmethod
    def _archived_outcome(rdir: Path, vm: VMRef) -> dict[str, Any] | None:
        """The VM the newest archived rollback recreated, while the migration still names the
        server that rollback replaced (the one its definition recorded, SDD §7.3); None once the
        migration follows it, or when no rollback recreated one."""
        definition_path = rdir / DEFINITION_FILE
        archives = sorted(rdir.glob("handover-journal.rolled-back-*.json"))
        if not archives or not definition_path.exists():
            return None
        definition = _read_json(definition_path)
        done = _read_json(archives[-1]).get("done") or {}
        created = done.get("rb:create_source")
        if definition.get("server_id") != vm.source_id or not created:
            return None
        new_ids = {
            a["volume_id"]: str(done.get(f"rb:manage_src:{a['volume_id']}", {}).get("volume_id"))
            for a in definition["attachments"]
            if f"rb:manage_src:{a['volume_id']}" in done
        }
        return _recreated_vm(vm, str(created["server_id"]), new_ids)

    def _start(self, conn: Any, server_id: str) -> None:
        server = conn.compute.get_server(server_id)
        if str(_attr(server, "status", "")).upper() != "ACTIVE":
            conn.compute.start_server(server)
        conn.compute.wait_for_server(
            server, status="ACTIVE", interval=self.poll_s, wait=self.wait_s
        )
