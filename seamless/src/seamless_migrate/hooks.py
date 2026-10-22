"""The hook runner (SDD §7.2 Hooks, §13.3, §23.3, plan task N4): how a catalog hook runs at a
migration's point - the context document it gets, its idempotency key, a playbook's or a webhook's
run bounded by its timeout, and the ``migration.action`` that records it."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from .ai.redaction import redact, redact_cut
from .config import Settings
from .domain.models import Event, Hook, HookPoint, Migration, Plan, Provider
from .events import new_event
from .executors.ansible import ansible_env, ansible_yaml, stop_process_group
from .security.secrets import SecretNotFound, read_secret

#: how many of a playbook's last lines a failure's detail is looked for in
_TAIL_LINES = 400
#: a line of a playbook's output read whole up to this size, in pieces above it
_LINE_LIMIT = 2**22

Log = Callable[[str], Awaitable[None]]


def idempotency_key(migration_id: str, attempt: int, point: str) -> str:
    """The key every run of a point within an attempt shares (SDD §7.2): a resumed step's hooks run
    again with it, and a retry - a new attempt - gets a new one."""
    return f"{migration_id}.{attempt}.{point}"


def context_document(
    migration: Migration,
    plan: Plan,
    point: HookPoint,
    source: Provider,
    destination: Provider,
) -> dict[str, Any]:
    """What a hook gets (SDD §7.2): the migration, its point, both providers, the destination
    server, the addresses before and after, the floating IPs and the key - never a credential."""
    vm = migration.vm
    after = [a for nic in vm.nics for a in plan.nic_target(vm.source_id, nic)[1]]
    return {
        "migration_id": migration.id,
        "attempt": migration.attempts,
        "point": point,
        "plan_id": plan.id,
        "vm_name": vm.name,
        "source": {"id": source.id, "label": source.label},
        "destination": {"id": destination.id, "label": destination.label},
        "server_id": migration.destination_server_id,
        "addresses": {"before": [a for nic in vm.nics for a in nic.fixed_ips], "after": after},
        "floating_ips": [a for nic in vm.nics for a in nic.floating_ips],
        "idempotency_key": idempotency_key(migration.id, migration.attempts, point),
    }


@dataclass
class HookRun:
    """One run of a hook (SDD §7.2): its outcome, and a failure's reason - redacted."""

    hook: str
    kind: str
    point: str
    idempotency_key: str
    outcome: Literal["succeeded", "failed"]
    duration_s: float
    detail: str | None = None


def hook_action_event(migration: Migration, run: HookRun, actor: str = "orchestrator") -> Event:
    """The ``migration.action`` a run records (SDD §4.3, §7.2): never the URL, the secret's name,
    the answer or the output."""
    return new_event(
        "migration.action",
        f"hook {run.hook} at {run.point} {run.outcome}",
        plan_id=migration.plan_id,
        migration_id=migration.id,
        actor=actor,
        data={
            "action": "hook",
            "hook": run.hook,
            "kind": run.kind,
            "point": run.point,
            "idempotency_key": run.idempotency_key,
            "outcome": run.outcome,
            "duration_s": run.duration_s,
            "detail": run.detail,
        },
    )


class _Failed(Exception):
    """A run's failure, by the reason its event names."""


class HookRunner:
    """Runs one hook with a context document (SDD §7.2); ``transport`` stands in for the network in
    tests."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def run(
        self, hook: Hook, context: dict[str, Any], run_dir: Path, log: Log | None = None
    ) -> HookRun:
        """Run ``hook`` with ``context`` - a playbook under ``run_dir`` - within its timeout."""
        started = time.monotonic()
        detail: str | None = None
        try:
            if hook.kind == "playbook":
                runner = self._playbook(hook, context, run_dir, log)
            else:
                runner = self._webhook(hook, context)
            await asyncio.wait_for(runner, hook.timeout_s)
        except TimeoutError:
            detail = f"timed out after {hook.timeout_s} s"
        except _Failed as exc:
            detail = str(exc)
        return HookRun(
            hook=hook.name,
            kind=hook.kind,
            point=str(context["point"]),
            idempotency_key=str(context["idempotency_key"]),
            outcome="failed" if detail is not None else "succeeded",
            duration_s=round(time.monotonic() - started, 3),
            detail=detail,
        )

    async def _playbook(
        self, hook: Hook, context: dict[str, Any], run_dir: Path, log: Log | None
    ) -> None:
        """ansible-playbook -i localhost, -c local <playbook> -e @context.yml in the hook's own
        directory, with the executor's credential-free environment (SDD §7.2)."""
        workdir = run_dir / "hooks" / f"{context['point']}-{hook.name}"
        await asyncio.to_thread(_prepare, workdir, ansible_yaml({"seamless_hook": context}))
        try:
            env = await asyncio.to_thread(ansible_env, self.settings)
            cmd = [
                self.settings.ansible_playbook,
                "-i", "localhost,",
                "-c", "local",
                str(hook.playbook),
                "-e", "@context.yml",
            ]  # fmt: skip
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(workdir),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                    limit=_LINE_LIMIT,
                )
            except OSError as exc:
                raise _Failed(f"cannot run ansible-playbook: {type(exc).__name__}") from None
            lines: deque[str] = deque(maxlen=_TAIL_LINES)
            try:
                await _read_output(proc, lines, log)
                returncode = await proc.wait()
            finally:
                if proc.returncode is None:
                    # a timeout or a cancel: the playbook does not outlive its run (SDD §7.2)
                    await asyncio.shield(stop_process_group(proc))
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)
        if returncode != 0:
            last = next(
                (ln for ln in reversed(lines) if "fatal" in ln.lower() or "error" in ln.lower()),
                lines[-1] if lines else "",
            )
            raise _Failed(f"exit {returncode}: {redact_cut(last, 500)}".rstrip(": "))

    async def _webhook(self, hook: Hook, context: dict[str, Any]) -> None:
        """An HTTPS POST of the document with the bearer token of the hook's secret and the
        idempotency key; no redirect followed, any 2xx a success (SDD §7.2, §13.3)."""
        try:
            values = await asyncio.to_thread(read_secret, str(hook.secret), self.settings)
        except SecretNotFound:
            values = {}
        token = values.get("token")
        if not token:
            raise _Failed("its secret holds no token")
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(context["idempotency_key"]),
        }
        try:
            async with httpx.AsyncClient(
                transport=self.transport, follow_redirects=False, timeout=hook.timeout_s
            ) as client:
                answer = await client.post(str(hook.url), json=context, headers=headers)
        except httpx.HTTPError as exc:
            raise _Failed(type(exc).__name__) from None
        if not 200 <= answer.status_code < 300:
            raise _Failed(f"HTTP {answer.status_code}")


def _prepare(workdir: Path, context_yaml: str) -> None:
    """The hook's run directory (0700) and its context file (0600), written before the run."""
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, mode=0o700)
    os.chmod(workdir, 0o700)
    fd = os.open(workdir / "context.yml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(context_yaml)


async def _read_output(
    proc: asyncio.subprocess.Process, lines: deque[str], log: Log | None
) -> None:
    """Each line of the playbook's output, to the migration's log through redact() (SDD §7.2)."""
    assert proc.stdout is not None
    eof = False
    while not eof:
        try:
            raw = await proc.stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            raw, eof = exc.partial, True
        except asyncio.LimitOverrunError:
            raw = await proc.stdout.read(_LINE_LIMIT)
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip()
        lines.append(line)
        if log is not None and line.strip():
            await log(redact(line))
