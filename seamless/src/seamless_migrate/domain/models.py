"""Pydantic v2 domain models (SDD §4.2).

JSON field names are exactly the attribute names. Timestamps are timezone-aware UTC and
serialize as ISO-8601 strings ending in ``Z``; naive inputs are interpreted as UTC.
"""

from __future__ import annotations

import ipaddress
import math
import re
import secrets
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from ..guest_os import GuestOS, identify
from .enums import (
    DecisionStatus,
    DecisionType,
    Phase,
    PlanStatus,
    ProviderKind,
    ProviderRole,
    ReasonCode,
    Severity,
    Strategy,
    SyncPassKind,
)

GIB = 2**30
#: Fraction of the provisioned size assumed used when a disk does not report usage (SDD §4.2).
USED_FALLBACK_RATIO = 0.6


def utcnow() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_to_utc)]


def new_plan_id() -> str:
    return f"plan-{secrets.token_hex(4)}"


def new_migration_id() -> str:
    return f"mig-{secrets.token_hex(5)}"


def new_recommendation_id() -> str:
    return f"rec-{secrets.token_hex(6)}"


def new_policy_id() -> str:
    return f"pol-{secrets.token_hex(6)}"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ConversionHostConfig(_Model):
    manage: bool = True
    name: str | None = None
    flavor: str | None = None
    external_network: str | None = None
    image: str | None = None
    ssh_user: str = "cloud-user"
    address: str | None = None
    #: CIDR allowed to SSH to the conversion host (Security.md SEC-03)
    ssh_allowed_cidr: str | None = None
    #: secret holding the private key for an existing conversion host (required for VMware)
    ssh_key_secret: str | None = None


ProviderStatus = Literal["unknown", "ok", "degraded", "error"]


#: Presets and display only (SDD §4.2); ``kind`` decides the code path.
Distribution = Literal["openstack_community", "kolla", "rhosp", "rhoso", "vmware"]
DISTRIBUTION_KIND: dict[str, ProviderKind] = {
    "openstack_community": ProviderKind.openstack,
    "kolla": ProviderKind.openstack,
    "rhosp": ProviderKind.openstack,
    "rhoso": ProviderKind.rhoso,
    "vmware": ProviderKind.vmware,
}


