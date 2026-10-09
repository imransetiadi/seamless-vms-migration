from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from seamless_migrate.domain.enums import Strategy
from seamless_migrate.domain.models import SyncPass
from seamless_migrate.planning.estimator import (
    EstimatorParams,
    calibrated_change_rate,
    estimate,
    estimate_final_downtime,
    invalid_estimator_overrides,
    observed_scan_rate,
    params_for_plan,
    scan_seconds,
    unknown_estimator_overrides,
)
from tests.factories import GIB, make_disk, make_plan, make_vm

MIB = 2**20
P = EstimatorParams()


def vm_with(size_gb: int, used_gb: float | None, rate_mib: float | None = None, disks: int = 1):
    return make_vm(
        disks=[
            make_disk(
                id=f"d{i}",
                size_gb=size_gb // disks,
                used_gb=None if used_gb is None else used_gb / disks,
                bootable=i == 0,
            )
            for i in range(disks)
        ],
        change_rate_bps=None if rate_mib is None else rate_mib * MIB,
    )


def test_cold_downtime_formula():
    vm = vm_with(200, 100.0)  # 100 GiB used, link 125 MiB/s
    est = estimate(vm, Strategy.cold, P, slo_s=300)
    assert est.downtime_s == pytest.approx(60 + 30 + 819.2 + 60 + 120)
    assert (est.precopy_s, est.passes, est.final_delta_bytes) == (0, 0, 0)
    assert est.total_s == pytest.approx(est.downtime_s)
    assert est.meets_slo is False and est.eligible is True and est.strategy == Strategy.cold


def test_warm_converges_and_counts_passes():
    vm = vm_with(100, 50.0, rate_mib=4)
    est = estimate(vm, Strategy.warm, P, slo_s=600)
    # scan = max(Dmax/S, D/(S·P)) = max(204.8, 51.2) = 204.8 s (one disk)
    # T1 = 30 + max(50 GiB / 125 MiB/s, scan) = 439.6 s; Δ1 = U = 50 GiB > 1 GiB -> pass 2
    # Δ2 = 4 MiB/s · 439.6 s = 1758.4 MiB, T2 = 30 + max(204.8, 14.07) = 234.8 -> Δ2 > 1 GiB
    # Δ3 = 4 MiB/s · 234.8 s = 939.2 MiB, T3 = 234.8 -> Δ3 <= 1 GiB: converged after 3 passes
    assert est.passes == 3
    assert est.precopy_s == pytest.approx(439.6 + 2 * 234.8)
    assert est.final_delta_bytes == int(939.2 * MIB)
    assert est.downtime_s == pytest.approx(60 + 30 + 204.8 + 60 + 120)
    assert est.total_s == pytest.approx(est.precopy_s + est.downtime_s)
    assert est.meets_slo is True

    # a guest writing faster than the scan floor allows never converges: capped at max_passes
    busy = estimate(vm_with(100, 50.0, rate_mib=10), Strategy.warm, P, slo_s=600)
    assert busy.passes == P.max_passes == 5  # total passes, including the first full pass
    assert busy.precopy_s == pytest.approx(439.6 + 4 * 234.8)
    assert busy.final_delta_bytes == int(10 * 234.8 * MIB)

    capped = estimate(
        vm_with(100, 50.0, rate_mib=10), Strategy.warm, EstimatorParams(max_passes=2), slo_s=600
    )
    assert capped.passes == 2
    single = estimate(
        vm_with(100, 50.0, rate_mib=10), Strategy.warm, EstimatorParams(max_passes=1), slo_s=600
    )
    assert single.passes == 1 and single.final_delta_bytes == int(10 * 439.6 * MIB)


def test_warm_default_change_rate_used_when_unknown():
    est = estimate(vm_with(100, 50.0), Strategy.warm, P, slo_s=600)
    # Δ1 = U > 1 GiB -> pass 2 (Δ2 = 2 MiB/s · 439.6 s = 879.2 MiB, T2 = 234.8 s) -> converged
    assert est.passes == 2 and est.final_delta_bytes == int(2 * 234.8 * MIB)


def test_warm_scan_floor_applies():
    vm = vm_with(1000, 100.0, rate_mib=0.1)
    est = estimate(vm, Strategy.warm, P, slo_s=300)
    scan = 1000 * GIB / P.scan_bps
    assert scan == pytest.approx(2048)
    # the final delta is tiny (Δf/L ~ 1.7 s) but the whole device must be scanned
    assert est.downtime_s == pytest.approx(60 + 30 + scan + 60 + 120)
    assert est.meets_slo is False


