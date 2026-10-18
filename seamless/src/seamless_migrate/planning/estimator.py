"""Downtime / duration estimator (SDD §9.1) and its per-migration calibration."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

from ..domain.enums import Strategy, SyncPassKind
from ..domain.models import GIB, Estimate, Plan, SyncPass, VMRef


@dataclass(frozen=True)
class EstimatorParams:
    link_bps: float = 131072000.0  # 125 MiB/s
    scan_bps: float = 524288000.0  # 500 MiB/s local read+hash per disk stream
    change_rate_bps: float = 2097152.0  # 2 MiB/s default guest write rate
    shutdown_s: float = 60.0
    boot_s: float = 120.0
    create_s: float = 60.0
    snapshot_s: float = 30.0
    handover_per_volume_s: float = 20.0
    v2v_s: float = 300.0
    v2v_inplace_s: float = 120.0
    convergence_threshold_bytes: int = 1073741824
    max_passes: int = 5
    #: disks of one VM synced concurrently (the collection's DEFAULT_PARALLEL_DISKS)
    parallel_disks: int = 4
    #: aggregate storage-path ceiling A of a conversion host (None = S·P)
    max_aggregate_scan_bps: float | None = None


#: Fixed overhead of a VMware CBT delta pass (SDD §9.1: ``Tk = 10 + Δk/L``).
VMWARE_PASS_OVERHEAD_S = 10.0
_FIELD_TYPES = {f.name: f.type for f in fields(EstimatorParams)}
_INT_FIELDS = frozenset({"convergence_threshold_bytes", "max_passes", "parallel_disks"})
#: Plan fields that keep precedence over ``Plan.estimator_overrides``.
PLAN_OWNED_FIELDS = frozenset({"link_bps", "convergence_threshold_bytes", "max_passes"})


def unknown_estimator_overrides(overrides: Mapping[str, Any]) -> list[str]:
    """Keys of ``overrides`` that are not ``EstimatorParams`` fields (sorted)."""
    return sorted(k for k in overrides if k not in _FIELD_TYPES)


#: Override keys that belong to plan fields (rejected: set the plan field instead).
_PLAN_FIELD_FOR = {"convergence_threshold_bytes": "convergence_threshold_bytes",
                   "max_passes": "max_sync_passes"}  # fmt: skip


def invalid_estimator_overrides(overrides: Mapping[str, Any]) -> list[str]:
    """Problems with ``Plan.estimator_overrides`` (empty = valid), one message per key.

    Unknown keys, keys owned by plan fields (``link_bps`` is accepted but the plan's value
    keeps precedence, SDD §9.1) and values that are not finite positive numbers are rejected —
    NaN and infinity too, which ``value <= 0`` alone lets through; ``parallel_disks`` must be
    at least 1.
    """
    problems = []
    for key in sorted(overrides):
        value = overrides[key]
        if key not in _FIELD_TYPES:
            problems.append(f"{key}: unknown field")
        elif key in _PLAN_FIELD_FOR:
            problems.append(f"{key}: set the plan field {_PLAN_FIELD_FOR[key]} instead")
        elif (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            problems.append(f"{key}: must be a finite positive number")
        elif key == "parallel_disks" and int(value) < 1:
            problems.append(f"{key}: must be at least 1")
    return problems


def params_for_plan(plan: Plan, base: EstimatorParams | None = None) -> EstimatorParams:
    """Estimator parameters for ``plan``.

    ``plan.estimator_overrides`` override any ``EstimatorParams`` field (unknown keys are
    ignored here; the API rejects them); the plan's own ``link_bps``,
    ``convergence_threshold_bytes`` and ``max_sync_passes`` keep precedence.
    """
    params = base or EstimatorParams()
    updates: dict[str, Any] = {}
    for key, value in (plan.estimator_overrides or {}).items():
        if key not in _FIELD_TYPES or key in PLAN_OWNED_FIELDS:
            continue
        updates[key] = int(value) if key in _INT_FIELDS else float(value)
    updates.update(
        link_bps=float(plan.link_bps),
        convergence_threshold_bytes=int(plan.convergence_threshold_bytes),
        max_passes=int(plan.max_sync_passes),
    )
    return replace(params, **updates)


def scan_seconds(vm: VMRef, params: EstimatorParams) -> float:
    """Time to read and hash every disk once: ``max(Dmax/S, D/min(S·P, A))``."""
    if not vm.disks:
        return 0.0
    largest = max(d.size_gb for d in vm.disks) * GIB
    aggregate = params.scan_bps * max(1, int(params.parallel_disks))
    if params.max_aggregate_scan_bps is not None and params.max_aggregate_scan_bps > 0:
        aggregate = min(aggregate, float(params.max_aggregate_scan_bps))
    return max(largest / params.scan_bps, vm.disk_bytes / aggregate)


def _precopy_passes(
    first_pass_s: float,
    first_pass_bytes: float,
    next_pass_s: Callable[[float], float],
    disk_bytes: int,
    change_rate: float,
    params: EstimatorParams,
) -> tuple[list[float], float]:
    """Run the convergence loop; return pass durations and the final delta Δf (bytes).

    Pass ``k`` (k ≥ 2) transfers ``Δk = min(D, c·T(k−1))`` and runs while the previous pass
    carried more than the threshold (``Δ(k−1) > threshold``, with ``Δ1`` = the first pass's
    data) and ``k ≤ max_passes`` — the runtime convergence rule of SDD §5.3.
    """
    durations = [first_pass_s]
    previous = first_pass_bytes
    while previous > params.convergence_threshold_bytes and len(durations) < params.max_passes:
        delta = min(float(disk_bytes), change_rate * durations[-1])
        durations.append(next_pass_s(delta))
        previous = delta
    return durations, min(float(disk_bytes), change_rate * durations[-1])


def first_pass_seconds(vm: VMRef, strategy: Strategy, params: EstimatorParams) -> float:
    """The expected duration of a warm migration's first pass (SDD §9.1): it copies the used
    data - on OpenStack it also scans every disk once, fresh destination volumes are not read
    (--assume-zero)."""
    strategy = Strategy(strategy)
    if strategy == Strategy.warm:
        return params.snapshot_s + max(vm.used_bytes / params.link_bps, scan_seconds(vm, params))
    if strategy == Strategy.vmware_warm:
        return vm.used_bytes / params.link_bps
    raise ValueError(f"{strategy} has no pre-copy passes")


def estimate(vm: VMRef, strategy: Strategy, params: EstimatorParams, slo_s: float) -> Estimate:
    """Estimate pre-copy time, passes and downtime for ``vm`` under ``strategy``.

    The result is always ``eligible=True``; eligibility is decided by the selector.
    """
    strategy = Strategy(strategy)
    p = params
    disk = vm.disk_bytes
    used = vm.used_bytes
    rate = vm.change_rate_bps if vm.change_rate_bps is not None else p.change_rate_bps
    link = p.link_bps
    precopy_s = 0.0
    passes = 0
    final_delta = 0.0

    if strategy == Strategy.cold:
        copy_s, conversion_s = p.snapshot_s + used / link, 0.0
    elif strategy == Strategy.warm:
        scan = scan_seconds(vm, p)
        durations, final_delta = _precopy_passes(
            first_pass_seconds(vm, strategy, p),
            used,
            lambda d: p.snapshot_s + max(scan, d / link),
            disk,
            rate,
            p,
        )
        precopy_s, passes = sum(durations), len(durations)
        copy_s, conversion_s = p.snapshot_s + max(scan, final_delta / link), 0.0
    elif strategy == Strategy.storage_handover:
        copy_s, conversion_s = len(vm.disks) * p.handover_per_volume_s, 0.0
    elif strategy == Strategy.vmware_cold:
        copy_s, conversion_s = used / link, p.v2v_s
    elif strategy == Strategy.vmware_warm:
        durations, final_delta = _precopy_passes(
            first_pass_seconds(vm, strategy, p),
            used,
            lambda d: VMWARE_PASS_OVERHEAD_S + d / link,
            disk,
            rate,
            p,
        )
        precopy_s, passes = sum(durations), len(durations)
        copy_s, conversion_s = final_delta / link, p.v2v_inplace_s
    else:  # pragma: no cover - exhaustive over the enum
        raise ValueError(f"unknown strategy {strategy}")
    parts = _parts(p, copy_s, conversion_s)
    downtime = sum(parts.values())

    return Estimate(
        strategy=strategy,
        eligible=True,
        reasons=[],
        precopy_s=precopy_s,
        passes=passes,
        downtime_s=downtime,
        total_s=precopy_s + downtime,
        final_delta_bytes=int(final_delta),
        meets_slo=downtime <= slo_s,
        downtime_parts=parts,
    )


def _parts(p: EstimatorParams, copy_s: float, conversion_s: float) -> dict[str, float]:
    """The downtime's parts, shown by the Downtime panel (SDD §4, §9.1, ADV-16)."""
    return {
        "shutdown_s": p.shutdown_s,
        "copy_s": copy_s,
        "conversion_s": conversion_s,
        "create_s": p.create_s,
        "boot_s": p.boot_s,
    }