class Provider(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str
    kind: ProviderKind
    role: ProviderRole
    endpoint: str
    cloud: str | None = None
    credentials_secret: str | None = None
    region: str | None = None
    verify_tls: bool = True
    ca_cert_path: str | None = None
    conversion_host: ConversionHostConfig | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    status: ProviderStatus = "unknown"
    status_message: str | None = None
    last_checked_at: UTCDateTime | None = None
    distribution: Distribution | None = None
    #: server-owned: when PUT …/credentials / …/conversion-key last wrote the secret store
    credentials_updated_at: UTCDateTime | None = None
    conversion_key_updated_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def _distribution_matches_kind(self) -> Provider:
        if self.distribution is not None and DISTRIBUTION_KIND[self.distribution] != self.kind:
            raise ValueError(
                f"distribution {self.distribution} belongs to kind "
                f"{DISTRIBUTION_KIND[self.distribution]}, not {self.kind}"
            )
        return self


DiskKind = Literal["volume", "ephemeral", "image_root", "vmdk"]


class Disk(_Model):
    id: str
    name: str | None = None
    size_gb: int
    used_gb: float | None = None
    bootable: bool = False
    volume_type: str | None = None
    device: str | None = None
    kind: DiskKind = "volume"
    multiattach: bool = False
    encrypted: bool = False
    independent: bool = False
    # Cinder "host@backend#pool" of a volume (admin only); decides the handover reference (§7.3.1)
    pool: str | None = None


class Nic(_Model):
    network: str
    mac: str | None = None
    fixed_ips: list[str] = Field(default_factory=list)
    vnic_type: str = "normal"
    mtu: int | None = None


PowerState = Literal["running", "stopped", "paused", "error", "transitioning", "unknown"]


class VMRef(_Model):
    """A source VM as inventoried (SDD §4.2). Tenant-controlled strings (name, tags, …) are
    stored in JSON documents: PostgreSQL JSONB rejects NUL characters, so they are stripped."""

    source_id: str
    name: str
    project: str | None = None
    flavor: str | None = None
    vcpus: int
    ram_mb: int
    disks: list[Disk] = Field(default_factory=list)
    nics: list[Nic] = Field(default_factory=list)
    power_state: PowerState = "unknown"
    os_type: str | None = None
    host: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    flavor_extra_specs: dict[str, str] = Field(default_factory=dict)
    cbt_enabled: bool | None = None
    snapshot_count: int = 0
    tools_ok: bool | None = None

    @field_validator("name", "project", "flavor", "os_type", "host", mode="before")
    @classmethod
    def _strip_nul(cls, value: Any) -> Any:
        return value.replace("\x00", "") if isinstance(value, str) else value

    @field_validator("tags", "flavor_extra_specs", mode="before")
    @classmethod
    def _strip_nul_map(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(k).replace("\x00", ""): (v.replace("\x00", "") if isinstance(v, str) else v)
                for k, v in value.items()
            }
        return value

    change_rate_bps: float | None = None
    #: the Nova keypair the server boots with (OpenStack); VMware: None (SDD §4.1)
    key_name: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disk_bytes(self) -> int:
        return sum(d.size_gb for d in self.disks) * GIB

    @computed_field  # type: ignore[prop-decorator]
    @property
    def used_bytes(self) -> int:
        if all(d.used_gb is not None for d in self.disks):
            return int(sum(float(d.used_gb or 0.0) for d in self.disks) * GIB)
        return int(self.disk_bytes * USED_FALLBACK_RATIO)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def guest_os(self) -> GuestOS:
        """Family, version, lifecycle and conversion support of the guest (SDD §9.5)."""
        return identify(self.os_type)

    def root_disk(self) -> Disk | None:
        """The boot disk: first bootable disk, else the first disk."""
        for disk in self.disks:
            if disk.bootable:
                return disk
        return self.disks[0] if self.disks else None


class Mappings(_Model):
    networks: dict[str, str] = Field(default_factory=dict)
    flavors: dict[str, str] = Field(default_factory=dict)
    volume_types: dict[str, str] = Field(default_factory=dict)
    projects: dict[str, str] = Field(default_factory=dict)


class HandoverConfig(_Model):
    enabled: bool = False
    #: source volume_type -> RHOSO cinder host "hostgroup@backend#pool"
    backend_map: dict[str, str] = Field(default_factory=dict)


class CutoverWindow(_Model):
    start: UTCDateTime
    end: UTCDateTime

    def contains(self, moment: datetime) -> bool:
        return self.start <= _to_utc(moment) <= self.end


DEFAULT_CONSOLE_PATTERNS = ["login:", "Cloud-init v\\. .* finished", "Reached target .*Multi-User"]


ProbeKind = Literal["tcp", "http", "https"]


#: a NIC override's key: six hex pairs, lower case (SDD §4.2, §12)
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


class NicOverride(_Model):
    """A VM NIC's network and addresses at RHOSO (SDD §4.2): None keeps where the plan maps its
    network, or its own addresses."""

    network: str | None = None
    fixed_ips: list[str] | None = None

    @field_validator("fixed_ips")
    @classmethod
    def _canonical(cls, value: list[str] | None) -> list[str] | None:
        """Valid addresses in their canonical form ("fd00:0::0005" is "fd00::5"); the others stay
        as given, for invalid_plan_settings to name."""
        if value is None:
            return None
        return [str(ipaddress.ip_address(a)) if _is_ip(a) else a for a in value]


class AppProbe(_Model):
    """An application probe (SDD §7.5, ADV-18): a TCP connect, or an HTTP(S) GET of ``path``."""

    kind: ProbeKind
    port: int
    #: http(s) only
    path: str = "/"
    #: http(s) only: the status that passes; None = any 2xx or 3xx
    expect_status: int | None = None


class ProbeResult(_Model):
    """What one application probe found (SDD §7.5): unreachable is not failed (ADV-18)."""

    target: str
    kind: ProbeKind
    result: Literal["passed", "failed", "unreachable"]
    #: the time to the service's answer; None when it did not answer
    latency_ms: float | None = None
    at: UTCDateTime
    detail: str = ""


class VerificationRecord(_Model):
    """The last verification of a migration: its checks and probe results (SDD §7.5)."""

    at: UTCDateTime
    passed: bool
    checks: list[dict[str, Any]] = Field(default_factory=list)
    probes: list[ProbeResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VerificationConfig(_Model):
    tcp_ports: list[int] = Field(default_factory=list)
    # Windows guests probe these instead and skip the console check (SDD §7.5)
    windows_tcp_ports: list[int] = Field(default_factory=list)
    probe_address: Literal["fixed", "floating"] = "fixed"
    console_success_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CONSOLE_PATTERNS)
    )
    timeout_s: int = 600
    auto_rollback: bool = True
    use_advisor: bool = True
    #: the application probes of every VM; Plan.probe_overrides replaces them per VM (ADV-18)
    app_probes: list[AppProbe] = Field(default_factory=list)


