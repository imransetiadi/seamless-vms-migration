"""Which credential each step acts with (SDD §8, §27.5, §27.6, plan task TC5): the one selection
rule of the control plane. ``credential_project`` reads §27.5's table and a plan's
``source_project`` and ``destination_project``, and answers the Keystone project whose credential
a step acts with on a side, or None for the provider's own; ``implementation`` hands what acts
with it from the provider registry, ``host_pin`` the pin of the conversion host it reaches
(§26.4), and ``declared_keys`` and ``host_address`` that host's declared keys and the address it
is looked up by - the provider's own host's alone (§10, §26.10 decisions 12 and 13). No
executor, helper or route picks a credential, a pin or a host on its own, and a project
credential the plan names is never replaced by the provider's own (§27.5, "No fallback")."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from .domain.enums import ProviderRole, Strategy
from .domain.models import HostKeyPin, Provider
from .planning.networks import policy_profile_of
from .planning.preflight import (
    FIP_ADDRESS_ROLES,
    credential_hold,
    project_check,
    project_words,
)
from .providers.base import ProviderError


class Step(StrEnum):
    """The rows of SDD §27.5's table."""

    #: the provider check, its series, policy profile and storage pools (§10)
    CHECK = "check"
    #: Placement: the GPU capacity and a server's allocations (§22.3, §22.7, §22.8)
    GPU = "gpu"
    #: the VMs; the networks, quotas, floating IPs, keypairs, zones, flavors and volume types
    INVENTORY = "inventory"
    #: the pre-staging's networks, subnets, routers, security groups and keypairs; the conversion
    #: hosts (§7.2)
    PRESTAGE = "prestage"
    #: the pre-staging's flavor pair: the export as the source project, the import as the
    #: destination provider (§7.2)
    FLAVORS = "flavors"
    #: a migration's pre-copy, sync, cutovers, verification, rollback, cancel cleanup, finalize
    #: and source watch (§7.2, §7.5, §8.1)
    MIGRATION = "migration"
    #: the storage handover: its source half by id as the provider, its destination half (§7.3)
    HANDOVER = "handover"
    #: the floating IP move, return and release; Octavia's drain, join and restore (§8.3, §8.5)
    NETWORK = "network"
    #: the Designate swap (§8.4)
    DNS = "dns"
    #: VMware: the migration kit's dst_cloud and its rollback's calls (§7.2, §27.9)
    KIT = "kit"


_PLAN: Literal["plan"] = "plan"
_PROVIDER: Literal["provider"] = "provider"
Whose = Literal["plan", "provider"] | None

#: SDD §27.5: by step, whose credential acts at the source and at the destination - the plan's
#: (its project's there, else the provider's own) or the provider's own; None where the step has
#: no such side. The floating IP reservation is ``reservation``'s.
TABLE: dict[Step, tuple[Whose, Whose]] = {
    Step.CHECK: (_PROVIDER, _PROVIDER),
    Step.GPU: (None, _PROVIDER),
    Step.INVENTORY: (_PLAN, _PLAN),
    Step.PRESTAGE: (_PLAN, _PLAN),
    Step.FLAVORS: (_PLAN, _PROVIDER),
    Step.MIGRATION: (_PLAN, _PLAN),
    Step.HANDOVER: (_PROVIDER, _PLAN),
    Step.NETWORK: (_PLAN, _PLAN),
    Step.DNS: (None, _PLAN),
    Step.KIT: (_PROVIDER, _PROVIDER),
}


class HasProjects(Protocol):
    """What the selection reads of a plan (SDD §4.2)."""

    source_project: str | None
    destination_project: str | None


def credential_project(
    step: Step, side: ProviderRole | str, plan: HasProjects | None
) -> str | None:
    """The Keystone project whose credential ``step`` acts with on ``side`` (SDD §27.5): the plan's
    project there - its source_project at the source, its destination_project at the destination
    - for the work inside a project; None for the provider's own credential, which acts for the
    check, Placement, the flavor import, the handover's source half and the migration kit, and on
    a side whose plan names no project (0.4.0's rule). A step on a side its row has none of is a
    programming error."""
    role = ProviderRole(side)
    whose = TABLE[Step(step)][0 if role == ProviderRole.source else 1]
    if whose is None:
        raise ValueError(f"step {step} has no {role} side (SDD §27.5)")
    if whose == _PROVIDER or plan is None:
        return None
    return plan.source_project if role == ProviderRole.source else plan.destination_project


