"""The DNS swap (SDD §8.4, §23.5, plan task N7): which addresses a VM's record moves between, and
the two back ends that move it with a compare-and-swap - Designate, through the destination
provider's credential, and an RFC 2136 server, through a TSIG-signed UPDATE (the `dns` extra)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .config import Settings
from .domain.models import DnsTarget, Migration, Nic, Plan, VMRef
from .providers.base import ProviderError, missing_dependency
from .security.secrets import SecretNotFound, read_secret

log = logging.getLogger(__name__)

#: Designate writes asynchronously: the swap reads the recordset this often, this long (SDD §8.4)
DESIGNATE_POLL_S = 2.0
DESIGNATE_ACTIVE_S = 60.0
#: one RFC 2136 exchange's bound
RFC2136_TIMEOUT_S = 10.0
#: the records a validation reads at once (SDD §8.4, §9.3)
TTL_READS = 8

Outcome = Literal["swapped", "conflict"]


class DnsSwapError(Exception):
    """A swap that failed (SDD §8.4): never a TSIG key in its message."""


@dataclass
class Record:
    """A record as a back end read it: its addresses, canonical and sorted, and its TTL."""

    values: list[str]
    ttl: int | None


def _canonical(address: str) -> str:
    try:
        return ipaddress.ip_address(address).compressed
    except ValueError:
        return address


def _version(address: str) -> int | None:
    try:
        return ipaddress.ip_address(address).version
    except ValueError:
        return None


def _fqdn(zone: str) -> str:
    return zone if zone.endswith(".") else f"{zone}."


def record_name(target: DnsTarget) -> str:
    """The record's fully qualified name (SDD §8.4): the target's record in its zone - a record
    written fully qualified (a trailing dot) as it is."""
    if target.record.endswith("."):
        return target.record
    return f"{target.record}.{_fqdn(target.zone)}"


def rdtype_for(address: str) -> Literal["A", "AAAA"]:
    return "AAAA" if _version(address) == 6 else "A"


def source_address(vm: VMRef) -> tuple[Nic, str, bool]:
    """The address the VM's record holds before the swap (SDD §8.4): its first floating IP when it
    has one, else its first fixed IP - with its NIC, and whether it is floating. DnsSwapError when
    the VM has no address."""
    for nic in vm.nics:
        if nic.floating_ips:
            return nic, _canonical(nic.floating_ips[0]), True
    for nic in vm.nics:
        if nic.fixed_ips:
            return nic, _canonical(nic.fixed_ips[0]), False
    raise DnsSwapError(f"{vm.name} has no address for its DNS record")


def swap_addresses(m: Migration, plan: Plan, server: dict[str, Any] | None) -> tuple[str, str]:
    """The VM's address at the source and the one taking its place (SDD §8.4): the new server's
    floating IP on the port holding the NIC's address when the old one was floating - ``server``
    as get_server reads it -, else the NIC's first address of the old one's family at the
    destination: its override's, else its own. DnsSwapError when there is none."""
    nic, old, floating = source_address(m.vm)
    targets = [_canonical(a) for a in plan.nic_target(m.vm.source_id, nic)[1]]
    candidates = targets
    if floating:
        ports = (server or {}).get("ports") or []
        held = (
            port.get("floating_ips") or []
            for port in ports
            if {_canonical(a) for a in port.get("fixed_ips") or []} & set(targets)
        )
        candidates = next(held, [])
    version = _version(old)
    for address in candidates:
        if version is not None and _version(address) == version:
            return old, _canonical(address)
    if floating:
        raise DnsSwapError(
            f"no floating IP holds the place of {old}: the new server's port of "
            f"{', '.join(targets) or 'the NIC'} has none"
        )
    raise DnsSwapError(f"{m.vm.name} has no IPv{version} address in place of {old}")


class Backend(Protocol):
    """A DNS back end of the swap (SDD §8.4): Designate through the destination provider, or an
    RFC 2136 server with its TSIG key."""

    async def read(self, name: str, rdtype: str) -> Record | None:
        """The record ``name`` of ``rdtype`` as the server holds it; None when it holds none."""

    async def replace(
        self, name: str, rdtype: str, old: str, new: str
    ) -> tuple[Outcome, list[str]]:
        """Compare-and-swap ``old`` to ``new``: ("swapped", []), or ("conflict", the addresses the
        record holds) when it holds anything but ``old`` - or ``new``, a swap already made."""


