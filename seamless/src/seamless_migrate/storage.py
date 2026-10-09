"""Cinder driver families and the references a storage handover uses (SDD §7.3.1).

Pure functions: the handover executor (§7.3), the strategy selector (§9.2) and the OpenStack
provider (§10, ``storage_backends``) share them, so planning and execution agree on what a volume's
manage reference and destination pool are.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

StorageFamily = Literal["rbd", "netapp_nfs", "netapp_block", "other"]
SUPPORTED: frozenset[str] = frozenset({"rbd", "netapp_nfs", "netapp_block"})


class StorageError(ValueError):
    """A volume's storage reference cannot be resolved; the message says why."""


def storage_family(capabilities: Mapping[str, Any]) -> StorageFamily:
    """Driver family of a Cinder pool from its ``get_pools?detail=True`` capabilities."""
    vendor = str(capabilities.get("vendor_name") or "").lower()
    protocol = str(capabilities.get("storage_protocol") or "").lower()
    if protocol == "ceph":
        return "rbd"
    if "netapp" in vendor:
        if protocol == "nfs":
            return "netapp_nfs"
        if protocol in {"iscsi", "fc", "fibre_channel"}:
            return "netapp_block"
    return "other"


def split_host(host: str) -> tuple[str, str | None]:
    """``host@backend#pool`` → (``host@backend``, ``pool``); the pool may be absent."""
    backend, sep, pool = host.partition("#")
    return backend, (pool if sep and pool else None)


def manage_reference(family: str, pool: str | None, name: str) -> dict[str, str]:
    """The ``ref`` of ``POST /manageable_volumes`` for an object called ``name`` in ``pool``."""
    if family == "rbd":
        return {"source-name": name}
    if family == "netapp_nfs":
        if not pool:
            raise StorageError(f"an NFS share is needed to manage {name}")
        return {"source-name": f"{pool.rstrip('/')}/{name}"}
    if family == "netapp_block":
        if not pool:
            raise StorageError(f"a FlexVol is needed to manage {name}")
        return {"source-name": f"/vol/{pool}/{name}"}
    raise StorageError(f"unsupported storage family {family!r} for a handover")


def _export(share: str) -> str:
    """Export path of an NFS share ``address:/export`` (the address may differ per cloud)."""
    return share.partition(":")[2].rstrip("/") if ":" in share else share.rstrip("/")


def resolve_destination(
    family: str,
    source_pool: str | None,
    target: str,
    destination_backends: Iterable[Mapping[str, Any]],
) -> str:
    """Destination Cinder host (``host@backend#pool``) for a volume of ``family``.

    ``target`` is ``plan.handover.backend_map[type]``; ``destination_backends`` the destination's
    ``storage_backends`` capability (``pool`` and ``family`` per entry).
    """
    if family not in SUPPORTED:
        raise StorageError(
            f"unsupported storage family {family!r}: handover supports Ceph RBD and NetApp ONTAP "
            "(NFS, iSCSI, FC)"
        )
    backend, wanted = split_host(target)
    pools = [
        (str(b.get("pool")), str(b.get("family")))
        for b in destination_backends
        if split_host(str(b.get("pool")))[0] == backend
    ]
    if family == "rbd":
        if wanted:
            return target
        if len(pools) == 1:
            return pools[0][0]
        raise StorageError(f"{backend} has {len(pools)} pools: name the pool in backend_map")
    if not pools:
        raise StorageError(
            f"the destination lists no pools for {backend}: the NetApp pool cannot be checked"
        )
    families = sorted({f for _, f in pools})
    candidates = [p for p, f in pools if f == family]
    if not candidates:
        raise StorageError(
            f"{backend} is {', '.join(families)}, the source volume is {family}: "
            "map the volume type to a backend of the same driver family"
        )
    if family == "netapp_nfs":
        source_export = _export(source_pool or "")
        matches = [p for p in candidates if _export(split_host(p)[1] or "") == source_export]
        what = f"export {source_export}"
    else:
        matches = [p for p in candidates if split_host(p)[1] == source_pool]
        what = f"FlexVol {source_pool}"
    if not matches:
        raise StorageError(f"{backend} has no pool for the {what} (same SVM required)")
    chosen = matches[0]
    if wanted and split_host(chosen)[1] != wanted:
        raise StorageError(f"backend_map names {target}, but the {what} is {chosen}")
    return chosen
