"""What the advisor sends, declared (SDD §13.4, ADV-06).

The strategy evidence and the workload description are rendered from field specs, so the advisor
sends nothing a spec does not declare, and ``data_sent()`` - ``GET /advisor/data-sent`` - lists the
same specs: the panel changes when a serializer does. Redaction stays where it was: the advisor
redacts the rendered text, and ``JevClient.call`` every argument (§13.4).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..domain.models import Estimate, Finding, Plan, VMRef

MIB = 2**20
GIB = 2**30
#: the last characters of the console the verification review may send (§14.2)
CONSOLE_EXCERPT_CHARS = 6000


@dataclass(frozen=True)
class StrategyInput:
    vm: VMRef
    estimates: Sequence[Estimate]
    plan: Plan
    findings: Sequence[Finding]


@dataclass(frozen=True)
class FieldSpec:
    """One field the advisor sends: its key and label for the panel, and how it is rendered."""

    key: str
    label: str
    render: Callable[[Any], str]


def _gib(n: int) -> str:
    return f"{n / GIB:.0f} GiB"


def _disks(c: StrategyInput) -> str:
    return "; ".join(
        f"{d.kind} {d.size_gb} GiB"
        + (f" type {d.volume_type}" if d.volume_type else "")
        + (" multi-attach" if d.multiattach else "")
        + (" encrypted" if d.encrypted else "")
        + (" independent" if d.independent else "")
        for d in c.vm.disks
    )


def _write_rate(c: StrategyInput) -> str:
    rate = c.vm.change_rate_bps
    return (
        f"{rate / MIB:.1f} MiB/s measured"
        if rate is not None
        else "unknown (default 2 MiB/s assumed)"
    )


def _estimates(c: StrategyInput) -> str:
    return "; ".join(
        f"{e.strategy}: downtime {e.downtime_s:.0f} s, pre-copy {e.precopy_s:.0f} s over "
        f"{e.passes} pass(es), "
        + ("eligible" if e.eligible else f"INELIGIBLE ({'; '.join(e.reasons)})")
        for e in c.estimates
    )


#: the jev_decide evidence of a strategy decision, in the order it is written (§14.2)
STRATEGY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "vm",
        "VM name, vCPU, RAM and number of disks",
        lambda c: (
            f"VM {c.vm.name}: {c.vm.vcpus} vCPU, {c.vm.ram_mb} MB RAM, {len(c.vm.disks)} disk(s) "
        ),
    ),
    FieldSpec(
        "disks",
        "Each disk: kind, size, volume type, multi-attach, encrypted, independent",
        lambda c: f"[{_disks(c)}], ",
    ),
    FieldSpec(
        "size",
        "Provisioned and used size",
        lambda c: f"provisioned {_gib(c.vm.disk_bytes)}, used {_gib(c.vm.used_bytes)}. ",
    ),
    FieldSpec(
        "write_rate",
        "Guest write rate, measured or assumed",
        lambda c: f"Guest write rate {_write_rate(c)}. ",
    ),
    FieldSpec(
        "slo", "The plan's downtime SLO", lambda c: f"Downtime SLO {c.plan.downtime_slo_s} s. "
    ),
    FieldSpec(
        "link", "The plan's link bandwidth", lambda c: f"Link {c.plan.link_bps / MIB:.0f} MiB/s. "
    ),
    FieldSpec(
        "findings",
        "Pre-flight finding codes and severities",
        lambda c: (
            f"Findings: {', '.join(f'{f.code} ({f.severity})' for f in c.findings) or 'none'}. "
        ),
    ),
    FieldSpec(
        "estimates",
        "Per strategy: downtime, pre-copy, passes and eligibility with its reasons",
        lambda c: f"Estimates: {_estimates(c)}.",
    ),
)

#: the other jev_decide arguments of a strategy decision ("evidence" renders STRATEGY_FIELDS)
STRATEGY_REQUEST_FIELDS: dict[str, str] = {
    "decision": "The question, naming the VM",
    "evidence": "",
    "priorities": "Minimize downtime within the plan's SLO; the simpler strategy on near ties",
    "candidates": "The eligible strategies with their descriptions",
    "requirements": "The two requirements every strategy must meet",
}

#: the jev_classify text of a VM (§14.2)
WORKLOAD_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "VM name", lambda vm: f"name={vm.name}"),
    FieldSpec("os", "Guest OS", lambda vm: f"os={vm.os_type or 'unknown'}"),
    FieldSpec(
        "tags",
        "Tags",
        lambda vm: f"tags={','.join(f'{k}:{v}' for k, v in sorted(vm.tags.items())) or 'none'}",
    ),
    FieldSpec(
        "disks",
        "Disk sizes",
        lambda vm: f"disks={'+'.join(f'{d.size_gb}GiB' for d in vm.disks) or 'none'}",
    ),
    FieldSpec(
        "flavor",
        "Flavor, or vCPU and RAM",
        lambda vm: f"flavor={vm.flavor or f'{vm.vcpus}vcpu/{vm.ram_mb}MB'}",
    ),
)

CONSOLE_LABEL = f"The last {CONSOLE_EXCERPT_CHARS:,} characters of the console, redacted"


def render_strategy(c: StrategyInput) -> str:
    return "".join(spec.render(c) for spec in STRATEGY_FIELDS)


def render_workload(vm: VMRef) -> str:
    return " ".join(spec.render(vm) for spec in WORKLOAD_FIELDS)


def console_allowed(settings: Settings) -> bool:
    """The console excerpt leaves only for an on-prem model, or when allowed (SDD §13.4)."""
    return settings.jev_deployment == "on_prem" or settings.console_screening_send_to_remote


def data_sent(settings: Settings) -> list[dict[str, Any]]:
    """What each decision type sends to Jev, from the specs above (``GET /advisor/data-sent``)."""
    on = settings.jev_mode != "off"
    console = on and console_allowed(settings)
    local = (
        None
        if console
        else "Jev is off: nothing is sent."
        if not on
        else "The console excerpt stays in this installation: the advisor is remote and "
        "SEAMLESS_CONSOLE_SCREENING_SEND_TO_REMOTE is off."
    )

    def fields(pairs: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"key": key, "label": label} for key, label in pairs]

    request = [(k, v) for k, v in STRATEGY_REQUEST_FIELDS.items() if k != "evidence"]
    return [
        {
            "decision_type": "migration_strategy",
            "tool": "jev_decide",
            "fields": fields(
                [request[0], *((s.key, s.label) for s in STRATEGY_FIELDS), *request[1:]]
            ),
            "sent": on,
            "note": None if on else "Jev is off: nothing is sent.",
        },
        {
            "decision_type": "workload_tier",
            "tool": "jev_classify",
            "fields": fields([(s.key, s.label) for s in WORKLOAD_FIELDS]),
            "sent": on,
            "note": None if on else "Jev is off: nothing is sent.",
        },
        {
            "decision_type": "verification_review",
            "tool": "jev_verify",
            "fields": fields(
                [
                    ("claims", "The two boot claims to verify"),
                    ("checks", "Each verification check: name, result and detail"),
                    *([("console", CONSOLE_LABEL)] if console else []),
                ]
            ),
            "sent": on,
            "note": None if console else local,
        },
        {
            "decision_type": "console_screening",
            "tool": "jev_screen",
            "fields": fields([("console", CONSOLE_LABEL)]),
            "sent": console,
            "note": local,
        },
    ]