class DesignateBackend:
    """A target's Designate zone, through the destination provider's credential (SDD §8.4, §10)."""

    def __init__(
        self,
        target: DnsTarget,
        destination: Any,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = DESIGNATE_POLL_S,
        timeout_s: float = DESIGNATE_ACTIVE_S,
    ) -> None:
        self.zone = _fqdn(target.zone)
        self.destination = destination
        self.sleep, self.clock = sleep, clock
        self.poll_s, self.timeout_s = poll_s, timeout_s

    async def read(self, name: str, rdtype: str) -> Record | None:
        found = await self.destination.dns_recordset(self.zone, name, rdtype)
        if found is None:
            return None
        return Record(sorted(_canonical(r) for r in found.get("records") or []), found.get("ttl"))

    async def replace(
        self, name: str, rdtype: str, old: str, new: str
    ) -> tuple[Outcome, list[str]]:
        """Compare-and-swap ``old`` to ``new``: ("swapped", []) once Designate made it ACTIVE -
        a recordset that holds ``new`` already is waited for alone -, ("conflict", what it holds)
        when it holds anything else."""
        found = await self.destination.dns_recordset(self.zone, name, rdtype)
        held = sorted(_canonical(r) for r in (found or {}).get("records") or [])
        if found is None or held not in ([old], [new]):
            return "conflict", held
        if held == [old]:
            await self.destination.update_dns_recordset(found["zone_id"], found["id"], [new])
        elif found.get("status") == "ACTIVE":
            return "swapped", []
        deadline = self.clock() + self.timeout_s
        while True:
            current = await self.destination.dns_recordset(self.zone, name, rdtype) or {}
            status = current.get("status")
            if status == "ACTIVE":
                return "swapped", []
            if status == "ERROR":
                raise DnsSwapError(f"Designate set the recordset {name} to ERROR")
            if self.clock() >= deadline:
                raise DnsSwapError(
                    f"the recordset {name} was not ACTIVE after {self.timeout_s:g} s ({status})"
                )
            await self.sleep(self.poll_s)


