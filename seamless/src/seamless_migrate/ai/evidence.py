"""The evidence of a strategy recommendation (SDD §14.4, ADV-02).

Built by deterministic code from what validation already holds — the VM, its plan, the estimates
and the pre-flight findings, an earlier run — never asked of the model, and stored with the
recommendation so it reads as it was when recorded. Every effect is measured against one option.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..domain.enums import ProviderKind, Severity, Strategy
from ..domain.models import (
    AdvisorReason,
    Estimate,
    EvidenceEffect,
    EvidenceItem,
    EvidenceSource,
    EvidenceSourceKind,
    Finding,
    Migration,
    Plan,
    VMRef,
)
from ..planning.preflight import GPU_CODES
from .redaction import replace_unstorable

log = logging.getLogger(__name__)

#: as the dashboard names the options (lib/advisor.ts, ADV-03)
OPTION_LABELS: dict[str, str] = {
    "cold": "Cold migration",
    "warm": "Warm migration",
    "storage_handover": "Storage handover",
    "vmware_cold": "VMware cold migration",
    "vmware_warm": "VMware warm migration (CBT)",
}
PROVIDER_LABELS: dict[str, str] = {"openstack": "OpenStack", "vmware": "VMware vSphere"}
V2V_TEXT: dict[str, str] = {
    "supported": "supported",
    "tech_preview": "tech preview",
    "unverified": "unverified",
    "unsupported": "unsupported",
    "unknown": "unknown",
}
#: the options virt-v2v converts
CONVERSIONS = frozenset({"vmware_cold", "vmware_warm"})
#: findings that bear on an option; an info one reports something that resolved itself
BEARING = frozenset({Severity.blocker, Severity.warning})
#: longest reason text kept from Jev
REASON_CHARS = 2000

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _round(value: float) -> int:
    """Half up, as JavaScript's Math.round for the non-negative values here."""
    return int(value + 0.5)


def _fixed(value: float, decimals: int) -> str:
    """JavaScript's toFixed: half up on the exact binary value (812.5 -> "813", 1.005 -> "1.00")."""
    return str(Decimal(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))


def _scaled(value: float) -> str:
    return _fixed(value, 0) if value >= 100 else _fixed(value, 1)


def format_bytes(n: float) -> str:
    """IEC sizes as the dashboard writes them (lib/format.ts formatBytes)."""
    value = abs(float(n))
    sign = "-" if n < 0 else ""
    if value < 1024:
        return f"{sign}{_round(value)} B"
    unit = 0
    while value >= 1024 and unit < len(_BYTE_UNITS) - 1:
        value /= 1024
        unit += 1
    text = _scaled(value)
    if float(text) >= 1024 and unit < len(_BYTE_UNITS) - 1:
        value /= 1024
        unit += 1
        text = _scaled(value)
    return f"{sign}{text} {_BYTE_UNITS[unit]}"


def format_duration(seconds: float) -> str:
    """Durations as the dashboard writes them (lib/format.ts formatDuration)."""
    s = max(0.0, float(seconds))
    if s < 10:
        tenths = _round(s * 10) / 10
        if tenths < 10:
            return f"{_fixed(tenths, 0)} s" if tenths == int(tenths) else f"{_fixed(tenths, 1)} s"
    total = _round(s)
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    if total < 86_400:
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    return f"{total // 86_400}d {(total % 86_400) // 3600}h"