def final_downtime_parts(
    vm: VMRef, strategy: Strategy, last_pass_s: float, params: EstimatorParams
) -> dict[str, float]:
    """The parts of the downtime if cut over now, after a pass of ``last_pass_s`` (SDD §5.3)."""
    rate = vm.change_rate_bps if vm.change_rate_bps is not None else params.change_rate_bps
    delta = min(float(vm.disk_bytes), rate * max(0.0, last_pass_s))
    p = params
    if Strategy(strategy) == Strategy.vmware_warm:
        return _parts(p, delta / p.link_bps, p.v2v_inplace_s)
    return _parts(p, p.snapshot_s + max(scan_seconds(vm, p), delta / p.link_bps), 0.0)


def estimate_final_downtime(
    vm: VMRef, strategy: Strategy, last_pass_s: float, params: EstimatorParams
) -> float:
    """Downtime if cut over now, given the duration of the last completed pass (SDD §5.3)."""
    return sum(final_downtime_parts(vm, strategy, last_pass_s, params).values())


def estimate_forced_downtime(
    vm: VMRef, strategy: Strategy, last_pass_s: float | None, params: EstimatorParams
) -> float:
    """The downtime a Force cutover expects (SDD §5.4): after a pass as long as the last one that
    ended, or - before any pass ended (``None``) - after the running first pass."""
    if last_pass_s is None:
        last_pass_s = first_pass_seconds(vm, strategy, params)
    return estimate_final_downtime(vm, strategy, last_pass_s, params)


