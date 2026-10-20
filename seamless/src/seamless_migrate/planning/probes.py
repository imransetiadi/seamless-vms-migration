"""Application probes (SDD §7.5, §9.6, ADV-18): what a VM's verification probes, and what the rules
suggest probing.

A suggestion follows the VM's tier (§9.4's rules) and the service its name, tags or OS names; it is
recorded at validation and never runs until a person adds it to the plan.
"""

from __future__ import annotations

import re

from ..domain.models import AppProbe, Plan, VMRef
from .gpu import vm_gpus
from .waves import heuristic_tier

DATABASE_TIER = "stateful_database"
#: the port NVIDIA's DCGM exporter serves a GPU's metrics on, suggested for every GPU VM outside the
#: database tier (SDD §9.6, §22.8)
DCGM_EXPORTER_PORT = 9400


def _tcp(port: int) -> AppProbe:
    return AppProbe(kind="tcp", port=port)


def _http(port: int) -> AppProbe:
    return AppProbe(kind="http", port=port)


# (tier, substrings, whole tokens, probe) — the first match of the VM's tier wins (SDD §9.6)
_SERVICES: tuple[tuple[str, tuple[str, ...], frozenset[str], AppProbe], ...] = (
    (DATABASE_TIER, ("postgres", "pgsql"), frozenset({"pg"}), _tcp(5432)),
    (DATABASE_TIER, ("mysql", "mariadb", "galera"), frozenset(), _tcp(3306)),
    (DATABASE_TIER, ("mssql", "sqlserver"), frozenset(), _tcp(1433)),
    (DATABASE_TIER, ("oracle",), frozenset({"ora"}), _tcp(1521)),
    (DATABASE_TIER, ("mongo",), frozenset(), _tcp(27017)),
    (DATABASE_TIER, ("cassandra",), frozenset(), _tcp(9042)),
    (DATABASE_TIER, ("db2",), frozenset(), _tcp(50000)),
    (DATABASE_TIER, ("elastic",), frozenset(), _http(9200)),
    (DATABASE_TIER, ("couchdb",), frozenset(), _http(5984)),
    ("middleware_queue", ("rabbit",), frozenset(), _tcp(5672)),
    ("middleware_queue", ("kafka",), frozenset(), _tcp(9092)),
    ("middleware_queue", ("activemq", "artemis"), frozenset({"amq"}), _tcp(61616)),
    ("middleware_queue", ("redis",), frozenset(), _tcp(6379)),
    ("middleware_queue", ("memcache",), frozenset(), _tcp(11211)),
    ("middleware_queue", ("nats",), frozenset(), _tcp(4222)),
    ("middleware_queue", ("zookeeper",), frozenset(), _tcp(2181)),
    ("middleware_queue", ("tomcat", "jboss", "wildfly"), frozenset(), _http(8080)),
    ("middleware_queue", ("weblogic",), frozenset(), _http(7001)),
    ("middleware_queue", ("websphere",), frozenset(), _http(9080)),
    (
        "infrastructure_service",
        (
            "ldap",
            "freeipa",
            "active-directory",
            "activedirectory",
            "domain-controller",
            "domaincontroller",
        ),
        frozenset({"ad", "dc", "ipa", "idm"}),
        _tcp(389),
    ),
    ("infrastructure_service", ("dns",), frozenset({"bind", "ns", "ns1", "ns2"}), _tcp(53)),
    ("infrastructure_service", ("kerberos",), frozenset(), _tcp(88)),
)


def _text(vm: VMRef) -> str:
    # the name and tag values, never the OS: an OS name is no service (SDD §9.6, review PL-03)
    return " ".join([vm.name, *vm.tags.values()]).lower()


def suggest_probes(vm: VMRef) -> list[AppProbe]:
    """The rules' suggestion for ``vm`` (SDD §9.6): its tier's probe, then for a GPU VM tcp 9400,
    the DCGM exporter's port (§22.8); empty when the table names no service and the VM has no
    GPU. A database VM keeps its engine's probe alone, or none: the exporter's port says nothing of
    the database, whose probe APP_PROBE_MISSING and §7.5's review ask for (PR #64 review)."""
    tier = heuristic_tier(vm)
    gpu = [_tcp(DCGM_EXPORTER_PORT)] if tier != DATABASE_TIER and vm_gpus(vm) else []
    if tier == "stateless_web":
        return [_http(80), *gpu]
    text = _text(vm)
    tokens = set(re.split(r"[^a-z0-9]+", text))
    for service_tier, substrings, words, probe in _SERVICES:
        if service_tier == tier and (any(s in text for s in substrings) or tokens & words):
            return [probe.model_copy(), *gpu]
    return gpu


def probes_for(vm: VMRef, plan: Plan) -> list[AppProbe]:
    """The probes ``vm``'s verification runs: its override, else the plan's (SDD §7.5)."""
    if vm.source_id in plan.probe_overrides:
        return list(plan.probe_overrides[vm.source_id])
    return list(plan.verification.app_probes)


def is_database(vm: VMRef) -> bool:
    """A VM of the ``stateful_database`` tier by §9.4's rules."""
    return heuristic_tier(vm) == DATABASE_TIER


def describe_probe(probe: AppProbe) -> str:
    """ "TCP 5432", "HTTP 9200 /", "HTTPS 443 /health (204)"."""
    if probe.kind == "tcp":
        return f"TCP {probe.port}"
    status = f" ({probe.expect_status})" if probe.expect_status is not None else ""
    return f"{probe.kind.upper()} {probe.port} {probe.path}{status}"