def test_warm_scan_uses_largest_disk_and_parallel_streams():
    four = make_vm(disks=[make_disk(id=f"d{i}", size_gb=100, used_gb=10.0) for i in range(4)])
    eight = make_vm(disks=[make_disk(id=f"d{i}", size_gb=50, used_gb=5.0) for i in range(8)])
    lopsided = make_vm(
        disks=[
            make_disk(id="big", size_gb=300, used_gb=10.0),
            make_disk(id="small", size_gb=20, used_gb=1.0),
        ]
    )
    assert scan_seconds(four, P) == pytest.approx(204.8)  # 4 disks in parallel: one disk's scan
    assert scan_seconds(eight, P) == pytest.approx(204.8)  # D/(S·P): parallelism capped at 4
    assert scan_seconds(lopsided, P) == pytest.approx(614.4)  # largest disk dominates
    assert scan_seconds(four, EstimatorParams(parallel_disks=1)) == pytest.approx(819.2)
    # an aggregate storage-path ceiling A caps the parallel scan: D / min(S·P, A)
    capped = EstimatorParams(max_aggregate_scan_bps=1190 * MIB)
    assert scan_seconds(four, capped) == pytest.approx(400 * 1024 / 1190)  # ~344 s
    assert scan_seconds(lopsided, capped) == pytest.approx(614.4)  # Dmax/S still dominates
    assert scan_seconds(four, EstimatorParams(max_aggregate_scan_bps=None)) == pytest.approx(204.8)
    est = estimate(four, Strategy.warm, P, slo_s=600)
    assert est.downtime_s == pytest.approx(60 + 30 + 204.8 + 60 + 120)


def test_sdd_worked_example():
    vm = vm_with(200, 120.0)  # one 200 GiB disk, 120 GiB used, defaults
    assert scan_seconds(vm, P) == pytest.approx(409.6)
    warm = estimate(vm, Strategy.warm, P, slo_s=600)
    cold = estimate(vm, Strategy.cold, P, slo_s=600)
    handover = estimate(vm, Strategy.storage_handover, P, slo_s=600)
    assert warm.downtime_s == pytest.approx(60 + 30 + 409.6 + 60 + 120)  # ~680 s
    assert cold.downtime_s == pytest.approx(270 + 120 * GIB / P.link_bps)  # ~1253 s
    assert handover.downtime_s == pytest.approx(260)
    # Performance.md §4.2: pass 1 moves U, pass 2 carries 2,026 MiB (> 1 GiB), pass 3 carries
    # 879 MiB and converges — three passes, 1,892 s; on 10 Gbit/s two passes, 879 s.
    assert warm.passes == 3
    assert warm.precopy_s == pytest.approx(1013.0 + 440.0 + 440.0, abs=1.5)
    fast = estimate(vm, Strategy.warm, replace(P, link_bps=1250 * MIB), slo_s=600)
    assert (fast.passes, fast.downtime_s) == (2, pytest.approx(warm.downtime_s))
    assert fast.precopy_s == pytest.approx(2 * 440.0, abs=1.5)


def test_handover_downtime_independent_of_size():
    small = estimate(vm_with(40, 10.0, disks=2), Strategy.storage_handover, P, slo_s=300)
    huge = estimate(vm_with(4000, 3000.0, disks=2), Strategy.storage_handover, P, slo_s=300)
    assert small.downtime_s == huge.downtime_s == pytest.approx(60 + 2 * 20 + 60 + 120)
    assert (huge.precopy_s, huge.passes, huge.final_delta_bytes) == (0, 0, 0)
    assert huge.meets_slo is True


def test_vmware_warm_uses_exact_delta():
    vm = vm_with(1000, 100.0, rate_mib=2)
    est = estimate(vm, Strategy.vmware_warm, P, slo_s=600)
    t1 = 100 * GIB / P.link_bps  # 819.2 s; Δ1 = U > 1 GiB -> pass 2
    d2 = 2 * MIB * t1  # 1638.4 MiB > 1 GiB -> pass 3
    t2 = 10 + d2 / P.link_bps
    d3 = 2 * MIB * t2  # ~46 MiB -> converged after pass 3
    t3 = 10 + d3 / P.link_bps
    df = 2 * MIB * t3
    assert est.passes == 3
    assert est.precopy_s == pytest.approx(t1 + t2 + t3)
    assert est.final_delta_bytes == int(df)
    # CBT knows the changed blocks: no device scan term (D/S would be 2048 s)
    assert est.downtime_s == pytest.approx(60 + df / P.link_bps + 120 + 60 + 120)


def test_vmware_cold_formula():
    est = estimate(vm_with(200, 100.0), Strategy.vmware_cold, P, slo_s=300)
    assert est.downtime_s == pytest.approx(60 + 819.2 + 300 + 60 + 120)
    assert est.passes == 0 and est.final_delta_bytes == 0


def test_params_for_plan_uses_plan_knobs():
    plan = make_plan(link_bps=1e9, convergence_threshold_bytes=5, max_sync_passes=3)
    params = params_for_plan(plan)
    assert (params.link_bps, params.convergence_threshold_bytes, params.max_passes) == (
        1e9,
        5,
        3,
    )
    assert params.parallel_disks == 4


