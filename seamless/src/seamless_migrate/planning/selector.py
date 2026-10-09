"""Strategy eligibility and deterministic selection (SDD §9.2)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..domain.enums import (
    SIMPLICITY_ORDER,
    ProviderKind,
    Severity,
    Strategy,
    strategies_for,
)
from ..domain.models import Estimate, Finding, Plan, VMRef
from ..storage import SUPPORTED, StorageError, resolve_destination, split_host

#: Two downtimes tie when they differ by less than 10 % (of the smaller one) or less than 60 s.
TIE_RELATIVE = 0.10
TIE_ABSOLUTE_S = 60.0


def simplicity(strategy: Strategy | str) -> int:
    return SIMPLICITY_ORDER.index(Strategy(strategy))


def _ids(disks: Iterable[Any]) -> str:
    return ", ".join(d.name or d.id for d in disks)


def eligibility(
    vm: VMRef,
    source_kind: ProviderKind | str,
    plan: Plan,
    src_caps: Mapping[str, Any],
    dst_caps: Mapping[str, Any],
    findings: Sequence[Finding] = (),
) -> dict[Strategy, list[str]]:
    """Map every strategy applicable to the source family to its ineligibility reasons.

    An empty list means eligible. ``src_caps``/``dst_caps`` are the providers' capabilities plus
    ``conversion_host`` (truthy when the provider has a conversion host configured).
    """
    kind = ProviderKind(source_kind)
    out: dict[Strategy, list[str]] = {s: [] for s in strategies_for(kind)}
    multiattach = [d for d in vm.disks if d.multiattach]
    in_error = vm.power_state in ("error", "transitioning")

    if kind == ProviderKind.vmware:
        if in_error:
            out[Strategy.vmware_cold].append(f"source VM is in {vm.power_state} state")
        if vm.cbt_enabled is not True:
            out[Strategy.vmware_warm].append("Changed Block Tracking (CBT) is not enabled")
        independent = [d for d in vm.disks if d.independent]
        if independent:
            out[Strategy.vmware_warm].append(f"independent disk(s): {_ids(independent)}")
    else:
        copy_reasons: list[str] = []
        if in_error:
            copy_reasons.append(f"source VM is in {vm.power_state} state")
        if not src_caps.get("conversion_host"):
            copy_reasons.append("no conversion host configured on the source provider")
        if not dst_caps.get("conversion_host"):
            copy_reasons.append("no conversion host configured on the destination provider")
        out[Strategy.cold].extend(copy_reasons)
        out[Strategy.warm].extend(copy_reasons)
        if multiattach:
            out[Strategy.warm].append(f"multi-attach disk(s): {_ids(multiattach)}")

        handover = out[Strategy.storage_handover]
        if not plan.handover.enabled:
            handover.append("storage handover is not enabled for this plan")
        not_volumes = [d for d in vm.disks if d.kind != "volume"]
        if not_volumes:
            handover.append(f"disk(s) not backed by Cinder volumes: {_ids(not_volumes)}")
        unmapped = sorted(
            {
                d.volume_type or "<default>"
                for d in vm.disks
                if d.kind == "volume" and (d.volume_type or "") not in plan.handover.backend_map
            }
        )
        if unmapped:
            handover.append(
                "volume type(s) without a RHOSO backend in handover.backend_map: "
                + ", ".join(unmapped)
            )
        if multiattach:
            handover.append(f"multi-attach disk(s): {_ids(multiattach)}")
        encrypted = [d for d in vm.disks if d.encrypted]
        if encrypted:
            handover.append(f"encrypted disk(s), which Cinder cannot unmanage: {_ids(encrypted)}")
        handover.extend(_storage_reasons(vm, plan, src_caps, dst_caps))
        if not src_caps.get("admin"):
            handover.append("admin rights are required on the source cloud")
        if not dst_caps.get("admin"):
            handover.append("admin rights are required on the destination cloud")

    for finding in findings:
        if finding.severity == Severity.blocker:
            for reasons in out.values():
                reasons.append(f"blocked by finding {finding.code}")
    return out


def _storage_reasons(
    vm: VMRef, plan: Plan, src_caps: Mapping[str, Any], dst_caps: Mapping[str, Any]
) -> list[str]:
    """Per-volume driver-family checks of a handover (SDD §7.3.1), where the pools are known.

    A disk whose pool the source does not list is left to the executor's check before the stop.
    """
    families = {
        str(b.get("pool")): str(b.get("family"))
        for b in src_caps.get("storage_backends") or []
        if isinstance(b, Mapping)
    }
    dst_backends = [b for b in dst_caps.get("storage_backends") or [] if isinstance(b, Mapping)]
    reasons = []
    for disk in vm.disks:
        if disk.kind != "volume" or not disk.pool or disk.pool not in families:
            continue
        target = plan.handover.backend_map.get(disk.volume_type or "")
        family = families[disk.pool]
        try:
            if family not in SUPPORTED:
                resolve_destination(family, None, target or "", [])
            elif target and dst_backends:
                resolve_destination(family, split_host(disk.pool)[1], target, dst_backends)
        except StorageError as exc:
            reasons.append(f"{disk.name or disk.id}: {exc}")
    return reasons


def tie_set(estimates: Sequence[Estimate]) -> list[Strategy]:
    """Eligible strategies whose downtime ties with the minimum, simplest first."""
    eligible = [e for e in estimates if e.eligible]
    if not eligible:
        return []
    best = min(e.downtime_s for e in eligible)
    ties = [
        e.strategy
        for e in eligible
        if (e.downtime_s - best) < TIE_RELATIVE * best or (e.downtime_s - best) < TIE_ABSOLUTE_S
    ]
    return sorted(ties, key=simplicity)


def advisor_scope(estimates: Sequence[Estimate]) -> list[Strategy]:
    """Strategies the advisor may choose between (SDD §9.2/§14.2); empty = no advice needed.

    All eligible strategies when none meets the SLO, else the tie set when it has 2+ members.
    """
    eligible = [e for e in estimates if e.eligible]
    if not eligible:
        return []
    if not any(e.meets_slo for e in eligible):
        return sorted((e.strategy for e in eligible), key=simplicity)
    ties = tie_set(eligible)
    return ties if len(ties) >= 2 else []


def select_strategy(vm: VMRef, estimates: Sequence[Estimate], plan: Plan) -> tuple[Strategy, str]:
    """Deterministically choose a strategy; return it with a human-readable reason."""
    by_strategy = {e.strategy: e for e in estimates}
    notes: list[str] = []

    def explain(reason: str) -> str:
        return "; ".join([*notes, reason]) if notes else reason

    def ineligible_note(label: str, strategy: Strategy) -> str:
        est = by_strategy.get(strategy)
        why = "; ".join(est.reasons) if est is not None else "not applicable to this source"
        return f"{label} {strategy} ignored (ineligible: {why or 'unknown'})"

    override = plan.strategy_overrides.get(vm.source_id)
    if override is not None:
        est = by_strategy.get(Strategy(override))
        if est is not None and est.eligible:
            return Strategy(override), f"override {override}"
        notes.append(ineligible_note("override", Strategy(override)))

    if plan.default_strategy != "auto":
        default = Strategy(plan.default_strategy)
        est = by_strategy.get(default)
        if est is not None and est.eligible:
            return default, explain(f"plan default strategy {default}")
        notes.append(ineligible_note("plan default", default))

    eligible = [e for e in estimates if e.eligible]
    if not eligible:
        fallback = min((e.strategy for e in estimates), key=simplicity, default=Strategy.cold)
        return fallback, explain("no eligible strategy; the migration is blocked")

    if plan.selection_policy == "simplest_meeting_slo":
        meeting = [e for e in eligible if e.meets_slo]
        if meeting:
            choice = min(meeting, key=lambda e: simplicity(e.strategy)).strategy
            return choice, explain(
                f"simplest strategy meeting the {plan.downtime_slo_s} s SLO: {choice}"
            )
        notes.append(f"no eligible strategy meets the {plan.downtime_slo_s} s SLO")

    ties = tie_set(eligible)
    choice = ties[0]
    est = by_strategy[choice]
    if len(ties) > 1:
        reason = (
            f"tie between {', '.join(ties)} (downtimes within 10 % or 60 s) broken by "
            f"simplicity: {choice}"
        )
    else:
        reason = f"minimal downtime: {choice} ({est.downtime_s:.0f} s)"
    if not any(e.meets_slo for e in eligible) and not notes:
        notes.append(f"no eligible strategy meets the {plan.downtime_slo_s} s SLO")
    return choice, explain(reason)
