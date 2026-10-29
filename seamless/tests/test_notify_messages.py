"""Topics, triggers and messages as pure functions (SDD §8.6, §24.5, plan task NT4)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from seamless_migrate.domain.enums import NotifyTopic, NotifyTrigger, Phase, Strategy
from seamless_migrate.domain.models import (
    Approval,
    Estimate,
    Event,
    ForceCutover,
    Wave,
)
from seamless_migrate.notify.messages import (
    build_message,
    email_message,
    slack_payload,
    triggers_for,
    webhook_document,
)
from tests.factories import make_migration, make_plan, make_vm

AT = datetime(2026, 10, 29, 10, 0, 5, tzinfo=UTC)
PUBLIC = "https://seamless.example.com/"
SECRETS = (
    "password=hunter2",
    "10.0.0.5",
    "web-01.corp.example",
    "rhosp17-dc1",
    "alice",
    "because I said so",
    "Traceback",
)


def plan(name="Finance apps", **kw):
    return make_plan(name=name, downtime_slo_s=600, **kw)


def estimate(downtime_s=120.0):
    return Estimate(
        strategy=Strategy.warm,
        precopy_s=1000,
        passes=3,
        downtime_s=downtime_s,
        total_s=1200,
        final_delta_bytes=1,
        meets_slo=True,
    )


def migration(p, **kw):
    data = {
        "plan_id": p.id,
        "vm": make_vm(name="web-01 <prod> & co"),
        "strategy": Strategy.warm,
        "phase": Phase.awaiting_cutover,
        "estimate": estimate(),
    }
    data.update(kw)
    return make_migration(**data)


def event(kind, m=None, p=None, seq=7, **data):
    return Event(
        seq=seq,
        ts=AT,
        kind=kind,
        plan_id=(p or m).id if (p or m) else None,
        migration_id=m.id if m is not None and kind.startswith("migration.") else None,
        actor="alice",
        message="web-01: cutover failed: password=hunter2 at 10.0.0.5 (because I said so)",
        data=data,
    )


def every_message(public_url=PUBLIC, redact=False):
    """One message per trigger, over the events of §24.5, with facts a message must not leak."""
    p = plan(waves=[Wave(id="w1", name="Wave one", order=1, vm_ids=["vm-1"])])
    m = migration(p, wave_id="w1", actual_downtime_s=95.0)
    cold = migration(p, strategy=Strategy.cold, phase=Phase.ready, wave_id="w1")
    kw = {"redact_names": redact, "public_url": public_url}
    cases = [
        (
            NotifyTrigger.approval_cutover,
            event("migration.phase", m, to="awaiting_cutover", reason="r"),
        ),
        (
            NotifyTrigger.approval_wave,
            event("wave.started", None, p, wave_id="w1", name="Wave one"),
        ),
        (
            NotifyTrigger.approval_advisor,
            event("decision.pending_approval", m, decision_type="workload_tier", target_id="rec-1"),
        ),
        (NotifyTrigger.downtime_started, event("migration.downtime_started", m)),
        (
            NotifyTrigger.downtime_ended,
            event("migration.downtime_ended", m, actual_downtime_s=95.0, cause="verified"),
        ),
        (
            NotifyTrigger.problem_failed,
            event(
                "migration.phase",
                m,
                **{"from": "cutover", "to": "failed", "reason": "boom", "source_stopped": True},
            ),
        ),
        (NotifyTrigger.problem_data_inconsistent, event("migration.data_inconsistent", m)),
        (NotifyTrigger.problem_source_running, event("migration.source_running", m)),
        (
            NotifyTrigger.outcome_completed,
            event("migration.phase", m, to="completed", reason="verified"),
        ),
        (
            NotifyTrigger.outcome_rolled_back,
            event("migration.phase", m, to="rolled_back", reason="rb"),
        ),
        (
            NotifyTrigger.outcome_finalized,
            event("migration.phase", m, to="finalized", reason="fin"),
        ),
        (NotifyTrigger.progress_plan_started, event("plan.started", None, p)),
        (
            NotifyTrigger.progress_plan_completed,
            event("plan.completed", None, p, migrations=3, succeeded=2),
        ),
        (
            NotifyTrigger.progress_wave_completed,
            event("wave.completed", None, p, wave_id="w1", name="Wave one"),
        ),
    ]
    out = []
    for trigger, e in cases:
        mig = m if e.migration_id or e.kind.startswith("decision.") else None
        out.append(build_message(trigger, e, p, mig, [m, cold], **kw))
    return p, m, out


def test_each_trigger_names_the_vm_the_plan_and_their_ids():
    """SDD §24.5: every message of a migration names the VM, and every message ends with
    `Plan {plan} ({plan_id})`, then `migration {id}` when it has one, then the link."""
    p, m, messages = every_message()
    assert len(messages) == 14 and len({msg.trigger for msg in messages}) == 14
    for msg in messages:
        assert msg.ending[0] == f"Plan Finance apps ({p.id})", msg.trigger
        assert msg.title and msg.lines and all(msg.lines), msg.trigger
        if msg.migration_id is not None:
            assert msg.migration_id == m.id and f"migration {m.id}" in msg.ending
            assert "web-01 <prod> & co" in msg.title, msg.trigger
            assert msg.vm_name == "web-01 <prod> & co" and msg.strategy == "warm"
        else:
            assert msg.trigger.value.startswith(("progress.", "approval.wave"))
            assert "Finance apps" in msg.title or "Wave one" in msg.title, msg.trigger
        assert msg.topic == msg.trigger.topic and isinstance(msg.topic, NotifyTopic)
        assert msg.plan_id == p.id and msg.event_seq == 7
    wave = next(msg for msg in messages if msg.trigger == NotifyTrigger.progress_wave_completed)
    assert wave.title == "Finance apps: Wave one completed" and wave.wave_name == "Wave one"
    completed = next(
        msg for msg in messages if msg.trigger == NotifyTrigger.progress_plan_completed
    )
    assert completed.lines == ["migrations: 3, succeeded: 2"]
    # a name is cut at 200 characters
    long = migration(p, vm=make_vm(name="x" * 300))
    msg = build_message(
        NotifyTrigger.outcome_finalized, event("migration.phase", long, to="finalized"), p, long
    )
    assert msg.title == "x" * 200 + " finalized"


def test_a_downtime_message_says_where_the_vm_runs_and_how_long_it_was_down():
    """SDD §24.5: downtime.started gives the stop's time, the estimate and the SLO;
    downtime.ended says, by its cause, that the VM is up on the destination (and whether the SLO
    was met) or runs again at the source, and how long it was down, in minutes and seconds."""
    p = plan()
    m = migration(p)
    started = build_message(
        NotifyTrigger.downtime_started, event("migration.downtime_started", m), p, m
    )
    assert started.title == "web-01 <prod> & co is down for its cutover"
    assert started.lines == [
        "The source VM stopped at 2026-10-29T10:00:05Z. Estimated downtime 2 min 0 s; "
        "SLO 10 min 0 s."
    ]
    assert started.estimate_s == 120.0 and started.slo_s == 600.0
    verified = build_message(
        NotifyTrigger.downtime_ended,
        event("migration.downtime_ended", m, actual_downtime_s=95.0, cause="verified"),
        p,
        m,
    )
    assert verified.title == "web-01 <prod> & co is up on the destination after 1 min 35 s"
    assert verified.lines == [
        "Verified at 2026-10-29T10:00:05Z. Downtime 1 min 35 s; SLO 10 min 0 s: met."
    ]
    assert verified.downtime_s == 95.0
    over = build_message(
        NotifyTrigger.downtime_ended,
        event("migration.downtime_ended", m, actual_downtime_s=601.0, cause="verified"),
        p,
        m,
    )
    assert over.lines[0].endswith("SLO 10 min 0 s: exceeded.")
    back = build_message(
        NotifyTrigger.downtime_ended,
        event("migration.downtime_ended", m, actual_downtime_s=40.0, cause="source_started"),
        p,
        m,
    )
    assert back.title == "web-01 <prod> & co runs again at the source after 40 s"
    assert back.lines == [
        "The cutover did not complete; the source VM runs again since 2026-10-29T10:00:05Z."
    ]
    # the failure's line follows whether the source is stopped, and the plan's rollback setting
    stopped = event(
        "migration.phase", m, **{"from": "cutover", "to": "failed", "source_stopped": True}
    )
    assert build_message(NotifyTrigger.problem_failed, stopped, p, m).lines == [
        "The source VM is stopped, and the plan's automatic rollback starts it again."
    ]
    manual = plan(verification={"auto_rollback": False})
    assert build_message(NotifyTrigger.problem_failed, stopped, manual, m).lines == [
        "The source VM is stopped and waits for a person, who rolls the migration back or "
        "retries it."
    ]
    running = event(
        "migration.phase", m, **{"from": "precopy", "to": "failed", "source_stopped": False}
    )
    failed = build_message(NotifyTrigger.problem_failed, running, p, m)
    assert failed.title == "web-01 <prod> & co failed in precopy" and failed.lines == [
        "The source VM runs."
    ]


def test_the_approval_trigger_follows_the_pending_approvals_rule():
    """SDD §5.4 rule 1, §16, §24.5: approval.cutover fires on the phase awaiting_cutover, a
    cutover request and a forced cutover - only while the plan requires approval and no approval
    counts; a plan without approvals, or a counted approval, fires none; the other triggers map
    one event each, and an unnamed event fires none."""
    p = plan(require_approval=True, require_distinct_approver=True)
    m = migration(p, cutover_requested_by="alice")
    gate = event("migration.phase", m, to="awaiting_cutover", reason="converged")
    assert triggers_for(gate, p, m) == [NotifyTrigger.approval_cutover]
    assert triggers_for(event("migration.action", m, action="cutover"), p, m) == [
        NotifyTrigger.approval_cutover
    ]
    forced = event("migration.force_cutover", m, target_id=m.id, reason="now")
    assert triggers_for(forced, p, m) == [NotifyTrigger.approval_cutover]
    assert triggers_for(event("migration.action", m, action="retry"), p, m) == []
    # the requester's own approval does not count under a distinct approver, so it still fires
    own = m.model_copy(update={"approvals": [Approval(actor="alice", at=AT)]})
    assert triggers_for(gate, p, own) == [NotifyTrigger.approval_cutover]
    counted = m.model_copy(update={"approvals": [Approval(actor="bob", at=AT)]})
    assert triggers_for(gate, p, counted) == []
    forcer = m.model_copy(
        update={
            "force_cutover": ForceCutover(
                actor="carol", at=AT, reason="r", delta_bytes=1, estimated_downtime_s=1.0
            ),
            "approvals": [Approval(actor="carol", at=AT)],
        }
    )
    assert triggers_for(gate, p, forcer) == [NotifyTrigger.approval_cutover]
    assert triggers_for(gate, plan(require_approval=False), m) == []
    # the other triggers, one per event
    for kind, data, trigger in (
        ("migration.phase", {"to": "failed"}, NotifyTrigger.problem_failed),
        ("migration.phase", {"to": "completed"}, NotifyTrigger.outcome_completed),
        ("migration.phase", {"to": "rolled_back"}, NotifyTrigger.outcome_rolled_back),
        ("migration.phase", {"to": "finalized"}, NotifyTrigger.outcome_finalized),
        ("migration.downtime_started", {}, NotifyTrigger.downtime_started),
        ("migration.downtime_ended", {"cause": "verified"}, NotifyTrigger.downtime_ended),
        ("migration.data_inconsistent", {}, NotifyTrigger.problem_data_inconsistent),
        ("migration.source_running", {}, NotifyTrigger.problem_source_running),
        ("decision.pending_approval", {"target_id": "rec-1"}, NotifyTrigger.approval_advisor),
        ("plan.paused", {}, NotifyTrigger.progress_plan_paused),
        ("plan.completed", {}, NotifyTrigger.progress_plan_completed),
        ("plan.cancelled", {}, NotifyTrigger.progress_plan_cancelled),
        ("plan.finalized", {}, NotifyTrigger.progress_plan_finalized),
        ("wave.completed", {"wave_id": "w1"}, NotifyTrigger.progress_wave_completed),
    ):
        assert triggers_for(event(kind, m, p, **data), p, m) == [trigger], kind
    for kind in (
        "migration.phase",
        "migration.sync_pass",
        "plan.created",
        "channel.created",
        "notification.sent",
    ):
        assert triggers_for(event(kind, m, p, to="syncing"), p, m) == [], kind


def test_a_waves_single_shot_cutovers_make_one_approval_message():
    """SDD §24.5: wave.started - or plan.started for a plan without waves - fires approval.wave
    beside its progress trigger when the wave releases single-shot migrations in ready to their
    gate, in one message that gives their number; none when nothing waits for an approval."""
    p = plan(
        waves=[Wave(id="w1", name="Wave one", order=1), Wave(id="w2", name="Wave two", order=2)]
    )
    colds = [
        migration(p, strategy=Strategy.cold, phase=Phase.ready, wave_id="w1") for _ in range(3)
    ]
    warm = migration(p, phase=Phase.ready, wave_id="w1")  # a warm migration pre-copies first
    other = migration(p, strategy=Strategy.cold, phase=Phase.ready, wave_id="w2")
    approved = migration(
        p,
        strategy=Strategy.cold,
        phase=Phase.ready,
        wave_id="w1",
        approvals=[Approval(actor="bob", at=AT)],
    )
    started = event("wave.started", None, p, wave_id="w1", name="Wave one")
    all_migrations = [*colds, warm, other, approved]
    assert triggers_for(started, p, None, all_migrations) == [
        NotifyTrigger.approval_wave,
        NotifyTrigger.progress_wave_started,
    ]
    msg = build_message(NotifyTrigger.approval_wave, started, p, None, all_migrations)
    assert msg.title == "Cutover approval needed: 3 VMs of Wave one"
    assert msg.lines == [
        "Wave Wave one started; 3 of its migrations wait at their cutover gate for an approval."
    ]
    assert msg.migration_id is None and msg.wave_id == "w1"
    # nothing waits: the progress trigger alone
    assert triggers_for(started, p, None, [warm, other]) == [NotifyTrigger.progress_wave_started]
    assert triggers_for(started, plan(require_approval=False), None, all_migrations) == [
        NotifyTrigger.progress_wave_started
    ]
    # a plan without waves: plan.started counts the plan's
    flat = plan()
    cold = migration(flat, strategy=Strategy.cold, phase=Phase.ready)
    plan_started = event("plan.started", None, flat)
    assert triggers_for(plan_started, flat, None, [cold]) == [
        NotifyTrigger.approval_wave,
        NotifyTrigger.progress_plan_started,
    ]
    msg = build_message(NotifyTrigger.approval_wave, plan_started, flat, None, [cold])
    assert msg.title == "Cutover approval needed: 1 VMs of Finance apps"
    assert triggers_for(plan_started, p, None, all_migrations) == [
        NotifyTrigger.progress_plan_started
    ]


def test_slack_values_are_escaped_so_no_name_mentions_a_channel():
    """SDD §24.5: every value of the Slack payload has &, < and > escaped, so a VM named
    `<!channel>` mentions nothing and forges no link; the header keeps Slack's 150 characters;
    nothing unfurls."""
    p = plan(name="Plan <!here> & co")
    m = migration(p, vm=make_vm(name="<!channel> web-01 <https://evil.example|click> & more"))
    msg = build_message(
        NotifyTrigger.outcome_completed,
        event("migration.phase", m, to="completed"),
        p,
        m,
        public_url=PUBLIC,
    )
    payload = slack_payload(msg)
    text = json.dumps(payload)
    assert "<!channel>" not in text and "<https://evil" not in text and "<!here>" not in text
    assert (
        "&lt;!channel&gt; web-01 &lt;https://evil.example|click&gt; &amp; more migrated"
        == payload["text"]
    )
    assert (
        payload["blocks"][0]["type"] == "header"
        and payload["blocks"][0]["text"]["type"] == "plain_text"
    )
    assert (
        payload["blocks"][1]["type"] == "section"
        and "Verified on the destination" in payload["blocks"][1]["text"]["text"]
    )
    context = payload["blocks"][2]
    assert context["type"] == "context"
    assert [e["text"] for e in context["elements"]] == [
        f"Plan Plan &lt;!here&gt; &amp; co ({p.id})",
        f"migration {m.id}",
        f"{PUBLIC}migrations/{m.id}",
    ]
    assert payload["unfurl_links"] is False and payload["unfurl_media"] is False
    long = migration(p, vm=make_vm(name="y" * 300))
    header = slack_payload(
        build_message(
            NotifyTrigger.outcome_finalized, event("migration.phase", long, to="finalized"), p, long
        )
    )
    assert len(header["blocks"][0]["text"]["text"]) == 150


def test_an_email_subject_holds_no_control_character():
    """SDD §24.5: `Subject: [Seamless] {title}` with every control character of a name replaced
    by a space (CVE-2024-6923), one audience in To, the key as the Message-ID, Auto-Submitted
    set, and the body the lines and the ending in UTF-8."""
    p = plan()
    m = migration(p, vm=make_vm(name="web-01\r\nBcc: victim@example.net\x00\x7f"))
    msg = build_message(
        NotifyTrigger.outcome_finalized, event("migration.phase", m, to="finalized"), p, m
    )
    mail = email_message(
        msg,
        sender="seamless@example.com",
        recipients=["owners@example.com", "ops@example.org"],
        key=f"app-owners_{msg.event_seq}",
        domain="seamless.example.com",
    )
    subject = mail["Subject"]
    assert subject == "[Seamless] web-01  Bcc: victim@example.net   finalized"
    assert "\n" not in subject and "\r" not in subject
    assert mail.get_all("Bcc") is None and mail["To"] == "owners@example.com, ops@example.org"
    assert mail["From"] == "seamless@example.com"
    assert mail["Message-ID"] == "<app-owners_7@seamless.example.com>"
    assert mail["Auto-Submitted"] == "auto-generated"
    body = mail.get_content()
    assert body.startswith("The migration can no longer be rolled back.\n\nPlan Finance apps (")
    assert f"migration {m.id}" in body
    raw = mail.as_bytes().decode("utf-8", "replace")
    assert raw.count("Bcc:") == 1 and "victim@example.net" in raw.split("\n\n", 1)[0]
    accented = migration(p, vm=make_vm(name="serveur-é"))
    mail = email_message(
        build_message(
            NotifyTrigger.outcome_finalized,
            event("migration.phase", accented, to="finalized"),
            p,
            accented,
        ),
        sender="s@example.com",
        recipients=["a@example.com"],
        key="k_1",
        domain="example.com",
    )
    assert mail["Subject"] == "[Seamless] serveur-é finalized"  # RFC 2047 on the wire


def test_no_message_carries_an_error_an_address_a_comment_or_an_actor():
    """SDD §24.5, §24.8: whatever the event's message, its reason, its actor or the migration's
    error hold, no message, format or document carries an error's text, an address, a hostname,
    a provider's name, a comment or who acted."""
    p = plan(name="Finance apps", description="owner comment: because I said so")
    m = migration(p, error="cutover: password=hunter2 at 10.0.0.5")
    kw = {"public_url": PUBLIC}
    seen = []
    p2, m2, messages = every_message()
    for msg in messages:
        seen.append(msg.text)
        seen.append(json.dumps(slack_payload(msg)))
        seen.append(json.dumps(webhook_document(msg)))
        seen.append(
            email_message(
                msg,
                sender="s@example.com",
                recipients=["o@example.com"],
                key="k_1",
                domain="example.com",
            ).as_string()
        )
    failed = build_message(
        NotifyTrigger.problem_failed,
        event(
            "migration.phase",
            m,
            **{
                "from": "cutover",
                "to": "failed",
                "reason": "password=hunter2",
                "source_stopped": True,
            },
        ),
        p,
        m,
        **kw,
    )
    seen.append(failed.text)
    for text in seen:
        for secret in SECRETS:
            assert secret not in text, (secret, text[:200])
    assert "alice" not in json.dumps([webhook_document(msg) for msg in messages])