# -- calibration (SDD §9.1 "Configuration and calibration") ----------------------------------
def calibrated_change_rate(
    previous: SyncPass | None, current: SyncPass, time_scale: float = 1.0
) -> float | None:
    """Guest write rate measured by a delta pass: bytes changed between the two snapshots.

    ``time_scale`` converts wall-clock intervals to model time (demo mode compresses time).
    """
    if previous is None or current.bytes_changed < 0:
        return None
    interval = (current.started_at - previous.started_at).total_seconds() * time_scale
    if interval <= 0:
        return None
    return current.bytes_changed / interval


def observed_scan_rate(sync_pass: SyncPass, vm: VMRef, params: EstimatorParams) -> float | None:
    """Per-stream scan throughput of a pass: the ``S`` under which the scan model gives its
    duration, ``max(min(Dmax, B), B / min(P, V)) / duration_s`` with ``B = bytes_scanned`` - a
    pass lasts as long as its largest disk's scan or its streams' share (SDD §9.1, review PL-07).

    Only delta/final passes calibrate it: pass 1 is usually link-bound and would understate it.
    ``None`` too for a pass at the aggregate ceiling (``A`` set, ``B / A >= duration_s``): it lasts
    as long whatever ``S`` is, so it does not tell ``S``.
    """
    if sync_pass.kind == SyncPassKind.full:
        return None
    duration, scanned = sync_pass.duration_s, sync_pass.bytes_scanned
    if not duration or duration <= 0 or scanned <= 0:
        return None
    ceiling = params.max_aggregate_scan_bps
    if ceiling is not None and ceiling > 0 and scanned / ceiling >= duration:
        return None
    streams = max(1, min(int(params.parallel_disks), len(vm.disks) or 1))
    largest = max((d.size_gb for d in vm.disks), default=0) * GIB
    return max(min(largest, scanned), scanned / streams) / duration
