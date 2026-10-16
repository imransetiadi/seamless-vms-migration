"""What leaves the installation is declared (SDD §13.4, ADV-06): the serializers render field specs,
and GET /advisor/data-sent lists the same specs."""

import json
from pathlib import Path

import pytest

from seamless_migrate.ai import payloads
from seamless_migrate.ai.advisor import Advisor
from seamless_migrate.ai.payloads import (
    STRATEGY_FIELDS,
    WORKLOAD_FIELDS,
    FieldSpec,
    console_allowed,
    data_sent,
)
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Strategy
from seamless_migrate.domain.models import Estimate, Finding
from tests.factories import make_disk, make_plan, make_vm

#: today's text, captured before the field specs existed: Jev receives the same bytes
GOLDEN = {
    "strategy_full": (
        "VM db-07: 8 vCPU, 32768 MB RAM, 2 disk(s) [volume 100 GiB type ceph-ssd "
        "multi-attach; volume 400 GiB type ceph-ssd encrypted independent], provisioned "
        "500 GiB, used 360 GiB. Guest write rate 5.0 MiB/s measured. Downtime SLO 900 s. "
        "Link 125 MiB/s. Findings: NET_MTU_SHRINK (warning). Estimates: cold: downtime "
        "3000 s, pre-copy 0 s over 0 pass(es), eligible; warm: downtime 400 s, pre-copy "
        "2000 s over 3 pass(es), INELIGIBLE (no CBT; independent disk)."
    ),
    "strategy_bare": (
        "VM web-09: 2 vCPU, 4096 MB RAM, 1 disk(s) [volume 20 GiB type ceph-ssd], "
        "provisioned 20 GiB, used 8 GiB. Guest write rate unknown (default 2 MiB/s "
        "assumed). Downtime SLO 900 s. Link 125 MiB/s. Findings: none. Estimates: cold: "
        "downtime 3000 s, pre-copy 0 s over 0 pass(es), eligible."
    ),
    "describe_full": (
        "name=db-07 os=linux tags=app:billing,tier:db disks=100GiB+400GiB flavor=m1.xlarge"
    ),
    "describe_bare": ("name=web-09 os=unknown tags=none disks=20GiB flavor=2vcpu/4096MB"),
}

FULL = make_vm(
    name="db-07",
    vcpus=8,
    ram_mb=32768,
    disks=[
        make_disk(id="d1", size_gb=100, used_gb=60.0, volume_type="ceph-ssd", multiattach=True),
        make_disk(id="d2", size_gb=400, used_gb=300.0, encrypted=True, independent=True),
    ],
    change_rate_bps=5 * 2**20,
    tags={"tier": "db", "app": "billing"},
    os_type="linux",
    flavor="m1.xlarge",
)
BARE = make_vm(
    name="web-09",
    disks=[make_disk(id="d3", size_gb=20, used_gb=8.0)],
    change_rate_bps=None,
    tags={},
    os_type=None,
    flavor=None,
)
PLAN = make_plan(downtime_slo_s=900)
ESTIMATES = [
    Estimate(
        strategy=Strategy.cold,
        eligible=True,
        precopy_s=0,
        passes=0,
        downtime_s=3000,
        total_s=3000,
        final_delta_bytes=0,
        meets_slo=False,
    ),
    Estimate(
        strategy=Strategy.warm,
        eligible=False,
        reasons=["no CBT", "independent disk"],
        precopy_s=2000,
        passes=3,
        downtime_s=400,
        total_s=2400,
        final_delta_bytes=10**8,
        meets_slo=True,
    ),
]
FINDINGS = [Finding(code="NET_MTU_SHRINK", severity="warning", message="m")]


def test_the_strategy_evidence_jev_receives_is_the_same_as_before():
    advisor = Advisor(None, Settings())
    assert advisor._strategy_evidence(FULL, ESTIMATES, PLAN, FINDINGS) == GOLDEN["strategy_full"]
    assert advisor._strategy_evidence(BARE, ESTIMATES[:1], PLAN, []) == GOLDEN["strategy_bare"]


def test_the_workload_description_jev_receives_is_the_same_as_before():
    advisor = Advisor(None, Settings())
    assert advisor._describe_vm(FULL) == GOLDEN["describe_full"]
    assert advisor._describe_vm(BARE) == GOLDEN["describe_bare"]


