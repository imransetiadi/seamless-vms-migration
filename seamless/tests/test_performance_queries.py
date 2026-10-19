"""Performance.md's lab queries do what they say, run with jq on sample migrations (Performance.md
§6, QASuite PERF-E2E-G2)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from seamless_migrate.config import find_repo_root

PERFORMANCE = find_repo_root() / "docs" / "Performance.md"


def _g2_program() -> str:
    """The jq program of Performance.md §6 item 6, the estimate accuracy (G2) query."""
    text = PERFORMANCE.read_text(encoding="utf-8")
    item = text[text.index("6. Estimate accuracy (G2)") :]
    return re.search(r"\| jq '\n(.*?)'\n", item, re.S).group(1)


def _migration(strategy: str, actual: float, estimated: float, passes: list[dict]) -> dict:
    return {
        "strategy": strategy,
        "phase": "completed",
        "actual_downtime_s": actual,
        "estimate": {"downtime_s": estimated},
        "sync_passes": passes,
    }


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_g2_counts_as_calibrated_only_a_delta_pass_that_carried_byte_counts():
    """Performance.md §6 item 6 (review RV37B-13): G2 judges the final estimate a delta pass
    calibrated. The migration kit's vmware_warm passes carry no byte counts, and the orchestrator
    calibrates nothing from them (SDD §9.1): such a migration is reported with the uncalibrated
    ones, never counted against the calibrated share."""
    counted = {"bytes_scanned": 10 * 2**30, "bytes_changed": 2**20, "bytes_transferred": 2**20}
    migrations = [
        _migration("warm", 100, 100, [{"kind": "full", **counted}, {"kind": "delta", **counted}]),
        _migration(
            "vmware_warm",
            400,
            300,
            [
                {"kind": "full", "bytes_scanned": 0, "bytes_changed": 0, "bytes_transferred": 0},
                {"kind": "delta", "bytes_scanned": 0, "bytes_changed": 0, "bytes_transferred": 0},
            ],
        ),
        _migration("cold", 100, 100, []),
    ]
    done = subprocess.run(
        ["jq", _g2_program()],
        input=json.dumps(migrations),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(done.stdout) == {
        "calibrated": {"n": 1, "within_30_pct": 1, "share": 1},
        "uncalibrated": {"n": 2, "within_30_pct": 1},
    }
