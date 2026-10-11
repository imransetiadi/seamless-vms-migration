"""The RHOSO network a NIC's network name resolves to (SDD §7.3 step 0, §9.3, §7.2).

One rule for every OpenStack strategy: the destination credential's own network of that name, else
the only network of that name it sees. Preflight resolves names with it over
``DestinationInventory.network_details``; the cold and warm export rewrite, and the handover, at the
cutover.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


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
