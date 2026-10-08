"""Wire-format enums (SDD §4.1). Values are the lower-case strings used on the wire."""

from __future__ import annotations

from enum import StrEnum


class ProviderKind(StrEnum):
    openstack = "openstack"
    vmware = "vmware"
    rhoso = "rhoso"


class ProviderRole(StrEnum):
    source = "source"
    destination = "destination"


class Strategy(StrEnum):
    cold = "cold"
    warm = "warm"
    storage_handover = "storage_handover"
    vmware_cold = "vmware_cold"
    vmware_warm = "vmware_warm"


class Phase(StrEnum):
    pending = "pending"
    validating = "validating"
    blocked = "blocked"
    ready = "ready"
    precopy = "precopy"
    syncing = "syncing"
    awaiting_cutover = "awaiting_cutover"
    cutover = "cutover"
    verifying = "verifying"
    completed = "completed"
    finalized = "finalized"
    failed = "failed"
    rolling_back = "rolling_back"
    rolled_back = "rolled_back"
    cancelled = "cancelled"


class Severity(StrEnum):
    blocker = "blocker"
    warning = "warning"
    info = "info"


class Role(StrEnum):
    """Ordered roles: viewer < operator < approver < admin (SDD §13.2)."""

    viewer = "viewer"
    operator = "operator"
    approver = "approver"
    admin = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_ORDER.index(self)

    def at_least(self, other: Role) -> bool:
        return self.rank >= Role(other).rank


_ROLE_ORDER = [Role.viewer, Role.operator, Role.approver, Role.admin]


class PlanStatus(StrEnum):
    draft = "draft"
    validated = "validated"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"


class SyncPassKind(StrEnum):
    full = "full"
    delta = "delta"
    final = "final"


#: Strategies considered per source family (SDD §9.2).
OPENSTACK_STRATEGIES = (Strategy.cold, Strategy.warm, Strategy.storage_handover)
VMWARE_STRATEGIES = (Strategy.vmware_cold, Strategy.vmware_warm)
WARM_STRATEGIES = frozenset({Strategy.warm, Strategy.vmware_warm})
SINGLE_SHOT_STRATEGIES = frozenset({Strategy.cold, Strategy.storage_handover, Strategy.vmware_cold})
#: Simplicity order used to break ties (lower index = simpler).
SIMPLICITY_ORDER = (
    Strategy.cold,
    Strategy.storage_handover,
    Strategy.warm,
    Strategy.vmware_cold,
    Strategy.vmware_warm,
)


def strategies_for(kind: ProviderKind | str) -> tuple[Strategy, ...]:
    """Strategies a source provider of ``kind`` can use."""
    return VMWARE_STRATEGIES if ProviderKind(kind) == ProviderKind.vmware else OPENSTACK_STRATEGIES