def test_a_channel_that_redacts_names_sends_ids():
    """SDD §24.5, §24.11 decision 8: a channel with redact_names sends the VM's, the plan's and
    the wave's ids where the others send their names."""
    p, m, messages = every_message(redact=True)
    for msg in messages:
        text = msg.text + json.dumps(slack_payload(msg)) + json.dumps(webhook_document(msg))
        assert "web-01" not in text and "Finance apps" not in text and "Wave one" not in text, (
            msg.trigger
        )
        assert msg.plan_name == p.id and msg.ending[0] == f"Plan {p.id} ({p.id})"
        if msg.migration_id is not None:
            assert msg.vm_name == m.id and m.id in msg.title
        if msg.wave_id is not None:
            assert msg.wave_name == "w1"
    cutover = next(msg for msg in messages if msg.trigger == NotifyTrigger.approval_cutover)
    assert cutover.title == f"Cutover approval needed: {m.id}"
    wave = next(msg for msg in messages if msg.trigger == NotifyTrigger.approval_wave)
    assert wave.title == "Cutover approval needed: 1 VMs of w1"


def test_a_link_carries_ids_alone_and_none_without_a_public_url():
    """SDD §15.1, §24.5: the link is built on SEAMLESS_PUBLIC_URL - /migrations/{id},
    /advisor/recommendations/{id} for approval.advisor, else /plans/{id} - from ids alone, never
    a name or a token; unset, a message carries no link."""
    p, m, messages = every_message()
    for msg in messages:
        assert msg.link is not None and msg.link.startswith("https://seamless.example.com/"), (
            msg.trigger
        )
        assert msg.link == msg.ending[-1]
        assert "web-01" not in msg.link and "Finance" not in msg.link and "token" not in msg.link
        if msg.trigger == NotifyTrigger.approval_advisor:
            assert msg.link == "https://seamless.example.com/advisor/recommendations/rec-1"
        elif msg.migration_id is not None:
            assert msg.link == f"https://seamless.example.com/migrations/{m.id}"
        else:
            assert msg.link == f"https://seamless.example.com/plans/{p.id}"
    _, _, bare = every_message(public_url=None)
    for msg in bare:
        assert msg.link is None and len(msg.ending) in (1, 2), msg.trigger
        assert "https://" not in msg.text
        assert webhook_document(msg)["data"]["link"] is None
    _, _, no_slash = every_message(public_url="https://seamless.example.com")
    assert no_slash[0].link == f"https://seamless.example.com/migrations/{no_slash[0].migration_id}"


