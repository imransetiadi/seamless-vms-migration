"""Post-cutover verification (SDD §7.5).

Checks: ``server_active`` (destination server ``ACTIVE``), ``ports_up`` (all ports ``ACTIVE``),
``tcp:<port>`` for every configured port (5 s connect timeout from the control plane),
``app:<kind>:<port>`` for each application probe (ADV-18: passed, failed - refused or a wrong
status - or unreachable, which does not fail it) and ``console`` (a success pattern in the last 200
console lines; skipped with a warning when the console log is unavailable). Polls until
``timeout_s`` (in model time in demo mode); the deterministic result passes when every non-skipped
check is ok. The console excerpt is kept in ``evidence`` (untrusted text).
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx

from .ai.redaction import redact
from .config import Settings
from .domain.models import AppProbe, ProbeResult, VerificationRecord, utcnow
from .planning.probes import is_database, probes_for
from .providers.base import ProviderError

log = logging.getLogger(__name__)
#: Nova states no amount of polling leaves (SDD §7.5): the verification fails at once.
TERMINAL_STATES = frozenset({"ERROR", "DELETED", "SOFT_DELETED"})
TCP_TIMEOUT_S = 5.0
CONSOLE_LINES = 200
CONSOLE_WARNING = "console log unavailable; console check skipped"
WINDOWS_CONSOLE = "Windows writes no boot messages to the serial console; console check skipped"
WINDOWS_NO_PORTS = "no TCP port probed for this Windows guest: set windows_tcp_ports (e.g. 3389)"
#: the reasons the rules ask review for a passed verification (SDD §7.5, ADV-18)
DATABASE_REVIEW = "database VM without an application probe: only its boot was verified"
UNREACHABLE_REVIEW = "application probe unreachable from the control plane: "


@dataclass
class VerificationResult:
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    #: one per application probe (SDD §7.5, ADV-18)
    probes: list[ProbeResult] = field(default_factory=list)

    def failed_checks(self) -> list[str]:
        return [c["name"] for c in self.checks if not c.get("ok") and not c.get("skipped")]

    def unreachable_targets(self) -> list[str]:
        """Probes the control plane could not reach: not a failure, a reason for review."""
        return [p.target for p in self.probes if p.result == "unreachable"]

    def summary(self) -> dict[str, Any]:
        """Persistable summary (no console text)."""
        return {
            "passed": self.passed,
            "checks": [dict(c) for c in self.checks],
            "probes": [p.model_dump(mode="json") for p in self.probes],
            "warnings": list(self.evidence.get("warnings", [])),
        }

    def record(self, at: datetime | None = None) -> VerificationRecord:
        """``Migration.verification`` (SDD §7.5): the checks and probe results, no console."""
        return VerificationRecord(
            at=at or utcnow(),
            passed=self.passed,
            checks=[dict(c) for c in self.checks],
            probes=list(self.probes),
            warnings=list(self.evidence.get("warnings", [])),
        )


@dataclass(frozen=True)
class ProbeAnswer:
    """One application probe's outcome; ``answered`` when the service answered (for latency)."""

    result: Literal["passed", "failed", "unreachable"]
    detail: str
    answered: bool


def _check(name: str, ok: bool, detail: str, skipped: bool = False) -> dict[str, Any]:
    # a detail may quote a provider's error: it reaches events any viewer reads (SDD §12, §13.3)
    out: dict[str, Any] = {"name": name, "ok": ok, "detail": redact(detail)}
    if skipped:
        out["skipped"] = True
    return out


async def _tcp_probe(host: str, port: int) -> tuple[bool, str]:
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TCP_TIMEOUT_S)
    except (OSError, TimeoutError, ValueError, OverflowError) as exc:
        # a port asyncio refuses (above 65535, negative) is closed, never an error (SDD §7.5)
        return False, f"{host}:{port} unreachable ({type(exc).__name__})"
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True, f"connected to {host}:{port} in {(time.monotonic() - started) * 1000:.0f} ms"


