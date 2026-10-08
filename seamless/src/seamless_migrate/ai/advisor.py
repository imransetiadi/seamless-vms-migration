"""Bounded AI advisor (SDD §14.2).

The advisor never makes an ineligible strategy eligible, never skips approval and never triggers
rollback or finalize. It only (a) recommends a strategy inside the deterministic scope, (b)
classifies workloads into wave tiers with a heuristic fallback, and (c) may flag a passed
verification for human review.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from ..config import Settings
from ..domain.enums import Strategy
from ..domain.models import AdvisorNote, Estimate, Finding, Plan, VMRef
from ..planning.selector import advisor_scope, select_strategy
from ..planning.waves import TIERS, heuristic_tier
from .jev import JevClient, JevUnavailable
from .memory import redact

log = logging.getLogger(__name__)
GIB = 2**30
MIB = 2**20

STRATEGY_DESCRIPTIONS = {
    Strategy.cold: "Stop the VM, copy all used data over the network, boot on RHOSO.",
    Strategy.warm: "Pre-copy snapshots while the VM runs, then stop it and sync only changed "
    "blocks (the whole device is scanned).",
    Strategy.storage_handover: "Stop the VM and hand its Ceph-backed volumes from the source "
    "Cinder to RHOSO Cinder via unmanage/manage; no data is copied.",
    Strategy.vmware_cold: "Power off the VM, copy all used data with vmware-migration-kit and "
    "convert it with virt-v2v.",
    Strategy.vmware_warm: "Copy the VM with CBT-based passes while it runs, then power it off, "
    "copy the final changed blocks and convert in place.",
}
STRATEGY_REQUIREMENTS = [
    "Estimated downtime is within the SLO",
    "The source VM keeps running until cutover",
]
TIER_CLASSES = [
    {
        "id": "stateless_web",
        "description": "Stateless web or API front ends that can be rebuilt or scaled "
        "horizontally; no durable local data. Example: nginx, httpd, node front end.",
    },
    {
        "id": "middleware_queue",
        "description": "Message brokers, caches and application middleware holding transient "
        "or replicated state. Example: RabbitMQ, Kafka broker, Redis cache, Tomcat app tier.",
    },
    {
        "id": "infrastructure_service",
        "description": "Shared infrastructure many systems depend on: directory, DNS, DHCP, "
        "NTP, identity, monitoring. Example: Active Directory domain controller, FreeIPA, BIND.",
    },
    {
        "id": "stateful_database",
        "description": "Databases or storage services with durable local data where "
        "consistency matters. Example: PostgreSQL, MySQL, Oracle, MongoDB, Elasticsearch.",
    },
    {
        "id": "legacy_os",
        "description": "Any VM whose guest OS is end-of-life (RHEL/CentOS 6 or older, Windows "
        "Server 2008 or older), regardless of role; takes precedence over other classes.",
    },
    {
        "id": "manual_review",
        "description": "Not enough information to classify the VM confidently; a human must "
        "decide its wave.",
    },
]
CLASSIFY_PURPOSE = "Group VMs into migration waves by workload risk tier"
CLASSIFY_BATCH = 25
VERIFICATION_CLAIMS = [
    "The guest operating system finished booting",
    "No kernel panic or filesystem errors are reported",
]
SCREEN_PURPOSE = (
    "Interpret a guest console log to decide whether the VM booted successfully after migration"
)
INJECTION_REASON = "console output contained instructions aimed at an AI agent"
CONSOLE_EXCERPT_CHARS = 6000


def _gib(n: int) -> str:
    return f"{n / GIB:.0f} GiB"


def _check_state(check: dict[str, Any]) -> str:
    if check.get("skipped"):
        return "skipped"
    return "ok" if check.get("ok") else "FAILED"


class Advisor:
    def __init__(self, jev: JevClient | None, settings: Settings) -> None:
        self.jev = jev
        self.settings = settings

    @property
    def jev_enabled(self) -> bool:
        return self.jev is not None and self.jev.enabled

    def _redact(self, text: str, vm_names: Iterable[str] = ()) -> str:
        names = list(vm_names) if self.settings.memory_redact_names else None
        return redact(text, names)

    # -- strategy ----------------------------------------------------------------------------
    def _strategy_evidence(
        self, vm: VMRef, estimates: Sequence[Estimate], plan: Plan, findings: Sequence[Finding]
    ) -> str:
        disks = "; ".join(
            f"{d.kind} {d.size_gb} GiB"
            + (f" type {d.volume_type}" if d.volume_type else "")
            + (" multi-attach" if d.multiattach else "")
            + (" encrypted" if d.encrypted else "")
            + (" independent" if d.independent else "")
            for d in vm.disks
        )
        rate = (
            f"{vm.change_rate_bps / MIB:.1f} MiB/s measured"
            if vm.change_rate_bps is not None
            else "unknown (default 2 MiB/s assumed)"
        )
        est_text = "; ".join(
            f"{e.strategy}: downtime {e.downtime_s:.0f} s, pre-copy {e.precopy_s:.0f} s over "
            f"{e.passes} pass(es), "
            + ("eligible" if e.eligible else f"INELIGIBLE ({'; '.join(e.reasons)})")
            for e in estimates
        )
        finding_text = ", ".join(f"{f.code} ({f.severity})" for f in findings) or "none"
        text = (
            f"VM {vm.name}: {vm.vcpus} vCPU, {vm.ram_mb} MB RAM, {len(vm.disks)} disk(s) "
            f"[{disks}], provisioned {_gib(vm.disk_bytes)}, used {_gib(vm.used_bytes)}. "
            f"Guest write rate {rate}. Downtime SLO {plan.downtime_slo_s} s. "
            f"Link {plan.link_bps / MIB:.0f} MiB/s. Findings: {finding_text}. "
            f"Estimates: {est_text}."
        )
        return self._redact(text, [vm.name])

    async def recommend_strategy(
        self,
        vm: VMRef,
        estimates: Sequence[Estimate],
        plan: Plan,
        findings: Sequence[Finding] = (),
    ) -> AdvisorNote | None:
        """Ask Jev to break a tie (or pick when nothing meets the SLO); ``None`` when not needed.

        ``note.data["applied"]`` tells the caller whether to switch to ``note.data["selected"]``.
        """
        scope = advisor_scope(estimates)
        if not scope or not self.jev_enabled:
            return None
        deterministic, reason = select_strategy(vm, estimates, plan)
        eligible = [e for e in estimates if e.eligible]
        candidates = [
            {"id": e.strategy.value, "description": STRATEGY_DESCRIPTIONS[e.strategy]}
            for e in eligible
        ]
        base_data: dict[str, Any] = {
            "deterministic": deterministic.value,
            "deterministic_reason": reason,
            "scope": [s.value for s in scope],
            "candidates": [c["id"] for c in candidates],
        }
        assert self.jev is not None
        try:
            response = await self.jev.decide(
                decision=self._redact(
                    f"Which migration strategy should be used for VM {vm.name} moving to "
                    "RHOSO 18.0?",
                    [vm.name],
                ),
                evidence=self._strategy_evidence(vm, estimates, plan, findings),
                priorities=(
                    f"Minimize downtime within the SLO of {plan.downtime_slo_s} s; prefer the "
                    "simpler strategy when downtimes differ by less than 10 %; never pick an "
                    "ineligible strategy"
                ),
                candidates=candidates,
                requirements=list(STRATEGY_REQUIREMENTS),
                escalate_on_contradiction=True,
            )
        except JevUnavailable as exc:
            return AdvisorNote(
                kind="strategy",
                source="rules",
                summary=f"Jev unavailable ({exc}); kept the deterministic choice {deterministic}",
                data={**base_data, "selected": None, "applied": False},
            )

        rec = response.get("recommendation") or {}
        selected = rec.get("selected")
        confidence = rec.get("confidence")
        status = rec.get("status") or response.get("status")
        confident = isinstance(confidence, int | float) and float(confidence) >= (
            self.settings.jev_min_confidence
        )
        in_scope = isinstance(selected, str) and selected in base_data["scope"]
        applied = bool(
            in_scope
            and confident
            and not rec.get("escaped")
            and status not in ("escalate", "invalid_response")
        )
        if applied:
            summary = f"Jev recommends {selected} (confidence {confidence:.2f}); applied"
        elif selected is None or not in_scope:
            summary = (
                f"Jev answered {selected or status or 'no selection'}; kept the deterministic "
                f"choice {deterministic}"
            )
        else:
            summary = (
                f"Jev suggested {selected} (confidence {confidence}, status {status or 'ok'}); "
                f"below the bar, kept {deterministic}"
            )
        return AdvisorNote(
            kind="strategy",
            source="jev",
            summary=summary,
            confidence=float(confidence) if isinstance(confidence, int | float) else None,
            data={
                **base_data,
                "selected": selected,
                "applied": applied,
                "status": status,
                "escaped": bool(rec.get("escaped")),
                "probabilities": rec.get("probabilities") or {},
                "contradicted_requirements": rec.get("contradicted_requirements") or [],
                "warnings": response.get("warnings") or [],
            },
        )

    # -- classification ----------------------------------------------------------------------
    def _describe_vm(self, vm: VMRef) -> str:
        tags = ",".join(f"{k}:{v}" for k, v in sorted(vm.tags.items()))
        disks = "+".join(f"{d.size_gb}GiB" for d in vm.disks) or "none"
        text = (
            f"name={vm.name} os={vm.os_type or 'unknown'} tags={tags or 'none'} disks={disks} "
            f"flavor={vm.flavor or f'{vm.vcpus}vcpu/{vm.ram_mb}MB'}"
        )
        return self._redact(text, [vm.name])

    async def classify_workloads(self, vms: Sequence[VMRef]) -> tuple[dict[str, str], AdvisorNote]:
        tiers = {vm.source_id: heuristic_tier(vm) for vm in vms}
        if not self.jev_enabled or not vms:
            return tiers, AdvisorNote(
                kind="classification",
                source="rules",
                summary=f"Classified {len(vms)} VM(s) with the deterministic heuristic",
                data={"jev": 0, "fallback": list(tiers), "tiers": dict(tiers)},
            )
        assert self.jev is not None
        accepted: set[str] = set()
        errors: list[str] = []
        for start in range(0, len(vms), CLASSIFY_BATCH):
            batch = vms[start : start + CLASSIFY_BATCH]
            items = [{"id": vm.source_id, "text": self._describe_vm(vm)} for vm in batch]
            try:
                response = await self.jev.classify(
                    items=items, classes=TIER_CLASSES, purpose=CLASSIFY_PURPOSE
                )
            except JevUnavailable as exc:
                errors.append(str(exc))
                continue
            for result in response.get("results") or []:
                rid = result.get("id")
                tier = result.get("classification")
                if (
                    rid in tiers
                    and result.get("decision") == "auto"
                    and tier in TIERS
                    and result.get("status") != "invalid_response"
                ):
                    tiers[rid] = tier
                    accepted.add(rid)
        fallback = [vid for vid in tiers if vid not in accepted]
        source = "jev" if accepted else "rules"
        summary = f"Jev classified {len(accepted)} VM(s); {len(fallback)} used the heuristic" + (
            f" (Jev errors: {len(errors)})" if errors else ""
        )
        return tiers, AdvisorNote(
            kind="classification",
            source=source,
            summary=summary,
            data={
                "jev": len(accepted),
                "fallback": fallback,
                "tiers": dict(tiers),
                "errors": errors,
            },
        )

    # -- verification review -------------------------------------------------------------------
    async def review_verification(self, vm: VMRef, result: Any) -> AdvisorNote | None:
        """Screen the console excerpt and cross-check the boot claims; may flag for review.

        Returns a note whose ``data["review_required"]``/``data["review_reason"]`` the caller
        applies to the migration. ``result.passed`` is never modified.
        """
        if not self.jev_enabled:
            return None
        assert self.jev is not None
        checks = "; ".join(
            f"{c.get('name')}={_check_state(c)}"
            + (f" ({c.get('detail')})" if c.get("detail") else "")
            for c in result.checks
        )
        evidence = [{"id": "checks", "text": self._redact(checks, [vm.name])}]
        review_required = False
        review_reason: str | None = None
        screen: dict[str, Any] = {}
        console = (result.evidence or {}).get("console")
        if console:
            excerpt = str(console)[-CONSOLE_EXCERPT_CHARS:]
            try:
                screened = await self.jev.screen(text=excerpt, purpose=SCREEN_PURPOSE)
                rec = screened.get("recommendation") or {}
                screen = {
                    "action": rec.get("action"),
                    "reason": rec.get("reason"),
                    "injection": (screened.get("probabilities") or {}).get("injection"),
                }
            except JevUnavailable as exc:
                screen = {"action": "unavailable", "reason": str(exc)}
            if screen.get("action") == "block":
                review_required = True
                review_reason = INJECTION_REASON
            elif screen.get("action") == "pass":
                evidence.append({"id": "console", "text": self._redact(excerpt, [vm.name])})
            # review / skip / unavailable: untrusted text stays out of the evidence

        results: list[dict[str, Any]] = []
        try:
            verified = await self.jev.verify(claims=list(VERIFICATION_CLAIMS), evidence=evidence)
            results = list(verified.get("results") or [])
        except JevUnavailable as exc:
            return AdvisorNote(
                kind="verification",
                source="jev",
                summary=f"Verification review unavailable: {exc}",
                data={
                    "review_required": review_required,
                    "review_reason": review_reason,
                    "screen": screen,
                    "results": [],
                    "passed": bool(result.passed),
                },
            )
        for item in results:
            if item.get("verdict") == "contradicted" and item.get("action") == "auto":
                review_required = True
                review_reason = review_reason or f"advisor contradicted: {item.get('claim')}"
        confidences = [
            float(i["confidence"]) for i in results if isinstance(i.get("confidence"), int | float)
        ]
        verdicts = ", ".join(f"{i.get('verdict')}" for i in results) or "none"
        summary = (
            f"Review required: {review_reason}"
            if review_required
            else f"Boot claims {verdicts}; no review needed"
        )
        return AdvisorNote(
            kind="verification",
            source="jev",
            summary=summary,
            confidence=min(confidences) if confidences else None,
            data={
                "review_required": review_required,
                "review_reason": review_reason,
                "screen": screen,
                "results": [
                    {k: i.get(k) for k in ("id", "claim", "verdict", "confidence", "action")}
                    for i in results
                ],
                "passed": bool(result.passed),
            },
        )
