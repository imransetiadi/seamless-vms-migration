"""Lab verification of changed-extent tracking (QASuite LAB-CE01…LAB-CE03 and PERF-E2E-SM1b;
SDD §28.10, plan task CE13) against a real source cloud with Ceph and a destination. Skipped
unless the lab is configured; never runs in CI - ``tests/lab/test_lab_rehearsal.py`` runs it there
through the real executor code against the strict doubles.

    SEAMLESS_LAB_CLOUDS_YAML=~/lab/clouds.yaml \\
    SEAMLESS_LAB_SOURCE_CLOUD=rhosp17 SEAMLESS_LAB_DEST_CLOUD=rhoso18 \\
    SEAMLESS_LAB_EXTENTS_SERVER=<id of a disposable warm VM on Ceph volumes> \\
    SEAMLESS_LAB_CEPH_FSID=<the source cluster's fsid> SEAMLESS_LAB_CEPH_MONITORS=mon-0,mon-1 \\
    SEAMLESS_LAB_CEPH_KEY_FILE=~/lab/seamless.key \\
    SEAMLESS_LAB_MAPPINGS='{"networks": {"tenant": "tenant"}, "flavors": {}, "volume_types": {}}' \\
    .venv/bin/pytest -m lab tests/lab/test_lab_changed_extents.py -v -s

Read-only by default: the passes copy to temporary volumes and the source VM keeps running.
SEAMLESS_LAB_DESTRUCTIVE=1 runs the cutovers (LAB-CE01, LAB-CE03): the source VM stops and a
destination server is created. Use a disposable test server only.

Optional: SEAMLESS_LAB_CEPH_CLIENT (seamless); SEAMLESS_LAB_PRESTAGE=1 pre-stages the plan first
(the conversion hosts, from SEAMLESS_LAB_CONVERSION_HOSTS, a JSON {"source": {...},
"destination": {...}} of ConversionHostConfig); SEAMLESS_LAB_CHECKSUM_CMD, a command that prints
the source volume's checksum and the destination's, one per line, the operator's own way to read
both (an ssh to each conversion host, for one); SEAMLESS_LAB_WRITER_CMD, a command that writes to
the source VM's disks while it runs (the 2 or 10 MiB/s fio writer of LAB-CE01/CE02), stopped after
the passes; SEAMLESS_LAB_CE02_PASSES (50); SEAMLESS_LAB_FALLBACK_CMDS, a JSON of reason to a command
that provokes it before a pass ("base_gone": delete the base snapshot, "resized": extend the
volume, "query_failed": block the monitors, ...; SDD §28.5); SEAMLESS_LAB_ANSIBLE_PLAYBOOK.
The key is read from its file for the run's keyring alone and never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from seamless_migrate.config import Settings, find_repo_root
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy
from seamless_migrate.domain.models import (
    CephAccess,
    ConversionHostConfig,
    Mappings,
    Migration,
    Plan,
    Provider,
)
from seamless_migrate.executors.ansible import AnsibleExecutor
from seamless_migrate.executors.base import StepName, StepResult
from tests.executor_support import make_ctx
from tests.factories import make_migration, make_plan

pytestmark = pytest.mark.lab

ENV = (
    "SEAMLESS_LAB_CLOUDS_YAML",
    "SEAMLESS_LAB_SOURCE_CLOUD",
    "SEAMLESS_LAB_DEST_CLOUD",
    "SEAMLESS_LAB_EXTENTS_SERVER",
    "SEAMLESS_LAB_CEPH_FSID",
    "SEAMLESS_LAB_CEPH_MONITORS",
    "SEAMLESS_LAB_CEPH_KEY_FILE",
)


@dataclass
class Lab:
    """The lab's clouds and the migration the tests drive, pass by pass, through the executor."""

    settings: Settings
    source: Provider
    destination: Provider
    server_id: str
    mappings: Mappings
    #: the provider registry the executor uses; None is the real one (the rehearsal injects doubles)
    providers: Any = None
    plan: Plan | None = None
    migration: Migration | None = None
    _executor: AnsibleExecutor | None = field(default=None, repr=False)

    @property
    def destructive(self) -> bool:
        return os.environ.get("SEAMLESS_LAB_DESTRUCTIVE") == "1"

    def executor(self) -> AnsibleExecutor:
        if self._executor is None:
            self._executor = AnsibleExecutor(self.settings, providers=self.providers)
        return self._executor

    async def ready(self) -> None:
        """The source checked (its rbd pools' fsids, SDD §10), the VM read, the plan and its
        migration made - once; then each pass adds to the same migration."""
        if self.migration is not None:
            return
        from seamless_migrate.providers.openstack import OpenStackProvider

        src = OpenStackProvider(self.source, self.settings)
        capabilities = await src.check()
        self.source = self.source.model_copy(update={"capabilities": capabilities})
        pools = [b for b in capabilities.get("storage_backends", []) if b.get("family") == "rbd"]
        print("\nsource rbd pools: " + json.dumps(pools, indent=2))
        assert any(
            b.get("ceph_fsid") == self.source.ceph.fsid
            for b in pools  # type: ignore[union-attr]
        ), "no rbd pool of the source reports the cluster's fsid (SDD §28.4): nothing to track"
        vm = await src.get_vm(self.server_id)
        self.plan = make_plan(
            source_provider_id=self.source.id,
            destination_provider_id=self.destination.id,
            vm_ids=[self.server_id],
            extent_tracking=True,
            mappings=self.mappings,
        )
        self.migration = make_migration(plan_id=self.plan.id, vm=vm, strategy=Strategy.warm)
        if os.environ.get("SEAMLESS_LAB_PRESTAGE") == "1":
            await self.executor().prestage(self.plan, self.source, self.destination)

    async def run_pass(self, step: StepName) -> StepResult:
        """One pass through the real executor; its extent use printed and kept on the migration."""
        await self.ready()
        assert self.plan is not None and self.migration is not None
        ctx, _ = make_ctx(self.plan, self.migration, self.source, self.destination, self.settings)
        started = time.monotonic()
        result = await self.executor().run(step, ctx)
        took = time.monotonic() - started
        p = result.sync_pass
        assert p is not None, f"{step}: no pass recorded"
        self.migration.sync_passes.append(p)
        use = {dev: u.model_dump() for dev, u in p.extents.items()}
        print(f"\npass {p.number} ({p.kind}, {step}): {took:.0f} s, {json.dumps(use)}")
        return result


