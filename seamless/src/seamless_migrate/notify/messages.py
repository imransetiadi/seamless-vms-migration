"""Topics, triggers and messages as pure functions (SDD §8.6, §24.5, plan task NT4): which
triggers an event fires, the message each one makes - a title, lines and an ending -, and the
three formats, an email, a Slack payload and a Standard Webhooks document. The functions read
no error, no reason, no address, no provider name, no comment and no actor: a message never
holds one (§24.5, §24.8)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from ..domain.enums import SINGLE_SHOT_STRATEGIES, NotifyTopic, NotifyTrigger, Phase
from ..domain.models import Event, Migration, Plan, to_utc

#: a name in a message is cut here (SDD §24.5)
NAME_LIMIT = 200
#: Slack's limit on a header block's text (SDD §24.5)
SLACK_HEADER_LIMIT = 150
#: the subject's prefix (SDD §24.5)
SUBJECT_PREFIX = "[Seamless] "
#: the texts of §24.5 that say "the destination": a message never names a provider (§24.8), so
#: SDD §1's naming rule exempts them as fixed texts (tests/test_preflight.py, FIXED_TEXTS)
TEXTS = {
    "up": "{vm} is up on the destination after {down}",
    "source_running": (
        "Seen running at {at}: it writes data the destination no longer receives. Stop it or "
        "roll the migration back; finalize refuses meanwhile."
    ),
    "completed": (
        "Verified on the destination at {at}; downtime {down}; SLO {slo}. The source VM stays "
        "until the migration is finalized."
    ),
    "rolled_back": "Its source VM runs, and the destination server was removed.",
}

_PROGRESS = {
    "plan.started": NotifyTrigger.progress_plan_started,
    "plan.paused": NotifyTrigger.progress_plan_paused,
    "plan.completed": NotifyTrigger.progress_plan_completed,
    "plan.cancelled": NotifyTrigger.progress_plan_cancelled,
    "plan.finalized": NotifyTrigger.progress_plan_finalized,
    "wave.started": NotifyTrigger.progress_wave_started,
    "wave.completed": NotifyTrigger.progress_wave_completed,
}
_PHASE = {
    "failed": NotifyTrigger.problem_failed,
    "completed": NotifyTrigger.outcome_completed,
    "rolled_back": NotifyTrigger.outcome_rolled_back,
    "finalized": NotifyTrigger.outcome_finalized,
}


@dataclass(frozen=True)
class Message:
    """A message (SDD §24.5): the same title and lines for every kind; the fields the webhook
    document carries beside them."""

    trigger: NotifyTrigger
    title: str
    lines: list[str]
    plan_id: str
    plan_name: str
    event_seq: int
    event_kind: str
    event_ts: datetime
    migration_id: str | None = None
    vm_name: str | None = None
    strategy: str | None = None
    phase: str | None = None
    wave_id: str | None = None
    wave_name: str | None = None
    downtime_s: float | None = None
    estimate_s: float | None = None
    slo_s: float | None = None
    link: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def topic(self) -> NotifyTopic:
        return self.trigger.topic

    @property
    def ending(self) -> list[str]:
        """Every message ends with the plan, the migration when it has one, and the link."""
        lines = [f"Plan {self.plan_name} ({self.plan_id})"]
        if self.migration_id is not None:
            lines.append(f"migration {self.migration_id}")
        if self.link is not None:
            lines.append(self.link)
        return lines

    @property
    def text(self) -> str:
        return "\n".join([*self.lines, "", *self.ending])


# ------------------------------------------------------------------------------ the triggers


def _counted_approvals(m: Migration, plan: Plan) -> bool:
    from ..orchestrator import counted_approvals

    return bool(counted_approvals(m, plan))


def _needs_approval(m: Migration | None, plan: Plan) -> bool:
    """The Pending approvals rule (SDD §5.4 rule 1, §16): the plan requires approval and no
    approval counts."""
    return m is not None and plan.require_approval and not _counted_approvals(m, plan)


def waiting_single_shots(
    plan: Plan, wave_id: str | None, migrations: Sequence[Migration]
) -> list[Migration]:
    """The single-shot migrations in ``ready`` a wave releases to their gate (SDD §24.5): the
    ones an approval.wave message counts."""
    return [
        m
        for m in migrations
        if m.plan_id == plan.id
        and (wave_id is None or m.wave_id == wave_id)
        and Phase(m.phase) == Phase.ready
        and m.strategy in SINGLE_SHOT_STRATEGIES
        and _needs_approval(m, plan)
    ]


def triggers_for(
    event: Event, plan: Plan, migration: Migration | None, migrations: Sequence[Migration] = ()
) -> list[NotifyTrigger]:
    """The triggers ``event`` fires (SDD §24.5): none for an event the table does not name."""
    kind, data = event.kind, event.data
    if kind == "migration.phase":
        to = str(data.get("to", ""))
        if to == "awaiting_cutover":
            return [NotifyTrigger.approval_cutover] if _needs_approval(migration, plan) else []
        trigger = _PHASE.get(to)
        return [trigger] if trigger else []
    if kind == "migration.action" and data.get("action") == "cutover":
        return [NotifyTrigger.approval_cutover] if _needs_approval(migration, plan) else []
    if kind == "migration.force_cutover":
        return [NotifyTrigger.approval_cutover] if _needs_approval(migration, plan) else []
    if kind == "decision.pending_approval":
        return [NotifyTrigger.approval_advisor]
    if kind == "migration.downtime_started":
        return [NotifyTrigger.downtime_started]
    if kind == "migration.downtime_ended":
        return [NotifyTrigger.downtime_ended]
    if kind == "migration.data_inconsistent":
        return [NotifyTrigger.problem_data_inconsistent]
    if kind == "migration.source_running":
        return [NotifyTrigger.problem_source_running]
    progress = _PROGRESS.get(kind)
    if progress is None:
        return []
    triggers = [progress]
    if kind == "wave.started" or (kind == "plan.started" and not plan.waves):
        wave_id = data.get("wave_id") if kind == "wave.started" else None
        if waiting_single_shots(plan, wave_id, migrations):
            triggers.insert(0, NotifyTrigger.approval_wave)
    return triggers


# ------------------------------------------------------------------------------- the message


def _cut(name: str) -> str:
    return name[:NAME_LIMIT]


def _when(value: Any) -> str:
    """A time in ISO 8601 UTC (SDD §4, §24.5)."""
    if isinstance(value, datetime):
        return to_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        try:
            return _when(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return "an unknown time"
    return "an unknown time"


def duration(seconds: float | None) -> str:
    """Minutes and seconds (SDD §24.5)."""
    if seconds is None:
        return "unknown"
    whole = int(round(seconds))
    minutes, secs = divmod(whole, 60)
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


def _wave_name(plan: Plan, wave_id: str | None, redact: bool) -> str | None:
    if wave_id is None:
        return None
    for wave in plan.waves:
        if wave.id == wave_id:
            return wave.id if redact else _cut(wave.name)
    return wave_id


def _link(public_url: str | None, path: str) -> str | None:
    """The dashboard's page, on SEAMLESS_PUBLIC_URL (SDD §15.1): ids alone, never a token."""
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}{path}"