def option_label(option: str) -> str:
    return OPTION_LABELS.get(option, option)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def build_strategy_evidence(
    vm: VMRef,
    plan: Plan,
    provider_kind: ProviderKind | str,
    estimates: Sequence[Estimate],
    findings: Sequence[Finding],
    option: str,
    earlier: Migration | None,
    *,
    migration_id: str,
) -> list[EvidenceItem]:
    """The facts a strategy recommendation rests on, heaviest first, each measured against
    ``option`` (SDD §14.4). A fact that is unknown is left out."""
    kind = ProviderKind(provider_kind)
    items: list[EvidenceItem] = []

    def add(
        key: str,
        label: str,
        value: Any,
        display: str,
        effect: EvidenceEffect,
        weight: int,
        source: EvidenceSourceKind,
        ref_id: str,
    ) -> None:
        items.append(
            EvidenceItem(
                key=key,
                label=label,
                value=value,
                display=display,
                effect=effect,
                weight=weight,
                source=EvidenceSource(kind=source, ref_id=ref_id),
            )
        )

    found = next((e for e in estimates if str(e.strategy) == option), None)
    if found is not None:
        add(
            "downtime_estimate",
            "Estimated downtime",
            found.downtime_s,
            f"Estimated downtime {format_duration(found.downtime_s)}, "
            f"SLO {format_duration(plan.downtime_slo_s)}",
            "supports" if found.meets_slo else "opposes",
            100,
            "estimate",
            migration_id,
        )
    if kind == ProviderKind.vmware and vm.cbt_enabled is not None:
        if option == Strategy.vmware_warm:
            cbt_effect: EvidenceEffect = "supports" if vm.cbt_enabled else "opposes"
        else:
            cbt_effect = "context"
        add(
            "cbt",
            "Changed Block Tracking",
            vm.cbt_enabled,
            f"Changed Block Tracking is {'on' if vm.cbt_enabled else 'off'}",
            cbt_effect,
            90,
            "inventory",
            vm.source_id,
        )
    if earlier is not None and earlier.attempts > 0:
        n = earlier.attempts
        # the strategies those attempts used, as each retry recorded them - not the migration's
        # strategy now, which a later validation may have changed; data recorded before names none
        tried = list(dict.fromkeys(str(s) for s in earlier.attempt_strategies))
        used = f" with {', '.join(option_label(s) for s in tried)}" if tried else ""
        add(
            "earlier_runs",
            "Earlier attempts",
            n,
            f"{n} earlier {_plural(n, 'attempt', 'attempts')}{used}",
            "opposes" if option in tried else "context",
            85,
            "run",
            earlier.id,
        )
    strategy = Strategy(option)
    # nothing about GPUs reaches Jev in 0.3.0: the GPU codes stay out (SDD §14.4, §22.12)
    applying = [
        f
        for f in findings
        if f.severity in BEARING and f.applies_to(strategy) and f.code not in GPU_CODES
    ]
    unmapped = [f for f in applying if f.code.startswith("MAP_")]
    add(
        "mappings",
        "Mappings",
        len(unmapped),
        f"Not mapped: {', '.join(dict.fromkeys(f.code for f in unmapped))}"
        if unmapped
        else "Networks, flavors and volume types are mapped",
        "opposes" if unmapped else "supports",
        80,
        "preflight",
        migration_id,
    )
    others = [f for f in applying if not f.code.startswith("MAP_")]
    n = len(others)
    add(
        "findings",
        "Pre-flight findings",
        n,
        f"{n} {_plural(n, 'finding applies', 'findings apply')} to {option_label(option)}: "
        f"{', '.join(dict.fromkeys(f.code for f in others))}"
        if others
        else f"No blocker or warning applies to {option_label(option)}",
        "opposes" if others else "supports",
        75,
        "preflight",
        migration_id,
    )
    guest = vm.guest_os
    if guest.family != "unknown":
        if option in CONVERSIONS:
            os_effect: EvidenceEffect = (
                "supports"
                if guest.v2v == "supported"
                else "opposes"
                if guest.v2v in ("unsupported", "unverified")
                else "context"
            )
        else:
            os_effect = "context"
        add(
            "guest_os",
            "Guest OS",
            guest.label,
            f"{guest.label}; virt-v2v {V2V_TEXT[guest.v2v]}",
            os_effect,
            70,
            "inventory",
            vm.source_id,
        )
    add(
        "disk_total",
        "Disk size",
        vm.disk_bytes,
        f"{format_bytes(vm.disk_bytes)} provisioned, {format_bytes(vm.used_bytes)} used",
        "context",
        60,
        "inventory",
        vm.source_id,
    )
    disks = len(vm.disks)
    add(
        "disk_count",
        "Disks",
        disks,
        f"{disks} {_plural(disks, 'disk', 'disks')}",
        "context",
        50,
        "inventory",
        vm.source_id,
    )
    if vm.change_rate_bps is not None:
        add(
            "write_rate",
            "Guest write rate",
            vm.change_rate_bps,
            f"Guest write rate {format_bytes(vm.change_rate_bps)}/s, measured",
            "context",
            40,
            "inventory",
            vm.source_id,
        )
    window = plan.cutover_window
    add(
        "cutover_window",
        "Cutover window",
        f"{window.start.isoformat()}/{window.end.isoformat()}" if window else None,
        f"Cutover window: {window.start:%Y-%m-%d %H:%M} – {window.end:%Y-%m-%d %H:%M} UTC"
        if window
        else "Cutover window: any time",
        "context",
        30,
        "plan",
        plan.id,
    )
    add(
        "source_kind",
        "Source platform",
        kind.value,
        f"Source platform: {PROVIDER_LABELS.get(kind.value, kind.value)}",
        "context",
        20,
        "provider",
        plan.source_provider_id,
    )
    return sorted(items, key=lambda item: -item.weight)


def evidence_count(items: Iterable[EvidenceItem]) -> int:
    """What a policy's minimum counts: the items that support or oppose (SDD §14.4)."""
    return sum(1 for item in items if item.effect != "context")


def keep_reasons(
    raw: Iterable[Any], items: Sequence[EvidenceItem], *, recommendation_id: str
) -> list[AdvisorReason]:
    """Jev's reasons that name one of the evidence keys; one naming an unknown key is dropped and
    logged (SDD §14.4). Jev's ``jev_decide`` gives none today."""
    keys = {item.key for item in items}
    kept: list[AdvisorReason] = []
    for reason in raw:
        if not isinstance(reason, Mapping):
            continue
        key = reason.get("key", reason.get("evidence"))
        text = reason.get("text")
        if not isinstance(key, str) or not isinstance(text, str) or not text.strip():
            continue
        if key not in keys:
            log.warning(
                "advisor reason names an unknown evidence key %r: dropped (recommendation %s)",
                key,
                recommendation_id,
            )
            continue
        # outside text: a NUL or a lone surrogate, which the store refuses, written U+FFFD (§13.4)
        kept.append(AdvisorReason(key=key, text=replace_unstorable(text.strip())[:REASON_CHARS]))
    return kept