def _causes(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: BaseException | None = exc
    while seen is not None and seen not in chain:
        chain.append(seen)
        seen = seen.__cause__ or seen.__context__
    return chain


def _connect_failure(where: str, exc: BaseException) -> ProbeAnswer:
    """Refused or a TLS error: the guest answered, its service did not - failed. Anything else
    (timeout, no route) leaves the guest unreached from here - unreachable (SDD §7.5)."""
    chain = _causes(exc)
    if any(isinstance(e, ConnectionRefusedError) for e in chain):
        return ProbeAnswer("failed", f"{where} refused the connection", False)
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return ProbeAnswer("failed", f"{where}: TLS handshake failed", False)
    return ProbeAnswer("unreachable", f"{where} unreachable ({type(exc).__name__})", False)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


async def _app_probe(host: str, probe: AppProbe) -> ProbeAnswer:
    """An application probe from the control plane (SDD §7.5): a TCP connect, or GET ``path``."""
    where = f"{host}:{probe.port}"
    if probe.kind == "tcp":
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, probe.port), TCP_TIMEOUT_S
            )
        except (OSError, TimeoutError, ValueError, OverflowError) as exc:
            return _connect_failure(where, exc)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return ProbeAnswer("passed", f"connected to {where}", True)
    url = f"{probe.kind}://{_url_host(host)}:{probe.port}{probe.path}"
    try:
        # the certificate cannot name the address probed: only the status is read; and the probe
        # goes to the guest itself, never through a proxy from the environment (SDD §7.5)
        async with httpx.AsyncClient(
            verify=False,  # noqa: S501
            timeout=TCP_TIMEOUT_S,
            trust_env=False,
        ) as client:
            response = await client.get(url)
    except httpx.ConnectTimeout as exc:
        return _connect_failure(where, exc)
    except httpx.ConnectError as exc:
        return _connect_failure(where, exc)
    except httpx.TimeoutException:
        return ProbeAnswer("failed", f"{url}: no answer within {TCP_TIMEOUT_S:.0f} s", False)
    except httpx.HTTPError as exc:
        return ProbeAnswer("failed", f"{url}: {type(exc).__name__}", False)
    status = response.status_code
    if probe.expect_status is not None:
        ok = status == probe.expect_status
        detail = f"HTTP {status}" if ok else f"HTTP {status}, expected {probe.expect_status}"
    else:
        ok = 200 <= status < 400
        detail = f"HTTP {status}"
    return ProbeAnswer("passed" if ok else "failed", detail, True)


def _target(address: str, probe: AppProbe) -> str:
    if probe.kind == "tcp":
        return f"{address}:{probe.port}"
    return f"{probe.kind}://{_url_host(address)}:{probe.port}{probe.path}"


def review_reason(vm: Any, plan: Any, result: VerificationResult) -> str | None:
    """Why a passed verification needs a person (SDD §7.5): a probe the control plane could not
    reach, or a database VM that only booted; ``None`` when nothing asks for review."""
    unreachable = result.unreachable_targets()
    if unreachable:
        return UNREACHABLE_REVIEW + ", ".join(unreachable)
    if vm is not None and is_database(vm) and not probes_for(vm, plan):
        return DATABASE_REVIEW
    return None


def _pattern_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        try:
            if re.search(pattern, text):
                return pattern
        except re.error:
            if pattern in text:
                return pattern
    return None