class Rfc2136Backend:
    """A target's RFC 2136 server, over TCP, each message signed with its TSIG key (SDD §8.4,
    §13.3); the key is read when the back end is made and reaches no message or log."""

    def __init__(
        self, target: DnsTarget, settings: Settings, *, timeout_s: float = RFC2136_TIMEOUT_S
    ) -> None:
        try:
            import dns.name
            import dns.tsig
            import dns.tsigkeyring
        except ImportError:
            raise missing_dependency("dns", "dnspython") from None
        self.zone = _fqdn(target.zone)
        host, _, port = (target.server or "").rpartition(":")
        if not host or not port.isdigit():
            host, port = target.server or "", "53"
        self.host, self.port = host.strip("[]"), int(port)
        self.timeout_s = timeout_s
        try:
            values = read_secret(str(target.tsig_secret), settings)
        except SecretNotFound:
            values = {}
        if not values.get("name") or not values.get("secret"):
            raise DnsSwapError("the TSIG secret holds no key (its keys name and secret)")
        algorithm = values.get("algorithm") or "hmac-sha256"
        try:
            self.keyname = dns.name.from_text(values["name"])
            self.keyring = dns.tsigkeyring.from_text({values["name"]: values["secret"]})
            self.algorithm = dns.name.from_text(algorithm)
        except Exception as exc:
            # never the exception's text: it may quote the secret
            raise DnsSwapError(
                f"the TSIG secret's key cannot be used ({type(exc).__name__})"
            ) from None

    @property
    def where(self) -> str:
        return f"{self.host}:{self.port}"

    async def _exchange(self, message: Any) -> Any:
        import dns.asyncquery

        try:
            return await dns.asyncquery.tcp(
                message, self.host, port=self.port, timeout=self.timeout_s
            )
        except Exception as exc:
            raise DnsSwapError(f"{self.where}: no answer ({type(exc).__name__})") from None

    async def read(self, name: str, rdtype: str) -> Record | None:
        import dns.message
        import dns.rcode
        import dns.rdataclass
        import dns.rdatatype

        query = dns.message.make_query(name, rdtype)
        query.use_tsig(self.keyring, keyname=self.keyname, algorithm=self.algorithm)
        answer = await self._exchange(query)
        if answer.rcode() not in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
            raise DnsSwapError(
                f"{self.where} refused the query ({dns.rcode.to_text(answer.rcode())})"
            )
        rrset = answer.get_rrset(
            answer.answer,
            dns.name.from_text(name),
            dns.rdataclass.IN,
            dns.rdatatype.from_text(rdtype),
        )
        if rrset is None:
            return None
        return Record(sorted(_canonical(r.to_text()) for r in rrset), rrset.ttl)

    async def replace(
        self, name: str, rdtype: str, old: str, new: str
    ) -> tuple[Outcome, list[str]]:
        """One UPDATE whose prerequisite is the RRset holding exactly ``old`` (RFC 2136 §2.4.2):
        deleted, ``new`` added with the TTL read just before; NXRRSET is read again - the new
        address already there is a swap a restart interrupted, anything else a conflict."""
        import dns.rcode
        import dns.rdataset
        import dns.update

        record = await self.read(name, rdtype)
        held = record.values if record is not None else []
        if held == [new]:
            return "swapped", []
        if record is None or held != [old]:
            return "conflict", held
        update = dns.update.UpdateMessage(
            self.zone, keyring=self.keyring, keyname=self.keyname, keyalgorithm=self.algorithm
        )
        ttl = record.ttl if record.ttl is not None else 300
        update.present(name, dns.rdataset.from_text("IN", rdtype, ttl, old))
        update.delete(name, rdtype)
        update.add(name, ttl, rdtype, new)
        answer = await self._exchange(update)
        rcode = answer.rcode()
        if rcode == dns.rcode.NOERROR:
            return "swapped", []
        if rcode == dns.rcode.NXRRSET:
            again = await self.read(name, rdtype)
            values = again.values if again is not None else []
            return ("swapped", []) if values == [new] else ("conflict", values)
        raise DnsSwapError(f"{self.where} refused the update ({dns.rcode.to_text(rcode)})")


def backend_for(target: DnsTarget, destination: Any, settings: Settings) -> Backend:
    """The back end of a target (SDD §8.4): Designate through ``destination``, the destination
    provider's implementation, or the target's RFC 2136 server."""
    if target.kind == "designate":
        return DesignateBackend(target, destination)
    return Rfc2136Backend(target, settings)


def describe(error: BaseException) -> str:
    """A failure's text for its event and the migration's error (SDD §8.4): a swap's own message,
    a provider's, else the failure's kind - never anything else a library may have put in it."""
    if isinstance(error, (DnsSwapError, ProviderError)):
        return str(error)
    return type(error).__name__


async def record_ttl(
    target: DnsTarget, vm: VMRef, destination: Any, settings: Settings
) -> int | None:
    """The TTL of the VM's record as a validation reads it (SDD §8.4, §9.3): None when it cannot
    be read - not judged -, the failure's kind logged, never the record's name."""
    try:
        _, old, _ = source_address(vm)
        record = await backend_for(target, destination, settings).read(
            record_name(target), rdtype_for(old)
        )
    except Exception as exc:
        log.info("a DNS target's record was not read: %s", type(exc).__name__)
        return None
    return record.ttl if record is not None else None


async def record_ttls(
    plan: Plan, vms: Iterable[VMRef], destination: Any, settings: Settings
) -> dict[str, int | None]:
    """The TTL of each DNS target's record, by VM id, TTL_READS read at once (SDD §8.4, §9.3):
    a server that does not answer holds a validation up one read's bound per TTL_READS VMs."""
    targets = plan.network_cutover.dns_targets
    wanted = [vm for vm in vms if vm.source_id in targets]
    gate = asyncio.Semaphore(TTL_READS)

    async def one(vm: VMRef) -> int | None:
        async with gate:
            return await record_ttl(targets[vm.source_id], vm, destination, settings)

    ttls = await asyncio.gather(*(one(vm) for vm in wanted))
    return {vm.source_id: ttl for vm, ttl in zip(wanted, ttls, strict=True)}
