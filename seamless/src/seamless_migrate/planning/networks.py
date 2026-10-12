"""The RHOSO network a NIC's network name resolves to (SDD §7.3 step 0, §9.3, §7.2), and whether
the destination credential may put a port there (SDD §6.6).

One rule for every strategy: the destination credential's own network of that name, else the only
network of that name it sees. Preflight resolves names with it over
``DestinationInventory.network_details``; the cold and warm export rewrite, the handover, and the
VMware migration kit's network_map (``kit_network_value``), before each run.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Collection, Iterable, Sequence
from typing import Any

from ..domain.models import Nic
from .ipam import address_refusal

# the roles Neutron's policy (2023.1 create_port:fixed_ips:ip_address, :mac_address,
# :port_security_enabled) lets choose a port's address, MAC or port security on another project's
# network; the collection's server_port.PORT_ROLES (SDD §6.6)
PORT_ROLES = ("admin", "advsvc")


def port_refusal(network: dict[str, Any], roles: Collection[str] | None) -> str | None:
    """Why the destination credential may not put a port on ``network`` (a ``network_details``
    entry), or None (SDD §6.6). Another project's network takes it only when shared with the
    credential's project or external - an admin sees every project's networks - and only from a
    token with one of ``PORT_ROLES``: every recreated port asks for its address or MAC. An
    external network that is not shared, the credential's own included, also needs the admin
    role: Nova attaches no other to a server. ``roles`` None (not reported) leaves the role
    halves unjudged."""
    if not network.get("own"):
        if not (network.get("shared") or network.get("external")):
            return "it is neither shared with the credential's project nor external"
        if roles is not None and not set(roles) & set(PORT_ROLES):
            return (
                "Neutron lets only the admin or advsvc role, or that project, choose a port's "
                "address or MAC there, and the credential's token has neither"
            )
    # Nova's network:attach_external_network, admin only by default (2023.1)
    if network.get("external") and not network.get("shared"):
        if roles is not None and "admin" not in roles:
            return (
                "it is external and not shared, and Nova lets only the admin role attach such a "
                "network to a server; the credential's token lacks it"
            )
    return None


def resolve_network(
    networks: Sequence[dict[str, Any]], name: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The network ``name`` resolves to among ``networks`` (``network_details`` entries), or None
    and why the rule cannot pick one; (None, None) when no network carries the name."""
    own = [n for n in networks if n.get("name") == name and n.get("own")]
    if len(own) == 1:
        return own[0], None
    if own:
        return None, f"{len(own)} networks of the credential's project are named {name!r}"
    seen = [n for n in networks if n.get("name") == name]
    if len(seen) == 1:
        return seen[0], None
    if not seen:
        return None, None
    projects = ", ".join(sorted({str(n.get("project_id")) for n in seen}))
    return None, (
        f"{len(seen)} networks of other projects are named {name!r} ({projects}), "
        "none of the credential's project"
    )


def kit_network_key(nic: Nic) -> str:
    """What the VMware migration kit maps ``nic`` by in its network_map (SDD §7.2): the NIC's
    ``network_key``, else (a VM inventoried before it was recorded) its network's name."""
    return nic.network_key or nic.network


def kit_ipv4(addresses: Iterable[str]) -> list[str]:
    """The guest addresses the migration kit sends Neutron, as its FixedIPsForNeutron: IPv4 ones
    only, an IPv4-mapped IPv6 address as its IPv4 (SDD §7.2)."""
    out = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is None:
                continue
            ip = ip.ipv4_mapped
        out.append(str(ip))
    return out


def kit_subnet(
    nic: Nic, subnets: Sequence[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """The subnet of its network the VMware migration kit gives ``nic``'s port (SDD §7.2), among
    the network's own ``subnets``: the one that holds every IPv4 guest address of the NIC as a
    host address - the kit sends them all with that one subnet, else with the network's first -
    or any of them (an IPv4 one first) when the NIC has none, or None when the network has no
    subnet; else (None, why) the kit's port could not take the addresses."""
    addresses = kit_ipv4(nic.fixed_ips)
    v4 = [s for s in subnets if ipaddress.ip_network(str(s.get("cidr")), strict=False).version == 4]
    if not addresses:
        chosen = v4 or list(subnets)
        return (chosen[0] if chosen else None), None
    holders = [
        s
        for s in v4
        if all(address_refusal(a, [s], neutron=True, mac=nic.mac) is None for a in addresses)
    ]
    if holders:
        return holders[0], None
    # say what is wrong: an address no subnet takes, else addresses spread over several subnets
    for address in addresses:
        why = address_refusal(address, list(subnets), neutron=True, mac=nic.mac)
        if why:
            return None, f"{address} {why}"
    return None, (
        f"{', '.join(addresses)} lie in different subnets of the port's network: the migration kit "
        "gives a NIC's IPv4 addresses one subnet"
    )


def kit_network_value(network: dict[str, Any], subnet: dict[str, Any] | None) -> str:
    """The migration kit's network_map value (SDD §7.2): "{network id},{subnet id}", the
    network's id alone when it has no subnet; the kit looks the network up by its id first."""
    return f"{network['id']},{subnet['id']}" if subnet is not None else str(network["id"])
