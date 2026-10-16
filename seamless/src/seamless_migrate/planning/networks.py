"""The RHOSO network a NIC's network name resolves to (SDD §7.3 step 0, §9.3, §7.2), and whether
the destination credential may put a port there (SDD §6.6).

One rule for every strategy: the destination credential's own network of that name, else the only
network of that name it sees. Preflight resolves names with it over
``DestinationInventory.network_details``; the cold and warm export rewrite, the handover, and the
VMware migration kit's network_map (``kit_network_value``), before each run.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

from ..domain.models import Nic
from .ipam import address_refusal
from .platforms import destination_release

# the roles Neutron's policy (create_port:fixed_ips:ip_address, :mac_address,
# :port_security_enabled) lets choose a port's address, MAC or port security on another project's
# network, by the destination's policy profile: the 2023.1 rules, the defaults from 2023.2, and
# the role both count; the collection's server_port.PORT_ROLES_BY_PROFILE (SDD §6.6, §21.5)
PORT_ROLES_BY_PROFILE = {
    "legacy": ("admin", "advsvc"),
    "secure_rbac": ("admin", "service"),
    "unknown": ("admin",),
}
# what each profile's rule says when the token has none of its roles; ``{where}`` is the
# destination provider's label, which a text built for a migration names (SDD §1 naming)
_ROLE_RULE = {
    "legacy": "Neutron lets only the admin or advsvc role, or that project, choose a port's "
    "address or MAC there, and the credential's token has neither",
    "secure_rbac": "Neutron's secure-RBAC defaults let only the admin or service role, or that "
    "project, choose a port's address or MAC there, and the credential's token has neither",
    # a check is allowed while a plan runs, a PATCH that declares is not (SDD §12, §21.5)
    "unknown": "the policy profile of {where} is unknown, so only the admin role, or that "
    "project, may choose a port's address or MAC there, and the credential's token lacks it "
    "(check {where} so its series is detected, or declare its policy profile or series)",
}


def policy_profile_of(capabilities: Mapping[str, Any] | None, destination: Any) -> str:
    """The policy profile of ``destination`` (a Provider) as its check report ``capabilities``
    gives it (SDD §10.1, §21.5). A report that names none - reset by a provider change or new
    credentials, or kept from before 0.2.0 - reads as the provider's declarations alone
    (``destination_release`` with no maximum read), never legacy by default (review RV18-04);
    ``unknown`` for a value no rule knows, and without a provider."""
    profile = (capabilities or {}).get("policy_profile")
    if profile is None and destination is not None:
        profile = destination_release(
            destination.kind, destination.release, destination.policy_profile, "", ""
        )["policy_profile"]
    return profile if profile in PORT_ROLES_BY_PROFILE else "unknown"


def port_refusal(
    network: dict[str, Any], roles: Collection[str] | None, profile: str = "legacy", *, where: str
) -> str | None:
    """Why the destination credential may not put a port on ``network`` (a ``network_details``
    entry), or None (SDD §6.6). Another project's network takes it only when shared with the
    credential's project or external - an admin sees every project's networks - and only from a
    token with a role the destination's policy ``profile`` counts (``PORT_ROLES_BY_PROFILE``;
    ``policy_profile_of`` reads it): every recreated port asks for its
    address or MAC. An external network that is not shared, the credential's own included, also
    needs the admin role: Nova attaches no other to a server. ``roles`` None (not reported)
    leaves the role halves unjudged. ``where`` is the destination provider's label: every
    caller builds the refusal for a migration, which names it (SDD §1 naming)."""
    if profile not in PORT_ROLES_BY_PROFILE:
        profile = "unknown"
    if not network.get("own"):
        if not (network.get("shared") or network.get("external")):
            return "it is neither shared with the credential's project nor external"
        if roles is not None and not set(roles) & set(PORT_ROLES_BY_PROFILE[profile]):
            return _ROLE_RULE[profile].format(where=where)
    # Nova's network:attach_external_network, admin only by default (2023.1)
    if network.get("external") and not network.get("shared"):
        if roles is not None and "admin" not in roles:
            return (
                "it is external and not shared, and Nova lets only the admin role attach such a "
                "network to a server; the credential's token lacks it"
            )
    return None


def port_refusal_at_run(
    network: dict[str, Any], roles: Collection[str] | None, profile: str = "legacy", *, where: str
) -> str | None:
    """§6.6's rule as a run judges it before the source stops (SDD §7.2, §7.3 step 0): ``roles``
    None - neither the token nor the destination's check report names any - refuses a port that
    needs a role, where ``port_refusal`` leaves that half unjudged: a report new credentials reset
    never lets a token through. The refusal names the provider to check, ``where`` (SDD §1
    naming; reviews RV19-01, RV20-07, RV23-07)."""
    refused = port_refusal(network, roles, profile, where=where)
    if refused is None and roles is None and port_refusal(network, [], profile, where=where):
        return (
            "the credential's token roles could not be read, and a port there needs a role: "
            f"check {where}, then retry"
        )
    return refused


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


def kit_network_key(nic: Nic) -> str | None:
    """What the VMware migration kit maps ``nic`` by in its network_map (SDD §7.2): the NIC's
    ``network_key``; None on a VM validated before it was recorded, never its network's name -
    the kit looks a NIC on a distributed port group up by the group's key."""
    return nic.network_key or None


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