def implementation(registry: Any, provider: Provider, project_id: str | None) -> Any:
    """What acts on ``provider`` with the credential of ``project_id`` (SDD §10): the registry's
    implementation of the provider's own credential for None - ``get(provider)``, as 0.4.0 asked
    for it -, else of its project credential, which is never the provider's own: a project the
    provider holds no credential for is the registry's provider error."""
    if project_id is None:
        return registry.get(provider)
    return registry.get(provider, project_id)


def for_step(registry: Any, step: Step, provider: Provider, plan: HasProjects | None) -> Any:
    """What acts on ``provider`` - its side its role - for ``step`` of ``plan`` (SDD §27.5)."""
    return implementation(registry, provider, credential_project(step, provider.role, plan))


def run_row(strategy: Strategy | str) -> Step:
    """The row of §27.5 a migration's run of ``strategy`` is (SDD §7.2, §7.3): the storage
    handover's, the migration kit's for a VMware strategy, else a migration's."""
    strategy = Strategy(strategy)
    if strategy == Strategy.storage_handover:
        return Step.HANDOVER
    if strategy in (Strategy.vmware_cold, Strategy.vmware_warm):
        return Step.KIT
    return Step.MIGRATION


def cutover_projects(
    plan: HasProjects | None, strategy: Strategy | str
) -> list[tuple[ProviderRole, str]]:
    """The project credentials a cutover of ``strategy`` and its rollback act with, by side, the
    source's first (SDD §5.4 rule 6, §27.5, §27.7, plan task TC7): each project the selection names
    for the rows they act in - the run's (a storage handover's two halves, the migration kit's),
    the network cut-over's floating IPs and Octavia members, the DNS swap and the verification's
    read of the destination -, whatever the plan moves. The providers' own credentials are none of
    them: their check reads no expiry."""
    run = run_row(strategy)
    rows = {
        ProviderRole.source: (run, Step.NETWORK),
        ProviderRole.destination: (run, Step.NETWORK, Step.DNS, Step.MIGRATION),
    }
    out: list[tuple[ProviderRole, str]] = []
    for side, steps in rows.items():
        for step in steps:
            project = credential_project(step, side, plan)
            if project is not None and (side, project) not in out:
                out.append((side, project))
    return out


def cutover_hold(
    plan: HasProjects,
    strategy: Strategy | str,
    source: Provider,
    destination: Provider,
    now: datetime,
) -> str | None:
    """Why rule 6 of the cutover gate holds a cutover of ``strategy`` at ``now`` (SDD §5.4, §27.7,
    plan task TC7): the first of ``cutover_projects`` whose credential its provider's record says
    may not last the cutover and a rollback (``planning/preflight.credential_hold``), in words that
    name its project, its provider and the plan's side, never the credential; None when none
    holds it - a plan that names no project among them."""
    for side, project_id in cutover_projects(plan, strategy):
        provider = source if side == ProviderRole.source else destination
        why = credential_hold(provider, project_id, now)
        if why is not None:
            words = project_words(project_id, project_check(provider.capabilities, project_id))
            return (
                f"the credential of project {words} at {provider.label}, the plan's {side} "
                f"project, {why}"
            )
    return None


def host_pin(provider: Provider, project_id: str | None) -> HostKeyPin | None:
    """The pin of the conversion host the credential of ``project_id`` reaches on ``provider``'s
    side (SDD §4.2, §10, §26.4; the owner's decision of 2026-10-29, after plan task TC5): the
    provider's own pin for its own credential (None), else that project credential's own - none
    for a project the provider holds no credential for, never the provider's."""
    if project_id is None:
        return provider.conversion_host_pin
    held = provider.project_credentials.get(project_id)
    return held.conversion_host_pin if held is not None else None


def with_host_pin(provider: Provider, project_id: str | None, pin: HostKeyPin) -> Provider | None:
    """``provider`` with ``pin`` as the pin of the credential of ``project_id`` (SDD §26.4): its
    own pin for None, else that project credential's, every other pin as it was; None for a
    project the provider no longer holds a credential for - its pin went with it (§12)."""
    if project_id is None:
        return provider.model_copy(update={"conversion_host_pin": pin})
    held = provider.project_credentials.get(project_id)
    if held is None:
        return None
    entry = held.model_copy(update={"conversion_host_pin": pin})
    credentials = {**provider.project_credentials, project_id: entry}
    return provider.model_copy(update={"project_credentials": credentials})


