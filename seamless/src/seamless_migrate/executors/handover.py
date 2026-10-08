"""Storage-handover executor for clouds sharing a Ceph cluster (SDD §7.3).

Cutover per VM (data volumes first, then the boot volume): stop the source, record its
definition and attachment order, detach data volumes, ``os-unmanage`` every volume at the source,
delete the source server, ``manage`` the RBD images in RHOSO and boot the destination server from
them. Every completed sub-step is journaled in ``handover-journal.json`` so a crash resumes
without repeating work; rollback reverses the journal (6 → 3) and recreates the source server.
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
from .ansible import classify_failure, run_dir
from .base import PermanentStepError, StepContext, StepName, StepResult

log = logging.getLogger(__name__)
T = TypeVar("T")

JOURNAL_FILE = "handover-journal.json"
DEFINITION_FILE = "source-server.json"
ROOT_DEVICES = ("/dev/vda", "/dev/sda", "/dev/xvda")


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
            attachments.append(
                {
                    "volume_id": _attr(volume, "id"),
                    "device": device,
                    "boot": device in ROOT_DEVICES,
                    "name": _attr(volume, "name"),
                    "size": _attr(volume, "size"),
                    "volume_type": _attr(volume, "volume_type"),
                    "host": _attr(volume, "host"),
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

    def _unmanage(self, conn: Any, volume_id: str) -> None:
        volume = conn.block_storage.get_volume(volume_id)
        conn.block_storage.post(f"/volumes/{volume_id}/action", json={"os-unmanage": None})
        conn.block_storage.wait_for_delete(volume, interval=self.poll_s, wait=self.wait_s)

    def _manage(
        self,
        conn: Any,
        host: str,
        source_name: str,
        name: str | None,
        volume_type: str | None,
        bootable: bool,
    ) -> str:
        body: dict[str, Any] = {
            "host": host,
            "ref": {"source-name": source_name},
            "name": name,
            "bootable": bootable,
        }
        if volume_type:
            body["volume_type"] = volume_type
        response = conn.block_storage.post("/manageable_volumes", json={"volume": body})
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
    def _bdm(attachments: list[dict[str, Any]], ids: dict[str, str]) -> list[dict[str, Any]]:
        ordered = sorted(attachments, key=lambda a: (not a["boot"], a["device"] or ""))
        return [
            {
                "boot_index": 0 if a["boot"] else -1,
                "uuid": ids[a["volume_id"]],
                "source_type": "volume",
                "destination_type": "volume",
                "delete_on_termination": False,
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
            missing = sorted(
                {
                    a["volume_type"] or "<default>"
                    for a in definition["attachments"]
                    if (a["volume_type"] or "") not in backend_map
                }
            )
            if missing:
                raise PermanentStepError(f"no handover backend for volume type(s): {missing}")
            await asyncio.to_thread(_write_json, definition_path, definition)
            await asyncio.to_thread(journal.mark, "save_definition")
        definition = await asyncio.to_thread(_read_json, definition_path)
        attachments: list[dict[str, Any]] = definition["attachments"]

        for att in attachments:
            key = f"detach:{att['volume_id']}"
            if att["boot"] or journal.done(key):
                continue

            def detach(vid: str = att["volume_id"]) -> None:
                server = src.compute.get_server(server_id)
                src.compute.delete_volume_attachment(server, vid, ignore_missing=True)
                src.block_storage.wait_for_status(
                    src.block_storage.get_volume(vid),
                    status="available",
                    interval=self.poll_s,
                    wait=self.wait_s,
                )

            await self._call(f"detach {att['volume_id']}", detach)
            await asyncio.to_thread(journal.mark, key)

        for att in attachments:
            key = f"unmanage_src:{att['volume_id']}"
            if not journal.done(key):
                await self._call(
                    f"unmanage {att['volume_id']} at the source",
                    lambda vid=att["volume_id"]: self._unmanage(src, vid),
                )
                await asyncio.to_thread(journal.mark, key)

        if not journal.done("delete_source"):

            def delete_source() -> None:
                server = src.compute.get_server(server_id)
                src.compute.delete_server(server, ignore_missing=True)
                src.compute.wait_for_delete(server, interval=self.poll_s, wait=self.wait_s)

            await self._call("delete the source server", delete_source)
            await asyncio.to_thread(journal.mark, "delete_source")

        dest_ids: dict[str, str] = {}
        for att in attachments:
            key = f"manage_dst:{att['volume_id']}"
            if not journal.done(key):
                vtype = att["volume_type"] or ""
                dest_id = await self._call(
                    f"manage {att['volume_id']} in RHOSO",
                    lambda a=att, t=vtype: self._manage(
                        dst,
                        backend_map[t],
                        f"volume-{a['volume_id']}",
                        a["name"],
                        mappings.volume_types.get(t, a["volume_type"]),
                        a["boot"],
                    ),
                )
                await asyncio.to_thread(journal.mark, key, dest_id=dest_id)
            dest_ids[att["volume_id"]] = journal.get(key)["dest_id"]

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
                new_id = await self._call(
                    f"manage {source_name} back at the source",
                    lambda a=att, s=source_name: self._manage(
                        src, a["host"], s, a["name"], a["volume_type"], a["boot"]
                    ),
                )
                await asyncio.to_thread(journal.mark, key, volume_id=new_id)
            new_ids[vid] = journal.get(key)["volume_id"]

        any_unmanaged = any(journal.done(f"unmanage_src:{a['volume_id']}") for a in attachments)
        new_server_id = server_id
        if any_unmanaged:
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
                        self._bdm(attachments, new_ids),
                    ),
                )
                await asyncio.to_thread(journal.mark, "rb:create_source", server_id=created)
            new_server_id = journal.get("rb:create_source")["server_id"]
        else:
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