def test_the_webhook_document_is_the_standard_webhooks_payload():
    """SDD §24.5: `{"type": trigger, "timestamp": the event's ts, "data": {...}}` with the title,
    the lines, the link, the topic, the event's seq and kind, the plan's and the wave's id and
    name, the migration's id, VM name, strategy and phase, and the downtime, the estimate and
    the SLO in seconds."""
    p = plan(waves=[Wave(id="w1", name="Wave one", order=1)])
    m = migration(p, wave_id="w1", actual_downtime_s=95.0)
    e = event("migration.downtime_ended", m, actual_downtime_s=95.0, cause="verified")
    doc = webhook_document(build_message(NotifyTrigger.downtime_ended, e, p, m, public_url=PUBLIC))
    assert doc == {
        "type": "downtime.ended",
        "timestamp": "2026-10-29T10:00:05Z",
        "data": {
            "title": "web-01 <prod> & co is up on the destination after 1 min 35 s",
            "lines": [
                "Verified at 2026-10-29T10:00:05Z. Downtime 1 min 35 s; SLO 10 min 0 s: met."
            ],
            "link": f"{PUBLIC}migrations/{m.id}",
            "topic": "downtime",
            "event_seq": 7,
            "event_kind": "migration.downtime_ended",
            "plan_id": p.id,
            "plan_name": "Finance apps",
            "wave_id": "w1",
            "wave_name": "Wave one",
            "migration_id": m.id,
            "vm_name": "web-01 <prod> & co",
            "strategy": "warm",
            "phase": "awaiting_cutover",
            "downtime_s": 95.0,
            "estimate_s": 120.0,
            "slo_s": 600.0,
        },
    }
    json.dumps(doc)  # JSON-ready as it is
    # a naive event time is read as UTC; the document's time is the event's, not the send's
    later = e.model_copy(update={"ts": AT + timedelta(hours=2)})
    assert (
        webhook_document(build_message(NotifyTrigger.downtime_ended, later, p, m))["timestamp"]
        == "2026-10-29T12:00:05Z"
    )
    # a plan message: no migration fields
    doc = webhook_document(
        build_message(NotifyTrigger.progress_plan_paused, event("plan.paused", None, p), p, None)
    )
    assert doc["type"] == "progress.plan_paused" and doc["data"]["migration_id"] is None
    assert doc["data"]["vm_name"] is None and doc["data"]["topic"] == "progress"
    with pytest.raises(KeyError):
        _ = doc["data"]["error"]
