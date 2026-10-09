"""Post-cutover verification (SDD §7.5).

Checks: ``server_active`` (destination server ``ACTIVE``), ``ports_up`` (all ports ``ACTIVE``),
``tcp:<port>`` for every configured port (5 s connect timeout from the control plane) and
``console`` (a success pattern in the last 200 console lines; skipped with a warning when the
console log is unavailable). Polls until ``timeout_s``; the deterministic result passes when every
non-skipped check is ok. The console excerpt is kept in ``evidence`` (untrusted text).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .providers.base import ProviderError

log = logging.getLogger(__name__)
#: Nova states no amount of polling leaves (SDD §7.5): the verification fails at once.
TERMINAL_STATES = frozenset({"ERROR", "DELETED", "SOFT_DELETED"})
TCP_TIMEOUT_S = 5.0
CONSOLE_LINES = 200
CONSOLE_WARNING = "console log unavailable; console check skipped"
WINDOWS_CONSOLE = "Windows writes no boot messages to the serial console; console check skipped"
WINDOWS_NO_PORTS = "no TCP port probed for this Windows guest: set windows_tcp_ports (e.g. 3389)"


@dataclass
class VerificationResult:
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def failed_checks(self) -> list[str]:
        return [c["name"] for c in self.checks if not c.get("ok") and not c.get("skipped")]

    def summary(self) -> dict[str, Any]:
        """Persistable summary (no console text)."""
        return {
            "passed": self.passed,
            "checks": [dict(c) for c in self.checks],
            "warnings": list(self.evidence.get("warnings", [])),
        }


def _check(name: str, ok: bool, detail: str, skipped: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "ok": ok, "detail": detail}
    if skipped:
        out["skipped"] = True
    return out


async def _tcp_probe(host: str, port: int) -> tuple[bool, str]:
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TCP_TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        return False, f"{host}:{port} unreachable ({type(exc).__name__})"
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True, f"connected to {host}:{port} in {(time.monotonic() - started) * 1000:.0f} ms"


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
    ) -> None:
        self.dst = dst_provider
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        self.poll_s = poll_s
        self._probe = probe

    async def verify(self, ctx: Any) -> VerificationResult:
        cfg = ctx.plan.verification
        deadline = self._clock() + max(0, cfg.timeout_s)
        attempt = 0
        while True:
            attempt += 1
            result = await self._attempt(ctx)
            result.evidence["attempts"] = attempt
            if result.passed or result.evidence.get("terminal") or self._clock() >= deadline:
                return result
            await self._sleep(min(self.poll_s, max(0.0, deadline - self._clock())))

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
        for port in probe_ports:
            if address is None:
                checks.append(_check(f"tcp:{port}", False, f"no {cfg.probe_address} address"))
                continue
            ok, detail = await self._probe(address, int(port))
            checks.append(_check(f"tcp:{port}", ok, detail))

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
        return VerificationResult(passed, checks, evidence)
