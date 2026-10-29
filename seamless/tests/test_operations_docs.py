"""The operations documents name what the code does (review 38b): the dashboard's button for a
paused plan, the action that applies a Jev setting, and every L2 place that records who acted."""

from __future__ import annotations

import os
import re
import subprocess

from seamless_migrate.config import find_repo_root
from seamless_migrate.domain.models import (
    AdvisorRecommendation,
    Migration,
    NotificationChannel,
    NotificationSubscription,
)

ROOT = find_repo_root()


def test_operations_md_names_the_button_a_paused_plan_shows():
    """Operations.md §5 (review RV38B-13): the drain's plans are resumed with the button the plan
    page shows a paused plan - Resume, not Start."""
    ops = (ROOT / "docs" / "Operations.md").read_text(encoding="utf-8")
    named = re.search(r"Or press \*\*(\w+)\*\* on each plan", ops).group(1)
    page = (ROOT / "dashboard" / "src" / "pages" / "PlanDetail.tsx").read_text(encoding="utf-8")
    label = re.search(r"\{resumes \? '(\w+)' :", page).group(1)
    assert named == label


def test_operations_md_applies_a_jev_setting_with_up_which_recreates_the_sidecar():
    """Operations.md §3.4, README (review RV38B-10): the Jev settings are the jev sidecar's
    environment, and restart recreates the control plane alone (--no-deps): the doc sends them to
    up, which recreates what changed."""
    compose = (ROOT / "deploy" / "compose" / "compose.yaml").read_text(encoding="utf-8")
    jev = compose[compose.index("\n  jev:") : compose.index("\n  seamless:")]
    assert "TYPESAFE_API_KEY" in jev and "JEV_MCP_AUTH_TOKEN" in jev
    env = {**os.environ, "SEAMLESS_HOST_OS": "Linux", "SEAMLESS_DOCKER_CONTEXT": "default"}
    out = subprocess.run(
        ["make", "-n", "--no-print-directory", "-C", str(ROOT), "seamless-restart"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    up = next(line for line in out.splitlines() if "--force-recreate --no-deps --wait" in line)
    assert up.split("--wait-timeout 300")[-1].split()[:1] == ["seamless"] and " jev" not in up
    ops = (ROOT / "docs" / "Operations.md").read_text(encoding="utf-8")
    row = next(line for line in ops.splitlines() if "`TYPESAFE_API_KEY`" in line)
    assert row.split("|")[2].strip().startswith("`up`"), row
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    bullet = readme[readme.index("* **After a change:**") :].split("\n* ", 1)[0]
    assert "TYPESAFE_API_KEY" in bullet and "needs `up`" in bullet


def test_the_erasure_bullet_names_every_l2_field_that_records_a_user():
    """MEMORY.md §3.5 (reviews RV37B-12, RV38B-12; plan task NT11): an erasure request reaches
    every L2 field that records who acted - a recommendation's requested_by and a notification
    subscription's created_by among them -, the reasons and messages that name the person, and the
    addresses an email notification channel sends to: its recipients are personal data
    (SDD §24.8)."""
    assert "requested_by" in AdvisorRecommendation.model_fields
    assert "cutover_requested_by" in Migration.model_fields
    assert "recipients" in NotificationChannel.model_fields
    assert "created_by" in NotificationSubscription.model_fields
    memory = (ROOT / "docs" / "MEMORY.md").read_text(encoding="utf-8")
    bullet = memory[memory.index("* **Erasure requests:**") :]
    bullet = bullet[: bullet.index("\n* **", 5)]
    for field in (
        "approvals[].actor",
        "phase_history[].actor",
        "cutover_requested_by",
        "force_cutover.actor",
        "`requested_by`",
        "updated_by",
        "Force cutover by",
        "`recipients`",
        "`created_by`",
    ):
        assert field in bullet, field
