"""What releases a migration that keeps its plan from changing (SDD §12): one case table for the
control plane and the dashboard's mock."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from seamless_migrate.domain.enums import Phase
from seamless_migrate.orchestrator import in_flight_refusal
from tests.factories import make_migration, make_vm

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "in_flight_refusal_cases.json").read_text()
)["cases"]


def _busy(case):
    stopped_at = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    return [
        make_migration(
            id=f"mig-{i:010x}",
            vm=make_vm(name=m["vm"]),
            phase=Phase(m["phase"]),
            downtime_started_at=stopped_at if m["source_stopped"] else None,
        )
        for i, m in enumerate(case["migrations"])
    ]


@pytest.mark.parametrize("case", CASES, ids=[c["note"].split(".")[0] for c in CASES])
def test_the_refusal_names_what_releases_each_vm(case):
    assert in_flight_refusal(_busy(case), case["before"]) == case["expected"]


def test_every_kind_of_wait_is_covered():
    """A failed migration whose source is stopped, one in flight, one awaiting finalize."""
    phases = {m["phase"] for c in CASES for m in c["migrations"]}
    assert {"failed", "completed", "awaiting_cutover", "precopy", "verifying"} <= phases
