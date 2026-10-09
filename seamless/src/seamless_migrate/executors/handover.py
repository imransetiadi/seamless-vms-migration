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
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ..config import Settings
from ..domain.enums import Strategy
from ..domain.models import Plan, Provider, utcnow
from ..storage import (
    StorageError,
    manage_reference,
    resolve_destination,
    split_host,
    storage_family,
)
from .ansible import classify_failure, run_dir
from .base import PermanentStepError, StepContext, StepName, StepResult

log = logging.getLogger(__name__)
T = TypeVar("T")

JOURNAL_FILE = "handover-journal.json"
DEFINITION_FILE = "source-server.json"
ROOT_DEVICES = ("/dev/vda", "/dev/sda", "/dev/xvda")
# PUT os-volume_attachments may change delete_on_termination from this compute microversion on
KEEP_MICROVERSION = "2.85"


def _checked(response: Any, what: str) -> Any:
    """Raise for an error answer: openstacksdk's raw Proxy calls (post/put/get) do not
    (``Proxy.request(raise_exc=False)``), so a refusal would otherwise pass silently."""
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        detail = str(getattr(response, "text", "") or "").strip().replace("\n", " ")[:300]
        raise RuntimeError(f"{what}: HTTP {status}: {detail}")
    return response


def _microversion(value: Any) -> tuple[int, int]:
    try:
        major, minor = str(value).split(".")[:2]
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