def _listed(decision_type: str, settings: Settings | None = None) -> dict:
    return next(
        i
        for i in data_sent(settings or Settings(jev_mode="http"))
        if i["decision_type"] == decision_type
    )


def test_every_field_spec_is_listed_with_its_label_and_tool():
    strategy = _listed("migration_strategy")
    assert strategy["tool"] == "jev_decide"
    keys = [f["key"] for f in strategy["fields"]]
    assert [s.key for s in STRATEGY_FIELDS] == [
        k for k in keys if k in {s.key for s in STRATEGY_FIELDS}
    ]
    assert len(keys) == len(set(keys)) and all(f["label"] for f in strategy["fields"])
    workload = _listed("workload_tier")
    # each item's key leaves too: its place in the batch, never the VM's id (SDD §14.2)
    assert (workload["tool"], [f["key"] for f in workload["fields"]]) == (
        "jev_classify",
        ["id", *(s.key for s in WORKLOAD_FIELDS)],
    )
    assert "not the VM's id" in workload["fields"][0]["label"]
    assert _listed("verification_review")["tool"] == "jev_verify"
    assert _listed("console_screening")["tool"] == "jev_screen"


def test_a_new_field_spec_is_listed_and_sent_without_another_change(monkeypatch):
    """The panel changes when the serializer changes (ADV-06 acceptance)."""
    canary = FieldSpec("canary", "Canary field", lambda ctx: " Canary 1.")
    monkeypatch.setattr(payloads, "STRATEGY_FIELDS", (*payloads.STRATEGY_FIELDS, canary))
    assert "canary" in [f["key"] for f in _listed("migration_strategy")["fields"]]
    text = Advisor(None, Settings())._strategy_evidence(BARE, ESTIMATES[:1], PLAN, [])
    assert text.endswith(" Canary 1.")


async def test_the_request_jev_decide_receives_has_exactly_the_listed_arguments():
    """The arguments built for a real tie, not only the declaration (SDD §13.4)."""
    tie = [
        Estimate(
            strategy=Strategy.cold,
            eligible=True,
            precopy_s=0,
            passes=0,
            downtime_s=1000,
            total_s=1000,
            final_delta_bytes=0,
            meets_slo=False,
        ),
        Estimate(
            strategy=Strategy.warm,
            eligible=True,
            precopy_s=900,
            passes=3,
            downtime_s=950,
            total_s=1850,
            final_delta_bytes=0,
            meets_slo=False,
        ),
    ]
    advice = await Advisor(None, Settings()).recommend_strategy(FULL, tie, PLAN, FINDINGS)
    assert advice is not None and advice.request is not None
    listed = {f["key"] for f in _listed("migration_strategy")["fields"]}
    evidence = {s.key for s in STRATEGY_FIELDS}
    assert set(advice.request) - {"escalate_on_contradiction"} == (listed - evidence) | {"evidence"}
    assert advice.request["evidence"] == Advisor(None, Settings())._strategy_evidence(
        FULL, tie, PLAN, FINDINGS
    )


@pytest.mark.parametrize(
    ("settings", "allowed"),
    [
        (Settings(jev_mode="http"), False),  # remote by default: the console stays local
        (Settings(jev_mode="http", jev_deployment="on_prem"), True),
        (Settings(jev_mode="http", console_screening_send_to_remote=True), True),
    ],
)
def test_the_console_excerpt_leaves_only_for_an_on_prem_model_or_when_allowed(settings, allowed):
    assert console_allowed(settings) is allowed
    item = _listed("console_screening", settings)
    assert item["sent"] is allowed
    assert (item["note"] is None) is allowed


def test_nothing_is_sent_while_jev_is_off():
    assert all(i["sent"] is False for i in data_sent(Settings(jev_mode="off")))


def test_the_data_sent_list_is_the_one_the_dashboards_mock_serves():
    """SDD §13.4 (review RV21-03): the fixture the dashboard's offline mock is held to is the
    API's own list, for a remote model whose console stays local - a label changed here fails
    this test until data_sent.json, and with it the mock, says the same."""
    shared = json.loads((Path(__file__).parent / "fixtures" / "data_sent.json").read_text())
    assert data_sent(Settings(jev_mode="http")) == shared["items"]
