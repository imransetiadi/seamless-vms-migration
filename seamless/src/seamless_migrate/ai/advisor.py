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
from . import payloads
from .decision import Advice
from .jev import JevClient, JevUnavailable
from .redaction import redact, replace_names

log = logging.getLogger(__name__)
GIB = 2**30
MIB = 2**20

STRATEGY_DESCRIPTIONS = {
    Strategy.cold: "Stop the VM, copy all used data over the network, boot on the destination.",
    Strategy.warm: "Pre-copy snapshots while the VM runs, then stop it and sync only changed "
    "blocks (the whole device is scanned).",
    Strategy.storage_handover: "Stop the VM and hand its Ceph-backed volumes from the source "
    "Cinder to the destination's Cinder via unmanage/manage; no data is copied.",
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
CONSOLE_EXCERPT_CHARS = payloads.CONSOLE_EXCERPT_CHARS


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
    def _for_strategy(
        self, text: str, vm: VMRef, destination_name: str | None, destination: str
    ) -> str:
        """Secrets redacted, then the VM's name (when names are redacted) and the destination
        provider's name - operator free text, in the eligibility reasons the operators read (T6) -
        replaced in one pass, longest first, so neither replacement splits the other: the VM's name
        blanked first left "jakarta-[vm]-prod" to send. The provider's name gives way to the
        destination's kind and series whatever the redaction setting, and wins when the two names
        are equal (SDD §1 naming, §13.4, reviews RV20-01, RV21-02)."""
        names = {vm.name: "[vm]"} if self.settings.memory_redact_names else {}
        if destination_name:
            names[destination_name] = destination
        return replace_names(redact(text), names)

    def _strategy_evidence(
        self,
        vm: VMRef,
        estimates: Sequence[Estimate],
        plan: Plan,
        findings: Sequence[Finding],
        destination_name: str | None = None,
        destination: str = "the destination",
    ) -> str:
        # rendered from the declared field specs (SDD §13.4, ADV-06): nothing else is sent
        text = payloads.render_strategy(payloads.StrategyInput(vm, estimates, plan, findings))
        return self._for_strategy(text, vm, destination_name, destination)

    async def recommend_strategy(
        self,
        vm: VMRef,
        estimates: Sequence[Estimate],
        plan: Plan,
        findings: Sequence[Finding] = (),
        destination: str = "the destination",
        destination_name: str | None = None,
    ) -> Advice | None:
        """Ask Jev to break a tie (or pick when nothing meets the SLO); ``None`` when no advice is
        needed. ``destination`` describes the destination by kind and series
        (``destination_descriptor``), and ``destination_name``, the provider's own name, never
        leaves (SDD §1 naming, §13.4). Returns the advice - or why Jev gave none; ``decide()``
        takes the decision (SDD §14.2, §14.4)."""
        scope = advisor_scope(estimates, plan.selection_policy)
        if not scope:
            return None
        deterministic, reason = select_strategy(vm, estimates, plan)
        eligible = [e for e in estimates if e.eligible]
        candidates = [
            {"id": e.strategy.value, "description": STRATEGY_DESCRIPTIONS[e.strategy]}
            for e in eligible
        ]
        if len(candidates) < 2:
            return None  # nothing to decide: jev_decide needs at least two candidates
        # the jev_decide arguments, built even when Jev is off: they are the input snapshot (§14.5)
        arguments: dict[str, Any] = {
            "decision": self._for_strategy(
                f"Which migration strategy should be used for VM {vm.name} moving to "
                f"{destination}?",
                vm,
                destination_name,
                destination,
            ),
            "evidence": self._strategy_evidence(
                vm, estimates, plan, findings, destination_name, destination
            ),
            "priorities": (
                f"Minimize downtime within the SLO of {plan.downtime_slo_s} s; prefer the "
                "simpler strategy when downtimes differ by less than 10 %; never pick an "
                "ineligible strategy"
            ),
            "candidates": candidates,
            "requirements": list(STRATEGY_REQUIREMENTS),
        }
        # exactly the declared arguments (SDD §13.4): GET /advisor/data-sent lists them
        request = {key: arguments[key] for key in payloads.STRATEGY_REQUEST_FIELDS}
        request["escalate_on_contradiction"] = True
        base: dict[str, Any] = {
            "scope": tuple(s.value for s in scope),
            "options": tuple(c["id"] for c in candidates),
            "deterministic": deterministic.value,
            "deterministic_reason": reason,
            "request": request,
        }
        if not self.jev_enabled:
            return Advice(
                option=None,
                confidence=None,
                unavailable="Jev is disabled (SEAMLESS_JEV_MODE=off)",
                **base,
            )
        assert self.jev is not None
        try:
            response = await self.jev.decide(**request)
        except JevUnavailable as exc:
            return Advice(option=None, confidence=None, unavailable=str(exc), **base)
        rec = response.get("recommendation") or {}
        selected = rec.get("selected")
        confidence = rec.get("confidence")
        probabilities = rec.get("probabilities") or {}
        model = response.get("model")
        # jev_decide gives no reasons today; any it gives are checked against the evidence (ADV-02)
        reasons = rec.get("reasons", response.get("reasons"))
        return Advice(
            option=selected if isinstance(selected, str) else None,
            confidence=float(confidence) if isinstance(confidence, int | float) else None,
            distribution={
                k: float(v) for k, v in probabilities.items() if isinstance(v, int | float)
            },
            status=rec.get("status") or response.get("status"),
            escaped=bool(rec.get("escaped")),
            model_version=model if isinstance(model, str) else None,
            reasons=tuple(reasons) if isinstance(reasons, list) else (),
            **base,
        )

    # -- classification ----------------------------------------------------------------------
    def _describe_vm(self, vm: VMRef) -> str:
        # rendered from the declared field specs (SDD §13.4, ADV-06)
        return self._redact(payloads.render_workload(vm), [vm.name])

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
            # SDD §14.2/§13.4: an opaque key per item, its place in the batch - the VM's own id
            # never leaves; the answer maps back through it
            keys = {f"item-{n}": vm.source_id for n, vm in enumerate(batch, 1)}
            items = [
                {"id": key, "text": self._describe_vm(vm)}
                for key, vm in zip(keys, batch, strict=True)
            ]
            try:
                response = await self.jev.classify(
                    items=items, classes=TIER_CLASSES, purpose=CLASSIFY_PURPOSE
                )
            except JevUnavailable as exc:
                errors.append(str(exc))
                continue
            for result in response.get("results") or []:
                rid = keys.get(str(result.get("id")))
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
    async def review_verification(
        self, vm: VMRef, result: Any, review_reason: str | None = None
    ) -> AdvisorNote | None:
        """Screen the console excerpt and cross-check the boot claims; may flag for review.

        ``review_reason`` is what the verification rules ask review for (SDD §7.5: a database VM
        without an application probe, an unreachable probe); it comes first. Returns a note whose
        ``data["review_required"]``/``data["review_reason"]`` the caller applies to the migration.
        ``result.passed`` is never modified.
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
        reasons: list[str] = [review_reason] if review_reason else []
        screen: dict[str, Any] = {}
        console = (result.evidence or {}).get("console")
        # with a remote model the excerpt stays in this installation unless allowed (SDD §13.4)
        console_sent = bool(console) and payloads.console_allowed(self.settings)
        if console_sent:
            # SDD §13.4/§14.2: the excerpt is redacted before it is screened — it is the one
            # place raw guest text leaves the control plane
            excerpt = self._redact(str(console)[-CONSOLE_EXCERPT_CHARS:], [vm.name])
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
                reasons.append(INJECTION_REASON)
            elif screen.get("action") == "pass":
                evidence.append({"id": "console", "text": excerpt})
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
                    "review_required": bool(reasons),
                    "review_reason": "; ".join(reasons) or None,
                    "screen": screen,
                    "results": [],
                    "passed": bool(result.passed),
                    "console_sent": console_sent,
                },
            )
        contradicted = [
            item
            for item in results
            if item.get("verdict") == "contradicted" and item.get("action") == "auto"
        ]
        if contradicted and INJECTION_REASON not in reasons:
            reasons.append(f"advisor contradicted: {contradicted[0].get('claim')}")
        review_required = bool(reasons)
        review_reason = "; ".join(reasons) or None
        confidences = [
            float(i["confidence"]) for i in results if isinstance(i.get("confidence"), int | float)
        ]
        # each verdict once, in the order seen: never "verified, verified" (ADV-18)
        verdicts = list(dict.fromkeys(str(i.get("verdict")) for i in results))
        if review_required:
            summary = f"Review required: {review_reason}"
        elif verdicts == ["verified"]:
            summary = "Boot verified; no review needed"
        else:
            named = " and ".join(verdicts) or "none"
            summary = f"Boot claims {named}, none contradicted; no review needed"
        if console and not console_sent:
            summary += (
                ". The console excerpt stayed in this installation (remote advisor): "
                "screened by the verification's own console patterns only"
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
                "console_sent": console_sent,
            },
        )