def declared_keys(provider: Provider, project_id: str | None) -> tuple[str, ...]:
    """The keys an operator declares for the conversion host the credential of ``project_id``
    reaches on ``provider``'s side (SDD §4.2, §10, §26.3; the owner's decision of 2026-10-29,
    §26.10 decision 12): the provider's ``conversion_host.ssh_host_keys`` for its own credential
    (None) - its own host's -, none for a project's host, a server Seamless creates in that
    project, which its project's pin alone checks whatever the provider declares."""
    host = provider.conversion_host
    if project_id is not None or host is None:
        return ()
    return tuple(host.ssh_host_keys)


def host_address(provider: Provider, project_id: str | None) -> str | None:
    """The address the conversion host the credential of ``project_id`` reaches is looked up by
    when no server bears its name (SDD §7.2, §10; the owner's decision of 2026-10-29, §26.10
    decision 13): the provider's ``conversion_host.address`` for its own credential (None) - its
    own host's -, none for a project's host, which is looked up by its name in its project alone
    and so never resolves to the provider's own host."""
    host = provider.conversion_host
    if project_id is not None or host is None:
        return None
    return host.address


class _Placement:
    """A destination's implementation whose Placement reads - a server's allocations, GPU capacity
    and flavors - another one makes (SDD §27.5: Placement is the provider's own credential's)."""

    _READS = frozenset({"server_allocations", "gpu_capacity", "gpu_flavors"})

    def __init__(self, impl: Any, placement: Any) -> None:
        self._impl, self._placement = impl, placement

    def __getattr__(self, name: str) -> Any:
        return getattr(self._placement if name in self._READS else self._impl, name)


def verification_view(registry: Any, destination: Provider, plan: HasProjects) -> Any:
    """What the verification reads the destination with (SDD §7.5, §22.8, §27.5): the plan's
    destination credential, and for a plan with a destination project its Placement reads - a GPU
    server's allocations - with the destination provider's own."""
    impl = for_step(registry, Step.MIGRATION, destination, plan)
    if credential_project(Step.MIGRATION, destination.role, plan) is None:
        return impl
    return _Placement(impl, for_step(registry, Step.GPU, destination, plan))


def may_choose_address(destination: Provider, project_id: str) -> bool:
    """Whether the project credential may choose a floating IP's address (SDD §8.3, §27.6): its
    roles, as the destination's last check read them (``projects``, §10), hold one
    DST_FIP_ADDRESS_UNAVAILABLE counts for the destination's policy profile; False when they were
    not read."""
    report = destination.capabilities.get("projects")
    entry = report.get(project_id) if isinstance(report, dict) else None
    roles = entry.get("roles") if isinstance(entry, dict) and entry.get("status") == "ok" else None
    if not isinstance(roles, list):
        return False
    profile = policy_profile_of(destination.capabilities, destination)
    return bool(set(roles) & set(FIP_ADDRESS_ROLES.get(profile, ("admin",))))


def reservation(plan: HasProjects, destination: Provider) -> tuple[str | None, str | None]:
    """Who reserves a keep-address floating IP at the destination (SDD §8.3, §27.5, §27.6): the
    project whose credential makes the call - None for the destination provider's own - and the
    project it is made for - None for the caller's own. The plan's destination credential when it
    may choose an address; else, for a plan with a destination project, the provider's own
    credential (its admin) for that project; a plan without one, the provider's own, as before."""
    project = plan.destination_project
    if project is None:
        return None, None
    if may_choose_address(destination, project):
        return project, None
    return None, project


def inventory_project(provider: Provider, project_id: str) -> str | None:
    """The credential ``GET /providers/{id}/inventory?project=`` reads with (SDD §12, §27.4): the
    project credential the provider holds for the project, else its own credential (None) when the
    project is its own credential's, as its last check names it; a ProviderError for any other
    project - the route's 404, nothing read."""
    if project_id in provider.project_credentials:
        return project_id
    if project_id == provider.own_project_id:
        return None
    raise ProviderError(
        f"provider {provider.id} holds no credential for project {project_id}, nor is it the "
        "project of its own credential, as its last check names it"
    )
