"""Where the control plane may connect on an admin's word (SDD §13.5, pentest 2026-11-19 P3,
P4.5): a provider's endpoint, the endpoints its Keystone catalog names, a conversion host's
address, a webhook hook's URL and a notification channel's.

An admin names these, and a check or a send connects to them from inside the deployment: without
a guard the answer's kind and timing tell which internal addresses and ports listen. One guard
judges every destination **after its name is resolved**:

* never a loopback, link-local, unspecified, multicast or reserved address - the node's metadata
  service (``169.254.169.254``) among them -, in any spelling: an IPv4-mapped IPv6 address, a
  decimal or hexadecimal IPv4 address, a name that resolves to one;
* never an address inside ``SEAMLESS_EGRESS_DENY_CIDRS`` - the cluster's pod and service networks;
* never a name of the cluster's own DNS (``*.svc``, ``*.cluster.local``) or ``localhost``;
* a private address (RFC 1918, RFC 6598, an IPv6 unique local one) is allowed - an on-premises
  OpenStack lives there - unless ``SEAMLESS_EGRESS_ALLOW_PRIVATE_CIDRS`` is set: then only inside
  one of its networks.

The guard judges the addresses a name resolves to at that moment; the connection resolves it
again. The NetworkPolicy of the deployment (Security.md §8) stays the second layer.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from urllib.parse import urlsplit

from ..config import Settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
#: ``resolver(host) -> addresses``: what the guard resolves a name with (tests give their own)
Resolver = Callable[[str], Sequence[str]]

#: the message every refusal starts with (SDD §13.5): what a route answers and a check reports
NOT_ALLOWED = "destination not allowed"
#: names of the cluster's own DNS, and the machine's own name (SDD §13.5)
_DENIED_NAMES = ("localhost",)
_DENIED_SUFFIXES = (".localhost", ".svc", ".cluster.local")
#: always refused beside what ``ipaddress`` calls loopback, link-local, multicast, reserved or
#: unspecified: "this network" and the IPv4-mapped, 6to4, Teredo and NAT64 forms, which carry an
#: IPv4 address the guard judges by itself
_DENIED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("0.0.0.0/8", "255.255.255.255/32", "192.0.0.0/24", "169.254.0.0/16", "fe80::/10")
)
#: the private ranges ``SEAMLESS_EGRESS_ALLOW_PRIVATE_CIDRS`` narrows
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
)
#: how long a name's resolution may take (seconds) before the destination is refused
RESOLVE_TIMEOUT_S = 5.0


class DestinationNotAllowed(ValueError):
    """The control plane does not connect there (SDD §13.5). Its text names the destination's
    host and why - never a path, a query or a credential."""


def _networks(values: Iterable[str]) -> list[IPNetwork]:
    return [ipaddress.ip_network(v.strip(), strict=False) for v in values if v.strip()]


def _embedded(address: IPAddress) -> IPAddress:
    """The IPv4 address an IPv6 address carries (mapped, 6to4, Teredo, NAT64), else itself."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address.sixtofour is not None:
            return address.sixtofour
        if address.teredo is not None:
            return address.teredo[1]
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def refused_address(address: str | IPAddress, settings: Settings) -> str | None:
    """Why the control plane does not connect to ``address``, else None (SDD §13.5)."""
    ip = _embedded(ipaddress.ip_address(address) if isinstance(address, str) else address)
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or any(ip in net for net in _DENIED_NETWORKS)
    ):
        return "a loopback, link-local or reserved address"
    if any(ip in net for net in _networks(settings.egress_deny_cidrs)):
        return "an address of the deployment's own networks (SEAMLESS_EGRESS_DENY_CIDRS)"
    allowed = _networks(settings.egress_allow_private_cidrs)
    if allowed and any(ip in net for net in _PRIVATE_NETWORKS):
        if not any(ip in net for net in allowed):
            return "a private address outside SEAMLESS_EGRESS_ALLOW_PRIVATE_CIDRS"
    return None


def literal_address(host: str) -> IPAddress | None:
    """``host`` as the address a connection would take it for, else None - a name. ``inet_aton``
    reads what a resolver reads: ``2130706433``, ``0x7f.1``, ``127.1`` are 127.0.0.1."""
    text = host.strip("[]")
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(text))
    except (OSError, ValueError):
        return None


def refused_host(host: str, settings: Settings) -> str | None:
    """Why ``host`` is refused before any resolution, else None: a literal address the guard
    refuses, or a name of the cluster's own DNS (SDD §13.5). What a route checks as it stores a
    URL - a name's addresses are judged when the control plane connects."""
    name = host.strip().rstrip(".").lower()
    if not name:
        return "no host"
    address = literal_address(name)
    if address is not None:
        return refused_address(address, settings)
    if name in _DENIED_NAMES or name.endswith(_DENIED_SUFFIXES):
        return "a name of the deployment's own DNS"
    return None


def _host_of(destination: str) -> str:
    """The host of a URL, or of a bare ``host`` / ``host:port`` (a conversion host's address)."""
    try:
        parts = urlsplit(destination if "://" in destination else f"//{destination}")
        return parts.hostname or ""
    except ValueError:
        return ""


def check_stored(destination: str, settings: Settings, field: str = "url") -> None:
    """Refuse a destination a route is about to store (SDD §13.5): no resolution, so a receiver
    whose name does not resolve yet can be registered."""
    host = _host_of(destination)
    why = refused_host(host, settings)
    if why is not None:
        raise DestinationNotAllowed(f"{field}: {NOT_ALLOWED}: {host or 'no host'} is {why}")


def system_resolver(host: str) -> list[str]:
    """Every address ``host`` resolves to (``getaddrinfo``)."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def check_destination(
    destination: str,
    settings: Settings,
    *,
    field: str = "destination",
    resolver: Resolver | None = None,
) -> list[str]:
    """Refuse ``destination`` - a URL or a host - unless every address its host resolves to is
    one the control plane may connect to (SDD §13.5); returns those addresses. A name that does
    not resolve is refused too: nothing would be reached, and the answer takes no longer."""
    host = _host_of(destination)
    why = refused_host(host, settings)
    if why is not None:
        raise DestinationNotAllowed(f"{field}: {NOT_ALLOWED}: {host or 'no host'} is {why}")
    literal = literal_address(host)
    if literal is not None:
        return [str(literal)]
    try:
        addresses = list((resolver or system_resolver)(host))
    except OSError:
        addresses = []
    if not addresses:
        raise DestinationNotAllowed(f"{field}: {NOT_ALLOWED}: {host} does not resolve")
    for address in addresses:
        why = refused_address(address.split("%", 1)[0], settings)
        if why is not None:
            raise DestinationNotAllowed(f"{field}: {NOT_ALLOWED}: {host} resolves to {why}")
    return addresses


async def check_destination_async(
    destination: str,
    settings: Settings,
    *,
    field: str = "destination",
    resolver: Resolver | None = None,
) -> list[str]:
    """``check_destination`` off the event loop, the resolution bounded by RESOLVE_TIMEOUT_S."""

    def run() -> list[str]:
        return check_destination(destination, settings, field=field, resolver=resolver)

    try:
        return await asyncio.wait_for(asyncio.to_thread(run), RESOLVE_TIMEOUT_S)
    except TimeoutError:
        host = _host_of(destination)
        raise DestinationNotAllowed(
            f"{field}: {NOT_ALLOWED}: {host} did not resolve in {RESOLVE_TIMEOUT_S:g} s"
        ) from None
