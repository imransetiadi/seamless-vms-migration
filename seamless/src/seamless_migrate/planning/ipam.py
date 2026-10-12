"""Neutron's IPAM rules for a port's fixed IPs (SDD §6.6, §7.3 step 0).

What Neutron 2023.1 refuses when a port is created with an address (``_get_subnet_for_fixed_ip``,
``check_subnet_ip``, ``_test_fixed_ips_for_port``): checked while the source still runs, since the
port is created only after it stops. The collection's destination check applies the same rules
(``server_port._address_refusal``).
"""

from __future__ import annotations

import ipaddress
from typing import Any

#: subnets whose addresses Neutron derives from the port's MAC
AUTO_ADDRESS_MODES = ("slaac", "dhcpv6-stateless")


def _attr(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def host_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, cidr: str) -> bool:
    """``check_subnet_ip`` for a port created with no owner, as Nova and Seamless create theirs:
    an IPv4 subnet's network address is never a port's, nor its broadcast address below a /31
    (netaddr gives a /31 or /32 none); an IPv6 subnet's first address is taken (SDD §6.6)."""
    net = ipaddress.ip_network(cidr, strict=False)
    if ip.version != net.version or ip not in net:
        return False
    if net.version == 4:
        return ip != net.network_address and (net.prefixlen >= 31 or ip != net.broadcast_address)
    return True


def auto_address(subnet: Any) -> bool:
    """Whether Neutron derives a port's address on ``subnet`` from its MAC: SLAAC or
    DHCPv6-stateless, by its address or RA mode."""
    return (
        _attr(subnet, "ipv6_address_mode") in AUTO_ADDRESS_MODES
        or _attr(subnet, "ipv6_ra_mode") in AUTO_ADDRESS_MODES
    )


def subnet_of(fixed_ip: dict[str, Any], subnets: list[Any]) -> Any | None:
    """The subnet ``fixed_ip`` goes to: the one its ``subnet_id`` names, else the first of the
    network's that holds its address, as Neutron picks it; None when none does."""
    if fixed_ip.get("subnet_id"):
        return next((s for s in subnets if _attr(s, "id") == fixed_ip["subnet_id"]), None)
    try:
        ip = ipaddress.ip_address(str(fixed_ip.get("ip_address")))
    except ValueError:
        return None
    return next((s for s in subnets if host_address(ip, str(_attr(s, "cidr")))), None)


def requested_ips(fixed_ips: list[dict[str, Any]], subnets: list[Any]) -> list[dict[str, Any]]:
    """The fixed IPs a port is created with through Neutron: all but those on a SLAAC or
    DHCPv6-stateless subnet, whose EUI-64 address of the MAC Neutron adds itself - sent, it is
    allocated twice, or refused (SDD §6.6, §7.3)."""
    out = []
    for fixed_ip in fixed_ips:
        subnet = subnet_of(fixed_ip, subnets)
        if subnet is None or not auto_address(subnet):
            out.append(fixed_ip)
    return out


def eui64(cidr: str, mac: str) -> ipaddress.IPv6Address:
    """The address SLAAC gives ``mac`` on ``cidr`` (oslo's ``get_ipv6_addr_by_EUI64``): the prefix
    with the MAC's modified EUI-64 interface identifier."""
    octets = [int(part, 16) for part in mac.split(":")]
    interface = [*octets[:3], 0xFF, 0xFE, *octets[3:]]
    interface[0] ^= 0x02
    prefix = ipaddress.ip_network(cidr, strict=False).network_address
    return ipaddress.IPv6Address(int(prefix) + int.from_bytes(bytes(interface), "big"))


def address_refusal(
    address: str, subnets: list[Any], *, neutron: bool, mac: str | None
) -> str | None:
    """Why Neutron refuses ``address`` alone on a port of the network whose subnets are
    ``subnets``, or None. The port is created through Neutron with ``mac`` when ``neutron``, else
    by Nova with a new MAC."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "is not an IP address"
    # an address alone: Neutron takes the first subnet of the network that holds it
    subnet = next((s for s in subnets if host_address(ip, str(_attr(s, "cidr")))), None)
    if subnet is None:
        return "is no host address of any subnet of the port's network"
    cidr = str(_attr(subnet, "cidr"))
    if auto_address(subnet):
        # Neutron takes an explicit address there only if it derives the same one
        if not neutron or not mac:
            return (
                f"lies on {cidr}, a SLAAC or DHCPv6-stateless subnet: through Nova the port gets a "
                "new MAC, and Neutron takes no explicit address there"
            )
        expected = eui64(cidr, mac)
        if ip != expected:
            return (
                f"is not {expected}, the address the SLAAC or DHCPv6-stateless subnet {cidr} "
                f"gives MAC {mac}"
            )
    return None