def test_estimator_overrides_with_plan_precedence():
    plan = make_plan(
        link_bps=2e8,
        estimator_overrides={
            "scan_bps": 1e9,
            "parallel_disks": 2,
            "boot_s": 30,
            "link_bps": 1.0,
            "max_passes": 9,
            "max_aggregate_scan_bps": 1190 * MIB,
        },
    )
    params = params_for_plan(plan)
    assert params.scan_bps == 1e9 and params.parallel_disks == 2 and params.boot_s == 30
    assert params.max_aggregate_scan_bps == 1190 * MIB
    assert params_for_plan(make_plan()).max_aggregate_scan_bps is None
    assert isinstance(params.parallel_disks, int)
    assert params.link_bps == 2e8, "Plan.link_bps keeps precedence"
    assert params.max_passes == plan.max_sync_passes, "the runtime pass cap stays the plan's"
    assert unknown_estimator_overrides({"scan_bps": 1, "bogus": 2, "Boot_S": 3}) == [
        "Boot_S",
        "bogus",
    ]
    assert unknown_estimator_overrides({}) == []


def test_invalid_estimator_overrides():
    assert invalid_estimator_overrides({}) == []
    assert invalid_estimator_overrides({"scan_bps": 1e9, "parallel_disks": 2, "link_bps": 1}) == []
    problems = invalid_estimator_overrides(
        {"bogus": 1, "scan_bps": 0, "boot_s": -5, "max_passes": 9,
         "convergence_threshold_bytes": 1, "snapshot_s": "30", "parallel_disks": True}
    )  # fmt: skip
    assert [p.split(":")[0] for p in problems] == sorted(
        ["bogus", "scan_bps", "boot_s", "max_passes", "convergence_threshold_bytes",
         "snapshot_s", "parallel_disks"]
    )  # fmt: skip
    assert any("unknown" in p for p in problems if p.startswith("bogus"))
    assert any("max_sync_passes" in p for p in problems if p.startswith("max_passes"))
    assert any("positive" in p for p in problems if p.startswith("scan_bps"))
    with pytest.raises(ValueError):
        make_plan(link_bps=0)
    with pytest.raises(ValueError):
        make_plan(max_sync_passes=0)


def test_invalid_estimator_overrides_refuses_non_finite_values():
    """SDD §9.1: NaN <= 0 is false, so a positivity check alone lets NaN and infinity through."""
    problems = invalid_estimator_overrides(
        {"scan_bps": float("nan"), "boot_s": float("inf"), "snapshot_s": float("-inf")}
    )
    assert [p.split(":")[0] for p in problems] == ["boot_s", "scan_bps", "snapshot_s"]
    assert all("finite positive number" in p for p in problems)
    assert invalid_estimator_overrides({"scan_bps": 1e12}) == []


def test_estimate_final_downtime_uses_scan_term():
    four = make_vm(
        disks=[make_disk(id=f"d{i}", size_gb=100, used_gb=10.0) for i in range(4)],
        change_rate_bps=2 * MIB,
    )
    assert estimate_final_downtime(four, Strategy.warm, 100.0, P) == pytest.approx(
        60 + 30 + 204.8 + 60 + 120
    )
    cbt = estimate_final_downtime(four, Strategy.vmware_warm, 100.0, P)
    assert cbt == pytest.approx(60 + 200 * MIB / P.link_bps + 120 + 60 + 120)


def test_calibration_helpers():
    t0 = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    first = SyncPass(
        number=1,
        kind="full",
        started_at=t0,
        ended_at=t0 + timedelta(seconds=100),
        bytes_scanned=40 * GIB,
        bytes_changed=10 * GIB,
        duration_s=100.0,
    )
    second = SyncPass(
        number=2,
        kind="delta",
        started_at=t0 + timedelta(seconds=200),
        ended_at=t0 + timedelta(seconds=250),
        bytes_scanned=40 * GIB,
        bytes_changed=100 * MIB,
        duration_s=50.0,
    )
    assert calibrated_change_rate(None, first) is None  # full passes do not calibrate c
    assert calibrated_change_rate(first, second) == pytest.approx(100 * MIB / 200)
    assert calibrated_change_rate(first, second, time_scale=10) == pytest.approx(100 * MIB / 2000)
    two_disks = make_vm(disks=[make_disk(id="a", size_gb=20), make_disk(id="b", size_gb=20)])
    # per-stream rate: 40 GiB in 50 s over min(P, V) = 2 streams
    assert observed_scan_rate(second, two_disks, P) == pytest.approx(40 * GIB / 50 / 2)
    final = second.model_copy(update={"kind": "final"})
    assert observed_scan_rate(final, two_disks, P) == pytest.approx(40 * GIB / 50 / 2)
    # pass 1 is usually link-bound: it never calibrates the scan rate
    assert observed_scan_rate(first, two_disks, P) is None
    assert observed_scan_rate(second.model_copy(update={"duration_s": 0}), two_disks, P) is None
    empty = second.model_copy(update={"bytes_scanned": 0})
    assert observed_scan_rate(empty, two_disks, P) is None