def build_message(
    trigger: NotifyTrigger,
    event: Event,
    plan: Plan,
    migration: Migration | None,
    migrations: Sequence[Migration] = (),
    *,
    redact_names: bool = False,
    public_url: str | None = None,
) -> Message:
    """The message a trigger makes of an event (SDD §24.5): its title, lines and fields."""
    m = migration
    plan_name = plan.id if redact_names else _cut(plan.name)
    vm = None if m is None else (m.id if redact_names else _cut(m.vm.name))
    data = event.data
    wave_id = (m.wave_id if m is not None else None) or data.get("wave_id")
    wave = _wave_name(plan, wave_id, redact_names)
    estimate = m.estimate.downtime_s if m is not None and m.estimate is not None else None
    slo = float(plan.downtime_slo_s)
    downtime = data.get("actual_downtime_s") if "actual_downtime_s" in data else None
    if downtime is None and m is not None:
        downtime = m.actual_downtime_s
    at = _when(event.ts)
    est, slo_text = duration(estimate), duration(slo)
    strategy = str(m.strategy) if m is not None else None
    phase = str(m.phase) if m is not None else None
    title, lines = "", []
    path = f"/migrations/{m.id}" if m is not None else f"/plans/{plan.id}"
    if trigger == NotifyTrigger.approval_cutover:
        title = f"Cutover approval needed: {vm}"
        lines = [
            f"{vm} waits at its cutover gate for an approval. Strategy {strategy}; estimated "
            f"downtime {est}; SLO {slo_text}."
        ]
        if plan.cutover_window is not None:
            start, end = _when(plan.cutover_window.start), _when(plan.cutover_window.end)
            lines.append(f"Cutover window {start} to {end}.")
    elif trigger == NotifyTrigger.approval_wave:
        waiting = waiting_single_shots(
            plan, data.get("wave_id") if event.kind == "wave.started" else None, migrations
        )
        n = len(waiting)
        where = wave if wave is not None else plan_name
        what = "Wave" if wave is not None else "Plan"
        title = f"Cutover approval needed: {n} VMs of {where}"
        lines = [
            f"{what} {where} started; {n} of its migrations wait at their cutover gate for an "
            "approval."
        ]
        path = f"/plans/{plan.id}"
    elif trigger == NotifyTrigger.approval_advisor:
        decision = str(data.get("decision_type", "advisor")).replace("_", " ")
        title = f"Advisor decision awaits approval: {vm}"
        lines = [
            f"The advisor's {decision} decision for {vm} waits for a person allowed to approve "
            "it, and the migration does not start meanwhile."
        ]
        target = data.get("target_id")
        if target is not None:
            path = f"/advisor/recommendations/{target}"
    elif trigger == NotifyTrigger.downtime_started:
        title = f"{vm} is down for its cutover"
        lines = [f"The source VM stopped at {at}. Estimated downtime {est}; SLO {slo_text}."]
    elif trigger == NotifyTrigger.downtime_ended:
        down = duration(downtime)
        if data.get("cause") == "source_started":
            title = f"{vm} runs again at the source after {down}"
            lines = [f"The cutover did not complete; the source VM runs again since {at}."]
        else:
            met = "met" if downtime is not None and downtime <= slo else "exceeded"
            title = TEXTS["up"].format(vm=vm, down=down)
            lines = [f"Verified at {at}. Downtime {down}; SLO {slo_text}: {met}."]
    elif trigger == NotifyTrigger.problem_failed:
        title = f"{vm} failed in {data.get('from', phase)}"
        if data.get("source_stopped"):
            if plan.verification.auto_rollback:
                lines = [
                    "The source VM is stopped, and the plan's automatic rollback starts it again."
                ]
            else:
                lines = [
                    "The source VM is stopped and waits for a person, who rolls the migration "
                    "back or retries it."
                ]
        else:
            lines = ["The source VM runs."]
    elif trigger == NotifyTrigger.problem_data_inconsistent:
        title = f"{vm}: its migration's figures do not hold together"
        lines = ["Its downtime is left out of the statistics until a person reviews it."]
    elif trigger == NotifyTrigger.problem_source_running:
        title = f"{vm} runs again at the source after its migration"
        lines = [TEXTS["source_running"].format(at=at)]
    elif trigger == NotifyTrigger.outcome_completed:
        title = f"{vm} migrated"
        lines = [TEXTS["completed"].format(at=at, down=duration(downtime), slo=slo_text)]
    elif trigger == NotifyTrigger.outcome_rolled_back:
        title = f"{vm} rolled back"
        lines = [TEXTS["rolled_back"]]
    elif trigger == NotifyTrigger.outcome_finalized:
        title = f"{vm} finalized"
        lines = ["The migration can no longer be rolled back."]
    else:  # progress.*
        verb = trigger.value.rsplit("_", 1)[1]
        if trigger.value.startswith("progress.wave"):
            title = f"{plan_name}: {wave} {verb}"
        else:
            title = f"{plan_name}: {verb}"
        counts = {k: v for k, v in data.items() if isinstance(v, int) and not isinstance(v, bool)}
        lines = [", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in counts.items()) or "—"]
        path = f"/plans/{plan.id}"
    return Message(
        trigger=trigger,
        title=title,
        lines=lines,
        plan_id=plan.id,
        plan_name=plan_name,
        event_seq=event.seq,
        event_kind=event.kind,
        event_ts=to_utc(event.ts),
        migration_id=m.id if m is not None else None,
        vm_name=vm,
        strategy=strategy,
        phase=phase,
        wave_id=wave_id,
        wave_name=wave,
        downtime_s=downtime,
        estimate_s=estimate,
        slo_s=slo,
        link=_link(public_url, path),
    )


# ------------------------------------------------------------------------------- the formats


def _header_safe(text: str) -> str:
    """Every control character a space: a name must never end a header (CVE-2024-6923)."""
    return "".join(" " if ord(ch) < 32 or ch == "\x7f" else ch for ch in text)


def email_message(
    message: Message, *, sender: str, recipients: Sequence[str], key: str, domain: str
) -> EmailMessage:
    """The plain-text email (SDD §24.5): one audience in To, the key as the Message-ID, and
    Auto-Submitted so responders stay quiet (RFC 3834)."""
    mail = EmailMessage()
    mail["Subject"] = SUBJECT_PREFIX + _header_safe(message.title)
    mail["From"] = sender
    mail["To"] = ", ".join(recipients)
    mail["Message-ID"] = f"<{key}@{domain}>"
    mail["Auto-Submitted"] = "auto-generated"
    mail.set_content(message.text + "\n", charset="utf-8")
    return mail


def slack_escape(text: str) -> str:
    """Slack's three escapes (SDD §24.5): a name can neither mention a channel nor forge a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(message: Message) -> dict[str, Any]:
    """The Slack incoming-webhook payload (SDD §24.5): text for the phone, blocks for the
    client, nothing unfurled."""
    title = slack_escape(message.title)
    return {
        "text": title,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title[:SLACK_HEADER_LIMIT]},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(map(slack_escape, message.lines))},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": slack_escape(line)} for line in message.ending
                ],
            },
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def webhook_document(message: Message) -> dict[str, Any]:
    """The Standard Webhooks payload (SDD §24.5): the type, the event's time and the message as
    fields."""
    return {
        "type": message.trigger.value,
        "timestamp": _when(message.event_ts),
        "data": {
            "title": message.title,
            "lines": list(message.lines),
            "link": message.link,
            "topic": message.topic.value,
            "event_seq": message.event_seq,
            "event_kind": message.event_kind,
            "plan_id": message.plan_id,
            "plan_name": message.plan_name,
            "wave_id": message.wave_id,
            "wave_name": message.wave_name,
            "migration_id": message.migration_id,
            "vm_name": message.vm_name,
            "strategy": message.strategy,
            "phase": message.phase,
            "downtime_s": message.downtime_s,
            "estimate_s": message.estimate_s,
            "slo_s": message.slo_s,
        },
    }
