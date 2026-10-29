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
from .gpu import gpu_names, vm_gpus
from .platforms import parse_microversion
from .preflight import project_check, project_roles, project_words

#: the compute microversion the handover updates the source server's delete_on_termination with
#: (SDD §7.3, §21.4; executors.handover.KEEP_MICROVERSION): a source below it cannot hand over
HANDOVER_COMPUTE_FLOOR = "2.85"

#: Two downtimes tie when they differ by less than 10 % (of the smaller one) or less than 60 s.
TIE_RELATIVE = 0.10
TIE_ABSOLUTE_S = 60.0


def simplicity(strategy: Strategy | str) -> int:
    return SIMPLICITY_ORDER.index(Strategy(strategy))


def _ids(disks: Iterable[Any]) -> str:
    return ", ".join(d.name or d.id for d in disks)


#: why a migration that holds an earlier attempt's destination volumes takes no storage handover
#: (SDD §9.2, review 39 lead L1)
HELD_VOLUMES_REASON = (
    "an earlier attempt keeps its destination volumes for a retry, which a storage handover "
    "would leave behind: migrate it warm again, which uses them, or cold, whose cutover removes "
    "them first"
)


def eligibility(
    vm: VMRef,
    source_kind: ProviderKind | str,
    plan: Plan,
    src_caps: Mapping[str, Any],
    dst_caps: Mapping[str, Any],
    findings: Sequence[Finding] = (),
    destination: str = "the destination",
    *,
    holds_volumes: bool = False,
) -> dict[Strategy, list[str]]:
    """Map every strategy applicable to the source family to its ineligibility reasons.

    An empty list means eligible. ``src_caps``/``dst_caps`` are the providers' capabilities plus
    ``conversion_host`` (truthy when the provider has a conversion host configured); a reason
    about the destination cloud names its provider, ``destination`` (SDD §1 naming).
    ``holds_volumes``: the migration holds the destination volumes an earlier attempt created
    (SDD §9.3's rule), which a storage handover would leave behind (§9.2).
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
        # vSphere takes no snapshot of a VM with a DirectPath I/O device, and the kit reads every
        # copy from one; powered off, its snapshot holds no device state (SDD §9.2, §22.6)
        passthrough = [g for g in vm_gpus(vm) if g.kind == "passthrough"]
        if passthrough:
            devices = gpu_names(passthrough)
            out[Strategy.vmware_warm].append(
                f"PCI passthrough device(s) {devices}: vSphere takes no snapshot of a VM with "
                "one - power it off, validate it again and migrate it cold"
            )
            if vm.power_state != "stopped":
                out[Strategy.vmware_cold].append(
                    f"PCI passthrough device(s) {devices} on a VM that is not powered off: power "
                    "it off, validate it again and migrate it cold"
                )
    else:
        copy_reasons: list[str] = []
        if in_error:
            copy_reasons.append(f"source VM is in {vm.power_state} state")
        if not src_caps.get("conversion_host"):
            copy_reasons.append("no conversion host configured on the source provider")
        if not dst_caps.get("conversion_host"):
            copy_reasons.append(f"no conversion host configured on {destination}")
        # through Nova os-migrate recreates each port with exactly one fixed IP and raises for any
        # other count, after the source is stopped (SDD §9.2); a dual-stack port is two addresses.
        # Through Neutron every port keeps all its fixed IPs, none included (§6.6).
        # the addresses a NIC gets: its override's, else its own (SDD §9.2)
        nics = [(n.network, plan.nic_target(vm.source_id, n)[1]) for n in vm.nics]
        odd_ports = [(net, ips) for net, ips in nics if len(ips) != 1]
        if odd_ports and plan.port_creation_mode == "nova":
            copy_reasons.append(
                "port(s) without exactly one fixed IP, which Nova cannot recreate: "
                + _ports(odd_ports)
            )
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
                f"volume type(s) without a backend of {destination} in handover.backend_map: "
                + ", ".join(unmapped)
            )
        if multiattach:
            handover.append(f"multi-attach disk(s): {_ids(multiattach)}")
        # through Nova the destination server gets one fixed IP per port and the others would be
        # dropped; through Neutron every one is kept (SDD §7.3 step 8)
        several = [(net, ips) for net, ips in nics if len(ips) > 1]
        if several and plan.port_creation_mode == "nova":
            handover.append(
                f"port(s) with several fixed IPs, of which one would be kept: {_ports(several)}"
            )
        encrypted = [d for d in vm.disks if d.encrypted]
        if encrypted:
            handover.append(f"encrypted disk(s), which Cinder cannot unmanage: {_ids(encrypted)}")
        handover.extend(_storage_reasons(vm, plan, src_caps, dst_caps, destination))
        if not src_caps.get("admin"):
            handover.append("admin rights are required on the source cloud")
        handover.extend(_destination_admin(plan, dst_caps, destination))
        # a gap this strategy alone has is its eligibility reason, never a finding; the update goes
        # to the source server, the destination never receives it (SDD §9.2, §21.4, RV18-07)
        compute = parse_microversion(src_caps.get("compute_microversion"))
        if compute is not None and compute < parse_microversion(HANDOVER_COMPUTE_FLOOR):
            handover.append(
                f"the source's compute API {src_caps['compute_microversion']} is below "
                f"{HANDOVER_COMPUTE_FLOOR} (the handover's delete_on_termination update of the "
                "source server)"
            )
        if holds_volumes:
            # a handover never removes what an earlier attempt left at the destination (SDD §9.2,
            # review 39 lead L1)
            handover.append(HELD_VOLUMES_REASON)

    for finding in findings:
        if finding.severity == Severity.blocker:
            for reasons in out.values():
                reasons.append(f"blocked by finding {finding.code}")
    return out


def _destination_admin(plan: Plan, dst_caps: Mapping[str, Any], destination: str) -> list[str]:
    """Why the credential of the handover's destination half lacks admin (SDD §9.2, §7.3): the
    provider's own - its check's admin flag - for a plan without a destination project; for a plan
    with one, that project's credential, which manages the volumes and creates the server while the
    provider's own acts in no part of that half - admin among the roles its check read, roles not
    read counting as none (§27.5, §27.6, owner decision 8, plan task TC6)."""
    project = plan.destination_project
    if project is None:
        return [] if dst_caps.get("admin") else [f"admin rights are required on {destination}"]
    if "admin" in (project_roles(dst_caps, project) or []):
        return []
    named = project_words(project, project_check(dst_caps, project))
    return [
        f"admin rights are required on {destination} in the credential of project {named}, which "
        "manages the volumes and creates the server"
    ]


def _ports(nics: Sequence[tuple[str, list[str]]]) -> str:
    """Each NIC by its network and addresses, e.g. "app-net (10.0.0.5, fd00::5)"."""
    return ", ".join(f"{network} ({', '.join(ips) or 'no fixed IP'})" for network, ips in nics)


def _storage_reasons(
    vm: VMRef,
    plan: Plan,
    src_caps: Mapping[str, Any],
    dst_caps: Mapping[str, Any],
    destination: str = "the destination",
) -> list[str]:
    """Per-volume driver-family checks of a handover (SDD §7.3.1), where the pools are known;
    a reason about the destination cloud names its provider, ``destination`` (SDD §1 naming).

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
                resolve_destination(
                    family, split_host(disk.pool)[1], target, dst_backends, destination
                )
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


def advisor_scope(estimates: Sequence[Estimate], policy: str) -> list[Strategy]:
    """Strategies the advisor may choose between (SDD §9.2/§14.2); empty = no advice needed.

    All eligible strategies when none meets the SLO, else the ``min_downtime`` tie set when it has
    2+ members. Under ``simplest_meeting_slo`` the simplest strategy meeting the SLO - the rule the
    operator chose - has no tie, so the advisor keeps out.
    """
    eligible = [e for e in estimates if e.eligible]
    if not eligible:
        return []
    if not any(e.meets_slo for e in eligible):
        return sorted((e.strategy for e in eligible), key=simplicity)
    if policy == "simplest_meeting_slo":
        return []
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

    # an infinite downtime ties with none (inf - inf is NaN): the simplest eligible is taken
    # (SDD §9.2, review RV51-22)
    ties = tie_set(eligible) or sorted((e.strategy for e in eligible), key=simplicity)
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