@pytest.fixture
def lab(tmp_path):
    missing = [name for name in ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"lab not configured: {', '.join(missing)}")
    pytest.importorskip("openstack")
    settings = Settings(
        clouds_yaml=Path(os.environ["SEAMLESS_LAB_CLOUDS_YAML"]).expanduser(),
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        collection_root=find_repo_root(),
        **(
            {"ansible_playbook": os.environ["SEAMLESS_LAB_ANSIBLE_PLAYBOOK"]}
            if os.environ.get("SEAMLESS_LAB_ANSIBLE_PLAYBOOK")
            else {}
        ),
    )
    hosts = json.loads(os.environ.get("SEAMLESS_LAB_CONVERSION_HOSTS") or "{}")
    source = Provider(
        id="lab-source",
        name="lab source",
        kind=ProviderKind.openstack,
        role=ProviderRole.source,
        # informational: the connection comes from the clouds.yaml entry
        endpoint=f"clouds.yaml:{os.environ['SEAMLESS_LAB_SOURCE_CLOUD']}",
        cloud=os.environ["SEAMLESS_LAB_SOURCE_CLOUD"],
        conversion_host=ConversionHostConfig(**hosts.get("source", {"name": "seamless-conv-src"})),
        ceph=CephAccess(
            fsid=os.environ["SEAMLESS_LAB_CEPH_FSID"],
            monitors=[m.strip() for m in os.environ["SEAMLESS_LAB_CEPH_MONITORS"].split(",")],
            client=os.environ.get("SEAMLESS_LAB_CEPH_CLIENT") or "seamless",
        ),
    )
    destination = Provider(
        id="lab-rhoso",
        name="lab RHOSO",
        kind=ProviderKind.rhoso,
        role=ProviderRole.destination,
        endpoint=f"clouds.yaml:{os.environ['SEAMLESS_LAB_DEST_CLOUD']}",
        cloud=os.environ["SEAMLESS_LAB_DEST_CLOUD"],
        conversion_host=ConversionHostConfig(
            **hosts.get("destination", {"name": "seamless-conv-dst"})
        ),
    )
    # the key, as the secret store holds it for the run's keyring (SDD §13.3, §28.4): never printed
    key = Path(os.environ["SEAMLESS_LAB_CEPH_KEY_FILE"]).expanduser().read_text().strip()
    secret = settings.secrets_dir / f"provider-{source.id}.ceph"
    secret.mkdir(parents=True, mode=0o700)
    (secret / "key").write_text(key)
    (secret / "key").chmod(0o600)
    mappings = Mappings(**json.loads(os.environ.get("SEAMLESS_LAB_MAPPINGS") or "{}"))
    return Lab(settings, source, destination, os.environ["SEAMLESS_LAB_EXTENTS_SERVER"], mappings)


async def _shell(command: str) -> str:
    """A lab command the operator named, run to completion; its output."""
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    assert proc.returncode == 0, f"{command!r} exited {proc.returncode}: {out.decode()}"
    return out.decode()


def _writer() -> subprocess.Popen | None:
    """The writer of LAB-CE01/CE02 while the passes run, when the lab names one."""
    command = os.environ.get("SEAMLESS_LAB_WRITER_CMD")
    return subprocess.Popen(command, shell=True) if command else None


def _stop(writer: subprocess.Popen | None) -> None:
    if writer is not None and writer.poll() is None:
        writer.terminate()
        writer.wait(timeout=30)


