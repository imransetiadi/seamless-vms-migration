"""Live checks against real Jev (stdio, npx) and agentmemory.

Skipped unless explicitly enabled:

* ``SEAMLESS_LIVE_JEV=1`` with ``TYPESAFE_API_KEY`` in the environment and ``npx`` on PATH;
* ``SEAMLESS_LIVE_MEMORY=1`` with ``SEAMLESS_MEMORY_URL`` (optionally ``SEAMLESS_MEMORY_SECRET``).

Each test makes the minimum number of calls (one ``jev_decide``; one remember + one search).
"""

from __future__ import annotations

import os
import shutil
import uuid

import pytest

from seamless_migrate.ai.jev import JevClient
from seamless_migrate.ai.memory import MemoryClient
from seamless_migrate.config import Settings

pytestmark = pytest.mark.live

LIVE_JEV = os.environ.get("SEAMLESS_LIVE_JEV") == "1"
LIVE_MEMORY = os.environ.get("SEAMLESS_LIVE_MEMORY") == "1"


@pytest.mark.skipif(not LIVE_JEV, reason="set SEAMLESS_LIVE_JEV=1 to call jev-mcp")
async def test_live_jev_decide():
    if not os.environ.get("TYPESAFE_API_KEY"):
        pytest.skip("TYPESAFE_API_KEY is not set")
    if shutil.which("npx") is None:
        pytest.skip("npx is not on PATH")
    client = JevClient(Settings(jev_mode="stdio", jev_timeout_s=180))
    candidates = [
        {"id": "cold", "description": "Stop the VM, copy all used data, boot on RHOSO."},
        {"id": "warm", "description": "Pre-copy while running, then sync changed blocks."},
        {
            "id": "storage_handover",
            "description": "Hand the shared-Ceph volumes over via Cinder unmanage/manage.",
        },
    ]
    out = await client.decide(
        decision="Which migration strategy should be used for VM db-02 moving from RHOSP 17.1 "
        "to RHOSO 18.0?",
        evidence="Two Cinder RBD volumes (100 GiB root, 600 GiB data), no multi-attach. Downtime "
        "SLO 600 s. Estimates: cold 3660 s (eligible); warm 1700 s (eligible); "
        "storage_handover 240 s (eligible, shared Ceph, volume types mapped).",
        priorities="Minimize downtime within the SLO of 600 s; prefer the simpler strategy when "
        "downtimes differ by less than 10 %; never pick an ineligible strategy",
        candidates=candidates,
        requirements=[
            "Estimated downtime is within the SLO",
            "The source VM keeps running until cutover",
        ],
    )
    rec = out["recommendation"]
    assert "selected" in rec and "confidence" in rec
    assert rec["selected"] is None or isinstance(rec["selected"], str)
    assert isinstance(out.get("checks", []), list)
    assert client.breaker_open is False and client.last_error is None


@pytest.mark.skipif(not LIVE_MEMORY, reason="set SEAMLESS_LIVE_MEMORY=1 to call agentmemory")
async def test_live_memory_roundtrip():
    url = os.environ.get("SEAMLESS_MEMORY_URL")
    if not url:
        pytest.skip("SEAMLESS_MEMORY_URL is not set")
    marker = f"seamless-livetest-{uuid.uuid4().hex[:10]}"
    client = MemoryClient(
        url, os.environ.get("SEAMLESS_MEMORY_SECRET"), "seamless-migrate-livetest"
    )
    assert await client.remember(
        f"{marker}: warm cutover of a 600G database converged after 3 passes",
        "fact",
        ["seamless", "warm", "livetest"],
    ), client.last_error
    hits = await client.search(f"{marker} warm cutover", limit=5)
    assert isinstance(hits, list), client.last_error
    assert client.last_error is None
    assert any(marker in (h.title + h.content) for h in hits)