class Verifier:
    def __init__(
        self,
        dst_provider: Any,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = 10.0,
        probe: Callable[[str, int], Awaitable[tuple[bool, str]]] = _tcp_probe,
        app_probe: Callable[[str, AppProbe], Awaitable[ProbeAnswer]] = _app_probe,
    ) -> None:
        self.dst = dst_provider
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        self.poll_s = poll_s
        self._probe = probe
        self._app_probe = app_probe
        # demo mode compresses time (SDD §7.4): the timeout and the polling run in model time
        self._scale = max(settings.demo_speed, 1.0) if settings.demo else 1.0

    async def verify(self, ctx: Any) -> VerificationResult:
        cfg = ctx.plan.verification
        deadline = self._clock() + max(0, cfg.timeout_s) / self._scale
        attempt = 0
        while True:
            attempt += 1
            result = await self._attempt(ctx)
            result.evidence["attempts"] = attempt
            # an unreachable probe is retried until the deadline, then does not fail (SDD §7.5)
            settled = result.passed and not result.unreachable_targets()
            if settled or result.evidence.get("terminal") or self._clock() >= deadline:
                return result
            await self._sleep(min(self.poll_s / self._scale, max(0.0, deadline - self._clock())))

    async def _run_app_probe(
        self, server_id: str, vm: Any, probe: AppProbe, address: str
    ) -> ProbeAnswer:
        # a destination that simulates its guests answers for them (demo, SDD §7.4)
        simulate = getattr(self.dst, "probe_service", None)
        if callable(simulate):
            answer: ProbeAnswer = await simulate(server_id, vm, probe, address)
            return answer
        return await self._app_probe(address, probe)

    async def _attempt(self, ctx: Any) -> VerificationResult:
        cfg = ctx.plan.verification
        server_id = ctx.migration.destination_server_id
        evidence: dict[str, Any] = {}
        if not server_id:
            return VerificationResult(
                False, [_check("server_active", False, "no destination server recorded")], evidence
            )
        try:
            server = await self.dst.get_server(server_id)
        except ProviderError as exc:
            return VerificationResult(False, [_check("server_active", False, str(exc))], evidence)

        checks = []
        status = str(server.get("status") or "UNKNOWN")
        checks.append(_check("server_active", status == "ACTIVE", f"status {status}"))
        if status.upper() in TERMINAL_STATES:
            # polling cannot turn an ERROR/DELETED server ACTIVE: fail fast, inside the window
            evidence["terminal"] = True
        ports = list(server.get("ports") or [])
        up = [p for p in ports if p.get("status") == "ACTIVE"]
        checks.append(
            _check(
                "ports_up",
                len(up) == len(ports),
                f"{len(up)}/{len(ports)} ports ACTIVE" if ports else "no ports on the server",
            )
        )

        # the guest family picks the profile (SDD §7.5)
        vm = getattr(ctx.migration, "vm", None)
        windows = vm is not None and vm.guest_os.family == "windows"
        probe_ports = list(cfg.windows_tcp_ports if windows else cfg.tcp_ports)
        warnings: list[str] = []
        if windows and not probe_ports:
            warnings.append(WINDOWS_NO_PORTS)

        key = "floating_ips" if cfg.probe_address == "floating" else "fixed_ips"
        address = next((ip for p in ports for ip in (p.get(key) or []) if ip), None)
        # a destination that simulates its guests answers their TCP checks too (demo, SDD §7.4)
        simulates = vm is not None and callable(getattr(self.dst, "probe_service", None))
        for port in probe_ports:
            if address is None:
                checks.append(_check(f"tcp:{port}", False, f"no {cfg.probe_address} address"))
                continue
            if simulates:
                tcp = AppProbe(kind="tcp", port=int(port))
                answer = await self._run_app_probe(server_id, vm, tcp, address)
                checks.append(_check(f"tcp:{port}", answer.result == "passed", answer.detail))
                continue
            ok, detail = await self._probe(address, int(port))
            checks.append(_check(f"tcp:{port}", ok, detail))

        # application probes (SDD §7.5, ADV-18): the VM's override, else the plan's
        probes: list[ProbeResult] = []
        app_probes = probes_for(vm, ctx.plan) if vm is not None else []
        for app in app_probes:
            name = f"app:{app.kind}:{app.port}"
            if address is None:
                detail = f"no {cfg.probe_address} address"
                checks.append(_check(name, False, detail))
                probes.append(
                    ProbeResult(
                        target=detail, kind=app.kind, result="failed", at=utcnow(), detail=detail
                    )
                )
                continue
            started = time.monotonic()
            answer = await self._run_app_probe(server_id, vm, app, address)
            latency = (time.monotonic() - started) * 1000 if answer.answered else None
            unreachable = answer.result == "unreachable"
            checks.append(
                _check(name, answer.result == "passed", answer.detail, skipped=unreachable)
            )
            probes.append(
                ProbeResult(
                    target=_target(address, app),
                    kind=app.kind,
                    result=answer.result,
                    latency_ms=round(latency, 1) if latency is not None else None,
                    at=utcnow(),
                    detail=redact(answer.detail),
                )
            )

        console: Any = None
        if windows:
            checks.append(_check("console", True, WINDOWS_CONSOLE, skipped=True))
        else:
            try:
                console = await self.dst.console_log(server_id, CONSOLE_LINES)
            except ProviderError:
                console = None
            if console is None:
                checks.append(_check("console", True, CONSOLE_WARNING, skipped=True))
                warnings.append(CONSOLE_WARNING)
        if warnings:
            evidence["warnings"] = warnings
        if console is not None:
            excerpt = "\n".join(str(console).splitlines()[-CONSOLE_LINES:])
            evidence["console"] = excerpt
            matched = _pattern_match(list(cfg.console_success_patterns), excerpt)
            checks.append(
                _check(
                    "console",
                    matched is not None,
                    f"matched {matched!r}"
                    if matched
                    else f"no success pattern in the last {CONSOLE_LINES} console lines",
                )
            )
        passed = all(c["ok"] for c in checks if not c.get("skipped"))
        return VerificationResult(passed, checks, evidence, probes)