def _existing_server(conn: Any, server_id: str) -> Any:
    """The server, or ``None`` when it is already gone (HTTP 404)."""
    try:
        return conn.compute.get_server(server_id)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404 or "NotFound" in type(exc).__name__:
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
        self._conns: dict[str, Any] = {}
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
        if provider.id not in self._conns:
            self._conns[provider.id] = await asyncio.to_thread(self._conn_factory, provider)
        return self._conns[provider.id]

    async def _call(self, what: str, fn: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(fn)
        except (PermanentStepError, asyncio.CancelledError):
            raise
        except Exception as exc:
            message = f"{what}: {exc}"
            raise classify_failure(message)(message) from exc

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
    def _capture_definition(self, conn: Any, server_id: str) -> dict[str, Any]:
        server = conn.compute.get_server(server_id)
        flavor = _attr(server, "flavor") or {}
        networks = []
        for port in conn.network.ports(device_id=server_id):
            network = conn.network.get_network(_attr(port, "network_id"))
            for ip in _attr(port, "fixed_ips") or []:
                networks.append(
                    {
                        "network_id": _attr(port, "network_id"),
                        "network": _attr(network, "name"),
                        "fixed_ip": ip.get("ip_address"),
                    }
                )
                break
        attachments = []
        for att in conn.compute.volume_attachments(server):
            volume = conn.block_storage.get_volume(_attr(att, "volume_id"))
            device = _attr(att, "device")
            dot = _attr(att, "delete_on_termination")
            attachments.append(
                {
                    "volume_id": _attr(volume, "id"),
                    "device": device,
                    "boot": device in ROOT_DEVICES,
                    "name": _attr(volume, "name"),
                    "size": _attr(volume, "size"),
                    "volume_type": _attr(volume, "volume_type"),
                    "host": _attr(volume, "host"),
                    "image_metadata": boot_properties(_attr(volume, "volume_image_metadata")),
                    "delete_on_termination": None if dot is None else bool(dot),
                }
            )
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
        }

    @staticmethod
    def _pools(conn: Any, side: str) -> dict[str, dict[str, Any]]:
        try:
            pools = list(conn.block_storage.backend_pools())
        except Exception as exc:
            raise PermanentStepError(
                f"cannot read the Cinder pools of the {side} (scheduler-stats/get_pools, admin): "
                f"{exc}; the manage reference depends on the driver family (SDD §7.3.1)"
            ) from exc
        return {str(_attr(p, "name")): dict(_attr(p, "capabilities") or {}) for p in pools}

    def _resolve_storage(
        self, src: Any, dst: Any, attachments: list[dict[str, Any]], backend_map: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        """Family, source pool and destination host of every volume (§7.3 step 0, §7.3.1)."""
        missing = sorted(
            {
                a["volume_type"] or "<default>"
                for a in attachments
                if (a["volume_type"] or "") not in backend_map
            }
        )
        if missing:
            raise PermanentStepError(f"no handover backend for volume type(s): {missing}")
        src_pools = self._pools(src, "source")
        dst_backends = [
            {"pool": name, "family": storage_family(caps)}
            for name, caps in self._pools(dst, "destination").items()
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
                    family, src_pool, backend_map[att["volume_type"] or ""], dst_backends
                )
            except StorageError as exc:
                raise PermanentStepError(f"volume {vid}: {exc}") from None
            storage[vid] = {"family": family, "src_pool": src_pool, "dst_host": dst_host}
        return storage

    def _readiness(
        self, src: Any, dst: Any, server_id: str, backend_map: dict[str, str]
    ) -> dict[str, Any]:
        """Step 0 as a read-only report: what each volume would become, and every problem."""
        attachments = self._capture_definition(src, server_id)["attachments"]
        problems: list[str] = []
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
            src_pools = self._pools(src, "source")
            dst_backends = [
                {"pool": name, "family": storage_family(caps)}
                for name, caps in self._pools(dst, "destination").items()
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
                            family, split_host(host)[1], target, dst_backends
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
        self, source: Provider, destination: Provider, server_id: str, backend_map: dict[str, str]
    ) -> dict[str, Any]:
        """Read-only handover check of one server (SDD §7.3 step 0): per volume its driver
        family, source pool, destination host and manage reference, and every problem that would
        refuse the handover. Changes nothing; the lab runs it before a real cutover."""
        src = await self._conn(source)
        dst = await self._conn(destination)
        return await self._call(
            "check the handover readiness",
            lambda: self._readiness(src, dst, server_id, backend_map),
        )

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
                f"the source compute API supports microversion {version or 'unknown'}; keeping the "
                f"volumes when the source server is deleted needs {KEEP_MICROVERSION}"
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

    def _unmanage(self, conn: Any, volume_id: str) -> None:
        volume = conn.block_storage.get_volume(volume_id)
        _checked(
            conn.block_storage.post(f"/volumes/{volume_id}/action", json={"os-unmanage": None}),
            f"Cinder refused to unmanage {volume_id}",
        )
        conn.block_storage.wait_for_delete(volume, interval=self.poll_s, wait=self.wait_s)

    def _manage(
        self,
        conn: Any,
        host: str,
        ref: dict[str, str],
        name: str | None,
        volume_type: str | None,
        bootable: bool,
    ) -> str:
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

    def _create_server(
        self,
        conn: Any,
        definition: dict[str, Any],
        flavor: str,
        networks: list[dict[str, Any]],
        bdm: list[dict[str, Any]],
    ) -> str:
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
        conn.compute.wait_for_server(
            server, status="ACTIVE", failures=["ERROR"], interval=self.poll_s, wait=self.wait_s
        )
        return str(_attr(server, "id"))

    async def _cutover(self, ctx: StepContext) -> StepResult:
        rdir = self.run_dir(ctx)
        journal = await asyncio.to_thread(_Journal, rdir / JOURNAL_FILE)
        src = await self._conn(ctx.source)
        dst = await self._conn(ctx.destination)
        mappings = ctx.plan.mappings
        backend_map = ctx.plan.handover.backend_map
        server_id = ctx.migration.vm.source_id

        if not journal.data["order"]:
            # step 0: everything that can be checked while the VM still runs (§7.3, §7.3.1)
            report = await self.readiness(ctx.source, ctx.destination, server_id, backend_map)
            if report["problems"]:
                raise PermanentStepError(
                    "storage handover refused: " + "; ".join(report["problems"])
                )

        if not journal.done("stop_source"):

            def stop() -> None:
                server = src.compute.get_server(server_id)
                if str(_attr(server, "status", "")).upper() != "SHUTOFF":
                    src.compute.stop_server(server)
                src.compute.wait_for_server(
                    server, status="SHUTOFF", interval=self.poll_s, wait=self.wait_s
                )

            await self._call("stop source server", stop)
            await asyncio.to_thread(journal.mark, "stop_source")
        await ctx.mark_downtime_start()

        definition_path = rdir / DEFINITION_FILE
        if not journal.done("save_definition"):
            definition = await self._call(
                "record source definition", lambda: self._capture_definition(src, server_id)
            )
            definition["storage"] = await asyncio.to_thread(
                self._resolve_storage, src, dst, definition["attachments"], backend_map
            )
            await asyncio.to_thread(_write_json, definition_path, definition)
            await asyncio.to_thread(journal.mark, "save_definition")
        definition = await asyncio.to_thread(_read_json, definition_path)
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
                await asyncio.to_thread(journal.mark, key)

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
            await asyncio.to_thread(journal.mark, "delete_source")

        # step 5: unmanage at the source (Cinder refuses attached volumes)
        for att in attachments:
            key = f"unmanage_src:{att['volume_id']}"
            if not journal.done(key):
                await self._call(
                    f"unmanage {att['volume_id']} at the source",
                    lambda vid=att["volume_id"]: self._unmanage(src, vid),
                )
                await asyncio.to_thread(journal.mark, key)

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
                    f"manage {att['volume_id']} in RHOSO",
                    lambda a=att, t=vtype, h=host, r=manage_ref: self._manage(
                        dst,
                        h,
                        r,
                        a["name"],
                        mappings.volume_types.get(t, a["volume_type"]),
                        a["boot"],
                    ),
                )
                await asyncio.to_thread(journal.mark, key, dest_id=dest_id)
            dest_ids[att["volume_id"]] = journal.get(key)["dest_id"]

        for att in attachments:
            metadata = att.get("image_metadata") or {}
            key = f"meta_dst:{att['volume_id']}"
            if metadata and not journal.done(key):
                await self._call(
                    f"restore the boot properties of {att['volume_id']} in RHOSO",
                    lambda v=dest_ids[att["volume_id"]], m=metadata: self._set_boot_properties(
                        dst, v, m
                    ),
                )
                await asyncio.to_thread(journal.mark, key)

        if not journal.done("create_server"):

            def create() -> str:
                networks = [
                    {
                        "uuid": _attr(
                            dst.network.find_network(
                                mappings.networks.get(n["network"], n["network"]),
                                ignore_missing=False,
                            ),
                            "id",
                        ),
                        "fixed_ip": n["fixed_ip"],
                    }
                    for n in definition["networks"]
                ]
                flavor = mappings.flavors.get(definition["flavor"], definition["flavor"])
                return self._create_server(
                    dst, definition, flavor, networks, self._bdm(attachments, dest_ids)
                )

            new_id = await self._call("create the destination server", create)
            await asyncio.to_thread(journal.mark, "create_server", server_id=new_id)
        return StepResult(
            destination_server_id=journal.get("create_server")["server_id"],
            details={"journal": list(journal.data["order"])},
        )

    # -- rollback --------------------------------------------------------------------------------
    async def _rollback(self, ctx: StepContext) -> StepResult:
        rdir = self.run_dir(ctx)
        journal = await asyncio.to_thread(_Journal, rdir / JOURNAL_FILE)
        src = await self._conn(ctx.source)
        server_id = ctx.migration.vm.source_id
        definition_path = rdir / DEFINITION_FILE
        if not journal.data["order"]:
            return StepResult(details={"source_running": True, "note": "nothing to roll back"})
        if not await asyncio.to_thread(definition_path.exists):
            # stopped but nothing else happened yet: just start the source again
            if not journal.done("rb:start_source"):
                await self._call("start the source server", lambda: self._start(src, server_id))
                await asyncio.to_thread(journal.mark, "rb:start_source")
            await asyncio.to_thread(journal.archive, "rolled-back")
            return StepResult(details={"source_running": True})

        dst = await self._conn(ctx.destination)
        definition = await asyncio.to_thread(_read_json, definition_path)
        attachments: list[dict[str, Any]] = definition["attachments"]

        if journal.done("create_server") and not journal.done("rb:delete_destination"):
            dest_server = journal.get("create_server")["server_id"]

            def delete_destination() -> None:
                server = _existing_server(dst, dest_server)
                if server is not None:
                    dst.compute.delete_server(server, ignore_missing=True)
                    dst.compute.wait_for_delete(server, interval=self.poll_s, wait=self.wait_s)

            await self._call("delete the destination server", delete_destination)
            await asyncio.to_thread(journal.mark, "rb:delete_destination")

        for att in reversed(attachments):
            managed = journal.get(f"manage_dst:{att['volume_id']}")
            key = f"rb:unmanage_dst:{att['volume_id']}"
            if managed and not journal.done(key):
                await self._call(
                    f"unmanage {managed['dest_id']} in RHOSO",
                    lambda vid=managed["dest_id"]: self._unmanage(dst, vid),
                )
                await asyncio.to_thread(journal.mark, key)

        new_ids: dict[str, str] = {}
        for att in reversed(attachments):
            vid = att["volume_id"]
            if not journal.done(f"unmanage_src:{vid}"):
                new_ids[vid] = vid
                continue
            key = f"rb:manage_src:{vid}"
            if not journal.done(key):
                managed = journal.get(f"manage_dst:{vid}")
                source_name = f"volume-{managed['dest_id']}" if managed else f"volume-{vid}"
                ref = (definition.get("storage") or {}).get(vid)
                back = (
                    manage_reference(ref["family"], ref["src_pool"], source_name)
                    if ref
                    else {"source-name": source_name}
                )
                new_id = await self._call(
                    f"manage {source_name} back at the source",
                    lambda a=att, r=back: self._manage(
                        src, a["host"], r, a["name"], a["volume_type"], a["boot"]
                    ),
                )
                await asyncio.to_thread(journal.mark, key, volume_id=new_id)
            new_ids[vid] = journal.get(key)["volume_id"]
            metadata = att.get("image_metadata") or {}
            meta_key = f"rb:meta_src:{vid}"
            if metadata and not journal.done(meta_key):
                await self._call(
                    f"restore the boot properties of {new_ids[vid]} at the source",
                    lambda v=new_ids[vid], m=metadata: self._set_boot_properties(src, v, m),
                )
                await asyncio.to_thread(journal.mark, meta_key)

        any_unmanaged = any(journal.done(f"unmanage_src:{a['volume_id']}") for a in attachments)
        source_gone = journal.done("delete_source") or (
            await asyncio.to_thread(_existing_server, src, server_id) is None
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
                await asyncio.to_thread(journal.mark, "rb:delete_stale_source")
            if not journal.done("rb:create_source"):
                networks = [
                    {"uuid": n["network_id"], "fixed_ip": n["fixed_ip"]}
                    for n in definition["networks"]
                ]
                created = await self._call(
                    "recreate the source server",
                    lambda: self._create_server(
                        src,
                        definition,
                        definition["flavor"],
                        networks,
                        self._bdm(attachments, new_ids, keep_original=True),
                    ),
                )
                await asyncio.to_thread(journal.mark, "rb:create_source", server_id=created)
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
                    await asyncio.to_thread(journal.mark, key)
            for att in attachments:
                key = f"rb:attach:{att['volume_id']}"
                if journal.done(f"detach:{att['volume_id']}") and not journal.done(key):
                    await self._call(
                        f"reattach {att['volume_id']}",
                        lambda a=att: src.compute.create_volume_attachment(
                            server_id, volume_id=a["volume_id"], device=a["device"]
                        ),
                    )
                    await asyncio.to_thread(journal.mark, key)
            if not journal.done("rb:start_source"):
                await self._call("start the source server", lambda: self._start(src, server_id))
                await asyncio.to_thread(journal.mark, "rb:start_source")

        details: dict[str, Any] = {"source_running": True}
        if new_server_id != server_id or any(k != v for k, v in new_ids.items()):
            vm = ctx.migration.vm
            disks = [d.model_copy(update={"id": new_ids.get(d.id, d.id)}) for d in vm.disks]
            details["vm"] = vm.model_copy(
                update={"source_id": new_server_id, "disks": disks}
            ).model_dump(mode="json")
        await asyncio.to_thread(journal.archive, "rolled-back")
        return StepResult(details=details)

    def _start(self, conn: Any, server_id: str) -> None:
        server = conn.compute.get_server(server_id)
        if str(_attr(server, "status", "")).upper() != "ACTIVE":
            conn.compute.start_server(server)
        conn.compute.wait_for_server(
            server, status="ACTIVE", interval=self.poll_s, wait=self.wait_s
        )
