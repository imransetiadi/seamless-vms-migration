"""The Octavia member helper (SDD §8.5, §23.7, plan task N9): the VM's memberships of a plan's
mapped pools, the address that joins the destination pool, Octavia's pace - one change per load
balancer at a time -, and what an OVN pool lacks of the source pool it stands for."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

from .domain.models import Migration, Plan, VMRef
from .providers.base import ProviderConflict, ProviderError

#: Octavia's pace (SDD §8.5): a refused change is sent again, a member read, this often, this long
LB_POLL_S = 2.0
LB_ACTIVE_S = 60.0
#: what the OVN provider serves (SDD §9.3, §23.7: its driver documentation) - L4 alone
OVN_PROTOCOLS = ("TCP", "UDP", "SCTP")
OVN_ALGORITHM = "SOURCE_IP_PORT"
OVN_MONITORS = ("TCP", "UDP-CONNECT", "SCTP")
#: the Octavia values read with "an"
_AN = frozenset({"HTTP", "HTTPS", "SCTP"})

T = TypeVar("T")


class LbHelperError(Exception):
    """A step of the member helper that failed (SDD §8.5)."""


def canonical(address: str) -> str:
    """An address as Neutron writes it, else as given."""
    try:
        return ipaddress.ip_address(address).compressed
    except ValueError:
        return address


def _version(address: str) -> int | None:
    try:
        return ipaddress.ip_address(address).version
    except ValueError:
        return None


def vm_addresses(vm: VMRef) -> set[str]:
    """The VM's fixed IPs, canonical: what a member of a mapped pool must be (SDD §8.5)."""
    return {canonical(a) for nic in vm.nics for a in nic.fixed_ips}


def join_address(m: Migration, plan: Plan, address: str) -> str:
    """The address that takes ``address``'s place in the destination pool (SDD §8.5, §8.4's rule):
    the first address of its family the NIC holding it has at the destination - its override's,
    else its own. LbHelperError when there is none."""
    version = _version(address)
    for nic in m.vm.nics:
        if address not in {canonical(a) for a in nic.fixed_ips}:
            continue
        for target in plan.nic_target(m.vm.source_id, nic)[1]:
            if version is not None and _version(target) == version:
                return canonical(target)
        raise LbHelperError(f"{m.vm.name} has no IPv{version} address in place of {address}")
    raise LbHelperError(f"{address} is no address of {m.vm.name}")


def _article(value: str) -> str:
    return "an" if value in _AN else "a"


def lacks(source: Mapping[str, Any], destination: Mapping[str, Any]) -> list[str]:
    """What the source pool uses that the destination pool's provider lacks (SDD §9.3): the OVN
    provider's L4 limits; another provider is not judged."""
    if str(destination.get("provider") or "").lower() != "ovn":
        return []
    out: list[str] = []
    protocol = source.get("protocol")
    if protocol and protocol not in OVN_PROTOCOLS:
        out.append(f"the {protocol} protocol")
    algorithm = source.get("lb_algorithm")
    if algorithm and algorithm != OVN_ALGORITHM:
        out.append(f"the {algorithm} algorithm")
    if source.get("persistence"):
        out.append(f"{source['persistence']} session persistence")
    monitor = source.get("monitor")
    if monitor and monitor not in OVN_MONITORS:
        out.append(f"{_article(monitor)} {monitor} health monitor")
    for listener in source.get("listeners") or []:
        if listener not in OVN_PROTOCOLS:
            tls = " (TLS terminated at the load balancer)" if listener == "TERMINATED_HTTPS" else ""
            out.append(f"{_article(listener)} {listener} listener{tls}")
    return list(dict.fromkeys(out))


def describe(error: BaseException) -> str:
    """A failure's text for its event and the migration's error (SDD §8.5): the helper's own
    message, a provider's, else the failure's kind - never what a library put in it."""
    if isinstance(error, (LbHelperError, ProviderError)):
        return str(error)
    return type(error).__name__


class Pace:
    """Octavia's pace (SDD §8.5): a load balancer takes one change at a time and refuses another
    (409) while it is PENDING_*, so a change is sent again, and a member read until ACTIVE, every
    ``poll_s`` for at most ``timeout_s`` each."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = LB_POLL_S,
        timeout_s: float = LB_ACTIVE_S,
    ) -> None:
        self.sleep, self.clock = sleep, clock
        self.poll_s, self.timeout_s = poll_s, timeout_s

    async def change(self, call: Callable[[], Awaitable[T]]) -> T:
        """``call``'s answer, sent again while Octavia refuses it as a conflict; the conflict once
        the bound passed."""
        deadline = self.clock() + self.timeout_s
        while True:
            try:
                return await call()
            except ProviderConflict:
                if self.clock() >= deadline:
                    raise
                await self.sleep(self.poll_s)

    async def active(
        self, read: Callable[[], Awaitable[Mapping[str, Any] | None]], what: str
    ) -> None:
        """Until ``read`` - the member - is ACTIVE; LbHelperError on ERROR, a member gone, or the
        bound."""
        deadline = self.clock() + self.timeout_s
        while True:
            found = await read()
            status = (found or {}).get("provisioning_status")
            if status == "ACTIVE":
                return
            if found is None:
                raise LbHelperError(f"{what} is gone")
            if status == "ERROR":
                raise LbHelperError(f"Octavia set {what} to ERROR")
            if self.clock() >= deadline:
                raise LbHelperError(f"{what} was not ACTIVE after {self.timeout_s:g} s ({status})")
            await self.sleep(self.poll_s)


async def member_of(provider: Any, pool: str, member: str) -> dict[str, Any] | None:
    """The pool's member by its id, as ``pool_members`` lists it; None when it has none."""
    return next((m for m in await provider.pool_members(pool) if m.get("id") == member), None)