async def _first_pass(lab: Lab) -> None:
    """The first pass scans every disk (`first_pass`, SDD §28.5); once per migration."""
    await lab.ready()
    assert lab.migration is not None
    if lab.migration.sync_passes:
        return
    first = await lab.run_pass(StepName.PRECOPY)
    assert first.sync_pass is not None and first.sync_pass.extents, (
        "the first pass recorded no extent use: the plan does not track, or the source has no "
        "Ceph access, no key or no rbd pool of its fsid (SDD §7.2, §28.9)"
    )
    assert all(
        (u.mode, u.reason) == ("scanned", "first_pass") for u in first.sync_pass.extents.values()
    )


async def test_lab_ce01_a_tracked_final_pass_leaves_equal_checksums(lab: Lab):
    """LAB-CE01: a checked pre-copy pass with no miss, then the final pass reads the tracked
    disks' extents; the checksums of source and destination are equal; the downtime is about
    `F + q + Δf/L` (Performance.md §4.6) - printed with `q`, for PERF-E2E-SM1b."""
    writer = _writer()
    try:
        await _first_pass(lab)
        check = await lab.run_pass(StepName.SYNC)
        assert check.sync_pass is not None
        checked = {dev for dev, u in check.sync_pass.extents.items() if u.mode == "checked"}
        assert checked, (
            "no disk of the server qualifies (a Cinder RBD volume of the cluster, not encrypted)"
        )
        assert all(u.missed == 0 for u in check.sync_pass.extents.values()), "a check missed"
        queries = [u.query_s for u in check.sync_pass.extents.values() if u.query_s is not None]
        print(f"checked {sorted(checked)}; q = {max(queries, default=None)} s")
    finally:
        _stop(writer)
    if not lab.destructive:
        pytest.skip("set SEAMLESS_LAB_DESTRUCTIVE=1 to cut the disposable test server over")
    final = await lab.run_pass(StepName.CUTOVER)
    assert final.sync_pass is not None and final.sync_pass.kind == "final"
    read = {dev for dev, u in final.sync_pass.extents.items() if u.mode == "read"}
    assert read == checked, f"the final pass read {sorted(read)}, not the checked {sorted(checked)}"
    print(
        f"final pass: {final.sync_pass.duration_s} s for {final.sync_pass.bytes_transferred} B; "
        f"destination server {final.destination_server_id}"
    )
    command = os.environ.get("SEAMLESS_LAB_CHECKSUM_CMD")
    if not command:
        pytest.skip("set SEAMLESS_LAB_CHECKSUM_CMD to compare the source and destination volumes")
    sums = await _shell(command)
    lines = [line.strip() for line in sums.splitlines() if line.strip()]
    print(f"checksums: {lines}")
    assert len(lines) >= 2 and lines[0] == lines[1], f"the checksums differ: {lines}"


async def test_lab_ce02_no_check_misses_a_write_under_io(lab: Lab):
    """LAB-CE02: at least 50 pass snapshots under I/O, each checked pass comparing the extents
    with what changed: no chunk changed outside them (SDD §28.4's Ceph #61616 guard)."""
    passes = int(os.environ.get("SEAMLESS_LAB_CE02_PASSES") or "50")
    writer = _writer()
    try:
        await _first_pass(lab)
        misses: list[tuple[int, str, int]] = []
        for _ in range(passes):
            result = await lab.run_pass(StepName.SYNC)
            assert result.sync_pass is not None
            for dev, use in result.sync_pass.extents.items():
                if use.mode == "checked" and use.missed:
                    misses.append((result.sync_pass.number, dev, use.missed))
        assert lab.migration is not None
        checked = sum(
            1 for p in lab.migration.sync_passes for u in p.extents.values() if u.mode == "checked"
        )
        print(f"{passes} passes, {checked} checks, misses: {misses}")
        assert checked > 0, "no pass checked a disk"
        assert misses == [], f"checks that missed a write: {misses}"
    finally:
        _stop(writer)


async def test_lab_ce03_every_fallback_scans_in_full_and_completes(lab: Lab):
    """LAB-CE03: each condition that takes a disk off its extents - the base gone, a resize, a
    retype, unreachable monitors, a wrong key, fast-diff off - scans it in full with its reason
    (SDD §28.5), and the migration still completes."""
    fallbacks: dict[str, str] = json.loads(os.environ.get("SEAMLESS_LAB_FALLBACK_CMDS") or "{}")
    if not fallbacks:
        pytest.skip(
            "set SEAMLESS_LAB_FALLBACK_CMDS to a JSON of reason to the command provoking it"
        )
    await _first_pass(lab)
    for reason, command in fallbacks.items():
        await _shell(command)
        result = await lab.run_pass(StepName.SYNC)
        assert result.sync_pass is not None
        seen = {dev: (u.mode, u.reason) for dev, u in result.sync_pass.extents.items()}
        assert any(use == ("scanned", reason) for use in seen.values()), (
            f"after {command!r} no disk scanned for {reason}: {seen}"
        )
    if not lab.destructive:
        pytest.skip("set SEAMLESS_LAB_DESTRUCTIVE=1 to cut the disposable test server over")
    final = await lab.run_pass(StepName.CUTOVER)
    assert final.destination_server_id, "the cutover created no destination server"
    print(f"completed: destination server {final.destination_server_id}")
