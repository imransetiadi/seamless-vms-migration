"""Risk-ordered wave planner (SDD §9.4)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..domain.models import VMRef, Wave
from .preflight import is_legacy_os

TIERS = (
    "stateless_web",
    "middleware_queue",
    "infrastructure_service",
    "stateful_database",
    "legacy_os",
    "manual_review",
)
MANUAL_REVIEW = "manual_review"
PILOT_SIZE = 3

# (tier, substrings, whole tokens) — evaluated in this order after the legacy-OS check.
_RULES: tuple[tuple[str, tuple[str, ...], frozenset[str]], ...] = (
    (
        "stateful_database",
        (
            "database",
            "postgres",
            "mysql",
            "mariadb",
            "oracle",
            "mongo",
            "elastic",
            "cassandra",
            "mssql",
            "couchdb",
            "galera",
            "sqlserver",
            "db2",
        ),
        frozenset({"db", "sql", "pg", "ora", "pgsql"}),
    ),
    (
        "middleware_queue",
        (
            "rabbit",
            "kafka",
            "activemq",
            "artemis",
            "queue",
            "broker",
            "redis",
            "memcache",
            "cache",
            "tomcat",
            "jboss",
            "wildfly",
            "weblogic",
            "websphere",
            "middleware",
            "nats",
            "zookeeper",
        ),
        frozenset({"mq", "amq", "esb", "app"}),
    ),
    (
        "infrastructure_service",
        (
            "ldap",
            "freeipa",
            "kerberos",
            "dns",
            "dhcp",
            "ntp",
            "domain-controller",
            "domaincontroller",
            "active-directory",
            "activedirectory",
            "monitor",
            "prometheus",
            "grafana",
            "zabbix",
            "nagios",
            "bastion",
            "jumphost",
            "vault",
            "keycloak",
            "puppet",
            "satellite",
            "backup",
            "syslog",
        ),
        frozenset({"ad", "dc", "ipa", "idm", "ns", "ns1", "ns2", "jump", "bind"}),
    ),
    (
        "stateless_web",
        (
            "web",
            "www",
            "nginx",
            "httpd",
            "apache",
            "frontend",
            "front-end",
            "haproxy",
            "portal",
            "proxy",
        ),
        frozenset({"api", "ui", "lb", "fe"}),
    ),
)


def _describe(vm: VMRef) -> str:
    parts = [vm.name, *vm.tags.values(), vm.os_type or ""]
    return " ".join(parts).lower()


def heuristic_tier(vm: VMRef) -> str:
    """Deterministic tier from an explicit ``tier`` tag, the guest OS, name and tags."""
    explicit = vm.tags.get("tier") or vm.tags.get("seamless/tier")
    if explicit in TIERS:
        return str(explicit)
    if is_legacy_os(vm.os_type):
        return "legacy_os"
    text = _describe(vm)
    tokens = set(re.split(r"[^a-z0-9]+", text))
    for tier, substrings, words in _RULES:
        if any(s in text for s in substrings) or tokens & words:
            return tier
    return MANUAL_REVIEW


def _rank(tier: str) -> int:
    return TIERS.index(tier) if tier in TIERS else TIERS.index(MANUAL_REVIEW)


@dataclass
class _Unit:
    """VMs that must share a wave (same ``tags['app']``) or a single VM."""

    vms: list[VMRef] = field(default_factory=list)
    rank: int = 0

    @property
    def size(self) -> int:
        return sum(v.disk_bytes for v in self.vms)

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.rank, self.size, min(v.name for v in self.vms))


def _units(vms: Sequence[VMRef], tiers: Mapping[str, str]) -> list[_Unit]:
    groups: dict[str, _Unit] = {}
    units: list[_Unit] = []
    for vm in vms:
        rank = _rank(tiers.get(vm.source_id) or heuristic_tier(vm))
        app = vm.tags.get("app")
        if app:
            unit = groups.get(app)
            if unit is None:
                unit = groups[app] = _Unit()
                units.append(unit)
        else:
            unit = _Unit()
            units.append(unit)
        unit.vms.append(vm)
        unit.rank = max(unit.rank, rank)
    for unit in units:
        unit.vms.sort(
            key=lambda v: (_rank(tiers.get(v.source_id) or heuristic_tier(v)), v.disk_bytes, v.name)
        )
    return sorted(units, key=lambda u: u.key)


def plan_waves(
    vms: Sequence[VMRef], tiers: Mapping[str, str], max_wave_size: int = 10
) -> list[Wave]:
    """Pilot wave, then tier/size ordered waves, then a final ``Manual review`` wave.

    VMs sharing ``tags['app']`` always land in the same wave (a wave may then exceed
    ``max_wave_size``); a unit containing a ``manual_review`` VM goes to the review wave.
    """
    max_wave_size = max(1, int(max_wave_size))
    units = _units(vms, tiers)
    review_rank = _rank(MANUAL_REVIEW)
    regular = [u for u in units if u.rank < review_rank]
    review = [u for u in units if u.rank >= review_rank]

    groups: list[tuple[str | None, list[VMRef]]] = []
    pilot: list[VMRef] = []
    remaining: list[_Unit] = []
    # SDD §9.4: the rank of the first group that did not fit; no riskier group takes its room
    ceiling: int | None = None
    for unit in regular:
        if len(pilot) + len(unit.vms) <= PILOT_SIZE and (ceiling is None or unit.rank <= ceiling):
            pilot.extend(unit.vms)
        else:
            if ceiling is None:
                ceiling = unit.rank
            remaining.append(unit)
    if pilot:
        groups.append(("Pilot", pilot))

    current: list[VMRef] = []
    for unit in remaining:
        if current and len(current) + len(unit.vms) > max_wave_size:
            groups.append((None, current))
            current = []
        current.extend(unit.vms)
    if current:
        groups.append((None, current))

    review_vms = [vm for unit in review for vm in unit.vms]
    if review_vms:
        groups.append(("Manual review", review_vms))

    waves: list[Wave] = []
    for index, (name, members) in enumerate(groups, start=1):
        waves.append(
            Wave(
                id=f"wave-{index}",
                name=name or f"Wave {index}",
                order=index,
                vm_ids=[vm.source_id for vm in members],
                depends_on=[waves[-1].id] if waves else [],
            )
        )
    return waves