class Wave(_Model):
    id: str
    name: str
    order: int
    vm_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    max_parallel: int = 5


DEFAULT_PRESTAGE = [
    "networks",
    "subnets",
    "routers",
    "router_interfaces",
    "security_groups",
    "security_group_rules",
]
#: What a plan may pre-stage (SDD §4.2): the collection's export_<r>/import_<r> playbook pairs,
#: never the workloads themselves; the defaults first, then the others in their creation order
PRESTAGE_RESOURCES = (
    *DEFAULT_PRESTAGE,
    "flavors",
    "keypairs",
    "images",
    "projects",
    "users",
    "users_keypairs",
    "user_project_role_assignments",
    "detached_volumes",
)


class PlanSpec(_Model):
    """Editable plan fields; this is the ``PlanCreate`` wire schema (SDD §12)."""

    name: str
    description: str | None = None
    source_provider_id: str
    destination_provider_id: str
    vm_ids: list[str]
    mappings: Mappings = Field(default_factory=Mappings)
    default_strategy: Strategy | Literal["auto"] = "auto"
    strategy_overrides: dict[str, Strategy] = Field(default_factory=dict)
    selection_policy: Literal["min_downtime", "simplest_meeting_slo"] = "min_downtime"
    downtime_slo_s: int = Field(default=600, ge=1)
    require_approval: bool = True
    #: maker-checker (SDD §5.4, ADV-17): the cutover's requester never counts as its approver
    require_distinct_approver: bool = True
    auto_cutover: bool = False
    cutover_window: CutoverWindow | None = None
    #: at least a minute: a shorter interval would run delta passes back to back
    keep_warm_interval_s: int = Field(default=900, ge=60)
    convergence_threshold_bytes: int = Field(default=1073741824, ge=0)
    max_sync_passes: int = Field(default=5, ge=1)
    link_bps: float = Field(default=131072000.0, gt=0)
    #: overrides of ``EstimatorParams`` fields for this plan (SDD §9.1)
    estimator_overrides: dict[str, float] = Field(default_factory=dict)
    handover: HandoverConfig = Field(default_factory=HandoverConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    #: per VM (source id): its application probes instead of verification.app_probes; [] = none
    probe_overrides: dict[str, list[AppProbe]] = Field(default_factory=dict)
    #: hours a migration may wait in completed before it is flagged for finalize (SDD §8.1, ADV-19);
    #: TODO(ADV-19): confirm 72 h — the owner's open question 14
    finalize_warning_h: int = Field(default=72, ge=1)
    prestage_resources: list[str] = Field(default_factory=lambda: list(DEFAULT_PRESTAGE))
    #: "neutron": OpenStack cold/warm recreate every port with its MAC, all its fixed IPs and its
    #: security groups (SDD §6.6, §7.2, §9.2); "nova": one fixed IP per port and a new MAC
    port_creation_mode: Literal["nova", "neutron"] = "nova"
    #: per VM (source id), per NIC MAC (lower case): its network and addresses at RHOSO; OpenStack
    #: sources (SDD §4.2, §7.2, §7.3, §9.2, §9.3)
    nic_overrides: dict[str, dict[str, NicOverride]] = Field(default_factory=dict)

    def nic_override(self, vm_id: str, mac: str | None) -> NicOverride | None:
        """The plan's override of a VM's NIC, by its MAC (SDD §4.2)."""
        if not mac:
            return None
        return (self.nic_overrides.get(vm_id) or {}).get(mac.lower())

    def nic_target(self, vm_id: str, nic: Nic) -> tuple[str, list[str]]:
        """Where a VM's NIC goes at RHOSO (SDD §9.2, §9.3): its override's network, else the
        plan's mapping of its network; its override's addresses, else its own."""
        entry = self.nic_override(vm_id, nic.mac)
        network = (entry.network if entry is not None else None) or self.mappings.networks.get(
            nic.network, nic.network
        )
        if entry is not None and entry.fixed_ips is not None:
            return network, list(entry.fixed_ips)
        return network, list(nic.fixed_ips)

    @field_validator("nic_overrides")
    @classmethod
    def _lower_case_macs(
        cls, value: dict[str, dict[str, NicOverride]]
    ) -> dict[str, dict[str, NicOverride]]:
        return {
            vm: {mac.lower(): entry for mac, entry in nics.items()} for vm, nics in value.items()
        }


PlanCreate = PlanSpec

#: SDD §12: what one probe list may hold, and the longest http(s) path
MAX_PROBES_PER_VM = 10
MAX_PROBE_PATH = 200


def _nic_override_problems(overrides: dict[str, dict[str, NicOverride]]) -> list[str]:
    """What makes a NIC override unusable before any cloud is asked (SDD §12)."""
    problems: list[str] = []
    for vm_id, nics in overrides.items():
        for mac in sorted(nics):
            entry, where = nics[mac], f"nic_overrides[{vm_id}][{mac}]"
            if not _MAC.fullmatch(mac):
                problems.append(f"{where}: not a MAC address")
            if entry.network is None and entry.fixed_ips is None:
                problems.append(f"{where}: give a network, addresses or both")
            if entry.network is not None and not entry.network.strip():
                problems.append(f"{where}: network must not be empty")
            if entry.fixed_ips is None:
                continue
            if not entry.fixed_ips:
                problems.append(
                    f"{where}: fixed_ips must list at least one address "
                    "(leave it out to keep the NIC's own)"
                )
            bad = [a for a in entry.fixed_ips if not _is_ip(a)]
            if bad:
                problems.append(f"{where}: not an IP address: {', '.join(bad)}")
            twice = sorted({a for a in entry.fixed_ips if entry.fixed_ips.count(a) > 1})
            if twice:
                problems.append(f"{where}: {', '.join(twice)} given twice")
    return problems


def _probe_problems(where: str, probes: Sequence[AppProbe]) -> list[str]:
    problems = [
        f"{where}: port {p.port} outside 1-65535" for p in probes if not 1 <= p.port <= 65535
    ]
    problems += [
        f"{where}: path {p.path!r} must start with '/' (at most {MAX_PROBE_PATH} characters)"
        for p in probes
        if p.kind != "tcp" and (not p.path.startswith("/") or len(p.path) > MAX_PROBE_PATH)
    ]
    problems += [
        f"{where}: expect_status {p.expect_status} outside 100-599"
        for p in probes
        if p.expect_status is not None and not 100 <= p.expect_status <= 599
    ]
    if len(probes) > MAX_PROBES_PER_VM:
        problems.append(f"{where}: {len(probes)} probes, at most {MAX_PROBES_PER_VM}")
    return problems


def repeated_vm_ids(vm_ids: Sequence[str]) -> list[str]:
    """VM ids a plan lists more than once: one VM, one migration (SDD §12)."""
    return sorted(vm_id for vm_id, count in Counter(vm_ids).items() if count > 1)


def invalid_plan_settings(spec: PlanSpec) -> list[str]:
    """Settings that would fail every verification or never open the cutover gate (SDD §12).

    Checked on create/patch and at validation; the model itself stays permissive so that plans
    stored before the check keep loading.
    """
    problems: list[str] = []
    verification = spec.verification
    for name in ("tcp_ports", "windows_tcp_ports"):
        bad = [port for port in getattr(verification, name) if not 1 <= port <= 65535]
        if bad:
            problems.append(f"verification.{name} {bad} outside 1-65535")
    if verification.timeout_s < 0:  # 0 checks once without polling
        problems.append(
            f"verification.timeout_s must not be negative (got {verification.timeout_s})"
        )
    # application probes (SDD §7.5, §12, ADV-18)
    problems += _probe_problems("verification.app_probes", verification.app_probes)
    for vm_id, probes in spec.probe_overrides.items():
        problems += _probe_problems(f"probe_overrides[{vm_id}]", probes)
    problems += _nic_override_problems(spec.nic_overrides)
    unknown = [r for r in spec.prestage_resources if r not in PRESTAGE_RESOURCES]
    if unknown:
        problems.append(f"prestage_resources: cannot pre-stage {', '.join(map(repr, unknown))}")
    window = spec.cutover_window
    if window is not None and window.end <= window.start:
        problems.append("cutover_window: end must be after start")
    # the JSON parser accepts the Infinity literal, which gt=0 lets through (SDD §9.1)
    if not math.isfinite(spec.link_bps):
        problems.append(f"link_bps must be a finite positive number (got {spec.link_bps})")
    return problems


#: Fields a client may send in ``PlanCreate`` / ``PATCH /plans/{id}``.
PLAN_EDITABLE_FIELDS = frozenset(PlanSpec.model_fields)


class Plan(PlanSpec):
    id: str = Field(default_factory=new_plan_id)
    waves: list[Wave] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.draft
    created_at: UTCDateTime = Field(default_factory=utcnow)
    updated_at: UTCDateTime = Field(default_factory=utcnow)

    def wave_of(self, vm_id: str) -> Wave | None:
        for wave in self.waves:
            if vm_id in wave.vm_ids:
                return wave
        return None


class SyncPass(_Model):
    number: int = Field(ge=1)
    kind: SyncPassKind
    started_at: UTCDateTime
    ended_at: UTCDateTime | None = None
    bytes_scanned: int = 0
    bytes_changed: int = 0
    bytes_transferred: int = 0
    duration_s: float | None = None


class Finding(_Model):
    code: str
    severity: Severity
    message: str
    remediation: str | None = None
    #: empty = applies to every strategy
    strategies: list[Strategy] = Field(default_factory=list)

    def applies_to(self, strategy: Strategy) -> bool:
        return not self.strategies or strategy in self.strategies


class Estimate(_Model):
    strategy: Strategy
    eligible: bool = True
    reasons: list[str] = Field(default_factory=list)
    precopy_s: float
    passes: int
    downtime_s: float
    total_s: float
    final_delta_bytes: int
    meets_slo: bool
    #: shutdown_s, copy_s (final sync or full copy), conversion_s, create_s, boot_s: they add up to
    #: downtime_s (SDD §4, §9.1, ADV-16)
    downtime_parts: dict[str, float] = Field(default_factory=dict)


AdvisorKind = Literal["strategy", "classification", "verification", "similar_incidents", "screen"]


class AdvisorNote(_Model):
    kind: AdvisorKind
    source: Literal["jev", "rules", "memory"]
    summary: str
    confidence: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: UTCDateTime = Field(default_factory=utcnow)


class AdvisorActor(_Model):
    """Who took a decision: ``policy:<id>``, ``user:<name>`` or ``system:fallback`` (SDD §14.4)."""

    kind: Literal["policy", "user", "system"]
    id: str


class AdvisorModelInfo(_Model):
    name: Literal["jev", "rules"]
    version: str
    deployment: Literal["on_prem", "remote", "unknown"] = "unknown"
    endpoint_host: str | None = None


class RecommendedOption(_Model):
    option: str | None = None
    #: 0..1 - the only number compared with a threshold (SDD §14.4)
    confidence: float | None = None
    #: raw probabilities per option, shown only in Details
    distribution: dict[str, float] = Field(default_factory=dict)


class Decision(_Model):
    final_option: str | None = None
    status: DecisionStatus
    reason_code: ReasonCode
    reason_text: str | None = None
    actor: AdvisorActor
    policy_id: str | None = None
    threshold_used: float | None = None
    decided_at: UTCDateTime = Field(default_factory=utcnow)
    #: the deciding policy's rules for a person's approve or override, as they were then (SDD
    #: §14.6); None: the fallback, or a decision stored before - the policy in force applies
    require_distinct_approver: bool | None = None
    require_override_reason: bool | None = None


class AdvisorSubject(_Model):
    kind: Literal["vm", "plan", "wave"]
    id: str
    name: str


EvidenceSourceKind = Literal["inventory", "preflight", "estimate", "plan", "provider", "run"]
EvidenceEffect = Literal["supports", "opposes", "context"]


class EvidenceSource(_Model):
    """Where an evidence item comes from: the VM, the migration, the plan or the provider."""

    kind: EvidenceSourceKind
    ref_id: str = Field(min_length=1)


class EvidenceItem(_Model):
    """One fact a recommendation rests on, built by code, never by the model (SDD §14.4, ADV-02)."""

    key: str
    label: str
    value: str | int | float | bool | None = None
    display: str
    #: measured against the recommendation's ``evidence_option``
    effect: EvidenceEffect
    #: relevance, 0..100: the card shows the heaviest
    weight: int = Field(ge=0, le=100)
    source: EvidenceSource


class SimilarIncident(_Model):
    """A past incident agentmemory knew when the recommendation was recorded (SDD §14.3)."""

    title: str
    content: str = Field(max_length=1000)
    score: float | None = None


class AdvisorReason(_Model):
    """A reason Jev gave that names one of the recommendation's evidence items."""

    key: str
    text: str


class AdvisorRecommendation(_Model):
    """A recommendation and the decision taken on it, stored apart (document kind
    ``recommendation``, SDD §4.2, §14.4). The engine reads only the decision."""

    id: str = Field(default_factory=new_recommendation_id)
    created_at: UTCDateTime = Field(default_factory=utcnow)
    decision_type: DecisionType
    subject: AdvisorSubject
    plan_id: str | None = None
    migration_id: str | None = None
    #: the plan's source provider
    provider_id: str | None = None
    model: AdvisorModelInfo
    recommendation: RecommendedOption
    decision: Decision
    #: the redacted input the recommendation rests on (document kind ``advisor_snapshot``, §14.5)
    input_snapshot_sha256: str | None = None
    #: the user whose plan validation recorded it: never the one who decides it (SDD §14.6)
    requested_by: str | None = None
    legacy: bool = False
    legacy_text: str | None = None
    #: the option every evidence effect is measured against (SDD §14.4, ADV-02)
    evidence_option: str | None = None
    #: what validation knew when it was recorded, heaviest first
    evidence: list[EvidenceItem] = Field(default_factory=list)
    #: at most 3, searched once when it was recorded
    similar_incidents: list[SimilarIncident] = Field(default_factory=list)
    #: Jev's reasons that name one of the evidence keys
    reasons: list[AdvisorReason] = Field(default_factory=list)


class AdvisorSnapshot(_Model):
    """A redacted advisor input, stored once under the SHA-256 of its canonical JSON (SDD §14.5)."""

    sha256: str
    created_at: UTCDateTime = Field(default_factory=utcnow)
    payload: dict[str, Any]

    @property
    def id(self) -> str:
        return self.sha256


class PolicyScope(_Model):
    """Where a policy applies: plans or source providers; neither = everywhere (SDD §14.6)."""

    provider_ids: list[str] = Field(default_factory=list)
    plan_ids: list[str] = Field(default_factory=list)


class AdvisorPolicySpec(_Model):
    """A policy as it is written (POST/PUT /advisor/policies, SDD §12, §14.6)."""

    name: str = Field(min_length=1, max_length=200)
    scope: PolicyScope = Field(default_factory=PolicyScope)
    #: none = every decision type
    decision_types: list[DecisionType] = Field(default_factory=list)
    mode: Literal["auto_apply_above_threshold", "always_require_approval"]
    threshold: float = Field(ge=0, le=1)
    #: the evidence items that support or oppose (SDD §14.4): a new policy asks for 3 (ADV-02); the
    #: built-in default asks for none
    min_evidence_for_auto_apply: int = Field(default=3, ge=0, le=100)
    require_override_reason: bool = True
    #: maker-checker (ADV-17): the decision's requester neither approves nor overrides it
    require_distinct_approver: bool = True

    @model_validator(mode="after")
    def _one_scope_level(self) -> AdvisorPolicySpec:
        if self.scope.provider_ids and self.scope.plan_ids:
            raise ValueError("a policy scopes either providers or plans, not both")
        return self


class AdvisorPolicy(AdvisorPolicySpec):
    """A stored advisor policy (document kind ``advisor_policy``, SDD §4.2, §14.6)."""

    id: str = Field(default_factory=new_policy_id)
    created_at: UTCDateTime = Field(default_factory=utcnow)
    updated_at: UTCDateTime = Field(default_factory=utcnow)
    updated_by: str


class ForceCutover(_Model):
    """A cutover before convergence, asked for with a reason (SDD §5.4, ADV-16)."""

    actor: str
    at: UTCDateTime
    reason: str
    #: the last pass's bytes_changed, and the estimated final downtime, when it was asked for
    delta_bytes: int
    estimated_downtime_s: float


class Approval(_Model):
    actor: str
    at: UTCDateTime = Field(default_factory=utcnow)
    comment: str | None = None


class PhaseChange(_Model):
    from_phase: Phase | None = None
    to_phase: Phase
    at: UTCDateTime = Field(default_factory=utcnow)
    reason: str
    actor: str


class Migration(_Model):
    id: str = Field(default_factory=new_migration_id)
    plan_id: str
    wave_id: str | None = None
    vm: VMRef
    strategy: Strategy
    phase: Phase = Phase.pending
    phase_history: list[PhaseChange] = Field(default_factory=list)
    progress_pct: float = 0
    bytes_total: int = 0
    bytes_transferred: int = 0
    sync_passes: list[SyncPass] = Field(default_factory=list)
    #: bytes of the passes dropped from sync_passes (SDD §5.4); bytes_transferred still counts them
    sync_bytes_dropped: int = 0
    estimate: Estimate | None = None
    estimates: list[Estimate] = Field(default_factory=list)
    #: per-stream scan throughput measured by the last warm pass (SDD §9.1 calibration)
    observed_scan_bps: float | None = None
    #: mappings matched automatically by pre-flight (e.g. the smallest fitting flavor)
    resolved_mappings: Mappings = Field(default_factory=Mappings)
    findings: list[Finding] = Field(default_factory=list)
    checkpoint: str | None = None
    downtime_started_at: UTCDateTime | None = None
    downtime_ended_at: UTCDateTime | None = None
    actual_downtime_s: float | None = None
    approvals: list[Approval] = Field(default_factory=list)
    cutover_requested: bool = False
    #: who asked for the cutover first, and when (SDD §5.4): never the approver that counts
    cutover_requested_by: str | None = None
    cutover_requested_at: UTCDateTime | None = None
    #: cutover window bypass granted with the request (SDD §5.4 rule 2); persisted
    force_window: bool = False
    advisor_notes: list[AdvisorNote] = Field(default_factory=list)
    #: the recommendation whose decision waits for approval: no start meanwhile (SDD §5.4, §14.6)
    advisor_hold: str | None = None
    #: the convergence rule that ended the passes (SDD §5.3, ADV-16)
    converged_by: Literal["threshold", "slo", "max_passes", "force"] | None = None
    force_cutover: ForceCutover | None = None
    #: the model seconds a simulated cutover spent with the source stopped (demo, SDD §5.2)
    downtime_model_s: float | None = None
    #: invariant violations found when it completed (SDD §5.5)
    data_issues: list[str] = Field(default_factory=list)
    #: the last verification: its checks and application probe results (SDD §7.5, ADV-18)
    verification: VerificationRecord | None = None
    #: the rules' probe suggestion at validation (SDD §9.6): runs only once added to the plan
    suggested_probes: list[AppProbe] = Field(default_factory=list)
    #: completed, not finalized: when its source VM was seen running again (SDD §8.1, ADV-19)
    source_running_at: UTCDateTime | None = None
    review_required: bool = False
    review_reason: str | None = None
    destination_server_id: str | None = None
    error: str | None = None
    attempts: int = 0
    #: each retried attempt's strategy, oldest first: the evidence names them (SDD §14.4)
    attempt_strategies: list[Strategy] = Field(default_factory=list)
    created_at: UTCDateTime = Field(default_factory=utcnow)
    updated_at: UTCDateTime = Field(default_factory=utcnow)

    def estimate_for(self, strategy: Strategy) -> Estimate | None:
        for est in self.estimates:
            if est.strategy == strategy:
                return est
        return None


#: SDD §5.4: the latest passes sync_passes keeps after the first plan.max_sync_passes
SYNC_PASSES_LATEST = 20


def next_pass_number(m: Migration) -> int:
    """The number of the migration's next pass; it keeps counting after passes are dropped."""
    return m.sync_passes[-1].number + 1 if m.sync_passes else 1


def keep_sync_history(m: Migration, keep_first: int, keep_latest: int = SYNC_PASSES_LATEST) -> None:
    """Drop the oldest passes between the first ``keep_first`` and the latest ``keep_latest``
    (SDD §5.4): a long wait for the cutover runs a keep-warm pass every interval, which must not
    grow the migration without bound. The dropped passes' bytes move to ``sync_bytes_dropped``."""
    excess = len(m.sync_passes) - keep_first - keep_latest
    if excess <= 0:
        return
    dropped = m.sync_passes[keep_first : keep_first + excess]
    m.sync_bytes_dropped += sum(p.bytes_transferred for p in dropped)
    del m.sync_passes[keep_first : keep_first + excess]


class Event(_Model):
    seq: int = 0
    ts: UTCDateTime = Field(default_factory=utcnow)
    kind: str
    plan_id: str | None = None
    migration_id: str | None = None
    actor: str = "system"
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ValidationItem(_Model):
    migration_id: str
    vm_name: str
    strategy: Strategy
    phase: Phase
    findings: list[Finding] = Field(default_factory=list)
    estimates: list[Estimate] = Field(default_factory=list)


class ValidationReport(_Model):
    """Answer of ``POST /plans/{id}/validate`` (SDD §12)."""

    plan_id: str
    ok: bool
    migrations: list[ValidationItem] = Field(default_factory=list)
