"""The RHOSO network a NIC's network name resolves to (SDD §7.3 step 0, §9.3, §7.2), and whether
the destination credential may put a port there (SDD §6.6).

One rule for every OpenStack strategy: the destination credential's own network of that name, else
the only network of that name it sees. Preflight resolves names with it over
``DestinationInventory.network_details``; the cold and warm export rewrite, and the handover, at the
cutover.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

# the roles Neutron's policy (2023.1 create_port:fixed_ips:ip_address, :mac_address,
# :port_security_enabled) lets choose a port's address, MAC or port security on another project's
# network; the collection's server_port.PORT_ROLES (SDD §6.6)
PORT_ROLES = ("admin", "advsvc")


def port_refusal(network: dict[str, Any], roles: Collection[str] | None) -> str | None:
    """Why the destination credential may not put a port on ``network`` (a ``network_details``
    entry), or None (SDD §6.6). Another project's network takes it only when shared with the
    credential's project or external - an admin sees every project's networks - and only from a
    token with one of ``PORT_ROLES``: every recreated port asks for its address or MAC. ``roles``
    None (not reported) leaves that half unjudged."""
    if network.get("own"):
        return None
    if not (network.get("shared") or network.get("external")):
        return "it is neither shared with the credential's project nor external"
    if roles is not None and not set(roles) & set(PORT_ROLES):
        return (
            "Neutron lets only the admin or advsvc role, or that project, choose a port's address "
            "or MAC there, and the credential's token has neither"
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
